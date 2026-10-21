"""Мелкие чистые функции: конфиг, поиск, письма, анкеты, мастер настройки."""
from urllib.parse import parse_qsl, urlparse

import pytest

from hh_agent import scout, wizard
from hh_agent.analyst import _chunks, greet
from hh_agent.config import Filters, Search
from hh_agent.questions import looks_like_questions


def test_example_config_loads(cfg):
    assert cfg.searches and set(cfg.sources()) <= {"hh", "habr"}
    assert cfg.path("x.txt").parent == cfg.base_dir


def _query(url):
    return dict(parse_qsl(urlparse(url).query))


def test_search_url_from_params_and_url():
    s = Search(name="t", params={"text": "ML", "experience": ["noExperience", "between1And3"], "only_with_salary": True})
    q = parse_qsl(urlparse(scout.search_url(s, 2)).query)
    assert ("experience", "noExperience") in q and ("experience", "between1And3") in q
    assert ("only_with_salary", "true") in q and ("page", "2") in q

    s = Search(name="t", url="https://hh.ru/search/vacancy?text=CV&page=5")
    assert _query(scout.search_url(s, 0)) == {"text": "CV", "page": "0"}  # page из url заменяется


def test_habr_search_url_pages_from_one_and_all_types():
    s = Search(name="t", source="habr", params={"q": "ML"})
    assert _query(scout.habr_search_url(s, 0)) == {"q": "ML", "page": "1", "type": "all"}


def _card(**kw):
    return {"title": "ML Engineer", "company": "Acme", "salary_from": None, "salary_to": None,
            "currency": None, "has_test": False, **kw}


@pytest.mark.parametrize("card, filters, reason", [
    (_card(), Filters(), None),
    (_card(title="Senior Java Developer"), Filters(title_include_any=["ML"]), "title: нет обязательных слов"),
    (_card(title="Lead ML"), Filters(title_exclude=["lead"]), "title: стоп-слово «lead»"),
    (_card(company="Outstaff Inc"), Filters(companies_exclude=["outstaff"]), "компания в чёрном списке «outstaff»"),
    (_card(salary_to=80_000, currency="RUR"), Filters(min_salary=100_000), "зарплата 80000 < 100000"),
    (_card(salary_to=2_000, currency="USD"), Filters(min_salary=100_000), None),  # валюта — не сравниваем
    (_card(has_test=True), Filters(skip_with_employer_test=True), "тест от работодателя"),
])
def test_prefilter(card, filters, reason):
    assert scout.prefilter(card, filters) == reason


def test_html_to_text():
    assert scout.html_to_text("<p>Задачи:</p><ul><li>CV</li><li>NLP &amp; LLM</li></ul>") == "Задачи:\n• CV\n• NLP & LLM"


def test_salary_and_responded():
    assert scout._salary({"from": 100, "to": 200, "currencyCode": "RUR"}) == (100, 200, "RUR")
    assert scout._salary({"noCompensation": {}}) == (None, None, None)
    assert scout._already_responded({"userLabels": ["response"]})
    assert not scout._already_responded({"userLabels": []})


@pytest.mark.parametrize("letter, start", [
    ("Меня заинтересовала вакансия…", "Здравствуйте!\n\n"),
    ("I am interested in the role…", "Hello!\n\n"),
    ("Добрый день! Меня заинтересовала…", "Добрый день!"),
])
def test_greet(letter, start):
    assert greet(letter).startswith(start)


def test_chunks():
    assert _chunks(range(5), 2) == [[0, 1], [2, 3], [4]]


def test_looks_like_questions():
    assert not looks_like_questions([{"type": "checkbox", "options": ["Согласен"]}])
    assert looks_like_questions([{"type": "text", "options": []}])


def test_wizard_set_keeps_comments_and_adds_missing_keys():
    text = "searches: []\nanalyst:\n  backend: codex    # комментарий\n  model: x\napply:\n  daily_limit: 20\n"
    out = wizard._set(text, "backend", "openai")
    assert "  backend: openai" in out and "# комментарий" in out
    out = wizard._set(out, "fallback_settings", {"model": "claude-opus-5-5"})
    assert '  model: x\n  fallback_settings: {"model": "claude-opus-5-5"}\n' in out
    assert out.endswith("apply:\n  daily_limit: 20\n")  # другие разделы не тронуты


@pytest.mark.parametrize("value, yaml_text", [
    ("codex", "codex"), ("http://localhost:11434/v1", "http://localhost:11434/v1"),
    ("yes", '"yes"'), ("ollama run qwen3", '"ollama run qwen3"'), (None, "null"), (3, "3"), ({}, "{}"),
])
def test_wizard_yaml(value, yaml_text):
    assert wizard._yaml(value) == yaml_text


def test_wizard_set_adds_missing_analyst_section():
    text = wizard._set("searches: []\n", "backend", "codex")
    assert "analyst:\n  backend: codex" in text and text.startswith("searches: []")


def test_no_long_breaks_when_break_every_is_zero(cfg):
    from hh_agent.autopilot import Site
    cfg.apply.auto.break_every = 0
    st = Site("hh", None)
    st.done = 5
    st.schedule_pause(cfg, 1.0, dry_run=False)  # раньше — деление на ноль, и поток сайта падал
    assert not st.on_break
