"""Вызовы LLM с ответом по JSON-схеме. Бэкенды (analyst.backend):
  codex       — Codex CLI (подписка ChatGPT);
  claude_code — Claude Code CLI `claude -p` (подписка claude.ai);
  api         — Claude API по ключу ANTHROPIC_API_KEY;
  openai      — любой OpenAI-совместимый API: OpenAI, OpenRouter, DeepSeek, Gemini, Mistral, Groq,
                локальные Ollama, LM Studio, vLLM (analyst.base_url, analyst.model, ключ — analyst.api_key_env);
  command     — любая программа: запрос в stdin, ответ (JSON) в stdout (analyst.command).

Кончились лимиты (LimitReached) — запрос уходит в analyst.fallback_backend, и все запросы к этому
бэкенду идут сразу туда до времени сброса из текста ошибки («try again at 4:37 PM»; хранится в
.llm-limits.json и действует и в следующих запусках), а если времени нет — до конца запуска.

Каждый вызов пишет в журнал событие `llm` с расходом токенов (для `hh-agent stats`)."""
from __future__ import annotations

import json
import math
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import TypeVar

from pydantic import BaseModel

from .config import Config

T = TypeVar("T", bound=BaseModel)

NO_TOOLS = "Не запускай команды и не читай файлы — нужен только ответ по схеме."

# Инструменты Codex, которые агенту не нужны: без их описаний каждый вызов на ~3 тыс. токенов дешевле.
# Свой ~11 тыс. токенов системный промпт Codex отправляет всегда.
CODEX_DISABLE = ("browser_use", "browser_use_external", "computer_use", "in_app_browser", "apps", "plugins",
                 "skill_search", "tool_suggest", "image_generation", "multi_agent", "goals", "sleep_tool",
                 "view_image", "shell_tool", "unified_exec")


def _usage(cfg: Config | None, purpose: str, backend: str, inp: int = 0, cached: int = 0, out: int = 0,
           cost_usd: float | None = None, model: str | None = None) -> None:
    if cfg is None:
        return
    from .events import log_event
    log_event(cfg, "llm", purpose=purpose, backend=backend, model=model, input=inp, cached=cached, output=out,
              **({"cost_usd": round(cost_usd, 5)} if cost_usd is not None else {}))


class LimitReached(Exception):
    """Кончились лимиты подписки или API — продолжать бессмысленно."""


def _schema(model: type[BaseModel]) -> dict:
    """JSON-схема в строгом виде: без лишних полей на любом уровне вложенности."""
    def strict(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
            for v in node.values():
                strict(v)
        elif isinstance(node, list):
            for v in node:
                strict(v)
        return node
    return strict(model.model_json_schema())


BACKENDS = ("codex", "claude_code", "api", "openai", "command")

LIMITS_FILE = ".llm-limits.json"  # бэкенд → до какого времени (epoch) у него нет лимитов
_limited: dict[str, float] | None = None  # то же в памяти; inf — время сброса неизвестно, до конца запуска
_limited_lock = threading.Lock()  # запросы идут из нескольких потоков


def _limits(cfg: Config) -> dict[str, float]:
    global _limited
    if _limited is None:
        try:
            _limited = {k: float(v) for k, v in json.loads(cfg.path(LIMITS_FILE).read_text()).items()}
        except (OSError, ValueError, AttributeError):
            _limited = {}
    return _limited


def reset_at(message: str, now: datetime | None = None) -> datetime | None:
    """Когда сбросится лимит, по тексту ошибки: «try again at 4:37 PM» (Codex), «… at Oct 21st, 2026 9:05 AM»,
    «try again in 2 hours 15 minutes». Время без даты — сегодня или, если уже прошло, завтра."""
    now = now or datetime.now()
    m = re.search(r"try again in ((?:\d+\s*(?:days?|d|hours?|h|minutes?|mins?|m)\b[\s,and]*)+)", message, re.I)
    if m:
        units = {"d": 86400, "h": 3600, "m": 60}
        secs = sum(int(n) * units[u[0].lower()] for n, u in re.findall(r"(\d+)\s*([a-z]+)", m.group(1), re.I))
        return now + timedelta(seconds=secs) if secs else None
    m = re.search(r"try again at ([^\"\n]+?)\.?(?:\"|\n|$)", message, re.I)
    if not m:
        return None
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", m.group(1).strip().rstrip("."))
    for fmt in ("%I:%M %p", "%I %p", "%H:%M", "%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p", "%b %d %I:%M %p"):
        try:
            t = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%Y" in fmt:
            return t
        if "%b" in fmt:
            t = t.replace(year=now.year)
            return t if t > now else t.replace(year=now.year + 1)
        t = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        return t if t > now else t + timedelta(days=1)
    return None


def ask_json(cfg: Config, system: str, user: str, model: type[T], purpose: str = "other",
             backend: str | None = None) -> T:
    """Один запрос к LLM. backend — другой бэкенд вместо analyst.backend (письма: analyst.letter_backend).
    purpose — для учёта токенов в stats."""
    b = backend or cfg.analyst.backend
    fb = cfg.analyst.fallback_backend
    if not fb or fb == b:
        return ask_backend(cfg, b, system, user, model, purpose)
    if _limits(cfg).get(b, 0) > time.time():
        return ask_backend(fallback_cfg(cfg), fb, system, user, model, purpose)
    try:
        return ask_backend(cfg, b, system, user, model, purpose)
    except LimitReached as e:
        until = reset_at(str(e))
        with _limited_lock:
            limits = _limits(cfg)
            first = limits.get(b, 0) <= time.time()
            limits[b] = until.timestamp() if until else math.inf
            try:  # время сброса переживает запуск: следующий тоже сразу пойдёт в запасной бэкенд
                cfg.path(LIMITS_FILE).write_text(json.dumps({k: v for k, v in limits.items() if v != math.inf}))
            except OSError:
                pass
        if first:
            from .events import log_event
            when = f"до {until:%d.%m %H:%M}" if until else "до конца запуска"
            print(f"{b}: кончились лимиты — {when} работаю через {fb}", flush=True)
            log_event(cfg, "llm_fallback", backend=b, fallback=fb, until=f"{until:%Y-%m-%dT%H:%M}" if until else None,
                      reason=str(e)[:300])
        return ask_backend(fallback_cfg(cfg), fb, system, user, model, purpose)


def fallback_cfg(cfg: Config) -> Config:
    """Конфиг для запасного бэкенда: поля analyst из analyst.fallback_settings (model, base_url, api_key_env…)."""
    if not cfg.analyst.fallback_settings:
        return cfg
    return cfg.model_copy(update={"analyst": cfg.analyst.model_copy(update=cfg.analyst.fallback_settings)})


def ask_backend(cfg: Config, b: str, system: str, user: str, model: type[T], purpose: str) -> T:
    if b == "codex":
        return run_codex(f"{system}\n{NO_TOOLS}\n\n{user}", model, cfg.analyst.codex_model, cfg, purpose)
    if b == "claude_code":
        return run_claude_cli(system, user, model, cfg, purpose)
    if b == "api":
        return run_api(system, user, model, cfg, purpose)
    if b == "openai":
        return run_openai(system, user, model, cfg, purpose)
    if b == "command":
        return run_command(system, user, model, cfg, purpose)
    raise SystemExit(f"analyst.backend: неизвестное значение {b!r} ({' | '.join(BACKENDS)})")


def _json_note(model: type[BaseModel]) -> str:
    return ("Ответь только JSON-объектом по этой JSON-схеме, без пояснений и без markdown:\n"
            + json.dumps(_schema(model), ensure_ascii=False))


def _parse(text: str, model: type[T]) -> T:
    """JSON из ответа модели: без обёртки ```json и текста вокруг объекта."""
    text = re.sub(r"^\s*```(?:json)?|```\s*$", "", (text or "").strip())
    start, end = text.find("{"), text.rfind("}")
    try:
        return model.model_validate_json(text[start:end + 1] if start >= 0 else text)
    except ValueError as e:
        raise RuntimeError(f"ответ модели не по схеме: {str(e)[:200]} — начало ответа: {text[:200]!r}")


def api_key(cfg: Config) -> str:
    """Ключ для openai: из переменной окружения, иначе из файла .env рядом с config.yaml
    (launchd не видит переменных из ~/.zshrc)."""
    name = cfg.analyst.api_key_env
    if not name:
        return ""
    if os.environ.get(name):
        return os.environ[name]
    env = cfg.path(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() == name:
                return v.strip().strip('"').strip("'")
    return ""


_FORMAT: dict[str, int] = {}  # base_url → какой response_format сервер принял (индекс в списке ниже)


def run_openai(system: str, user: str, model: type[T], cfg: Config, purpose: str = "other") -> T:
    """OpenAI-совместимый /chat/completions. Строгая JSON-схема, если сервер её поддерживает,
    иначе json_object, иначе просто схема в промпте."""
    a = cfg.analyst
    if not a.base_url or not a.model:
        raise SystemExit("analyst.backend: openai — задайте analyst.base_url и analyst.model (или uv run hh-agent setup).")
    url = a.base_url.rstrip("/") + "/chat/completions"
    key = api_key(cfg)
    if a.api_key_env and not key:
        raise SystemExit(f"Нет ключа: задайте переменную {a.api_key_env} или строку {a.api_key_env}=… в файле .env.")
    messages = [{"role": "system", "content": f"{system}\n\n{_json_note(model)}".strip()},
                {"role": "user", "content": user}]
    formats = [{"type": "json_schema", "json_schema": {"name": model.__name__, "schema": _schema(model), "strict": True}},
               {"type": "json_object"}, None]
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
    for i in range(_FORMAT.get(url, 0), len(formats)):
        body = {"model": a.model, "messages": messages, **a.openai_extra}
        if formats[i]:
            body["response_format"] = formats[i]
        req = urllib.request.Request(url, json.dumps(body).encode(), headers)
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:300]
            if e.code in (401, 403):
                raise SystemExit(f"{a.base_url}: ключ не подошёл ({e.code}): {msg}")
            if e.code == 429:
                raise LimitReached(f"{a.base_url}: {msg}")
            if e.code in (400, 422) and formats[i] is not None:
                continue  # сервер не знает такой response_format — пробуем проще
            raise RuntimeError(f"{a.base_url}: {e.code} {msg}")
        except (urllib.error.URLError, TimeoutError) as e:
            raise RuntimeError(f"{a.base_url}: нет ответа ({e})")
        _FORMAT[url] = i
        u = data.get("usage") or {}
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        _usage(cfg, purpose, "openai", u.get("prompt_tokens", 0), cached, u.get("completion_tokens", 0),
               model=data.get("model") or a.model)
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"{a.base_url}: неожиданный ответ {str(data)[:300]}")
        return _parse(text, model)
    raise RuntimeError(f"{a.base_url}: сервер не принял запрос ни в одном формате")


def run_command(system: str, user: str, model: type[T], cfg: Config, purpose: str = "other") -> T:
    """analyst.command: запрос — в stdin, JSON-ответ — из stdout (например, `ollama run qwen3`)."""
    cmd = cfg.analyst.command
    if not cmd:
        raise SystemExit("analyst.backend: command — задайте analyst.command (или uv run hh-agent setup).")
    prompt = f"{system}\n\n{user}\n\n{_json_note(model)}".strip()
    try:
        r = subprocess.run(shlex.split(cmd), input=prompt, capture_output=True, text=True, timeout=900)
    except FileNotFoundError:
        raise SystemExit(f"analyst.command: не найдена программа {shlex.split(cmd)[0]!r}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{cmd}: нет ответа за 15 минут")
    _usage(cfg, purpose, "command", model=cmd)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()[-300:]
        if "limit" in msg.lower() or "quota" in msg.lower():
            raise LimitReached(f"{cmd}: {msg}")
        raise RuntimeError(f"{cmd}: код выхода {r.returncode}: {msg}")
    return _parse(r.stdout, model)


def _codex_errors(stdout: str, stderr: str) -> str:
    """Текст ошибок Codex: stderr и события ошибок из --json. Остальные события (рассуждения, ответ модели)
    пересказывают вакансию — в них бывают «login» и «rate limit», которые к ошибке отношения не имеют."""
    out = [stderr]
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            out.append(line)
            continue
        if isinstance(ev, dict) and re.search(r"error|failed", str(ev.get("type", "")), re.I):
            out.append(json.dumps(ev, ensure_ascii=False))
    return "\n".join(out)


def run_codex(prompt: str, model: type[T], codex_model: str | None = None,
              cfg: Config | None = None, purpose: str = "other") -> T:
    """Один запуск `codex exec` в пустой папке, без доступа на запись, с ответом по JSON-схеме."""
    with tempfile.TemporaryDirectory() as d:
        schema, out = os.path.join(d, "schema.json"), os.path.join(d, "out.json")
        work = os.path.join(d, "work")
        os.mkdir(work)
        with open(schema, "w") as f:
            json.dump(_schema(model), f)
        cmd = ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
               "--ignore-user-config", "--json", "-C", work, "--output-schema", schema, "-o", out]
        for f in CODEX_DISABLE:
            cmd += ["--disable", f]
        if codex_model:
            cmd += ["-m", codex_model]
        if cfg and cfg.analyst.codex_effort:
            cmd += ["-c", f'model_reasoning_effort="{cfg.analyst.codex_effort}"']
        cmd.append("-")
        try:
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=600)
        except FileNotFoundError:
            raise SystemExit("Не найден `codex`. Установите Codex CLI или выберите другой бэкенд в config.yaml.")
        except subprocess.TimeoutExpired:
            raise RuntimeError("codex exec не ответил за 10 минут")
        for line in r.stdout.splitlines():  # --json: события; расход — в turn.completed
            if '"turn.completed"' in line:
                try:
                    u = json.loads(line).get("usage") or {}
                except json.JSONDecodeError:
                    continue
                _usage(cfg, purpose, "codex", u.get("input_tokens", 0), u.get("cached_input_tokens", 0),
                       u.get("output_tokens", 0) + u.get("reasoning_output_tokens", 0),
                       model=codex_model or "codex: модель по умолчанию")
        try:
            with open(out, encoding="utf-8") as f:
                return model.model_validate_json(f.read())
        except (OSError, ValueError):
            msg = (r.stderr or r.stdout).strip()[-300:]
            # Ошибки с --json приходят событиями в stdout, а в stderr бывают посторонние предупреждения.
            errors = _codex_errors(r.stdout, r.stderr)
            low = errors.lower()
            if "login" in low or "unauthorized" in low:
                raise SystemExit("Codex не авторизован: выполните `codex login`.")
            if re.search(r"usage limit|rate.?limit|quota|limit reached|hit your|too many requests", low):
                err = re.findall(r'"message"\s*:\s*"([^"]*limit[^"]*)"', errors, re.I)
                raise LimitReached(f"codex: {err[-1] if err else msg}")
            raise RuntimeError(f"codex exec: {msg or f'код выхода {r.returncode}'}")


def run_claude_cli(system: str, user: str, model: type[T], cfg: Config, purpose: str = "other") -> T:
    a = cfg.analyst
    cmd = [
        "claude", "-p", "--output-format", "json",
        "--tools", "", "--strict-mcp-config", "--no-session-persistence",
        "--system-prompt", system,
        "--json-schema", json.dumps(_schema(model), ensure_ascii=False),
        "--model", a.model, "--effort", a.effort,
    ]
    # Без ключа в окружении claude использует вход через claude.ai, то есть подписку.
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    try:
        r = subprocess.run(cmd, input=user, capture_output=True, text=True, env=env,
                           timeout=600, cwd=tempfile.gettempdir())
    except FileNotFoundError:
        raise SystemExit("Не найден `claude` (Claude Code). Установите его или выберите другой бэкенд.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("claude -p не ответил за 10 минут")
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        msg = (r.stderr or r.stdout).strip()[:300]
        if "limit" in msg.lower():
            raise LimitReached(msg)
        raise RuntimeError(f"claude -p: {msg or f'код выхода {r.returncode}'}")
    u = d.get("usage") or {}
    _usage(cfg, purpose, "claude_code",
           u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0),
           u.get("cache_read_input_tokens", 0), u.get("output_tokens", 0), d.get("total_cost_usd"),
           # modelUsage — какие модели реально отвечали; основная — с наибольшим выводом
           model=max(d.get("modelUsage") or {a.model: {}},
                     key=lambda m: (d.get("modelUsage") or {}).get(m, {}).get("outputTokens", 0)))
    if d.get("is_error") or not d.get("structured_output"):
        msg = str(d.get("result") or d.get("subtype"))[:300]
        if d.get("api_error_status") == 429 or "limit" in msg.lower():
            raise LimitReached(msg)
        raise RuntimeError(f"claude -p: {msg}")
    return model.model_validate(d["structured_output"])


_client = None


def run_api(system: str, user: str, model: type[T], cfg: Config, purpose: str = "other") -> T:
    global _client
    import anthropic

    _client = _client or anthropic.Anthropic()
    try:
        resp = _client.beta.messages.parse(
            model=cfg.analyst.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": cfg.analyst.effort},
            # Системный промпт одинаков для всех запросов — кешируем его.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=model,
        )
    except anthropic.AuthenticationError:
        raise SystemExit("Claude API: неверный ключ. Задайте ANTHROPIC_API_KEY.")
    except anthropic.RateLimitError as e:
        raise LimitReached(str(e))
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        raise RuntimeError(str(e))
    u = resp.usage
    cr, cc = getattr(u, "cache_read_input_tokens", 0) or 0, getattr(u, "cache_creation_input_tokens", 0) or 0
    _usage(cfg, purpose, "api", u.input_tokens + cr + cc, cr, u.output_tokens, model=resp.model)
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise RuntimeError(f"модель не вернула ответ (stop_reason={resp.stop_reason})")
    return resp.parsed_output
