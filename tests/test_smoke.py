"""Smoke: команды hh-agent запускаются настоящим процессом на временном конфиге и не падают с трейсбэком.
Сеть, браузер и LLM не нужны; LLM — подставная команда (analyst.backend: command)."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hh_agent import wizard
from hh_agent.config import load_config
from hh_agent.db import DB

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def home(tmp_path):
    """Папка агента: config.yaml из образца, резюме, база с парой вакансий."""
    shutil.copy(ROOT / "config.example.yaml", tmp_path / "config.yaml")
    shutil.copy(ROOT / "resume.example.md", tmp_path / "resume.md")
    (tmp_path / "resume.md").write_text("# ML Engineer\n\nPython, PyTorch.\n", encoding="utf-8")
    db = DB(tmp_path / "hh_agent.db")
    db.insert(1, title="ML Engineer", company="Acme", url="https://hh.ru/vacancy/1", status="scored", score=80)
    db.insert(2, title="Data Scientist", company="Beta", url="https://hh.ru/vacancy/2", status="manual",
              score=70, error="у работодателя тест")
    db.insert(1_000_000_001, title="MLOps", company="Gamma", url="https://career.habr.com/vacancies/1000000001",
              status="new", description="Описание")
    return tmp_path


def _cli(home: Path, *args: str, **kw) -> subprocess.CompletedProcess:
    r = subprocess.run([sys.executable, "-m", "hh_agent.cli", "-c", str(home / "config.yaml"), *args],
                       capture_output=True, text=True, cwd=home, timeout=60, stdin=subprocess.DEVNULL, **kw)
    assert "Traceback" not in r.stdout + r.stderr, r.stdout + r.stderr
    return r


def _set_analyst(home: Path, **values: str) -> None:
    """Задать analyst.* как setup — заменой строк, а не дописыванием: в YAML повторный ключ побеждает."""
    text = (home / "config.yaml").read_text(encoding="utf-8")
    for k, v in values.items():
        text = wizard._set(text, k, v)
    (home / "config.yaml").write_text(text, encoding="utf-8")
    cfg = load_config(home / "config.yaml")
    assert cfg.analyst.backend == "command" and cfg.analyst.fallback_backend is None  # настоящую LLM не зовём


def test_version_and_help(home):
    assert _cli(home, "--version").stdout.startswith("hh-agent ")
    out = _cli(home, "help").stdout
    assert all(cmd in out for cmd in ("autopilot", "review", "doctor"))


@pytest.mark.parametrize("args, expect", [
    (["stats"], "Найдено вакансий"),
    (["list", "scored"], "ML Engineer"),
    (["list", "manual"], "у работодателя тест"),
    (["requeue"], "Возвращено в очередь: 0"),  # тест работодателя без --all не возвращается
    (["requeue", "--all"], "Возвращено в очередь: 1"),
])
def test_offline_commands(home, args, expect):
    r = _cli(home, *args, env={**os.environ, "COLUMNS": "200"})
    assert r.returncode == 0 and expect in r.stdout


def test_interactive_command_refuses_without_terminal(home):
    r = _cli(home, "review")
    assert r.returncode != 0 and "интерактивная" in r.stderr


def test_doctor_runs(home):
    r = _cli(home, "doctor")
    assert r.returncode in (0, 1) and "Настройка" in r.stdout


def test_analyze_with_command_backend(home):
    """Оценка целиком: запрос уходит в программу (analyst.command), ответ ложится в базу."""
    reply = home / "reply.py"
    reply.write_text("import json, sys\nsys.stdin.read()\nprint(json.dumps({'items': [{'vacancy_id': 1000000001,"
                     " 'score': 88, 'verdict': 'apply', 'reasons': ['стек совпадает'], 'red_flags': [],"
                     " 'cover_letter': 'Здравствуйте!'}]}))\n", encoding="utf-8")
    _set_analyst(home, backend="command", letter_backend="same", command=f"{sys.executable} {reply}")
    r = _cli(home, "analyze")
    assert r.returncode == 0, r.stdout + r.stderr
    row = DB(home / "hh_agent.db").get(1_000_000_001)
    assert (row["status"], row["score"], row["cover_letter"]) == ("scored", 88, "Здравствуйте!")


def test_ctrl_c_stops_without_traceback(home):
    """Ctrl+C посреди запроса к LLM: «Остановлено», код 130, без трейсбэка и без ошибки в базе."""
    _set_analyst(home, backend="command", letter_backend="same", command="sleep 30")
    p = subprocess.Popen([sys.executable, "-m", "hh_agent.cli", "-c", str(home / "config.yaml"), "analyze"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=home,
                         start_new_session=True)
    time.sleep(3)  # запрос к «LLM» (sleep) уже идёт
    os.killpg(p.pid, signal.SIGINT)  # как Ctrl+C в терминале: сигнал всей группе процессов
    out, _ = p.communicate(timeout=30)
    assert p.returncode == 130, out
    assert "Остановлено (Ctrl+C)" in out and "Traceback" not in out
    assert DB(home / "hh_agent.db").get(1_000_000_001)["status"] == "new"  # оценится в следующий раз
