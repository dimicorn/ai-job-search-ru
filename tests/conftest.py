"""Общие фикстуры: конфиг из config.example.yaml и база — во временной папке теста, поэтому тесты
не трогают ваши config.yaml, журнал и базу. FakeBot — подставной Telegram-бот для recruiters."""
from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from hh_agent import llm, recruiters
from hh_agent.config import load_config
from hh_agent.db import DB

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg(tmp_path):
    shutil.copy(ROOT / "config.example.yaml", tmp_path / "config.yaml")
    return load_config(tmp_path / "config.yaml")


@pytest.fixture
def db(cfg):
    return DB(cfg.path(cfg.db_path))


@pytest.fixture(autouse=True)
def _fresh_llm_state():
    """Лимиты бэкендов и форматы ответа — глобальное состояние модуля llm."""
    llm._limited = None
    llm._FORMAT.clear()
    yield
    llm._limited = None


class Button:
    def __init__(self, text: str, kind: str = "ButtonTypeDefault"):
        self.text = text
        self.button = SimpleNamespace(type=type(kind, (), {})())


class FakeBot:
    """Чат с ботом: messages — от старых к новым; на каждое наше сообщение бот отвечает reply(text)."""

    def __init__(self, reply=None):
        self.messages: list[SimpleNamespace] = []
        self.sent: list[str] = []
        self.reply = reply or (lambda text: None)

    def add(self, text: str, ago: timedelta = timedelta(0), out: bool = False, buttons=None):
        m = SimpleNamespace(id=len(self.messages) + 1, out=out, message=text, date=datetime.now(UTC) - ago,
                            buttons=buttons, edit_date=None)
        m.click = lambda i, **kw: self.on_click(m, i)
        self.messages.append(m)
        return m

    def on_click(self, m, i):
        pass

    # то, что recruiters вызывает у клиента Telethon
    def send_message(self, entity, text, **kw):
        self.sent.append(text)
        self.add(text, out=True)
        answer = self.reply(text)
        if answer:
            self.add(*answer) if isinstance(answer, tuple) else self.add(answer)


@pytest.fixture
def bot(monkeypatch):
    """Подставной бот и заглушки сети Telegram для recruiters._dialog."""
    b = FakeBot()
    monkeypatch.setattr(recruiters, "_messages", lambda client, entity, n: b.messages[-n:])
    monkeypatch.setattr(recruiters, "_switch_command", lambda client, entity: "/change_vacancy")
    monkeypatch.setattr(recruiters, "_wait_bot", lambda *a, **k: True)
    monkeypatch.setattr(recruiters, "_idle", lambda seconds: None)
    return b
