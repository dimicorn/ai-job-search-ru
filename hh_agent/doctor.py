"""hh-agent doctor — проверить, всё ли готово к работе, и подсказать, как починить.

Быстрые проверки — без сети: окружение, конфиг, резюме, факты, Chromium, LLM, Telegram, расписание.
--online — ещё и вход на сайты и в Telegram и тестовый запрос к основной и запасной LLM
(дольше и тратит немного токенов). Код выхода 1, если есть ошибки (✗); предупреждения (!) — нет.
"""
from __future__ import annotations

import fcntl
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from .log import console

OK, WARN, FAIL = "ok", "warn", "fail"
_MARK = {OK: "[green]✓[/]", WARN: "[yellow]![/]", FAIL: "[red]✗[/]"}


class _Ping(BaseModel):
    ok: bool


class Doctor:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.results.append((status, name, detail))
        console.print(f" {_MARK[status]} {name}" + (f" — {detail}" if detail else ""), highlight=False)

    def section(self, title: str) -> None:
        console.print(f"\n[bold]{title}[/]")


def _run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _env(d: Doctor) -> None:
    d.section("Окружение")
    system = platform.system()
    wsl = "microsoft" in platform.release().lower()
    if system == "Darwin":
        d.add(OK, "ОС", f"macOS {platform.mac_ver()[0]}")
    elif system == "Linux":
        d.add(WARN if not wsl else OK, "ОС", ("WSL" if wsl else "Linux") +
              " — работает, но без уведомлений macOS, caffeinate и launchd (расписание — через cron)")
    else:
        d.add(FAIL, "ОС", f"{system} не поддерживается — запускайте в WSL")
    d.add(OK if sys.version_info >= (3, 11) else FAIL, "Python", platform.python_version())
    from .cli import _version
    d.add(OK, "hh-agent", _version())


def _config(d: Doctor, config_path: Path):
    from .config import load_config

    d.section("Настройка")
    if not config_path.exists():
        d.add(FAIL, config_path.name, "нет — запустите uv run hh-agent setup")
        return None
    try:
        cfg = load_config(config_path)
    except (SystemExit, Exception) as e:
        d.add(FAIL, config_path.name, f"не читается: {str(e)[:200]}")
        return None
    sources = ", ".join(cfg.sources())
    d.add(OK, config_path.name, f"поисков: {len(cfg.searches)} ({sources})")

    from .cli import resume_is_template
    path = cfg.path(cfg.analyst.resume_file)
    if resume_is_template(cfg):
        d.add(FAIL, path.name, "не заполнен — uv run hh-agent resume (скачать с hh.ru) или вставьте текст сами")
    else:
        n = len(path.read_text(encoding="utf-8").split())
        d.add(OK if n >= 150 else WARN, path.name,
              f"{n} слов" + ("" if n >= 150 else " — маловато: чем больше конкретики, тем точнее оценка и письма"))

    facts = [ln for ln in cfg.questions.facts.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    d.add(OK if facts else WARN, "questions.facts",
          f"{len(facts)} строк" if facts else "пусто — ответы на анкеты про зарплату, город, сроки выхода уйдут в manual")
    if cfg.questions.faq:
        faq = cfg.path(cfg.questions.faq_file)
        d.add(OK if faq.exists() else WARN, faq.name, "есть" if faq.exists() else "нет — скопируйте faq.example.yaml")
    if not cfg.analyst.preferences.strip():
        d.add(WARN, "analyst.preferences", "пусто — LLM не знает, какие вакансии вам интересны")
    return cfg


def _browser(d: Doctor, cfg) -> bool:
    """Chromium и профиль; True — профиль свободен (можно проверять вход на сайты)."""
    d.section("Браузер")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            exe = Path(p.chromium.executable_path)
        d.add(OK if exe.exists() else FAIL, "Chromium Playwright",
              "установлен" if exe.exists() else "нет — uv run playwright install chromium")
    except Exception as e:
        d.add(FAIL, "Chromium Playwright", f"{str(e)[:150]} — uv run playwright install chromium")
    profile = cfg.path(cfg.browser.profile_dir)
    d.add(OK if profile.exists() else WARN, "Профиль браузера",
          "есть" if profile.exists() else "ещё нет — uv run hh-agent login")
    lock = profile.parent / ".hh-agent.lock"
    if not lock.exists():
        return True
    with open(lock) as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
            return True
        except BlockingIOError:
            d.add(WARN, "Профиль занят", "сейчас идёт другой запуск hh-agent (autopilot, review…) — вход на сайты не проверяю")
            return False


def _llm(d: Doctor, cfg) -> None:
    from . import llm

    d.section("LLM")
    backends = [("основная", cfg.analyst.backend, cfg)]
    if cfg.analyst.letter_backend not in ("same", cfg.analyst.backend):
        backends.append(("письма", cfg.analyst.letter_backend, cfg))
    if cfg.analyst.fallback_backend:
        backends.append(("запасная", cfg.analyst.fallback_backend, llm.fallback_cfg(cfg)))
    else:
        d.add(WARN, "запасная LLM", "не задана — когда у основной кончатся лимиты, агент остановится "
                                    "(analyst.fallback_backend или uv run hh-agent setup)")
    for role, b, c in backends:
        name = f"{role}: {b}"
        a = c.analyst
        if b == "codex":
            if not shutil.which("codex"):
                d.add(FAIL, name, "нет codex в PATH — установите Codex CLI")
                continue
            code, out = _run(["codex", "login", "status"])
            d.add(OK if code == 0 and "logged in" in out.lower() else FAIL, name,
                  f"{a.codex_model or 'модель по умолчанию'}, effort {a.codex_effort or 'по умолчанию'}" if code == 0
                  else "нет входа — codex login")
        elif b == "claude_code":
            d.add(OK if shutil.which("claude") else FAIL, name,
                  f"{a.model}, effort {a.effort}" if shutil.which("claude") else "нет claude в PATH — установите Claude Code")
        elif b == "api":
            d.add(OK if os.environ.get("ANTHROPIC_API_KEY") else FAIL, name,
                  a.model if os.environ.get("ANTHROPIC_API_KEY") else "нет ANTHROPIC_API_KEY в окружении")
        elif b == "openai":
            if not a.base_url or not a.model:
                d.add(FAIL, name, "задайте base_url и model (uv run hh-agent setup)")
            elif a.api_key_env and not llm.api_key(c):
                d.add(FAIL, name, f"нет ключа {a.api_key_env} (в окружении или в .env)")
            else:
                d.add(OK, name, f"{a.model} @ {a.base_url}")
        elif b == "command":
            prog = (a.command or "").split()[0] if a.command else ""
            d.add(OK if prog and shutil.which(prog) else FAIL, name, a.command or "задайте analyst.command")
        else:
            d.add(FAIL, name, f"неизвестный бэкенд (варианты: {', '.join(llm.BACKENDS)})")
    try:
        limits = json.loads(cfg.path(llm.LIMITS_FILE).read_text())
    except (OSError, ValueError):
        limits = {}
    for b, until in limits.items():
        if until > time.time():
            d.add(WARN, f"лимит {b}", f"до {datetime.fromtimestamp(until):%d.%m %H:%M} запросы идут в запасную LLM")


def _telegram(d: Doctor, cfg) -> None:
    t = cfg.telegram
    d.section("Telegram (AI-рекрутеры)")
    if not t.enabled:
        d.add(OK, "выключено", "telegram.enabled: false — включайте, если откликаетесь в Сбер и т.п. (docs/telegram.md)")
        return
    if not (t.api_id and t.api_hash):
        d.add(FAIL, "ключи", "задайте telegram.api_id и api_hash (my.telegram.org → API development tools)")
        return
    session = cfg.path(t.session)
    if not session.with_suffix(".session").exists() and not session.exists():
        d.add(FAIL, "сессия", "нет входа — uv run hh-agent login")
        return
    d.add(OK, "ключи и сессия", f"боты: {', '.join(t.bots) or '—'}")


def _schedule(d: Doctor) -> None:
    if platform.system() != "Darwin":
        return
    d.section("Расписание")
    agent = Path.home() / "Library/LaunchAgents/ru.hh-agent.autopilot.plist"
    d.add(OK, "ежедневный autopilot", "включён (launchd)" if agent.exists()
          else "не включён — при желании: ./setup.sh (последний шаг)")


def _telegram_online(d: Doctor, cfg) -> None:
    t = cfg.telegram
    if not (t.enabled and t.api_id and t.api_hash):
        return
    from .recruiters import _client, session_lock
    lock = session_lock(cfg)
    if lock is None:  # второе подключение с той же сессией порвёт соединение у идущего запуска
        d.add(WARN, "вход в Telegram", "сессия занята другим запуском hh-agent — не проверяю")
        return
    try:
        client = _client(cfg)
        client.connect()
        try:
            ok = client.is_user_authorized()
        finally:
            client.disconnect()
        d.add(OK if ok else FAIL, "вход в Telegram", "есть" if ok else "нет — uv run hh-agent login")
    except Exception as e:
        d.add(WARN, "вход в Telegram", f"не проверить: {str(e)[:120]}")
    finally:
        lock.close()


def _online(d: Doctor, cfg, profile_free: bool) -> None:
    from . import llm
    from .browser import SITES, open_browser

    d.section("Онлайн")
    if profile_free:
        from .browser import is_logged_in
        try:
            with open_browser(cfg, headless=True) as (_, page):
                for src in cfg.sources():
                    ok = is_logged_in(page, src)
                    d.add(OK if ok else FAIL, f"вход на {SITES[src]}", "есть" if ok else "нет — uv run hh-agent login")
        except Exception as e:
            d.add(FAIL, "вход на сайты", f"не проверить: {str(e)[:150]}")
    _telegram_online(d, cfg)
    checks = [("основная", cfg.analyst.backend, cfg)]
    if cfg.analyst.fallback_backend:
        checks.append(("запасная", cfg.analyst.fallback_backend, llm.fallback_cfg(cfg)))
    for role, b, c in checks:
        started = time.monotonic()
        try:
            ok = llm.ask_backend(c, b, "Проверка связи.", 'Верни {"ok": true}.', _Ping, "other").ok
            d.add(OK if ok else WARN, f"LLM {role} ({b})", f"отвечает по схеме за {time.monotonic() - started:.0f} с")
        except llm.LimitReached as e:
            d.add(WARN, f"LLM {role} ({b})", f"лимит: {str(e)[:150]}")
        except (SystemExit, Exception) as e:
            d.add(FAIL, f"LLM {role} ({b})", str(e)[:200])


def run(config_path: Path, online: bool = False) -> int:
    d = Doctor()
    _env(d)
    cfg = _config(d, config_path)
    if cfg is not None:
        profile_free = _browser(d, cfg)
        _llm(d, cfg)
        _telegram(d, cfg)
        _schedule(d)
        if online:
            _online(d, cfg, profile_free)
    fails = sum(s == FAIL for s, _, _ in d.results)
    warns = sum(s == WARN for s, _, _ in d.results)
    console.print()
    if fails:
        console.print(f"[red]Ошибок: {fails}[/], предупреждений: {warns} — исправьте то, что отмечено ✗.")
    else:
        console.print("[green]Всё готово к работе[/]" + (f", предупреждений: {warns}" if warns else "") +
                      ("" if online else ". Вход на сайты и LLM проверит uv run hh-agent doctor --online."))
    return 1 if fails else 0
