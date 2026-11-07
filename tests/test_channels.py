"""Вакансии из Telegram-каналов: чтение новых постов, оценка, курсор канала. Telegram и LLM подменены."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from hh_agent import channels as ch
from hh_agent import recruiters as r
from hh_agent.llm import LimitReached

from .conftest import Button, FakeBot

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


# ── отклик кнопкой под постом бота вакансий (getmatch: @g_jobbot) ──────────
BOT = "g_jobbot"
HEAD = "ML Engineer [Remote] @ Acme"
POST = f"{HEAD}\n\n📍Москва\n\nСтек: Python, PyTorch. " + "Задачи интересные. " * 10
SENT = f"{HEAD}\n\n✅ Отклик отправлен.\n\n Отменить действие: /discard_x"


def _apply_btns(data=b"application__send__37153"):
    apply = Button("👉 Откликнуться в боте", "InlineButtonTypeCallback")
    apply.data = data
    return [[apply], [Button("Описание", "InlineButtonTypeUrlAuth")]]


class JobBot(FakeBot):
    """Чат с ботом вакансий — и как клиент Telethon для channels.apply_posts."""

    def get_entity(self, name):
        return SimpleNamespace(username=name, id=1)

    def get_messages(self, entity, ids):
        return next((m for m in self.messages if m.id == ids), None)


@pytest.fixture
def jobbot(cfg, bot, monkeypatch):
    cfg.telegram.enabled, cfg.telegram.channels = True, ["@g_jobbot"]
    cfg.analyst.min_score, cfg.apply.auto.min_score = 65, 70
    b = JobBot()
    monkeypatch.setattr(r, "_messages", lambda client, entity, n: b.messages[-n:])
    monkeypatch.setattr(ch, "notify", lambda *a: None)
    monkeypatch.setattr(r, "_decide", lambda *a, **k: pytest.fail("LLM не нужна"))
    return b


def _post(db, b, score=80, ago=timedelta(hours=1), apply_data="application__send__37153"):
    m = b.add(POST, ago, buttons=_apply_btns())
    db.save_tg_post(BOT, m.id, posted_at=(datetime.now() - ago).isoformat(timespec="seconds"),
                    link=f"https://t.me/{BOT}/{m.id}", title="ML Engineer", company="Acme", score=score,
                    reasons="стек совпадает", status="vacancy", text=POST, found_at="", apply_data=apply_data)
    return m


def _rstep(action, button=-1, text="", missing=""):
    return r.Step(action=action, text=text, button=button, missing_fact=missing, reason="", final=False, reusable=False)


def test_apply_button_found_by_text_and_callback():
    b = FakeBot()
    assert ch.apply_button(b.add(POST, buttons=_apply_btns())) == "application__send__37153"
    assert ch.apply_button(b.add(POST, buttons=[[Button("Описание", "InlineButtonTypeUrlAuth")]])) is None
    assert ch.apply_button(b.add(POST)) is None


def test_scan_stores_apply_button_and_skips_confirmations(cfg, db, chan, monkeypatch):
    m = chan.add(VACANCY)
    m.buttons = _apply_btns()
    chan.add(SENT + " Гражданство: Россия. " * 5)  # подтверждение отклика длиннее MIN_CHARS — не в LLM
    _llm(monkeypatch, {1: (True, 80, "ML Engineer")})
    ch.scan(cfg, db, chan)
    assert db.tg_post("ml_jobs", 1)["apply_data"] == "application__send__37153"
    assert db.tg_post("ml_jobs", 2)["status"] == "other"


def test_auto_press_confirmed_by_bot(cfg, db, jobbot):
    _post(db, jobbot)
    clicks = []
    jobbot.on_click = lambda m, i: (clicks.append(i), jobbot.add(SENT))
    assert ch.apply_posts(cfg, db, jobbot, "auto") == 1
    post = db.tg_post(BOT, 1)
    assert clicks == [0] and post["status"] == "applied" and "Отклик отправлен" in post["note"]
    assert db.applied_today() == 1  # в общем дневном лимите


def test_bot_question_is_answered_with_button(cfg, db, jobbot, monkeypatch):
    _post(db, jobbot)
    ask = "Подтвердите гражданство: 🇷🇺 Россия"
    jobbot.on_click = lambda m, i: jobbot.add(ask, buttons=[[Button("Подтвердить", "InlineButtonTypeCallback")]]) \
        if m.message == POST else jobbot.add(SENT)
    seen = {}

    def decide(cfg_, db_, msgs, vid, survey, hanging=None, vacancy=None):
        seen.update(msgs=[m.message for m in msgs], vacancy=vacancy)
        return _rstep("button", 0)

    monkeypatch.setattr(r, "_decide", decide)
    assert ch.apply_posts(cfg, db, jobbot, "auto") == 1
    assert seen == {"msgs": [ask], "vacancy": POST} and db.tg_post(BOT, 1)["status"] == "applied"


def test_unknown_question_goes_to_human(cfg, db, jobbot, monkeypatch):
    _post(db, jobbot)
    jobbot.on_click = lambda m, i: jobbot.add("Пришлите ссылку на GitHub")
    monkeypatch.setattr(r, "_decide", lambda *a, **k: _rstep("human", missing="ссылка на GitHub"))
    assert ch.apply_posts(cfg, db, jobbot, "auto") == 0
    post = db.tg_post(BOT, 1)
    assert post["status"] == "human" and post["note"] == "ссылка на GitHub"
    assert "GitHub" in cfg.path("logs/questions.md").read_text(encoding="utf-8")
    # вы ответили боту сами, он подтвердил — в следующий раз агент это видит и ничего не жмёт
    jobbot.add(SENT)
    jobbot.on_click = lambda m, i: pytest.fail("второй раз не нажимаем")
    ch.apply_posts(cfg, db, jobbot, "auto")
    assert db.tg_post(BOT, 1)["status"] == "applied"


def test_silent_bot_is_unconfirmed(cfg, db, jobbot):
    _post(db, jobbot)
    assert ch.apply_posts(cfg, db, jobbot, "auto") == 0
    assert db.tg_post(BOT, 1)["status"] == "unconfirmed" and db.applied_today() == 1


def test_manual_apply_is_recognised_without_click(cfg, db, jobbot):
    _post(db, jobbot)
    jobbot.add(SENT)  # вы откликнулись в Telegram сами
    jobbot.on_click = lambda m, i: pytest.fail("отклик уже есть")
    ch.apply_posts(cfg, db, jobbot, "auto")
    assert db.tg_post(BOT, 1)["status"] == "applied"


def test_old_post_without_stored_button_is_checked_live(cfg, db, jobbot):
    _post(db, jobbot, apply_data=None)  # сохранён до появления откликов кнопкой
    jobbot.on_click = lambda m, i: jobbot.add(SENT)
    assert ch.apply_posts(cfg, db, jobbot, "auto") == 1
    assert db.tg_post(BOT, 1)["apply_data"] == "application__send__37153"


@pytest.mark.parametrize("setup", ["low_score", "daily_limit", "dry_run", "too_old"])
def test_no_click(cfg, db, jobbot, setup):
    _post(db, jobbot, score=68 if setup == "low_score" else 80,  # 68 < apply.auto.min_score
          ago=timedelta(days=ch.APPLY_DAYS + 1) if setup == "too_old" else timedelta(hours=1))
    if setup == "daily_limit":
        cfg.apply.daily_limit = 0
    jobbot.on_click = lambda m, i: pytest.fail("нажимать нельзя")
    assert ch.apply_posts(cfg, db, jobbot, "auto", dry_run=setup == "dry_run") == 0
    assert db.tg_post(BOT, 1)["status"] == "vacancy"


def test_ask_mode(cfg, db, jobbot, monkeypatch):
    _post(db, jobbot, score=68)  # ниже порога auto, но не ниже analyst.min_score — спрашиваем
    _post(db, jobbot, score=66)
    answers = iter(["?", "y", "s"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    jobbot.on_click = lambda m, i: jobbot.add(SENT) if m.id == 1 else pytest.fail("пропущенную не жмём")
    assert ch.apply_posts(cfg, db, jobbot, "ask") == 1
    assert db.tg_post(BOT, 1)["status"] == "applied" and db.tg_post(BOT, 2)["status"] == "skipped"
