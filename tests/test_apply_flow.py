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

# Отклик уходит сразу: появляется «Резюме доставлено» с кнопкой «Add cover letter», а поверх страницы —
# окно «дополните профиль» из двух шагов с затемнением, которое перехватывает клики.
HH_RESPONDED_UPSELL = """<!doctype html><html lang="ru"><body>
<a href="#" data-qa="vacancy-response-link-top" onclick="this.remove();
   document.getElementById('done').hidden = false; document.getElementById('upsell').hidden = false;
   return false">Откликнуться</a>
<div id="done" hidden><p>CV delivered</p>
  <button type="button" data-qa="responded-success-attach-cover-letter-text"
          onclick="document.getElementById('dlg').hidden = false">Add cover letter</button></div>
<div id="dlg" role="dialog" hidden>
  <textarea data-qa="vacancy-response-popup-form-letter-input" name="letter"></textarea>
  <button data-qa="vacancy-response-submit-popup" onclick="
    window.sent = document.querySelector('[name=letter]').value; document.getElementById('dlg').remove();
    document.body.insertAdjacentHTML('beforeend',
      '<a data-qa=&quot;vacancy-response-link-view-topic&quot;>Вы откликнулись</a>')">Send</button>
</div>
<div id="upsell" hidden data-qa="modal-overlay" style="position:fixed; inset:0; z-index:10; background:#0008">
  <div role="dialog" data-step="2"><p>What format do you prefer?</p>
    <label><input type="checkbox" checked> Remote</label>
    <button data-qa="additional-data-collector__popup-close" onclick="this.closest('[role=dialog]').innerHTML =
      '<p>Shall we add a photo?</p><button data-qa=&quot;additional-data-collector__popup-close&quot; '
      + 'onclick=&quot;document.getElementById(\\'upsell\\').remove()&quot;>×</button>'">×</button>
  </div>
</div>
</body></html>"""

# «Откликнуться» сначала открывает капчу в том же role=dialog, что и форма; повторный клик после капчи —
# обычное окно отклика.
HH_CAPTCHA_THEN_FORM = """<!doctype html><html lang="ru"><body>
<a href="#" data-qa="vacancy-response-link-top" onclick="
   if (!window.clicked) document.getElementById('cap').hidden = false;
   else document.getElementById('dlg').hidden = false;
   window.clicked = (window.clicked || 0) + 1; return false">Откликнуться</a>
<div id="cap" role="dialog" hidden><p>Complete the CAPTCHA</p><input name="captcha_text"></div>
<div id="dlg" role="dialog" hidden>
  <textarea data-qa="vacancy-response-popup-form-letter-input" name="letter"></textarea>
  <button data-qa="vacancy-response-submit-popup" onclick="
    window.sent = document.querySelector('[name=letter]').value; document.getElementById('dlg').remove();
    document.body.insertAdjacentHTML('beforeend',
      '<a data-qa=&quot;vacancy-response-link-view-topic&quot;>Вы откликнулись</a>')">Отправить</button>
</div>
</body></html>"""

# Отклик ушёл сразу (отметка «Вы откликнулись»), кнопка «Add cover letter» есть, но поле письма не открывается.
HH_SENT_NO_LETTER = """<!doctype html><html lang="ru"><body>
<a href="#" data-qa="vacancy-response-link-top" onclick="this.remove(); document.body.insertAdjacentHTML('beforeend',
   '<a data-qa=&quot;vacancy-response-link-view-topic&quot;>Вы откликнулись</a>'
   + '<button type=&quot;button&quot; data-qa=&quot;responded-success-attach-cover-letter-text&quot;>Add cover letter</button>');
   return false">Откликнуться</a>
</body></html>"""

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


def test_hh_letter_after_response_behind_upsell(site, cfg):
    """Окно «дополните профиль» закрывается по всем шагам, затем письмо уходит через «Add cover letter»."""
    url = "https://hh.ru/vacancy/6"
    site.pages[url] = HH_RESPONDED_UPSELL
    status, _ = _applier(site, cfg).apply(6, url, LETTER)
    assert status == "applied"
    assert site.evaluate("window.sent") == LETTER


def test_hh_captcha_after_respond_is_not_a_form(site, cfg):
    """Капча после «Откликнуться» — не форма отклика: без человека отклик останавливается CaptchaError."""
    from hh_agent.browser import CaptchaError

    url = "https://hh.ru/vacancy/7"
    site.pages[url] = HH_CAPTCHA_THEN_FORM
    with pytest.raises(CaptchaError):
        _applier(site, cfg).apply(7, url, LETTER)
    assert site.evaluate("window.sent") is None


def test_hh_captcha_passed_then_respond_again(site, cfg, monkeypatch):
    """Человек прошёл капчу, а отклик не ушёл — агент снова жмёт «Откликнуться» и пишет письмо."""
    from hh_agent import applier

    def solve(page):  # капчу «проходит человек»
        if page.locator("#cap").is_visible():
            page.evaluate("document.getElementById('cap').remove()")

    monkeypatch.setattr(applier, "check_captcha", solve)
    url = "https://hh.ru/vacancy/8"
    site.pages[url] = HH_CAPTCHA_THEN_FORM
    status, _ = _applier(site, cfg).apply(8, url, LETTER)
    assert status == "applied"
    assert site.evaluate("[window.clicked, window.sent]") == [2, LETTER]


def test_hh_sent_without_letter_is_applied(site, cfg):
    """Отклик ушёл, а поля письма нет — это отклик (идёт в дневной лимит), а не manual."""
    url = "https://hh.ru/vacancy/9"
    site.pages[url] = HH_SENT_NO_LETTER
    status, note = _applier(site, cfg).apply(9, url, LETTER)
    assert status == "applied" and note.startswith("отклик ушёл без письма")


def test_hh_letter_crash_after_sent_is_applied(site, cfg, monkeypatch):
    """Сбой при добавлении письма после ушедшего отклика — applied, а не ошибка."""
    from hh_agent import applier

    def boom(self):
        raise RuntimeError("кнопку письма перекрыло окно")

    monkeypatch.setattr(applier.Applier, "_open_letter", boom)
    url = "https://hh.ru/vacancy/10"
    site.pages[url] = HH_SENT_NO_LETTER
    status, note = _applier(site, cfg).apply(10, url, LETTER)
    assert (status, note) == ("applied", "отклик ушёл без письма: сбой RuntimeError")


def test_hh_crash_before_respond_is_still_an_error(site, cfg, monkeypatch):
    """На странице отметка отклика, но «Откликнуться» в этот раз не нажимали — ошибка не маскируется."""
    from hh_agent import applier

    a = _applier(site, cfg)
    site.pages["https://hh.ru/vacancy/11"] = HH_APPLIED
    site.goto("https://hh.ru/vacancy/11")
    monkeypatch.setattr(applier.Applier, "_apply", lambda *args: (_ for _ in ()).throw(RuntimeError("нет сети")))
    with pytest.raises(RuntimeError):
        a.apply(11, "https://hh.ru/vacancy/11", LETTER)


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


def test_hh_resume_title_is_chosen_in_the_dialog(site, cfg):
    """Название резюме бывает и в заголовке вакансии — выбирать его надо в окне отклика."""
    cfg.apply.resume_title = "ML Engineer"
    resumes = ("<label><input type=radio name=resumeHash value=a checked> Аналитик данных</label>"
               "<label><input type=radio name=resumeHash value=b> ML Engineer</label>")
    url = "https://hh.ru/vacancy/6"
    site.pages[url] = HH_PAGE.format(questions=resumes)  # в заголовке страницы — тоже «ML Engineer»
    status, _ = _applier(site, cfg, dry_run=True).apply(6, url, LETTER)
    assert status == "dry_run" and site.is_checked("[name=resumeHash][value=b]")
