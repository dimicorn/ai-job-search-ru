"""Резюме с hh.ru → resume.md (hh-agent resume): агент уже вошёл на hh в своём профиле браузера,
поэтому берёт выгрузку резюме («Скачать → TXT», это HTML-документ) и делает из неё Markdown.

ФИО, дата рождения, телефон, почта и мессенджеры в resume.md не попадают: файл уходит в LLM
с каждым запросом, а для оценки вакансий и писем они не нужны. Город, гражданство и готовность
к переезду из шапки резюме сохраняются.
"""
from __future__ import annotations

import html as html_mod
import re
from typing import Any

from playwright.sync_api import Page

from .browser import HH, check_captcha, page_state
from .scout import html_to_text

# Разделы выгрузки hh: <p class="resume__block">Опыт работы — 3 года 2 месяца</p>
_BLOCK = re.compile(r'<p class="resume__block"[^>]*>(.*?)</p>', re.S | re.I)
# Из шапки (до первого раздела) — только это; ФИО и контакты отбрасываются целиком.
_HEADER_KEEP = re.compile(r"^(Проживает|Гражданство|Разрешение на работу|Готов|Не готов|Желательное время)", re.I)
_CONTACT = re.compile(
    r"\+\d[\d\s()\-]{8,}\d"                              # телефон в международном формате
    r"|\b8[\s(\-]*\d{3}[\s)\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}\b"  # и 8 (999) 123-45-67
    r"|[\w.+-]+@[\w-]+\.[\w.-]+"                         # почта
    r"|(?<![\w/])@[A-Za-z]\w{3,}"                         # ник в мессенджере
    r"|\b(?:telegram|телеграм|whatsapp|viber|skype)\s*:\s*\S+", re.I)  # «Telegram: …» (с двоеточием)


def _text(fragment: str) -> list[str]:
    return [line.strip() for line in html_to_text(re.sub(r"<(div|tr|p)\b", r"\n<\1", fragment)).splitlines()]


def to_markdown(export_html: str) -> str:
    """Выгрузка резюме hh (HTML) → Markdown без ФИО и контактов."""
    h = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", export_html or "", flags=re.S | re.I)
    title = re.search(r'Резюме\s*[«"]([^»"]+)[»"]', html_to_text(h))
    parts = _BLOCK.split(h)
    if len(parts) < 3:
        raise ValueError("не похоже на выгрузку резюме hh: нет разделов resume__block")
    header, rest = parts[0], parts[1:]

    out = [f"# {title.group(1).strip() if title else 'Резюме'}", ""]
    head = [line for line in _text(header) if line]
    for i, line in enumerate(head):
        if _HEADER_KEEP.match(line) and not _CONTACT.search(line):
            # «Гражданство:» / «…разрешение на работу:» — значение на следующей строке
            j = i
            while line.endswith(":") and j + 1 < len(head):
                j += 1
                line += " " + head[j]
            out.append(f"- {line}")
    out.append("")

    for name, body in zip(rest[::2], rest[1::2], strict=True):  # split с группой: имя, тело, имя, тело…
        name = html_mod.unescape(re.sub(r"<[^>]+>|\s+", " ", name)).strip()
        out += [f"## {name}", ""]
        lines, gap = [], False
        for line in _text(body):
            if not line or line == "•":  # пустая строка или маркер между записями — абзац
                gap = bool(lines)
                continue
            lines += ([""] if gap else []) + [line]
            gap = False
        out += lines + [""]
    text = "\n".join(out)
    text = _CONTACT.sub("", text.replace("\xa0", " "))
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def _resume_links(page: Page) -> list[tuple[str, str]]:
    """(хеш, название) ваших резюме со страницы «Мои резюме»."""
    page.goto(f"{HH}/applicant/resumes", wait_until="domcontentloaded")
    check_captcha(page)
    found: dict[str, str] = {}
    for href, text in page.eval_on_selector_all(
            "a[href^='/resume/']", "els => els.map(e => [e.getAttribute('href'), e.innerText])"):
        m = re.match(r"/resume/([0-9a-f]{20,})", href or "")
        if m and (m.group(1) not in found or not found[m.group(1)]):
            found[m.group(1)] = " ".join((text or "").split())
    return list(found.items())


def fetch(page: Page, title: str | None = None) -> tuple[str, str]:
    """Скачать резюме с hh: (название, Markdown). title — часть названия, если резюме несколько."""
    resumes = _resume_links(page)
    if not resumes:
        raise SystemExit("На hh.ru не нашлось ваших резюме — проверьте вход: uv run hh-agent login")
    pick = next((r for r in resumes if title and title.lower() in r[1].lower()), resumes[0])
    page.goto(f"{HH}/resume/{pick[0]}", wait_until="domcontentloaded")
    check_captcha(page)
    st: dict[str, Any] = page_state(page) or {}
    link = next((t["link"] for t in st.get("resumeExportTypes") or [] if t.get("name") == "txt"), None)
    if not link:
        raise SystemExit("hh.ru не дал выгрузку резюме — скопируйте текст резюме в resume.md вручную.")
    res = page.request.get(HH + link)
    if not res.ok:
        raise SystemExit(f"Выгрузка резюме: HTTP {res.status} — скопируйте текст резюме в resume.md вручную.")
    md = to_markdown(res.text())
    return md.splitlines()[0].lstrip("# ").strip(), md  # название — из заголовка резюме
