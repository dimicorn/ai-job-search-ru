"""Отклик целиком в настоящем Chromium (Applier): подставные страницы hh.ru и Хабр Карьеры отдаются
через page.route, без сети. Разметка — по образцу живых сайтов (data-qa hh, секция отклика Хабра)."""
from __future__ import annotations

import json

import pytest
import yaml

from hh_agent.applier import Applier, Mode

LETTER = "Здравствуйте!\n\nМеня заинтересовала вакансия."

# Окно отклика hh: «Откликнуться» открывает модальное окно, «Отправить» сохраняет то, что отправлено бы,
# в window.sent и показывает отметку «Вы откликнулись».
HH_PAGE = """<!doctype html><html lang="ru"><body>
<h1>ML Engineer</h1>
<a href="#" data-qa="vacancy-response-link-top"
   onclick="document.getElementById('dlg').hidden = false; return false">Откликнуться</a>
<div id="dlg" role="dialog" hidden>
  {questions}
  <textarea data-qa="vacancy-response-popup-form-letter-input" name="letter"></textarea>
  <button data-qa="vacancy-response-submit-popup" onclick="
    window.sent = Object.fromEntries([...document.querySelectorAll('#dlg textarea, #dlg input')]
                                     .map(e => [e.name, e.value]));
    document.getElementById('dlg').remove();
    document.body.insertAdjacentHTML('beforeend',
      '<a data-qa=&quot;vacancy-response-link-view-topic&quot;>Вы откликнулись</a>')">Отправить</button>
</div>
</body></html>"""

HH_APPLIED = """<!doctype html><html lang="ru"><body>
<a data-qa="vacancy-response-link-view-topic">Вы откликнулись</a></body></html>"""

QUESTION = """<div class="question"><p>Ваши ожидания по зарплате?</p><input type="text" name="q1"></div>"""

# Хабр Карьера: «Откликнуться» сразу отправляет отклик, затем появляется поле письма и «Дополнить отклик».
HABR_STATE = {"vacancy": {"response": {"kind": "direct"}, "archived": False}, "createResponse": {"response": None}}
HABR_PAGE = """<!doctype html><html lang="ru"><body>
<script type="application/json" data-ssr-state>{state}</script>
<section id="create-vacancy-response">
  <button onclick="window.responded = true; this.parentElement.innerHTML =
    '<textarea name=body></textarea><button onclick=&quot;window.sent = document.querySelector(\\'textarea\\').value;'
    + ' this.parentElement.innerHTML = \\'<p>Ваш отклик</p>\\'&quot;>Дополнить отклик</button>'">Откликнуться</button>
</section></body></html>"""


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import Error, sync_playwright
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True)
        except Error as e:
            pytest.skip(f"нет Chromium Playwright (uv run playwright install chromium): {str(e)[:80]}")
        yield b
        b.close()


@pytest.fixture
def site(browser):
    """Страница, которой сайт отдаёт pages[url]."""
    ctx = browser.new_context()
    page = ctx.new_page()
    pages: dict[str, str] = {}
    page.route("**/*", lambda route: route.fulfill(body=pages.get(route.request.url, "<html></html>"),
                                                   content_type="text/html; charset=utf-8"))
    page.pages = pages
    yield page
    ctx.close()


def _applier(page, cfg, **mode) -> Applier:
    a = Applier(page, cfg, Mode(interactive=False, **mode))
    a.form_timeout = a.confirm_timeout = 3000  # ошибка теста — за секунды, а не за 15 с
    return a


def test_hh_letter_in_modal(site, cfg):
    url = "https://hh.ru/vacancy/1"
    site.pages[url] = HH_PAGE.format(questions="")
    status, note = _applier(site, cfg).apply(1, url, LETTER)
    assert (status, note) == ("applied", None)
    assert site.evaluate("window.sent") == {"letter": LETTER}


def test_hh_dry_run_fills_but_does_not_send(site, cfg):
    url = "https://hh.ru/vacancy/2"
    site.pages[url] = HH_PAGE.format(questions="")
    status, _ = _applier(site, cfg, dry_run=True).apply(2, url, LETTER)
    assert status == "dry_run"
    assert site.evaluate("window.sent") is None
    assert site.input_value("[name=letter]") == LETTER


def test_hh_already_applied(site, cfg):
    url = "https://hh.ru/vacancy/3"
    site.pages[url] = HH_APPLIED
    assert _applier(site, cfg).apply(3, url, LETTER)[0] == "already_applied"


def test_hh_employer_question_from_faq(site, cfg):
    """Вопрос работодателя в окне отклика: ответ из faq.yaml (без LLM), письмо — в своё поле."""
    cfg.path(cfg.questions.faq_file).write_text(
        yaml.safe_dump([{"match": ["ожидания по зарплате"], "answer": "150 000 ₽"}], allow_unicode=True),
        encoding="utf-8")
    url = "https://hh.ru/vacancy/4"
    site.pages[url] = HH_PAGE.format(questions=QUESTION)
    status, _ = _applier(site, cfg).apply(4, url, LETTER, vacancy="ML Engineer")
    assert status == "applied"
    assert site.evaluate("window.sent") == {"q1": "150 000 ₽", "letter": LETTER}


def test_hh_question_without_facts_goes_to_manual(site, cfg, monkeypatch):
    """Не хватает личного факта — в auto не отправлять, а отложить в manual и записать вопрос."""
    from hh_agent import questions

    def ask(cfg_, qs, vacancy):
        return questions.QAnswers(answers=[questions.QAnswer(
            idx=qs[0]["idx"], kind="answer", text="[уточнить]", choices=[], needs_human=True,
            missing_fact="Ожидаемая зарплата", reusable=False)])

    monkeypatch.setattr(questions, "_ask", ask)
    url = "https://hh.ru/vacancy/5"
    site.pages[url] = HH_PAGE.format(questions=QUESTION)
    status, note = _applier(site, cfg).apply(5, url, LETTER)
    assert status == "manual" and "Ожидаемая зарплата" in note
    assert site.evaluate("window.sent") is None
    assert "Ожидаемая зарплата" in cfg.path("logs/questions.md").read_text(encoding="utf-8")


def test_habr_apply_then_add_letter(site, cfg):
    url = "https://career.habr.com/vacancies/1000000001"
    site.pages[url] = HABR_PAGE.format(state=json.dumps(HABR_STATE))
    status, note = _applier(site, cfg).apply(1000000001, url, LETTER)
    assert (status, note) == ("applied", "письмо добавлено к отклику")
    assert site.evaluate("window.responded") is True and site.evaluate("window.sent") == LETTER


def test_habr_dry_run_does_not_click(site, cfg):
    url = "https://career.habr.com/vacancies/1000000002"
    site.pages[url] = HABR_PAGE.format(state=json.dumps(HABR_STATE))
    status, _ = _applier(site, cfg, dry_run=True).apply(1000000002, url, LETTER)
    assert status == "dry_run" and site.evaluate("window.responded") is None


def test_habr_already_applied(site, cfg):
    url = "https://career.habr.com/vacancies/1000000003"
    state = {**HABR_STATE, "vacancy": {"response": {"kind": "applied"}}}
    site.pages[url] = HABR_PAGE.format(state=json.dumps(state))
    assert _applier(site, cfg).apply(1000000003, url, LETTER)[0] == "already_applied"
