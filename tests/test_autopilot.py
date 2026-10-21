"""autopilot целиком: потоки сайтов, общая очередь и лимиты, запись итогов в базу и журнал, остановка.
Браузер и формы отклика подменены (FakeApplier), потоки, база и журнал — настоящие."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from hh_agent import autopilot, stop
from hh_agent.events import read_events

HABR_ID = 1_000_000_000


@pytest.fixture(autouse=True)
def _fresh_stop(monkeypatch):
    monkeypatch.setattr(stop, "_EVENT", threading.Event())
    monkeypatch.setattr(stop, "_signum", 0)


class FakeApplier:
    """Отклик без браузера: статус по id вакансии (по умолчанию applied)."""
    calls: list[int] = []
    statuses: dict[int, tuple[str, str | None]] = {}
    on_apply = None

    def __init__(self, page, cfg, mode):
        self.mode = mode

    def apply(self, vid, url, letter, vacancy=""):
        FakeApplier.calls.append(vid)
        if FakeApplier.on_apply:
            FakeApplier.on_apply(vid)
        if self.mode.dry_run:
            return "dry_run", "форма заполнена"
        return FakeApplier.statuses.get(vid, ("applied", None))


@pytest.fixture
def auto(cfg, db, monkeypatch):
    """Конфиг без пауз и рабочих часов, браузер и формы — подставные."""
    au = cfg.apply.auto
    au.delay_seconds = au.break_seconds = (0, 0)
    au.active_hours, au.weekdays_only = (0, 24), False
    cfg.browser.delay_seconds = (0, 0)  # пауза dry-run
    cfg.telegram.enabled = False
    monkeypatch.setattr(autopilot, "POLL_SECONDS", 0.05)

    @contextmanager
    def fake_browser(*a, **kw):
        yield SimpleNamespace(), SimpleNamespace()

    monkeypatch.setattr(autopilot, "open_browser", fake_browser)
    monkeypatch.setattr(autopilot, "attach", fake_browser)
    monkeypatch.setattr(autopilot, "free_port", lambda: 0)
    monkeypatch.setattr(autopilot, "SiteTabs", lambda ctx, page: (lambda src: None))
    monkeypatch.setattr(autopilot, "is_logged_in", lambda page, src: True)
    monkeypatch.setattr(autopilot, "Applier", FakeApplier)
    FakeApplier.calls, FakeApplier.statuses, FakeApplier.on_apply = [], {}, None
    return cfg


def _vacancy(db, vid: int, score: int = 80, status: str = "scored") -> None:
    url = f"https://career.habr.com/vacancies/{vid}" if vid >= HABR_ID else f"https://hh.ru/vacancy/{vid}"
    db.insert(vid, title=f"ML Engineer {vid}", company="Acme", url=url, status=status, score=score,
              cover_letter="Здравствуйте!\n\nПисьмо.", description="Описание")


def _end(cfg) -> dict:
    return [e for e in read_events(cfg) if e["kind"] == "auto_end"][-1]


def test_applies_on_both_sites_until_queue_ends(auto, db):
    for vid in (1, 2, HABR_ID + 1):
        _vacancy(db, vid)
    _vacancy(db, 3, score=50)  # ниже порога
    autopilot.run_auto(auto, db)
    assert sorted(FakeApplier.calls) == [1, 2, HABR_ID + 1]
    assert {r["id"] for r in db.by_status("applied")} == {1, 2, HABR_ID + 1}
    assert db.applied_today() == 3
    assert _end(auto)["stop_reason"] == "кандидаты закончились" and _end(auto)["done"] == 3


def test_run_limit_is_shared_by_sites(auto, db):
    auto.apply.auto.max_per_run = 3
    for vid in (1, 2, 3, HABR_ID + 1, HABR_ID + 2, HABR_ID + 3):
        _vacancy(db, vid)
    autopilot.run_auto(auto, db)
    assert len(FakeApplier.calls) == 3 and db.applied_today() == 3
    assert _end(auto)["stop_reason"] == "лимит запуска"


def test_daily_limit_counts_earlier_applies(auto, db):
    auto.apply.daily_limit = 2
    _vacancy(db, 9, status="applied")
    db.update(9, applied_at="2099-01-01T10:00:00")  # отклик «сегодня» (applied_at ≥ сегодняшней даты)
    for vid in (1, 2, 3):
        _vacancy(db, vid)
    autopilot.run_auto(auto, db)
    assert db.applied_today() == 2 and len(FakeApplier.calls) == 1
    assert _end(auto)["stop_reason"] == "дневной лимит"


def test_outcomes_are_recorded(auto, db):
    for vid in (1, 2, 3):
        _vacancy(db, vid)
    FakeApplier.statuses = {1: ("manual", "у работодателя тест"), 2: ("unconfirmed", "не подтвердилось"),
                            3: ("already_applied", "отклик уже был")}
    autopilot.run_auto(auto, db)
    assert db.get(1)["status"] == "manual" and db.get(1)["error"] == "у работодателя тест"
    assert db.get(2)["status"] == "unconfirmed" and db.get(2)["applied_at"]  # мог уйти — в дневной лимит
    assert db.get(3)["status"] == "applied" and db.get(3)["applied_at"]  # узнали сейчас — в дневной лимит
    assert db.applied_today() == 2 and _end(auto)["done"] == 0  # но откликами этого запуска не считаются


def test_consecutive_errors_stop_the_site(auto, db):
    auto.apply.auto.max_consecutive_errors = 2
    for vid in (1, 2, 3):
        _vacancy(db, vid)

    def broken(vid):
        raise RuntimeError("вёрстка поменялась")

    FakeApplier.on_apply = broken
    autopilot.run_auto(auto, db)
    assert len(FakeApplier.calls) == 2
    assert len(db.by_status("error")) == 2 and len(db.by_status("scored")) == 1


def test_dry_run_sends_nothing(auto, db):
    for vid in (1, 2):
        _vacancy(db, vid)
    autopilot.run_auto(auto, db, dry_run=True)
    assert FakeApplier.calls and not db.by_status("applied") and db.applied_today() == 0


def test_stop_finishes_current_apply_and_takes_no_more(auto, db):
    for vid in (1, 2, 3):
        _vacancy(db, vid)
    FakeApplier.on_apply = lambda vid: stop._EVENT.set()  # SIGTERM пришёл во время первого отклика
    autopilot.run_auto(auto, db)
    assert len(FakeApplier.calls) == 1 and db.applied_today() == 1  # текущий отклик доделан
    assert _end(auto)["stop_reason"] == "остановлено (Ctrl+C)"


def test_autopilot_applies_to_vacancies_found_during_the_run(auto, db, monkeypatch):
    """Поиск и оценка идут параллельно с откликами: новые вакансии подхватываются по мере оценки."""
    def fake_scan(cfg, db_, page, sources):
        if "hh" in sources:
            _vacancy(db_, 7, status="new")
        return 1

    def fake_analyze(cfg, db_):
        for r in db_.by_status("new"):
            db_.update(r["id"], status="scored", score=90)
        return 1

    monkeypatch.setattr(autopilot, "scan", fake_scan)
    monkeypatch.setattr(autopilot.analyst_mod, "analyze_new", fake_analyze)
    autopilot.run_auto(auto, db, produce=True)
    assert FakeApplier.calls == [7] and db.get(7)["status"] == "applied"
