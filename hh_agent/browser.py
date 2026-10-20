"""Общий браузер: persistent-профиль Chromium с вашими сессиями hh.ru и Хабр Карьеры."""
from __future__ import annotations

import fcntl
import random
import socket
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def open_browser(cfg: Config, headless: bool = False,
                 cdp_port: int | None = None) -> Iterator[tuple[BrowserContext, Page]]:
    """Браузер с профилем агента. cdp_port — открыть отладочный порт (только 127.0.0.1), чтобы
    рабочие потоки подключались к этому же браузеру через attach()."""
    global _CFG, _HEADLESS
    _CFG, _HEADLESS = cfg, headless
    profile = cfg.path(cfg.browser.profile_dir)
    profile.mkdir(parents=True, exist_ok=True)
    # Профиль Chromium нельзя открыть в двух процессах: ждём, пока освободится (до 3 часов).
    lock = open(profile.parent / ".hh-agent.lock", "w")
    waited = 0
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if waited == 0:
                print("Браузер агента занят другим запуском hh-agent — жду…", flush=True)
            if waited > 3 * 3600:
                raise SystemExit("Профиль браузера занят больше 3 часов — выхожу.")
            time.sleep(30)
            waited += 30
    with lock, sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile),
            headless=headless,
            locale="ru-RU",
            viewport={"width": 1280, "height": 900},
            args=[f"--remote-debugging-port={cdp_port}"] if cdp_port else [],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            yield ctx, page
        finally:
            ctx.close()


@contextmanager
def attach(cdp_port: int) -> Iterator[tuple[BrowserContext, Page]]:
    """Своя вкладка общего браузера для рабочего потока. Синхронный Playwright привязан к потоку,
    поэтому каждый поток подключается к браузеру по CDP; профиль и сессии сайтов — общие."""
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        ctx = b.contexts[0]
        page = ctx.new_page()
        # Эмуляция из launch_persistent_context на подключения по CDP не распространяется.
        page.set_viewport_size({"width": 1280, "height": 900})
        page.set_extra_http_headers({"Accept-Language": "ru-RU,ru;q=0.9"})
        try:
            yield ctx, page
        finally:
            try:
                page.close()
            except Exception:
                pass
            b.close()  # отключиться от браузера; сам браузер закроет open_browser


def pause(rng: tuple[float, float]) -> None:
    time.sleep(random.uniform(*rng))


def page_state(page: Page) -> dict[str, Any] | None:
    return page.evaluate(_STATE_JS)


class CaptchaError(Exception):
    """hh.ru показал капчу, а человека рядом нет."""


# Капча hh бывает отдельной страницей, а бывает модальным окном поверх формы отклика
# («Complete the CAPTCHA» / «Подтвердите, что вы не робот») — после нажатия «Отправить».
_CAPTCHA_SEL = ("[data-qa='account-captcha-input'], [data-qa*='captcha'], iframe[src*='captcha'], "
                "img[src*='captcha'], input[name*='captcha' i]")
_CAPTCHA_TEXT = r"Complete the CAPTCHA|not a robot|не робот|Введите текст с картинки|Text from the picture"


def captcha_visible(page: Page) -> bool:
    import re
    if "captcha" in page.url:
        return True
    if page.locator(_CAPTCHA_SEL).locator("visible=true").count():
        return True
    return page.get_by_text(re.compile(_CAPTCHA_TEXT, re.I)).locator("visible=true").count() > 0


_CFG: Config | None = None   # выставляет open_browser
_HEADLESS = False


def notify(title: str, text: str) -> None:
    """Уведомление macOS со звуком."""
    safe = lambda s: s.replace("\\", " ").replace('"', "'")[:200]
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{safe(text)}" with title "{safe(title)}" sound name "Glass"'],
                       check=False, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _wait_for_human(page: Page) -> None:
    """auto: капчу проходит человек. Уведомление → ждём, пока окно капчи исчезнет; не дождались — CaptchaError."""
    from .events import log_event

    wait_s = (_CFG.apply.auto.captcha_wait_minutes * 60) if _CFG else 0
    if _HEADLESS or wait_s <= 0:
        raise CaptchaError("hh.ru показал капчу")
    page.bring_to_front()
    notify("hh-agent: пройдите капчу", f"Сайт просит подтвердить, что вы не робот. Жду {wait_s // 60} мин.")
    print(f"\n⚠️  hh.ru показал капчу. Пройдите её в окне браузера — жду до {wait_s // 60} мин…", flush=True)
    t0 = time.monotonic()
    while time.monotonic() - t0 < wait_s:
        page.wait_for_timeout(3000)
        if not captcha_visible(page):
            if _CFG:
                log_event(_CFG, "captcha", where="apply", solved=True, waited=round(time.monotonic() - t0))
            print("Капча пройдена — продолжаю.", flush=True)
            return
    raise CaptchaError("капчу не прошли вовремя")  # событие пишет вызывающий код (с id вакансии)


def check_captcha(page: Page) -> None:
    """hh.ru иногда показывает капчу. Агент её никогда не решает сам: в терминале ждёт Enter,
    в auto — уведомляет и ждёт, пока её пройдёт человек (apply.auto.captcha_wait_minutes)."""
    if captcha_visible(page):
        if not sys.stdin.isatty():
            _wait_for_human(page)
            return
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


class SiteTabs:
    """Отдельная вкладка браузера на каждый сайт: hh.ru — первая вкладка, Хабр Карьера — своя.
    tabs(source) возвращает вкладку сайта и выводит её на передний план."""

    def __init__(self, ctx: BrowserContext, first: Page):
        self.ctx = ctx
        self.pages: dict[str, Page] = {"hh": first}

    def __call__(self, source: str) -> Page:
        p = self.pages.get(source)
        if p is None or p.is_closed():
            p = self.pages[source] = self.ctx.new_page()
        p.bring_to_front()
        return p

    def missing_logins(self, urls: Iterable[str]) -> list[str]:
        return [s for s in sorted({source_of(u) for u in urls}) if not is_logged_in(self(s), s)]

