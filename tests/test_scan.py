"""Поиск (scout.scan) с подставной выдачей: новые вакансии, фильтры и актуальность статусов известных."""
from __future__ import annotations

import pytest

from hh_agent import scout
from hh_agent.config import Search


def _card(vid: int, title: str = "ML Engineer") -> dict:
    return dict(title=f"{title} {vid}", company="Acme", salary_from=None, salary_to=None, currency=None,
                area="Москва", url=f"https://hh.ru/vacancy/{vid}", experience=None, work_formats=[],
                has_test=False, letter_required=False)


@pytest.fixture
def site(cfg, monkeypatch):
    """Выдача hh из одной страницы: cards — [(id, откликались ли)]."""
    cfg.searches = [Search(name="t", params={"text": "ML", "order_by": "publication_time"}, max_pages=2)]
    cfg.browser.delay_seconds = (0, 0)
    cards: list[tuple[int, bool]] = []

    def search_page(page, s, n):
        return len(cards), ([(vid, _card(vid), resp) for vid, resp in cards] if n == 0 else [])

    def details(page, vid):
        return {"description": "Описание", "key_skills": [], "closed": False, "responded": False}

    monkeypatch.setitem(scout.SOURCES, "hh", (search_page, details))
    return cards


def test_new_vacancies_are_added_and_responded_filtered(cfg, db, site):
    site += [(1, False), (2, True)]
    assert scout.scan(cfg, db, page=None) == 1
    assert db.get(1)["status"] == "new"
    assert (db.get(2)["status"], db.get(2)["filter_reason"]) == ("filtered", "уже откликались")


@pytest.mark.parametrize("status", ["new", "scored", "skipped", "manual", "error", "unconfirmed"])
def test_known_vacancy_responded_on_site_becomes_applied(cfg, db, site, status):
    """Вы откликнулись сами (или неподтверждённый отклик всё же ушёл) — в базе статус обновляется."""
    db.insert(5, **_card(5), status=status, score=80, applied_at="2026-10-20T10:00:00" if status == "unconfirmed" else None)
    site.append((5, True))
    scout.scan(cfg, db, page=None)
    row = db.get(5)
    assert row["status"] == "applied" and f"было: {status}" in row["error"]
    if status == "unconfirmed":
        assert row["applied_at"] == "2026-10-20T10:00:00"  # дата отправки уже была — остаётся
    else:
        assert db.applied_today() == 1  # дата отклика — когда агент узнал, в дневной лимит идёт


@pytest.mark.parametrize("status", ["applied", "filtered"])
def test_final_statuses_are_kept(cfg, db, site, status):
    db.insert(6, **_card(6), status=status, filter_reason="стоп-слово «senior»" if status == "filtered" else None)
    site.append((6, True))
    scout.scan(cfg, db, page=None)
    assert db.get(6)["status"] == status and db.get(6)["error"] is None


def test_status_updated_even_when_page_has_only_known_vacancies(cfg, db, site):
    # выдача по дате, вся страница известна — поиск дальше не идёт, но статусы на ней обновляет
    db.insert(7, **_card(7), status="manual", score=70)
    db.insert(8, **_card(8), status="scored", score=70)
    site += [(7, True), (8, False)]
    scout.scan(cfg, db, page=None)
    assert db.get(7)["status"] == "applied" and db.get(8)["status"] == "scored"


@pytest.fixture
def same_title(cfg, monkeypatch, site):
    """Все карточки выдачи — одна вакансия («Инженер» в Acme), выложенная под разными id."""
    opened: list[int] = []
    search_page, details = scout.SOURCES["hh"]

    def details_spy(page, vid):
        opened.append(vid)
        return details(page, vid)

    def cards(vid):
        return {**_card(vid), "title": "Инженер"}

    monkeypatch.setitem(scout.SOURCES, "hh", (
        lambda page, s, n: (len(site), [(vid, cards(vid), resp) for vid, resp in site] if n == 0 else []),
        details_spy))
    return opened


def test_copies_of_one_vacancy_are_not_opened(cfg, db, site, same_title):
    site += [(10, False), (11, False), (12, False)]
    assert scout.scan(cfg, db, page=None) == 1
    assert same_title == [10]  # страницы копий не открывались
    assert db.get(10)["status"] == "new"
    for vid in (11, 12):
        assert (db.get(vid)["status"], db.get(vid)["filter_reason"]) == ("filtered", "дубль вакансии 10")


def test_duplicate_of_known_vacancy(cfg, db, site, same_title):
    db.insert(3, **{**_card(3), "title": "Инженер"}, status="skipped", score=20)
    site.append((20, False))
    scout.scan(cfg, db, page=None)
    assert db.get(20)["filter_reason"] == "дубль вакансии 3"


@pytest.mark.parametrize("status, reason", [("filtered", "закрыта"), ("error", None)])
def test_closed_or_failed_original_is_not_a_duplicate(cfg, db, site, same_title, status, reason):
    """Перевыложенная закрытая вакансия — снова живая; с ошибкой — оригинал так и не прочитан."""
    db.insert(3, **{**_card(3), "title": "Инженер"}, status=status, filter_reason=reason)
    site.append((20, False))
    assert scout.scan(cfg, db, page=None) == 1 and same_title == [20]


def test_duplicates_need_company_same_site_and_option(cfg, db):
    db.insert(1, title="Инженер", company="Acme", status="new")
    db.insert(2, title="Инженер", company=None, status="new")
    assert db.duplicate_of(5, "Инженер", "Acme") == 1
    assert db.duplicate_of(5, "Инженер", None) is None             # анонимный работодатель
    assert db.duplicate_of(5, "Инженер", "Other") is None
    assert db.duplicate_of(1_000_000_005, "Инженер", "Acme") is None  # Хабр не сверяется с hh
    assert db.duplicate_of(1, "Инженер", "Acme") is None             # сама с собой — не дубль


def test_duplicates_kept_when_option_off(cfg, db, site, same_title):
    cfg.filters.skip_duplicates = False
    site += [(10, False), (11, False)]
    assert scout.scan(cfg, db, page=None) == 2
