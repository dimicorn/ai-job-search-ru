"""Поднятие резюме в поиске hh.ru: какие резюме уже можно поднять и нажатие кнопки в их карточке.
Страница «Мои резюме» подменена через page.route."""
import json
import time

import pytest

from hh_agent import resume

HOUR = 3600 * 1000


def _resume(h, title, can_touch=True, next_at=None):
    return {"title": [{"string": title}], "_attributes": {"hash": h, "canTouch": can_touch, "nextTouchAt": next_at}}


def test_raisable_and_next_raise():
    now = 10 * HOUR
    st = {"applicantResumes": [
        _resume("a1", "ML-инженер", next_at=now - 1),          # 4 часа прошли
        _resume("b2", "Разработчик C++", can_touch=False),     # скрыто от работодателей
        _resume("c3", "Data Scientist", can_touch=False, next_at=now + HOUR),  # рано: подняли меньше 4 часов назад
        _resume("d4", "CV-инженер"),                           # ещё ни разу не поднимали
    ]}
    assert resume.raisable(st, now) == [("a1", "ML-инженер"), ("d4", "CV-инженер")]
    assert resume.next_raise(st, now).timestamp() * 1000 == now + HOUR
    assert resume.raisable({}, now) == [] and resume.next_raise({}, now) is None


@pytest.fixture
def page():
    from playwright.sync_api import Error, sync_playwright
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Error as e:
            pytest.skip(f"нет Chromium Playwright (uv run playwright install chromium): {str(e)[:80]}")
        yield browser.new_page()
        browser.close()


def _serve(page, raised: list[str], silent: bool = False):
    """Две карточки с кнопкой «Поднять в поиске»; нажатие сдвигает nextTouchAt у своего резюме (silent — нет)."""
    def body():
        now = time.time() * 1000
        rs = [_resume(h, t, can_touch=h not in raised, next_at=now + 4 * HOUR if h in raised else now - 1)
              for h, t in (("a1", "ML"), ("c3", "DS"))]
        state = {"userType": "applicant", "applicantResumes": rs}
        cards = "".join(f"<div><a data-qa='resume-card-link-{h}' href='/resume/{h}?hhtmFrom=my_resumes'>{h}</a>"
                        f"<button data-qa='resume-update-button resume-update-button_actions' "
                        f"onclick=\"fetch('/touch?{h}')\">Push higher in search</button></div>" for h in ("a1", "c3"))
        return f"<template id='HH-Lux-InitialState'>{json.dumps(state)}</template>{cards}"

    def touch(route):
        if not silent:
            raised.append(route.request.url.rsplit("?", 1)[1])
        route.fulfill(body="{}")

    page.route(resume.MY_RESUMES, lambda route: route.fulfill(content_type="text/html", body=body()))
    page.route("https://hh.ru/touch?*", touch)


def test_raise_all_clicks_each_card(page, monkeypatch):
    monkeypatch.setattr(resume, "pause", lambda rng: None)
    monkeypatch.setattr(resume, "CLICK_WAIT_MS", 200)
    raised: list[str] = []
    _serve(page, raised)
    titles, nxt = resume.raise_all(page)
    assert titles == ["ML", "DS"] and raised == ["a1", "c3"]
    assert nxt is not None and nxt.timestamp() > time.time() + 3 * 3600


def test_raise_all_reports_when_hh_ignored_click(page, monkeypatch):
    monkeypatch.setattr(resume, "pause", lambda rng: None)
    monkeypatch.setattr(resume, "CLICK_WAIT_MS", 200)
    _serve(page, [], silent=True)
    with pytest.raises(RuntimeError, match="не поднял резюме: «ML», «DS»"):
        resume.raise_all(page)
