"""Вызовы LLM с ответом по JSON-схеме: Codex CLI, Claude Code CLI или Claude API."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import TypeVar

from pydantic import BaseModel

from .config import Config

T = TypeVar("T", bound=BaseModel)

NO_TOOLS = "Не запускай команды и не читай файлы — нужен только ответ по схеме."


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


def ask_json(cfg: Config, system: str, user: str, model: type[T]) -> T:
    """Один запрос к бэкенду из analyst.backend."""
    b = cfg.analyst.backend
    if b == "codex":
        return run_codex(f"{system}\n{NO_TOOLS}\n\n{user}", model, cfg.analyst.codex_model)
    if b == "claude_code":
        return run_claude_cli(system, user, model, cfg)
    if b == "api":
        return run_api(system, user, model, cfg)
    raise SystemExit(f"analyst.backend: неизвестное значение {b!r} (codex | claude_code | api)")


def run_codex(prompt: str, model: type[T], codex_model: str | None = None) -> T:
    """Один запуск `codex exec` в пустой папке, без доступа на запись, с ответом по JSON-схеме."""
    with tempfile.TemporaryDirectory() as d:
        schema, out = os.path.join(d, "schema.json"), os.path.join(d, "out.json")
        work = os.path.join(d, "work")
        os.mkdir(work)
        with open(schema, "w") as f:
            json.dump(_schema(model), f)
        cmd = ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
               "-C", work, "--output-schema", schema, "-o", out]
        if codex_model:
            cmd += ["-m", codex_model]
        cmd.append("-")
        try:
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=600)
        except FileNotFoundError:
            raise SystemExit("Не найден `codex`. Установите Codex CLI или выберите другой бэкенд в config.yaml.")
        except subprocess.TimeoutExpired:
            raise RuntimeError("codex exec не ответил за 10 минут")
        try:
            with open(out, encoding="utf-8") as f:
                return model.model_validate_json(f.read())
        except (OSError, ValueError):
            msg = (r.stderr or r.stdout).strip()[-300:]
            low = msg.lower()
            if "login" in low or "unauthorized" in low:
                raise SystemExit("Codex не авторизован: выполните `codex login`.")
            if "limit" in low or "usage" in low:
                raise LimitReached(f"codex: {msg}")
            raise RuntimeError(f"codex exec: {msg or f'код выхода {r.returncode}'}")


def run_claude_cli(system: str, user: str, model: type[T], cfg: Config) -> T:
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
    if d.get("is_error") or not d.get("structured_output"):
        msg = str(d.get("result") or d.get("subtype"))[:300]
        if d.get("api_error_status") == 429 or "limit" in msg.lower():
            raise LimitReached(msg)
        raise RuntimeError(f"claude -p: {msg}")
    return model.model_validate(d["structured_output"])


_client = None


def run_api(system: str, user: str, model: type[T], cfg: Config) -> T:
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
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise RuntimeError(f"модель не вернула ответ (stop_reason={resp.stop_reason})")
    return resp.parsed_output
