from datetime import datetime, timedelta

import pytest

from hh_agent import recruiters as r

from .conftest import Button, FakeBot

BOT = "giga_recruiter_bot"
NO_MORE = "Сейчас у вас нет других вакансий, по которым можно пройти первичное интервью😉"
LIST = "Вы откликались на несколько вакансий. По какой из них продолжим?"
DONE = "Спасибо! Резюме передано рекрутеру."


@pytest.mark.parametrize("text, expected", [
    (NO_MORE, True),
    ("Больше нет вакансий для интервью", True),
    ("Есть ли у вас опыт с вакансиями такого типа?", False),  # вопрос, а не «вакансий нет»
    ("Получил Ваш отклик на позицию ML-инженер.", False),
    ("Спасибо за выбор вакансии!", False),
])
def test_no_more(text, expected):
    assert r._no_more(FakeBot().add(text)) is expected


def test_choice_reads_vacancy_list_by_bot_phrase():
    b = FakeBot()
    m = b.add(LIST, buttons=[[Button("1. Data Scientist")], [Button("2) ML Engineer")], [Button("Далее")]])
    assert r._choice(m) == [(0, "Data Scientist"), (1, "ML Engineer")]
    # нумерованные кнопки без фразы о выборе (например, оценка опроса) — не список вакансий
    assert r._choice(b.add("Оцените, пожалуйста, опрос", buttons=[[Button("1. Плохо")], [Button("5. Отлично")]])) is None


def test_current_survey_from_greeting():
    b = FakeBot()
    b.add("Получил Ваш отклик на позицию Senior CV Engineer.\nРасскажите о себе?")
    b.add("Ответ", out=True)
    assert r._current_survey(b.messages) == "Senior CV Engineer"


def test_human_pause_respects_limit(cfg):
    t = cfg.telegram
    assert 0 <= r._human_pause(t, "вопрос" * 200, "ответ" * 200, spent=0) <= t.max_reply_seconds
    assert r._human_pause(t, "?", "да", spent=10_000) == 0  # LLM думала дольше паузы — отвечаем сразу


def test_ago():
    assert r._ago(timedelta(minutes=41)) == "41 мин"
    assert r._ago(timedelta(hours=5, minutes=19)) == "5 ч 19 мин"


# ── когда просить бота сменить вакансию ─────────────────────────────
def _apply(db, vid, title, minutes_ago, company="Сбер"):
    db.insert(vid, title=title, company=company, status="applied",
              applied_at=(datetime.now() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds"))


def _want(cfg, db, b):
    return r._want_switch(cfg, db, None, BOT, None)


def test_want_switch_after_no_more(cfg, db, bot):
    db.save_tg_survey(BOT, "data scientist", "done")
    bot.add(LIST, timedelta(minutes=20), buttons=[[Button("1. Data Scientist")], [Button("2. ML Engineer")]])
    assert _want(cfg, db, bot)  # в списке есть вакансия без опроса

    bot.add(NO_MORE, timedelta(minutes=5))
    assert not _want(cfg, db, bot)  # бот сказал, что других нет: старый список не в счёт

    _apply(db, 1, "CV Engineer", minutes_ago=10)
    assert not _want(cfg, db, bot)  # отклик был до ответа «нет других»

    _apply(db, 2, "NLP Engineer", minutes_ago=1)
    assert _want(cfg, db, bot)  # новый отклик после него — повод спросить снова

    bot.add(LIST, buttons=[[Button("1. Data Scientist")], [Button("2. ML Engineer")]])
    assert _want(cfg, db, bot)  # новый список от бота


# ── диалог целиком ───────────────────────────────────────────────────
def _dialog(cfg, db, b):
    return r._dialog(cfg, db, b, BOT, None, dry_run=False)


def _start(db, b, text, ago):
    """В чате одно сообщение бота, уже обработанное в прошлом запуске; опрос закончен."""
    m = b.add(text, ago)
    db.save_tg_dialog(BOT, status="done", last_seen_id=m.id)


def test_idle_dialog_asks_for_another_vacancy_and_stops_on_no_more(cfg, db, bot, monkeypatch):
    monkeypatch.setattr(r, "_decide", lambda *a, **k: pytest.fail("LLM не нужна"))
    _start(db, bot, DONE, timedelta(hours=1))
    bot.reply = lambda text: NO_MORE
    assert _dialog(cfg, db, bot) == "done"
    assert bot.sent == ["/change_vacancy"]


def test_recent_no_more_is_not_asked_again(cfg, db, bot):
    _start(db, bot, NO_MORE, timedelta(hours=1))
    assert _dialog(cfg, db, bot) == "done"
    assert bot.sent == []


def test_old_no_more_is_rechecked_once(cfg, db, bot):
    _start(db, bot, NO_MORE, r.NO_MORE_RECHECK + timedelta(hours=1))
    bot.reply = lambda text: NO_MORE
    assert _dialog(cfg, db, bot) == "done"
    assert bot.sent == ["/change_vacancy"]


def _step(action, final=False):
    return r.Step(action=action, text="Есть опыт." if action == "reply" else "", button=-1, missing_fact="",
                  reason="", final=final, reusable=False)


def test_three_fruitless_switches_in_a_row_stop(cfg, db, bot, monkeypatch):
    monkeypatch.setattr(r, "_want_switch", lambda *a, **k: True)
    monkeypatch.setattr(r, "_decide", lambda *a, **k: _step("done"))
    _start(db, bot, DONE, timedelta(hours=1))
    bot.reply = lambda text: "Хорошо, сейчас посмотрю 🙂"
    assert _dialog(cfg, db, bot) == "done"
    assert bot.sent.count("/change_vacancy") == r.MAX_SWITCHES


def test_productive_switches_are_not_limited(cfg, db, bot, monkeypatch):
    """Каждая смена даёт вакансию и опрос — счётчик «подряд без толку» сбрасывается."""
    cfg.telegram.max_turns = 60
    vacancies = [f"Vacancy {i}" for i in range(1, 6)]
    left = lambda: [v for v in vacancies if r._norm(v) not in db.tg_surveys(BOT)]
    monkeypatch.setattr(r, "_want_switch", lambda *a, **k: bool(left()))
    monkeypatch.setattr(r, "_match_vacancy", lambda *a: None)
    monkeypatch.setattr(r, "_human_pause", lambda *a: 0)
    monkeypatch.setattr(r, "_decide", lambda cfg, db, msgs, *a, **k:
                        _step("reply", final=True) if "?" in msgs[-1].message else _step("done"))
    bot.on_click = lambda m, i: bot.add(f"Получил Ваш отклик на позицию {m.buttons[i][0].text[3:]}.\nВаш опыт?")
    bot.reply = lambda text: ((LIST, timedelta(0), False, [[Button(f"1. {left()[0]}")]]) if text == "/change_vacancy"
                              else DONE)
    _start(db, bot, DONE, timedelta(hours=1))
    assert _dialog(cfg, db, bot) == "done"
    assert bot.sent.count("/change_vacancy") == len(vacancies) > r.MAX_SWITCHES
    assert left() == []


def test_wait_bot_gives_up_on_endless_edits(monkeypatch):
    # бот без конца правит сообщение (таймер, «обрабатываю…») — раньше ожидание не кончалось никогда
    import itertools
    tick = itertools.count()
    monkeypatch.setattr(r, "_idle", lambda seconds: None)
    monkeypatch.setattr(r, "_snapshot", lambda client, entity: [(1, next(tick), "обрабатываю", 0)])
    assert r._wait_bot(None, None, timeout=0.05) is True


def test_empty_reply_goes_to_human(cfg, db, bot, monkeypatch):
    # пустое сообщение Telegram не примет — раньше это обрывало обход всех ботов
    monkeypatch.setattr(r, "notify", lambda *a: None)
    monkeypatch.setattr(r, "_human_pause", lambda *a: 0)
    empty = _step("reply").model_copy(update={"text": "  "})
    monkeypatch.setattr(r, "_decide", lambda *a, **k: empty)
    bot.add("Расскажите о вашем опыте с PyTorch?")
    assert _dialog(cfg, db, bot) == "human"
    assert bot.sent == []


def test_faq_does_not_answer_acknowledgement(cfg, db, bot, monkeypatch):
    """«Спасибо, про приписное понял. Расскажите об опыте с PyTorch?» — отвечает LLM, а не faq про приписное."""
    import yaml
    cfg.path(cfg.questions.faq_file).write_text(yaml.safe_dump(
        [{"match": ["приписное свидетельство"], "answer": "Да, есть приписное свидетельство."},
         {"match": ["зарплатные ожидания"], "answer": "150 000 ₽"}], allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(r, "_human_pause", lambda *a: 0)
    monkeypatch.setattr(r, "_decide", lambda *a, **k: _step("reply", final=True))
    bot.add("Спасибо, про приписное свидетельство понял. Расскажите о своём опыте с PyTorch?")
    _dialog(cfg, db, bot)
    assert bot.sent[0] == "Есть опыт."
    bot.add("Ваши зарплатные ожидания записал, передам рекрутеру.")  # без вопроса — не повод отвечать из faq
    _dialog(cfg, db, bot)
    assert "150 000 ₽" not in bot.sent


def test_old_greeting_does_not_override_just_chosen_vacancy(cfg, db, bot, monkeypatch):
    """Агент выбрал вакансию, бот ещё не поздоровался по ней: приветствие прошлого опроса — не текущий опрос
    (раньше он регистрировался как начатый и больше не предлагался)."""
    bot.add("Получил Ваш отклик на позицию Аналитик данных.", ago=timedelta(days=1))
    db.save_tg_survey(BOT, r._norm("Middle Data Scientist"), "active", 1)
    db.save_tg_dialog(BOT, status="active", survey="Middle Data Scientist", vacancy_id=1)
    bot.add("Спасибо за выбор вакансии!")
    monkeypatch.setattr(r, "_decide", lambda *a, **k: _step("wait"))
    _dialog(cfg, db, bot)
    assert "аналитик данных" not in db.tg_surveys(BOT)
    assert db.tg_dialog(BOT)["survey"] == "Middle Data Scientist"
    bot.add("Получил Ваш отклик на позицию Data Engineer.")  # новое приветствие — новый опрос, его видно
    assert r._current_survey(bot.messages, since=db.tg_survey_started(BOT, r._norm("Middle Data Scientist"))) \
        == "Data Engineer"
