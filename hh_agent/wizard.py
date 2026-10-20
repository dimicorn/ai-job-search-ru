"""Мастер настройки (hh-agent setup; при первом запуске — сам): config.yaml из образца и выбор LLM.

Подходит любая LLM: Codex или Claude Code по подписке, Claude API, любой OpenAI-совместимый API
(облачный или локальный — Ollama, LM Studio) или своя команда. Ключ API хранится в .env рядом
с config.yaml (файл не попадает в git), в конфиг пишется только имя переменной.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import urllib.request
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .log import console

# Облачные OpenAI-совместимые API: (название, base_url, переменная с ключом).
CLOUD = [
    ("OpenAI", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    ("OpenRouter — сотни моделей по одному ключу", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    ("DeepSeek", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    ("Mistral", "https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    ("Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY"),
]
LOCAL = [("Ollama", "http://localhost:11434/v1"), ("LM Studio", "http://localhost:1234/v1")]


class _Ping(BaseModel):
    ok: bool


def _ask(prompt: str, default: str | None = None) -> str:
    while True:
        v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
        if v or default is not None:
            return v or default or ""


def _pick(title: str, options: list[str]) -> int:
    console.print(f"\n[bold]{title}[/]")
    for i, o in enumerate(options, 1):
        console.print(f"  {i}) {o}")
    while True:
        v = input("> ").strip()
        if v.isdigit() and 1 <= int(v) <= len(options):
            return int(v) - 1


def _yaml(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, (int, float, bool)):
        return json.dumps(v)
    if isinstance(v, str) and re.fullmatch(r"[A-Za-z][\w./:@-]*", v) and v.lower() not in (
            "null", "true", "false", "yes", "no", "on", "off"):
        return v  # простое значение — без кавычек, как в остальном конфиге
    return json.dumps(v, ensure_ascii=False)  # JSON-строка и JSON-объект — валидный YAML


def _set(text: str, key: str, value: Any) -> str:
    """Задать analyst.<key> в тексте config.yaml, не трогая комментарии; нет ключа — добавить после model."""
    start = text.index("\nanalyst:")
    end = re.search(r"\n\S", text[start + 1:])
    end = start + 1 + end.start() if end else len(text)
    sec = text[start:end]
    line = f"  {key}: {_yaml(value)}"
    pat = re.compile(rf"^  {re.escape(key)}:[^\n#]*?(\s+#[^\n]*)?$", re.M)
    if pat.search(sec):
        sec = pat.sub(lambda m: (line.ljust(36) + " " + m.group(1).lstrip()) if m.group(1) else line, sec, count=1)
    else:  # после строки model, а если её нет — в конец раздела
        added = re.sub(r"^(  model:[^\n]*)$", lambda m: m.group(1) + "\n" + line, sec, count=1, flags=re.M)
        sec = added if added != sec else sec.rstrip("\n") + "\n" + line
    return text[:start] + sec + text[end:]


def _save_key(base: Path, name: str, key: str) -> None:
    env = base / ".env"
    lines = [ln for ln in (env.read_text(encoding="utf-8").splitlines() if env.exists() else [])
             if not ln.startswith(f"{name}=")]
    env.write_text("\n".join([*lines, f"{name}={key}"]) + "\n", encoding="utf-8")
    env.chmod(0o600)
    console.print(f"Ключ сохранён в {env} (только для вас, в git не попадает).")


def _models(base_url: str, key: str) -> list[str]:
    """Модели сервера (GET /models), если он умеет их перечислять."""
    req = urllib.request.Request(base_url.rstrip("/") + "/models",
                                 headers={"Authorization": f"Bearer {key}"} if key else {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return sorted(m["id"] for m in json.loads(r.read()).get("data", []))
    except Exception:
        return []


def _model(base_url: str, key: str) -> str:
    found = _models(base_url, key)
    if found:
        console.print("Модели на сервере: " + ", ".join(found[:40]) + (" …" if len(found) > 40 else ""))
    return _ask("Модель (как в документации сервиса, например из списка выше)", found[0] if len(found) == 1 else None)


LLM_OPTIONS = [
    "Codex CLI — по подписке ChatGPT (нужен `codex login`)",
    "Claude Code CLI — по подписке claude.ai (нужен `claude`)",
    "Claude API — по ключу ANTHROPIC_API_KEY, оплата по токенам",
    "OpenAI-совместимый API в облаке — OpenAI, OpenRouter, DeepSeek, Gemini, Mistral, Groq или свой адрес",
    "Локальная модель — Ollama или LM Studio",
    "Своя команда — любая программа: запрос в stdin, JSON-ответ в stdout",
]


def _fallback(base: Path, main: str) -> dict[str, Any]:
    """Спросить запасную LLM — на случай, когда у основной кончатся лимиты."""
    i = _pick("Запасная LLM — на неё агент перейдёт, когда у основной кончатся лимиты:",
              ["Не нужна — при исчерпании лимитов остановиться", *LLM_OPTIONS])
    if i == 0:
        return {"fallback_backend": None, "fallback_settings": {}}
    v = _choose(base, i - 1)
    v.pop("letter_backend", None)
    fb = v.pop("backend")
    if fb == main:
        console.print("[yellow]Запасная LLM — тот же бэкенд, что и основная: при исчерпании лимитов "
                      "она не поможет, если это та же подписка или ключ.[/]")
    return {"fallback_backend": fb, "fallback_settings": v}


def _choose(base: Path, i: int | None = None) -> dict[str, Any]:
    """Спросить, какую LLM использовать (i — уже выбранный пункт LLM_OPTIONS); вернуть значения для analyst."""
    if i is None:
        i = _pick("Какую LLM использовать для оценки вакансий, писем и ответов на вопросы?", LLM_OPTIONS)
    if i == 0:
        if not shutil.which("codex"):
            console.print("[yellow]codex не найден в PATH — установите Codex CLI и выполните `codex login`.[/]")
        return {"backend": "codex", "letter_backend": "codex"}
    if i == 1:
        if not shutil.which("claude"):
            console.print("[yellow]claude не найден в PATH — установите Claude Code.[/]")
        return {"backend": "claude_code", "letter_backend": "claude_code",
                "model": _ask("Модель Claude", "claude-opus-5-5")}
    if i == 2:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            console.print("[yellow]Задайте переменную ANTHROPIC_API_KEY (например, в ~/.zshrc).[/]")
        return {"backend": "api", "letter_backend": "api", "model": _ask("Модель Claude", "claude-opus-5-5")}
    if i == 3:
        j = _pick("Сервис:", [name for name, _, _ in CLOUD] + ["Другой OpenAI-совместимый адрес"])
        if j < len(CLOUD):
            _, url, env = CLOUD[j]
        else:
            url = _ask("Адрес API (до /chat/completions, например https://example.com/v1)")
            env = _ask("Имя переменной для ключа", "LLM_API_KEY")
        key = os.environ.get(env) or ""
        if not key:
            key = getpass.getpass(f"Ключ API (ввод скрыт; сохраню в .env как {env}): ").strip()
            if key:
                _save_key(base, env, key)
        return {"backend": "openai", "letter_backend": "openai", "base_url": url, "api_key_env": env,
                "model": _model(url, key)}
    if i == 4:
        j = _pick("Сервер:", [f"{name} — {url}" for name, url in LOCAL] + ["Другой локальный адрес"])
        url = LOCAL[j][1] if j < len(LOCAL) else _ask("Адрес API (до /chat/completions)", "http://localhost:8000/v1")
        if not _models(url, ""):
            console.print(f"[yellow]{url} не отвечает — запустите сервер (например, `ollama serve`).[/]")
        return {"backend": "openai", "letter_backend": "openai", "base_url": url, "api_key_env": None,
                "model": _model(url, "")}
    console.print("Команда получает весь запрос в stdin и должна напечатать JSON-объект по схеме из запроса.")
    return {"backend": "command", "letter_backend": "command",
            "command": _ask("Команда (например, ollama run qwen3:32b)")}


def setup(config_path: Path) -> None:
    from .config import load_config
    from .llm import ask_backend, fallback_cfg

    base = config_path.parent
    if not config_path.exists():
        shutil.copy(base / "config.example.yaml", config_path)
        console.print(f"Создан {config_path.name} из config.example.yaml.")
    for src, dst in (("resume.example.md", "resume.md"), ("faq.example.yaml", "faq.yaml")):
        if (base / src).exists() and not (base / dst).exists():
            shutil.copy(base / src, base / dst)
            console.print(f"Создан {dst} из {src}.")

    values = _choose(base)
    values.update(_fallback(base, values["backend"]))
    text = config_path.read_text(encoding="utf-8")
    for k, v in values.items():
        text = _set(text, k, v)
    config_path.write_text(text, encoding="utf-8")
    console.print(f"[green]LLM записана в {config_path.name}:[/] " + ", ".join(f"{k}={v}" for k, v in values.items()))

    cfg = load_config(config_path)
    checks = [("Основная LLM", cfg, cfg.analyst.backend)]
    if cfg.analyst.fallback_backend:
        checks.append(("Запасная LLM", fallback_cfg(cfg), cfg.analyst.fallback_backend))
    for name, c, backend in checks:
        console.print(f"{name} ({backend}): проверяю тестовым запросом…")
        try:
            ok = ask_backend(c, backend, "Проверка связи.", 'Верни {"ok": true}.', _Ping, "other").ok
            console.print("[green]отвечает по схеме.[/]" if ok else "[yellow]ответила, но ok=false.[/]")
        except (SystemExit, Exception) as e:
            console.print(f"[red]Тестовый запрос не прошёл:[/] {e}\nИсправьте раздел analyst в {config_path.name} "
                          "или запустите uv run hh-agent setup ещё раз.")
    console.print("\nДальше: заполните resume.md, в config.yaml — searches, analyst.preferences и questions.facts; "
                  "затем uv run hh-agent login.")
