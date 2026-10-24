"""Вакансии из Telegram-каналов: чтение новых постов, оценка, курсор канала. Telegram и LLM подменены."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from hh_agent import channels as ch
from hh_agent.llm import LimitReached

VACANCY = "Ищем ML-инженера в команду компьютерного зрения. " * 5  # длиннее MIN_CHARS


class FakeChannel:
    """Канал: posts — от старых к новым; iter_messages отдаёт их, как Telethon, — от новых к старым."""

    def __init__(self, username="ml_jobs"):
        self.entity = SimpleNamespace(username=username, id=777)
        self.posts: list[SimpleNamespace] = []

    def add(self, text: str, ago: timedelta = timedelta(0)):
        m = SimpleNamespace(id=len(self.posts) + 1, message=text, date=datetime.now(UTC) - ago)
        self.posts.append(m)
        return m

    def get_entity(self, name):
        return self.entity

    def iter_messages(self, entity, min_id=0, limit=None):
        return iter([m for m in reversed(self.posts) if m.id > min_id][:limit])


@pytest.fixture
def chan(cfg, monkeypatch):
    cfg.telegram.channels = ["@ML_jobs"]
    cfg.analyst.min_score = 65
    monkeypatch.setattr(ch, "notify", lambda *a: None)
    return FakeChannel()


def _llm(monkeypatch, verdicts: dict[int, tuple[bool, int, str]], seen: list | None = None):
    """Подставная оценка: id поста → (вакансия ли, балл, должность); id нет в verdicts — модель его пропустила."""
    def assess(cfg, posts):
        if seen is not None:
            seen.extend(m.id for m in posts)
        return {m.id: ch.PostItem(post_id=m.id, is_vacancy=verdicts[m.id][0], title=verdicts[m.id][2], company="Acme",
                                  score=verdicts[m.id][1], reasons=["стек совпадает"])
                for m in posts if m.id in verdicts}
    monkeypatch.setattr(ch, "_assess", assess)


@pytest.mark.parametrize("raw", ["@ML_jobs", "ml_jobs", "t.me/ml_jobs", "https://t.me/ml_jobs/123", "https://t.me/s/ml_jobs"])
def test_channel_name(raw):
    assert ch.channel_name(raw) == "ml_jobs"


def test_first_read_saves_matching_vacancies_with_links(cfg, db, chan, monkeypatch):
    chan.add(VACANCY, ago=timedelta(days=10))  # старше channels_days — не читаем
    chan.add(VACANCY)                          # 2 — подходит
    chan.add("Вебинар по ML завтра в 19:00")   # 3 — короткий: не вакансия, без LLM
    chan.add(VACANCY)                          # 4 — балл ниже порога
    chan.add("Курс по DS со скидкой. " * 10)   # 5 — не вакансия
    seen = []
    _llm(monkeypatch, {2: (True, 80, "ML Engineer"), 4: (True, 40, "Data Engineer"), 5: (False, 0, "")}, seen)
    assert ch.scan(cfg, db, chan) == 1
    assert seen == [2, 4, 5]
    assert db.tg_post("ml_jobs", 2)["link"] == "https://t.me/ml_jobs/2"
    assert {r["status"] for r in map(lambda i: db.tg_post("ml_jobs", i), (3, 4, 5))} == {"other", "low"}
    assert db.tg_post("ml_jobs", 1) is None
    assert [r["msg_id"] for r in db.tg_posts("vacancy", "2000-01-01")] == [2]


def test_next_read_takes_only_new_posts(cfg, db, chan, monkeypatch):
    chan.add(VACANCY)
    _llm(monkeypatch, {1: (True, 80, "ML Engineer")})
    ch.scan(cfg, db, chan)
    chan.add(VACANCY)
    seen = []
    _llm(monkeypatch, {2: (True, 90, "CV Engineer")}, seen)
    assert ch.scan(cfg, db, chan) == 1 and seen == [2]


def test_skipped_post_is_retried_and_cursor_waits(cfg, db, chan, monkeypatch):
    """Модель пропустила пост: курсор не сдвигается, в следующий раз в LLM идёт только он."""
    for _ in range(3):
        chan.add(VACANCY)
    chan.add("Короткий анонс")
    _llm(monkeypatch, {1: (True, 80, "A"), 3: (True, 80, "C")})
    ch.scan(cfg, db, chan)
    assert db.tg_channel_cursor("ml_jobs") == 0 and db.tg_post("ml_jobs", 2) is None
    seen = []
    _llm(monkeypatch, {2: (True, 70, "B")}, seen)
    ch.scan(cfg, db, chan)
    assert seen == [2] and db.tg_channel_cursor("ml_jobs") == 4


def test_llm_limit_stops_without_losing_posts(cfg, db, chan, monkeypatch):
    chan.add(VACANCY)

    def limited(cfg, posts):
        raise LimitReached("кончились лимиты")

    monkeypatch.setattr(ch, "_assess", limited)
    assert ch.scan(cfg, db, chan) == 0
    assert db.tg_channel_cursor("ml_jobs") == 0 and db.tg_post("ml_jobs", 1) is None


def test_title_stop_words_apply(cfg, db, chan, monkeypatch):
    cfg.filters.title_exclude = ["senior"]
    chan.add(VACANCY)
    _llm(monkeypatch, {1: (True, 90, "Senior ML Engineer")})
    assert ch.scan(cfg, db, chan) == 0
    assert db.tg_post("ml_jobs", 1)["status"] == "filtered"


def test_private_channel_link():
    assert ch.post_link(SimpleNamespace(username=None, id=12345), "x", 7) == "https://t.me/c/12345/7"


def test_prompt_has_resume_and_posts(cfg, db, chan, monkeypatch):
    cfg.path(cfg.analyst.resume_file).write_text("# Резюме\nPyTorch, OpenCV", encoding="utf-8")
    calls = []

    def ask_json(cfg_, system, user, model, purpose):
        calls.append((system, user, purpose))
        return ch.PostBatch(items=[ch.PostItem(post_id=1, is_vacancy=True, title="ML", company="", score=70, reasons=[])])

    monkeypatch.setattr(ch, "ask_json", ask_json)
    chan.add(VACANCY)
    assert ch.scan(cfg, db, chan) == 1
    system, user, purpose = calls[0]
    assert "PyTorch, OpenCV" in system and '<post id="1">' in user and purpose == "channels"
