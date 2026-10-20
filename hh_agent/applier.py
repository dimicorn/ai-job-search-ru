"""Агент-исполнитель: отправляет отклик с письмом через ваш браузер.

Сайты: hh.ru и Хабр Карьера (career.habr.com) — сценарий выбирается по адресу вакансии.

Режимы:
  review  — после вашего [y]; если форма нестандартная, управление переходит к вам;
  auto    — без человека: случайные паузы, посимвольная печать письма, нестандартные
            формы (тесты, анкеты) откладываются в статус manual;
  dry_run — всё как в auto, но вместо отправки делается скриншот формы.
"""
from __future__ import annotations

import json
import random
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PWTimeout
from rich.panel import Panel

from . import questions
from .browser import CaptchaError, captcha_visible, check_captcha, page_state, pause, source_of
from .config import Config
from .events import SESSION, log_event
from .learning import Store
from .llm import LimitReached
from .log import console

SEL = {
    "respond": "[data-qa='vacancy-response-link-top']",
    "responded": "[data-qa='vacancy-response-link-view-topic']",
    "relocation": "[data-qa='relocation-warning-confirm']",
    "popup_letter": "[data-qa='vacancy-response-popup-form-letter-input']",
    "letter_toggle": "[data-qa='add-cover-letter'], [data-qa='vacancy-response-letter-toggle']",
    "popup_submit": "[data-qa='vacancy-response-submit-popup']",
    "informer_letter": "[data-qa='vacancy-response-letter-informer'] textarea",
    "informer_submit": "[data-qa='vacancy-response-letter-submit']",
}
# Запасные варианты: data-qa у hh меняются, а модальное окно и текст кнопок — реже.
FALLBACK = {
    "popup_letter": ("#RESPONSE_MODAL_FORM_ID textarea, textarea[form='RESPONSE_MODAL_FORM_ID'], "
                     "[role='dialog'] textarea, [data-qa*='modal'] textarea, [data-qa*='letter'] textarea, "
                     "textarea[name*='letter'], textarea[name*='cover']"),
    "informer_letter": "[data-qa*='letter-informer'] textarea, [data-qa*='response-letter'] textarea",
}
# Фрагмент формы для разбора: видимые модальные окна, иначе блок вокруг поля письма/кнопок отклика.
_FORM_FRAGMENT_JS = """() => {
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const dlg = [...document.querySelectorAll('[role=dialog], [data-qa*=modal], [data-qa*=popup]')].filter(vis);
  if (dlg.length) return dlg.map(d => d.outerHTML).join('\\n');
  const t = [...document.querySelectorAll('textarea')].find(vis)
         || [...document.querySelectorAll('[data-qa*=response]')].find(vis);
  if (!t) return null;
  let el = t;
  for (let i = 0; i < 5 && el.parentElement; i++) el = el.parentElement;
  return el.outerHTML;
}"""
# Интерфейс hh бывает и на английском (в нём «сопроводительное» переведено как «Add a CV»).
TOGGLE_TEXT = r"(Добавить|Написать) сопроводительное|Сопроводительное письмо|Add a CV|cover letter"
SUBMIT_TEXT = r"^\s*(Откликнуться|Отправить|Send application|Send|Apply|Respond)\s*$"
FORM = ", ".join([*(SEL[k] for k in ("popup_letter", "letter_toggle", "popup_submit", "informer_letter")),
                  *FALLBACK.values(), "[role='dialog']"])
AFTER_CLICK = f"{FORM}, {SEL['relocation']}"

# Хабр Карьера: «Откликнуться» сразу отправляет отклик, затем в той же секции появляется
# поле письма и кнопка «Дополнить отклик».
HABR_FORM = "#create-vacancy-response"
HABR_LETTER = f"{HABR_FORM} textarea"  # textarea[name=body] — единственное поле в секции


@dataclass
class Mode:
    interactive: bool = True   # можно ли спросить человека в терминале
    dry_run: bool = False      # не отправлять, а сделать скриншот
    human_like: bool = False   # паузы «чтения», между кликами и посимвольная печать
    shots_dir: Path | None = None


def _copy(text: str) -> None:
    try:
        subprocess.run(["pbcopy"], input=text.encode(), check=False)
    except OSError:
        pass


class Applier:
    def __init__(self, page: Page, cfg: Config, mode: Mode):
        self.page, self.cfg, self.mode = page, cfg, mode
        self.auto = cfg.apply.auto
        self.tuning = Store(cfg).tuning  # подстройка по правилам: паузы и таймауты
        self.form_timeout = self.tuning["form_timeout_ms"]
        self.confirm_timeout = self.tuning["confirm_timeout_ms"]

    # ── мелкие действия ────────────────────────────────────────────────
    def _beat(self) -> None:
        if self.mode.human_like:
            pause(self.auto.action_seconds)

    def _click(self, selector: str) -> None:
        self._beat()
        self.page.locator(selector).first.click()

    def _type(self, field, text: str) -> None:
        self._beat()
        lo, hi = self.auto.typing_delay_ms
        if self.mode.human_like and hi > 0:
            field.click()
            field.fill("")
            delay = random.uniform(lo, hi)
            # Печатаем построчно прямо в поле (не «в фокус страницы»). Переносы вставляем без
            # нажатия Enter: в окне отклика hh Enter сворачивает поле письма.
            for i, line in enumerate(text.split("\n")):
                if i:
                    field.focus()
                    self.page.keyboard.insert_text("\n")
                if line:
                    field.press_sequentially(line, delay=delay, timeout=len(line) * delay * 2 + 15_000)
        else:
            field.fill(text)
        try:
            ok = field.input_value(timeout=3000).strip() == text.strip()
        except Exception:
            ok = False
        if not ok:  # фокус ушёл или поле перерисовалось — вписываем письмо целиком
            field.fill(text, timeout=10_000)

    def _read_vacancy(self) -> None:
        """Пролистать вакансию, как будто её читают."""
        if not self.mode.human_like:
            return
        total = random.uniform(*self.auto.read_seconds) * self.tuning["pace_multiplier"]
        steps = random.randint(2, 5)
        for _ in range(steps):
            self.page.mouse.wheel(0, random.randint(250, 700))
            self.page.wait_for_timeout(total / steps * 1000)
        self.page.mouse.wheel(0, -5000)
        self._beat()

    def _find(self, key: str, fallback: str | None = None):
        """Видимый элемент: сначала по data-qa, затем по запасному селектору."""
        for sel in (SEL.get(key), fallback):
            if not sel:
                continue
            try:
                # Поля, размеченные как вопросы работодателя, не трогаем (иначе письмо затрёт ответ).
                loc = self.page.locator(sel).and_(self.page.locator(":not([data-hhagent-q])")).locator("visible=true")
                if loc.count():
                    return loc.first
            except Exception:  # кривой селектор не должен ронять отклик
                continue
        return None

    def _field(self, key: str):
        return self._find(key, FALLBACK.get(key))

    def _open_letter(self) -> None:
        """Раскрыть поле письма, если оно спрятано за кнопкой/ссылкой."""
        btn = self._find("letter_toggle")
        if btn is None:
            txt = self.page.get_by_text(re.compile(TOGGLE_TEXT, re.I)).locator("visible=true")
            btn = txt.first if txt.count() else None
        if btn is not None:
            self._beat()
            btn.click()
            self.page.wait_for_timeout(700)

    def _submit_button(self, key: str = "popup_submit"):
        btn = self._find(key)
        if btn is not None:
            return btn
        marked = self.page.locator("[data-hhagent-scope='1']")
        if marked.count():  # область формы с вопросами: её собственная кнопка submit
            sub = marked.first.locator("button[type=submit]").locator("visible=true")
            if sub.count():
                return sub.last
        dlg = self.page.locator("[role='dialog']").locator("visible=true")
        scope = marked.first if marked.count() else dlg.first if dlg.count() else self.page
        btn = scope.get_by_role("button", name=re.compile(SUBMIT_TEXT, re.I))
        return btn.last if btn.count() else None

    def _debug_dump(self, vid: int, why: str, sub: str = "") -> str:
        """Сохранить HTML, фрагмент формы и скриншот — материал для обучения в конце сессии.

        sub — подпапка: снимки Хабра (debug/habr/) обучение не разбирает, селекторы там свои."""
        d = self.cfg.path("debug") / sub
        d.mkdir(parents=True, exist_ok=True)
        try:
            (d / f"{vid}.html").write_text(self.page.content(), encoding="utf-8")
            frag = self.page.evaluate(_FORM_FRAGMENT_JS)
            if frag:
                (d / f"{vid}.form.html").write_text(frag, encoding="utf-8")
            self.page.screenshot(path=str(d / f"{vid}.png"))
            (d / f"{vid}.json").write_text(json.dumps({
                "vid": vid, "url": self.page.url, "reason": why, "session": SESSION,
                "ts": datetime.now().isoformat(timespec="seconds"),
            }, ensure_ascii=False), encoding="utf-8")
            return f"{why} (снимок формы: {(Path('debug') / sub / f'{vid}.png').as_posix()})"
        except Exception:
            return why

    def _wait_confirmed(self, timeout_ms: int | None = None) -> bool:
        """Отклик подтверждён: появилась отметка «Вы откликнулись» или ушли со страницы анкеты."""
        on_form_page = "vacancy_response" in self.page.url
        deadline = time.monotonic() + (timeout_ms or self.confirm_timeout) / 1000
        while time.monotonic() < deadline:
            if self.page.locator(SEL["responded"]).count():
                return True
            if captcha_visible(self.page):
                check_captcha(self.page)  # auto — CaptchaError (остановка), review — ждём, пока пройдёте
                deadline = time.monotonic() + (timeout_ms or self.confirm_timeout) / 1000
            if on_form_page and "vacancy_response" not in self.page.url:
                return True
            self.page.wait_for_timeout(500)
        return False

    def _hand_over(self, letter: str, why: str) -> tuple[str, str]:
        if not self.mode.interactive:
            return "manual", why
        _copy(letter)
        console.print(f"[yellow]Нужна ручная доработка: {why}.[/] Письмо скопировано в буфер обмена.")
        console.print("Закончите отклик в окне браузера.")
        done = input("Отклик отправлен? [y/n] ").strip().lower() in ("y", "д", "yes", "да")
        return ("applied", f"вручную: {why}") if done else ("manual", why)

    def _shot(self, vid: int) -> str:
        """Скриншот страницы для dry-run; возвращает « — путь» для заметки."""
        if not self.mode.shots_dir:
            return ""
        self.mode.shots_dir.mkdir(parents=True, exist_ok=True)
        path = self.mode.shots_dir / f"{vid}.png"
        self.page.screenshot(path=str(path), full_page=True)
        return f" — {path}"

    def _finish(self, vid: int) -> tuple[str, str | None]:
        """Последний шаг: отправить (или скриншот в dry-run)."""
        if self.mode.dry_run:
            return "dry_run", f"форма заполнена, не отправлено{self._shot(vid)}"
        if self.cfg.apply.submit_mode == "manual" and self.mode.interactive:
            input("Проверьте форму и нажмите отправку в браузере, затем Enter… ")
        else:
            btn = self._submit_button()
            if btn is None:
                return "manual", self._debug_dump(vid, "кнопка отправки не найдена")
            self._beat()
            btn.click()
        if self._wait_confirmed():
            return "applied", None
        # Не подтвердилось: сохраняем страницу и тексты ошибок формы — по ним видно, что не так.
        errs = self.page.evaluate("""() => [...document.querySelectorAll(
            '[role=alert], [data-qa*=error], [class*=error], [class*=Error], [aria-invalid=true]')]
            .filter(e => e.offsetWidth || e.offsetHeight).map(e => (e.innerText || e.getAttribute('aria-label') || '').trim())
            .filter(Boolean).slice(0, 5)""")
        why = "отправка не подтвердилась" + (f": {'; '.join(errs)[:300]}" if errs else "")
        return "unconfirmed", self._debug_dump(vid, why)

    # ── вопросы работодателя ──────────────────────────────────────────
    def _letter_selectors(self) -> list[str]:
        return [SEL["popup_letter"], SEL["informer_letter"]]

    def _questionnaire(self, vid: int, letter: str, vacancy: str) -> tuple[str, str | None]:
        """Ответить на вопросы (сколько бы их ни было), приложить письмо и отправить."""
        page = self.page
        if not self.cfg.questions.enabled:
            return self._hand_over(letter, "работодатель просит ответить на вопросы/тест")
        page.wait_for_timeout(1500)  # форма вопросов дорисовывается
        check_captcha(page)
        qs = questions.extract(page, self._letter_selectors())
        if not qs:
            if "vacancy_response" in page.url:
                return self._hand_over(letter, self._debug_dump(vid, "вопросы работодателя не распознаны"))
            return "manual", "вопросы исчезли из формы"
        if len(qs) > self.cfg.questions.max_fields:
            return self._hand_over(letter, self._debug_dump(vid, f"слишком много полей в анкете ({len(qs)})"))
        try:
            ans = questions.answer(self.cfg, qs, vacancy)
        except (LimitReached, RuntimeError) as e:
            return self._hand_over(letter, f"не удалось ответить на вопросы: {e}")
        missing = questions.log_unanswered(self.cfg, vid, page.url, qs, ans)
        summary = questions.describe(qs, ans)
        log_event(self.cfg, "questions", vid=vid, fields=len(qs), missing=missing, qa=summary[:4000])
        if missing and not self.mode.interactive:
            return "manual", f"нужен ответ человека: {'; '.join(missing)} (см. logs/questions.md)"

        questions.fill(page, qs, ans, letter, self._type)
        if not any(a.kind == "cover_letter" for a in ans.answers):
            field = self._field("popup_letter")
            if field is None:
                self._open_letter()
                field = self._field("popup_letter")
            if field is not None:
                self._type(field, letter)

        if self.mode.interactive:
            console.print(Panel(summary, title=f"Ответы на вопросы ({len(qs)})", expand=False))
            if missing:
                console.print(f"[yellow]Не хватило фактов: {'; '.join(missing)} — поправьте ответ в браузере.[/]")
            if self.cfg.questions.confirm_in_review and not self.mode.dry_run:
                ok = input("Отправить с этими ответами? [y — да / n — поправлю и отправлю сам] ").strip().lower()
                if ok not in ("y", "д", "yes", "да"):
                    return self._hand_over(letter, "ответы на вопросы правятся вручную")
        return self._finish(vid)

    # ── отклик ────────────────────────────────────────────────────────
    def apply(self, vid: int, url: str, letter: str, vacancy: str = "") -> tuple[str, str | None]:
        """Статус: applied | already_applied | manual | dry_run | unconfirmed (ошибки — исключения).

        Каждый отклик пишется в журнал событий."""
        t0 = time.monotonic()
        mode = "dry_run" if self.mode.dry_run else "auto" if not self.mode.interactive else "review"
        try:
            if source_of(url) == "habr":
                status, note = self._apply_habr(vid, url, letter)
            else:
                status, note = self._apply(vid, url, letter, vacancy)
        except CaptchaError:
            raise  # не сбой вёрстки: капчу разбирает вызывающий код
        except Exception as e:
            self._debug_dump(vid, f"сбой отклика: {type(e).__name__}")
            log_event(self.cfg, "error", where="apply", vid=vid, mode=mode, message=f"{type(e).__name__}: {e}"[:500])
            raise
        log_event(self.cfg, "apply", vid=vid, mode=mode, status=status, note=note,
                  seconds=round(time.monotonic() - t0))
        return status, note

    def _apply(self, vid: int, url: str, letter: str, vacancy: str) -> tuple[str, str | None]:
        page = self.page
        page.goto(url, wait_until="domcontentloaded")
        check_captcha(page)
        if page.locator(SEL["responded"]).count():
            return "already_applied", "отклик уже был"
        if not page.locator(SEL["respond"]).count():
            return "manual", "на странице нет кнопки отклика"
        self._read_vacancy()
        self._click(SEL["respond"])

        try:
            page.wait_for_selector(AFTER_CLICK, timeout=self.form_timeout)
        except PWTimeout:
            check_captcha(page)
            if "vacancy_response" in page.url:
                return self._questionnaire(vid, letter, vacancy)
            if self._wait_confirmed(1000):
                return "applied", "отклик ушёл сразу, без письма"
            return self._hand_over(letter, self._debug_dump(vid, "форма отклика не распознана"))

        if page.locator(SEL["relocation"]).count():
            self._click(SEL["relocation"])
            try:
                page.wait_for_selector(FORM, timeout=6000)
            except PWTimeout:
                pass
            if "vacancy_response" in page.url:
                return self._questionnaire(vid, letter, vacancy)

        if self.cfg.apply.resume_title:
            opt = page.get_by_text(self.cfg.apply.resume_title, exact=False).first
            if opt.count():
                self._beat()
                opt.click()

        # Вопросы работодателя прямо в окне отклика (их может быть сколько угодно или не быть вовсе).
        if self.cfg.questions.enabled and questions.looks_like_questions(
                questions.extract(page, self._letter_selectors())):
            return self._questionnaire(vid, letter, vacancy)

        # Вариант 1: модальное окно с полем письма. Проверяем первым: в dry-run здесь ничего не отправится.
        field = self._field("popup_letter")
        if field is not None:
            self._type(field, letter)
            return self._finish(vid)

        # Вариант 2: отклик ушёл сразу по клику «Откликнуться», письмо дописывается после.
        # Отклик уже отправлен (даже в dry-run), поэтому письмо добавляем в любом режиме.
        informer = self._field("informer_letter")
        if informer is not None:
            self._type(informer, letter)
            if self.cfg.apply.submit_mode == "manual" and self.mode.interactive:
                input("Проверьте письмо и нажмите «Отправить» в браузере, затем Enter… ")
            else:
                btn = self._submit_button("informer_submit")
                if btn is None:
                    return "unconfirmed", self._debug_dump(vid, "отклик ушёл, но письмо не отправлено")
                self._beat()
                btn.click()
            page.wait_for_timeout(1500)
            return "applied", "письмо добавлено после отклика"

        # Вариант 3: поле письма спрятано за кнопкой «Добавить сопроводительное».
        self._open_letter()
        field = self._field("popup_letter")
        if field is not None:
            self._type(field, letter)
            return self._finish(vid)

        return self._hand_over(letter, self._debug_dump(vid, "поле для письма не найдено"))


    # ── Хабр Карьера ──────────────────────────────────────────────────
    def _habr_button(self, text: str):
        btn = (self.page.locator(HABR_FORM)
               .get_by_role("button", name=re.compile(rf"^\s*{text}\s*$", re.I)).locator("visible=true"))
        return btn.first if btn.count() else None

    def _apply_habr(self, vid: int, url: str, letter: str) -> tuple[str, str | None]:
        page = self.page
        page.goto(url, wait_until="domcontentloaded")
        check_captcha(page)
        st = page_state(page) or {}
        v = st.get("vacancy") or {}
        kind = (v.get("response") or {}).get("kind")
        cr = st.get("createResponse") or {}
        if kind == "guest":
            return "manual", "нет входа в Хабр Карьеру — выполните hh-agent login"
        if kind == "applied" or cr.get("response"):
            return "already_applied", "отклик уже был"
        if v.get("archived"):
            return "manual", "вакансия в архиве"
        if v.get("externalLink"):
            if self.mode.interactive:
                page.goto(v["externalLink"], wait_until="domcontentloaded")
            return self._hand_over(letter, f"отклик на сайте компании: {v['externalLink']}")
        if cr.get("responsesThresholdNotify") and cr.get("responsesLeft") == 0:
            return "manual", "исчерпан лимит откликов на Хабр Карьере"

        page.wait_for_load_state("load")  # кнопка работает только после загрузки скриптов страницы
        btn = self._habr_button("Откликнуться")
        if btn is None:
            ph = st.get("placeholder") or {}
            return "manual", "на странице нет кнопки отклика" + (f": {ph['title']}" if ph.get("title") else "")
        self._read_vacancy()
        if self.mode.dry_run:  # здесь нечего заполнять заранее: отклик уходит по первому же клику
            return "dry_run", f"Хабр отправляет отклик сразу по «Откликнуться» — не нажато{self._shot(vid)}"

        manual = self.cfg.apply.submit_mode == "manual" and self.mode.interactive
        if manual:
            input("Нажмите «Откликнуться» в браузере (отклик уйдёт сразу), затем Enter — письмо впишу я… ")
        else:
            self._beat()
            btn.click()
        try:
            page.wait_for_selector(HABR_LETTER, state="visible", timeout=self.form_timeout)
        except PWTimeout:
            if self._habr_button("Откликнуться") is None:
                return "applied", "отклик ушёл, поле для письма не появилось"
            return "unconfirmed", self._debug_dump(vid, "после «Откликнуться» форма не изменилась", sub="habr")

        self._type(page.locator(HABR_LETTER).first, letter)
        if manual:
            input("Проверьте письмо и нажмите «Дополнить отклик» в браузере, затем Enter… ")
        else:
            add = self._habr_button("Дополнить отклик")
            if add is None:
                return "unconfirmed", self._debug_dump(vid, "отклик ушёл, но письмо не отправлено", sub="habr")
            self._beat()
            add.click()
        try:  # после сохранения форма сменяется карточкой отклика
            page.locator(HABR_LETTER).wait_for(state="hidden", timeout=self.confirm_timeout)
        except PWTimeout:
            return "unconfirmed", "отклик ушёл, сохранение письма не подтвердилось"
        return "applied", "письмо добавлено к отклику"


def apply_one(page: Page, url: str, letter: str, cfg: Config, vid: int = 0,
              vacancy: str = "") -> tuple[str, str | None]:
    """Отклик из review (интерактивно)."""
    return Applier(page, cfg, Mode(interactive=True)).apply(vid, url, letter, vacancy)
