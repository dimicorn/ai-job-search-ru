"""Общий браузер: persistent-профиль Chromium с вашими сессиями hh.ru и Хабр Карьеры."""
from __future__ import annotations

import random
import sys
import time
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright

from .config import Config

HH = "https://hh.ru"
HABR = "https://career.habr.com"
SITES = {"hh": "hh.ru", "habr": "Хабр Карьера"}
LOGIN_URL = {"hh": f"{HH}/account/login", "habr": f"{HABR}/users/auth/tmid"}

# Состояние страницы hh.ru лежит в <template id="HH-Lux-InitialState">,
# у Хабр Карьеры — в <script data-ssr-state>; оба — JSON.
_STATE_JS = """() => {
  const t = document.querySelector('template#HH-Lux-InitialState');
  if (t) return JSON.parse(t.content.textContent);
  const s = document.querySelector('script[data-ssr-state]');
  return s ? JSON.parse(s.textContent) : null;
}"""


def source_of(url: str) -> str:
    """Сайт вакансии по её адресу: habr | hh. Отдельной колонки в базе нет: id у сайтов не пересекаются
    (у hh — сотни миллионов, у Хабра — от миллиарда)."""
    return "habr" if "career.habr.com" in (url or "") else "hh"


@contextmanager
def open_browser(cfg: Config, headless: bool = False) -> Iterator[tuple[BrowserContext, Page]]:
    profile = cfg.path(cfg.browser.profile_dir)
    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile),
            headless=headless,
            locale="ru-RU",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            yield ctx, page
        finally:
            ctx.close()


def pause(rng: tuple[float, float]) -> None:
    time.sleep(random.uniform(*rng))


def page_state(page: Page) -> dict[str, Any] | None:
    return page.evaluate(_STATE_JS)


class CaptchaError(Exception):
    """hh.ru показал капчу, а человека рядом нет."""


def check_captcha(page: Page) -> None:
    """hh.ru иногда показывает капчу: в терминале ждём человека, без терминала — останавливаемся."""
    if "captcha" in page.url or page.locator("[data-qa='account-captcha-input'], iframe[src*='captcha']").count():
        if not sys.stdin.isatty():
            raise CaptchaError("hh.ru показал капчу")
        input("\n⚠️  hh.ru показал капчу. Пройдите её в окне браузера и нажмите Enter… ")


def is_logged_in(page: Page, source: str = "hh") -> bool:
    if source == "habr":
        # Гостя Хабр Карьера перенаправляет со страницы откликов на /users/auth_required.
        page.goto(f"{HABR}/responses", wait_until="domcontentloaded")
        return "/users/auth" not in page.url
    # hh.ru не перенаправляет анонима на логин, поэтому смотрим тип пользователя в состоянии страницы.
    page.goto(f"{HH}/applicant/resumes", wait_until="domcontentloaded")
    st = page_state(page) or {}
    return st.get("userType") == "applicant"


def missing_logins(page: Page, urls: Iterable[str]) -> list[str]:
    """Сайты из списка вакансий, на которые не выполнен вход."""
    return [s for s in sorted({source_of(u) for u in urls}) if not is_logged_in(page, s)]
