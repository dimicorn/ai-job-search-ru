"""Агент-разведчик: обходит поисковую выдачу, применяет фильтры, собирает полные описания."""
from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from playwright.sync_api import Page
from rich.console import Console

from .browser import HABR, HH, SITES, check_captcha, page_state, pause
from .config import Config, Filters, Search
from .db import DB
from .events import log_event

console = Console()

# Найденная карточка: (id вакансии, поля для базы, уже откликались ли).
Card = tuple[int, dict[str, Any], bool]


def _query_url(base: str, s: Search, page: tuple[str, str]) -> str:
    """URL выдачи: из готового url поиска или из params; номер страницы — пара (параметр, значение)."""
    if s.url:
        q = [(k, v) for k, v in parse_qsl(urlparse(s.url).query) if k != page[0]]
    else:
        q = []
        for k, v in s.params.items():
            for item in v if isinstance(v, list) else [v]:
                q.append((k, str(item).lower() if isinstance(item, bool) else str(item)))
    q.append(page)
    return urlunparse(urlparse(base)._replace(query=urlencode(q)))


def search_url(s: Search, page_no: int) -> str:
    return _query_url(s.url or f"{HH}/search/vacancy", s, ("page", str(page_no)))


def _salary(comp: dict[str, Any] | None) -> tuple[int | None, int | None, str | None]:
    if not comp or "noCompensation" in comp:
        return None, None, None
    return comp.get("from"), comp.get("to"), comp.get("currencyCode")


def _work_formats(v: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for el in v.get("workFormats") or []:
        out += el.get("workFormatsElement", []) if isinstance(el, dict) else [el]
    return out


def _already_responded(v: dict[str, Any]) -> bool:
    labels = {str(x).upper() for x in v.get("userLabels") or []}
    return bool(labels & {"RESPONSE", "INVITATION", "DISCARD", "RESPONDED"})


def prefilter(card: dict[str, Any], f: Filters) -> str | None:
    """Быстрые фильтры по карточке из выдачи. Возвращает причину отсева или None."""
    title = card["title"].lower()
    if f.title_include_any and not any(w.lower() in title for w in f.title_include_any):
        return "title: нет обязательных слов"
    for w in f.title_exclude:
        if w.lower() in title:
            return f"title: стоп-слово «{w}»"
    company = (card["company"] or "").lower()
    for c in f.companies_exclude:
        if c.lower() in company:
            return f"компания в чёрном списке «{c}»"
    top = card["salary_to"] or card["salary_from"]
    if top is None and f.skip_without_salary:
        return "зарплата не указана"
    if f.min_salary and top is not None and card["currency"] in (None, "RUR") and top < f.min_salary:
        return f"зарплата {top} < {f.min_salary}"
    if f.skip_with_employer_test and card["has_test"]:
        return "тест от работодателя"
    return None


def html_to_text(s: str) -> str:
    s = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", s or "")
    s = re.sub(r"<li[^>]*>", "• ", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(s)).strip()


# ── hh.ru ─────────────────────────────────────────────────────────────
def hh_search_page(page: Page, s: Search, n: int) -> tuple[int | None, list[Card]]:
    page.goto(search_url(s, n), wait_until="domcontentloaded")
    check_captcha(page)
    res = (page_state(page) or {}).get("vacancySearchResult") or {}
    cards = []
    for v in res.get("vacancies") or []:
        vid = int(v["vacancyId"])
        sf, st, cur = _salary(v.get("compensation"))
        cards.append((vid, dict(
            title=v.get("name", ""),
            company=(v.get("company") or {}).get("visibleName") or (v.get("company") or {}).get("name"),
            salary_from=sf, salary_to=st, currency=cur,
            area=(v.get("area") or {}).get("name"),
            url=(v.get("links") or {}).get("desktop") or f"{HH}/vacancy/{vid}",
            experience=v.get("workExperience"),
            work_formats=_work_formats(v),
            has_test=bool(v.get("userTestPresent")),
            letter_required=bool(v.get("@responseLetterRequired")),
        ), _already_responded(v)))
    return res.get("totalResults"), cards


def hh_fetch_details(page: Page, vid: int) -> dict[str, Any] | None:
    page.goto(f"{HH}/vacancy/{vid}", wait_until="domcontentloaded")
    check_captcha(page)
    st = page_state(page) or {}
    v = (((st.get("vacancyView") or {}).get("vacancyFull") or {}).get("vacancy")) or {}
    if not v:
        return None
    return {
        "description": html_to_text(v.get("description", "")),
        "key_skills": v.get("keySkills") or [],
        "closed": bool(v.get("closedForApplicants") or (v.get("status") or {}).get("archived")),
        "responded": page.locator("[data-qa='vacancy-response-link-view-topic']").count() > 0,
    }


# ── Хабр Карьера ──────────────────────────────────────────────────────
def habr_search_url(s: Search, page_no: int) -> str:
    """Выдачу отдаёт JSON API сайта с теми же параметрами, что и у страницы /vacancies. Страницы — с 1."""
    url = _query_url(f"{HABR}/api/frontend/vacancies", s, ("page", str(page_no + 1)))
    has_type = any(k == "type" for k, _ in parse_qsl(urlparse(url).query))
    return url if has_type else f"{url}&type=all"  # all — все вакансии, а не только «подходящие»


def habr_search_page(page: Page, s: Search, n: int) -> tuple[int | None, list[Card]]:
    # Запрос идёт с куками профиля — тогда в response.kind видно, откликались ли вы уже.
    res = page.request.get(habr_search_url(s, n))
    if not res.ok:
        raise RuntimeError(f"Хабр Карьера: HTTP {res.status} на странице выдачи {n + 1}")
    data = res.json()
    cards = []
    for v in data.get("list") or []:
        sal = v.get("salary") or {}
        locs = ", ".join(x.get("title", "") for x in v.get("locations") or [])
        cards.append((int(v["id"]), dict(
            title=v.get("title", ""),
            company=(v.get("company") or {}).get("title"),
            salary_from=sal.get("from"), salary_to=sal.get("to"),
            currency=(sal.get("currency") or "").upper() or None,  # rur → RUR, как у hh
            area=locs or ("Удалённо" if v.get("remoteWork") else None),
            url=f"{HABR}{v.get('href') or '/vacancies/' + str(v['id'])}",
            experience=(v.get("salaryQualification") or {}).get("title"),
            work_formats=["REMOTE"] if v.get("remoteWork") else [],
            has_test=False,
            letter_required=False,
        ), (v.get("response") or {}).get("kind") == "applied"))
    return (data.get("meta") or {}).get("totalResults"), cards


def habr_fetch_details(page: Page, vid: int) -> dict[str, Any] | None:
    page.goto(f"{HABR}/vacancies/{vid}", wait_until="domcontentloaded")
    check_captcha(page)
    st = page_state(page) or {}
    v = st.get("vacancy") or {}
    if not v:
        return None
    return {
        "description": html_to_text(v.get("description", "")),
        "key_skills": [x["title"] for x in v.get("skills") or [] if x.get("title")],
        "closed": bool(v.get("archived")),
        "responded": (v.get("response") or {}).get("kind") == "applied"
                     or bool((st.get("createResponse") or {}).get("response")),
    }


SOURCES = {
    "hh": (hh_search_page, hh_fetch_details),
    "habr": (habr_search_page, habr_fetch_details),
}


def sorted_by_date(s: Search) -> bool:
    """Выдача отсортирована по дате (hh: order_by=publication_time, Хабр: sort=date)."""
    q = dict(parse_qsl(urlparse(s.url).query)) if s.url else {k: str(v) for k, v in s.params.items()}
    return q.get("order_by") == "publication_time" or q.get("sort") == "date"


def scan(cfg: Config, db: DB, page: Page, sources: set[str] | None = None) -> int:
    """sources — искать только на этих сайтах (autopilot ищет на hh и Хабре в разных потоках)."""
    added = 0
    for s in cfg.searches:
        if sources is not None and s.source not in sources:
            continue
        search_page, fetch_details = SOURCES[s.source]
        console.rule(f"[bold]Поиск: {s.name}[/] ({SITES[s.source]})")
        stats = {"source": s.source, "total": None, "pages": 0, "added": 0, "filtered": 0, "seen": 0}
        by_date = sorted_by_date(s)
        for n in range(s.max_pages):
            total, cards = search_page(page, s, n)
            if n == 0:
                stats["total"] = total
                console.print(f"Всего в выдаче: {total if total is not None else '?'}")
            if not cards:
                break
            stats["pages"] += 1
            if by_date and all(db.exists(vid) for vid, _, _ in cards):
                # Выдача по дате: страница целиком из известных — дальше только более старые.
                stats["seen"] += len(cards)
                console.print(f"Страница {n + 1}: новых нет — дальше только уже известные, поиск закончен.")
                break
            for vid, card, responded in cards:
                if db.exists(vid):
                    stats["seen"] += 1
                    continue
                card["search_name"] = s.name
                if responded:
                    db.insert(vid, **card, status="filtered", filter_reason="уже откликались")
                    stats["filtered"] += 1
                    continue
                reason = prefilter(card, cfg.filters)
                if reason:
                    db.insert(vid, **card, status="filtered", filter_reason=reason)
                    stats["filtered"] += 1
                    continue

                pause(cfg.browser.delay_seconds)
                d = fetch_details(page, vid)
                if d is None:
                    db.insert(vid, **card, status="error", error="не удалось прочитать вакансию")
                    continue
                desc_l = d["description"].lower()
                bad = next((w for w in cfg.filters.description_exclude if w.lower() in desc_l), None)
                if d["closed"] or d["responded"] or bad:
                    why = "закрыта" if d["closed"] else "уже откликались" if d["responded"] else f"описание: «{bad}»"
                    db.insert(vid, **card, description=d["description"], key_skills=d["key_skills"],
                              status="filtered", filter_reason=why)
                    stats["filtered"] += 1
                    continue
                db.insert(vid, **card, description=d["description"], key_skills=d["key_skills"], status="new")
                added += 1
                stats["added"] += 1
                console.print(f"  [green]+[/] {card['title']} — {card['company']}")
            pause(cfg.browser.delay_seconds)
        log_event(cfg, "scan", search=s.name, **stats)
    return added
