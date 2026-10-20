"""Обновить образцы страниц для тестов разбора: uv run python tests/fixtures/refresh.py

Открывает (только чтение, через профиль браузера агента) первую выдачу hh.ru и Хабр Карьеры
из config.yaml и по одной вакансии и сохраняет в tests/fixtures/ только публичные поля вакансий,
которые читает разбор (scout.parse_*). Данные о вашем профиле, откликах и контактах работодателя
не сохраняются. После обновления запустите uv run pytest: упавшие тесты покажут, что поменялось
в разметке сайтов. Не запускайте одновременно с autopilot — профиль браузера один.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from hh_agent import scout  # noqa: E402
from hh_agent.browser import open_browser, page_state  # noqa: E402
from hh_agent.config import load_config  # noqa: E402

OUT = Path(__file__).resolve().parent
N = 3  # вакансий в образце выдачи

HH_CARD = ("vacancyId", "name", "company", "compensation", "area", "links", "workExperience", "workFormats",
           "userTestPresent", "@responseLetterRequired")
HH_COMPANY = ("id", "name", "visibleName")
HH_VACANCY = ("vacancyId", "name", "description", "keySkills", "closedForApplicants", "status")
HABR_CARD = ("id", "title", "company", "salary", "locations", "remoteWork", "href", "salaryQualification")
HABR_VACANCY = ("id", "title", "description", "skills", "archived", "externalLink")


def pick(d: dict, keys) -> dict:
    return {k: d[k] for k in keys if k in d}


def short(html: str, n: int = 1500) -> str:
    return html if len(html) <= n else html[:n].rsplit("<", 1)[0]


def main() -> None:
    cfg = load_config(ROOT / "config.yaml")
    hh = next(s for s in cfg.searches if s.source == "hh")
    habr = next(s for s in cfg.searches if s.source == "habr")
    with open_browser(cfg, headless=True) as (_, page):
        page.goto(scout.search_url(hh, 0), wait_until="domcontentloaded")
        res = (page_state(page) or {})["vacancySearchResult"]
        with_salary = [v for v in res["vacancies"] if "noCompensation" not in (v.get("compensation") or {})]
        without = [v for v in res["vacancies"] if "noCompensation" in (v.get("compensation") or {})]
        cards = []
        for v in (with_salary[:N - 1] + without[:1])[:N]:
            c = pick(v, HH_CARD)
            c["company"] = pick(v.get("company") or {}, HH_COMPANY)
            c["userLabels"] = []  # ваши отметки (откликались ли) не сохраняем
            cards.append(c)
        save("hh_search_state.json", {"vacancySearchResult": {"totalResults": res.get("totalResults"),
                                                              "vacancies": cards}})

        page.goto(f"{scout.HH}/vacancy/{cards[0]['vacancyId']}", wait_until="domcontentloaded")
        v = pick(page_state(page)["vacancyView"]["vacancyFull"]["vacancy"], HH_VACANCY)
        v["description"] = short(v.get("description", ""))
        save("hh_vacancy_state.json", {"vacancyView": {"vacancyFull": {"vacancy": v}}})

        data = page.request.get(scout.habr_search_url(habr, 0)).json()
        cards = []
        for v in data["list"][:N]:
            c = pick(v, HABR_CARD)
            c["company"] = pick(v.get("company") or {}, ("id", "title", "href"))
            c["response"] = {"kind": "direct"}
            cards.append(c)
        save("habr_search.json", {"list": cards, "meta": pick(data.get("meta") or {}, ("totalResults", "perPage"))})

        page.goto(f"{scout.HABR}/vacancies/{cards[0]['id']}", wait_until="domcontentloaded")
        v = pick(page_state(page)["vacancy"], HABR_VACANCY)
        v["description"] = short(v.get("description", ""))
        v["response"] = {"kind": "direct"}
        save("habr_vacancy_state.json", {"vacancy": v, "createResponse": {"response": None}})


def save(name: str, data) -> None:
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("сохранён", name)


if __name__ == "__main__":
    main()
