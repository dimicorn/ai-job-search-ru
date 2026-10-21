"""Разбор страниц hh.ru и Хабр Карьеры на сохранённых образцах (tests/fixtures).

Образцы сняты с живых сайтов скриптом tests/fixtures/refresh.py. Поменялась разметка сайта —
обновите образцы этим скриптом: упавшие тесты покажут, какое поле агент больше не находит.
Тесты с браузером (_STATE_JS, анкета) пропускаются, если не установлен Chromium Playwright.
"""
import copy
import json
from pathlib import Path

import pytest

from hh_agent import questions, scout
from hh_agent.browser import _STATE_JS

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


# ── hh.ru ────────────────────────────────────────────────────────────
def test_hh_search():
    state = load("hh_search_state.json")
    total, cards = scout.parse_hh_search(state)
    raw = state["vacancySearchResult"]["vacancies"]
    assert isinstance(total, int) and total > 0
    assert [vid for vid, _, _ in cards] == [int(v["vacancyId"]) for v in raw]
    for vid, c, responded in cards:
        assert c["title"] and c["company"]
        assert c["url"].startswith("https://") and str(vid) in c["url"]
        assert c["experience"] and isinstance(c["work_formats"], list) and c["work_formats"]
        assert responded is False
        scout.prefilter(c, scout.Filters())  # карточка годится для фильтров
    with_salary = [c for _, c, _ in cards if c["salary_from"] or c["salary_to"]]
    assert with_salary and all(c["currency"] == "RUR" for c in with_salary)
    assert any(c["salary_from"] is None and c["salary_to"] is None for _, c, _ in cards)  # «з/п не указана»


def test_hh_search_marks_responded_and_survives_empty_state():
    state = load("hh_search_state.json")
    state["vacancySearchResult"]["vacancies"][0]["userLabels"] = ["RESPONSE"]
    assert scout.parse_hh_search(state)[1][0][2] is True
    assert scout.parse_hh_search({}) == (None, [])


def test_hh_vacancy():
    state = load("hh_vacancy_state.json")
    d = scout.parse_hh_vacancy(state)
    assert d["description"] and "<" not in d["description"]
    assert isinstance(d["key_skills"], list) and d["closed"] is False
    closed = copy.deepcopy(state)
    closed["vacancyView"]["vacancyFull"]["vacancy"]["status"] = {"archived": True}
    assert scout.parse_hh_vacancy(closed)["closed"] is True
    assert scout.parse_hh_vacancy({}) is None


# ── Хабр Карьера ─────────────────────────────────────────────────────
def test_habr_search():
    data = load("habr_search.json")
    total, cards = scout.parse_habr_search(data)
    assert isinstance(total, int) and total > 0
    assert [vid for vid, _, _ in cards] == [int(v["id"]) for v in data["list"]]
    for vid, c, responded in cards:
        assert c["title"] and c["company"]
        assert c["url"] == f"{scout.HABR}/vacancies/{vid}"
        assert c["currency"] in (None, "RUR", "USD", "EUR")
        assert responded is False
    data["list"][0]["response"] = {"kind": "applied"}
    assert scout.parse_habr_search(data)[1][0][2] is True


def test_habr_vacancy():
    state = load("habr_vacancy_state.json")
    d = scout.parse_habr_vacancy(state)
    assert d["description"] and "<" not in d["description"]
    assert d["key_skills"] and all(isinstance(s, str) for s in d["key_skills"])
    assert d["closed"] is False and d["responded"] is False
    state["createResponse"] = {"response": {"id": 1}}
    assert scout.parse_habr_vacancy(state)["responded"] is True


# ── в браузере: состояние страницы и анкета работодателя ──────────────
@pytest.fixture(scope="module")
def page():
    from playwright.sync_api import Error, sync_playwright
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Error as e:
            pytest.skip(f"нет Chromium Playwright (uv run playwright install chromium): {str(e)[:80]}")
        yield browser.new_page()
        browser.close()


def test_state_js_reads_both_sites(page):
    state = {"vacancySearchResult": {"totalResults": 1}}
    page.set_content(f"<template id='HH-Lux-InitialState'>{json.dumps(state)}</template>")  # hh.ru
    assert page.evaluate(_STATE_JS) == state
    page.set_content(f"<script type='application/json' data-ssr-state>{json.dumps(state)}</script>")  # Хабр
    assert page.evaluate(_STATE_JS) == state
    page.set_content("<p>нет состояния</p>")
    assert page.evaluate(_STATE_JS) is None


def test_extract_employer_questions(page):
    page.set_content((FIX / "hh_response_form.html").read_text(encoding="utf-8"))
    qs = questions.extract(page, ["[data-qa='vacancy-response-popup-form-letter-input']"])
    got = [(q["type"], q["question"], q["options"]) for q in qs]
    assert got == [
        ("text", "Сколько лет вы работаете с PyTorch?", []),
        ("radio", "Какой формат работы вам подходит?", ["Офис", "Гибрид", "Удалённо"]),
        ("checkbox", "С какими фреймворками работали?", ["PyTorch", "TensorFlow", "JAX"]),
        ("select", "Город проживания", ["Москва", "Санкт-Петербург"]),
        ("checkbox", "", ["Согласен на обработку персональных данных"]),
    ]  # резюме, письмо и поле поиска вне окна отклика — не вопросы
    assert questions.looks_like_questions(qs)

    def answer(idx, text="", choices=()):
        return questions.QAnswer(idx=idx, kind="answer", text=text, choices=list(choices), needs_human=False,
                                 missing_fact="", reusable=False)
    answers = questions.QAnswers(answers=[answer(0, "3 года"), answer(1, choices=[2]), answer(2, choices=[0, 2]),
                                          answer(3, choices=[1]), answer(4, choices=[0])])
    questions.fill(page, qs, answers, letter="", type_text=lambda loc, text: loc.fill(text))
    assert page.input_value("[name=task_1]") == "3 года"
    assert page.is_checked("[name=task_2][value=c]")
    assert [page.is_checked(f"[name=task_3][value='{v}']") for v in "123"] == [True, False, True]
    assert page.input_value("[name=task_4]") == "Санкт-Петербург"
    assert page.is_checked("[name=consent]")

    # несуществующий вариант (модель ошиблась номером) — не ждать его 30 с, а оставить как есть
    answers = questions.QAnswers(answers=[answer(3, choices=[7]), answer(1, choices=[9])])
    questions.fill(page, qs, answers, letter="", type_text=lambda loc, text: loc.fill(text))
    assert page.input_value("[name=task_4]") == "Санкт-Петербург"


def test_habr_external_link_is_absolute(cfg, monkeypatch):
    """Хабр отдаёт ссылку на отклик на сайте компании относительной — открыть её как есть нельзя."""
    from types import SimpleNamespace

    from hh_agent import applier

    state = load("habr_vacancy_state.json")
    assert state["vacancy"]["externalLink"].startswith("/")
    visited = []
    page = SimpleNamespace(goto=lambda url, **kw: visited.append(url))
    monkeypatch.setattr(applier, "page_state", lambda p: state)
    monkeypatch.setattr(applier, "check_captcha", lambda p: None)
    monkeypatch.setattr(applier.Applier, "_hand_over", lambda self, letter, note: ("manual", note))
    status, note = applier.Applier(page, cfg, applier.Mode())._apply_habr(1, f"{scout.HABR}/vacancies/1", "")
    link = f"{scout.HABR}{state['vacancy']['externalLink']}"
    assert status == "manual" and visited[-1] == link and note.endswith(link)
