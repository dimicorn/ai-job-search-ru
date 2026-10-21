from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancies (
    id            INTEGER PRIMARY KEY,
    search_name   TEXT,
    title         TEXT,
    company       TEXT,
    salary_from   INTEGER,
    salary_to     INTEGER,
    currency      TEXT,
    area          TEXT,
    url           TEXT,
    experience    TEXT,
    work_formats  TEXT,
    has_test      INTEGER DEFAULT 0,
    letter_required INTEGER DEFAULT 0,
    description   TEXT,
    key_skills    TEXT,
    found_at      TEXT,
    -- new -> filtered | scored -> skipped | applied | unconfirmed | manual | error
    status        TEXT DEFAULT 'new',
    filter_reason TEXT,
    score         INTEGER,
    verdict       TEXT,
    reasons       TEXT,
    red_flags     TEXT,
    cover_letter  TEXT,
    applied_at    TEXT,
    error         TEXT,
    attempts      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_status ON vacancies(status);

-- Ссылки на ботов-рекрутеров из чатов hh; started — бот уже запущен по ссылке или переписка с ним была
CREATE TABLE IF NOT EXISTS tg_links (
    vacancy_id  INTEGER PRIMARY KEY,
    bot         TEXT,
    start_param TEXT,
    status      TEXT DEFAULT 'pending',
    found_at    TEXT,
    started_at  TEXT
);
-- Диалог с ботом: active | human (ждёт вас) | done; last_seen_id — последнее обработанное сообщение бота
CREATE TABLE IF NOT EXISTS tg_dialogs (
    bot          TEXT PRIMARY KEY,
    vacancy_id   INTEGER,
    status       TEXT,
    last_seen_id INTEGER DEFAULT 0,
    note         TEXT,
    updated_at   TEXT,
    survey       TEXT,   -- название вакансии текущего опроса
    switched_at  TEXT    -- когда агент последний раз просил бота сменить вакансию
);
-- Опросы по вакансиям у бота, у которого одна переписка на все вакансии: active | human | done
CREATE TABLE IF NOT EXISTS tg_surveys (
    bot         TEXT,
    title       TEXT,
    vacancy_id  INTEGER,
    status      TEXT,
    started_at  TEXT,
    finished_at TEXT,
    PRIMARY KEY (bot, title)
);
"""

COLUMNS = {
    "search_name", "title", "company", "salary_from", "salary_to", "currency", "area", "url",
    "experience", "work_formats", "has_test", "letter_required", "description", "key_skills",
    "found_at", "status", "filter_reason", "score", "verdict", "reasons", "red_flags",
    "cover_letter", "applied_at", "error", "attempts",
}


# Статусы, которые устаревают, когда отклик на вакансию появился на сайте. filtered («уже откликались»,
# стоп-слово) и applied не трогаем: по ним агент ничего не делает.
RESPONDABLE = ("new", "scored", "skipped", "manual", "error", "unconfirmed")


class DB:
    def __init__(self, path: Path):
        # Соединение — на поток (sqlite3 не делит их между потоками); WAL — чтение не ждёт записи.
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(vacancies)")}
        if "attempts" not in cols:  # миграция базы, созданной до появления обучения
            self.conn.execute("ALTER TABLE vacancies ADD COLUMN attempts INTEGER DEFAULT 0")
            self.conn.commit()
        tg_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(tg_dialogs)")}
        for col in ("survey", "switched_at"):  # таблица появилась раньше этих колонок
            if col not in tg_cols:
                self.conn.execute(f"ALTER TABLE tg_dialogs ADD COLUMN {col} TEXT")
                self.conn.commit()

    def exists(self, vid: int) -> bool:
        return self.conn.execute("SELECT 1 FROM vacancies WHERE id=?", (vid,)).fetchone() is not None

    def insert(self, vid: int, **fields: Any) -> None:
        fields = {k: _enc(v) for k, v in fields.items() if k in COLUMNS}
        fields.setdefault("found_at", datetime.now().isoformat(timespec="seconds"))
        cols = ", ".join(["id", *fields])
        marks = ", ".join("?" * (len(fields) + 1))
        self.conn.execute(f"INSERT OR IGNORE INTO vacancies ({cols}) VALUES ({marks})", (vid, *fields.values()))
        self.conn.commit()

    def update(self, vid: int, **fields: Any) -> None:
        bad = set(fields) - COLUMNS
        if bad:
            raise KeyError(bad)
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE vacancies SET {sets} WHERE id=?", (*map(_enc, fields.values()), vid))
        self.conn.commit()

    def by_status(self, *statuses: str, min_score: int | None = None) -> list[sqlite3.Row]:
        q = f"SELECT * FROM vacancies WHERE status IN ({','.join('?' * len(statuses))})"
        args: list[Any] = list(statuses)
        if min_score is not None:
            q += " AND score >= ?"
            args.append(min_score)
        q += " ORDER BY score DESC, found_at DESC"
        return self.conn.execute(q, args).fetchall()

    def mark_responded(self, vid: int, note: str) -> str | None:
        """Сайт показывает, что отклик есть (вы откликнулись сами, неподтверждённый всё же ушёл) — статус applied.
        Дата отклика — когда агент это узнал (в дневной лимит идёт); у unconfirmed она уже есть — не меняем.
        Возвращает прежний статус, если он поменялся."""
        row = self.get(vid)
        if row is None or row["status"] not in RESPONDABLE:
            return None
        self.update(vid, status="applied", error=f"{note} (было: {row['status']})",
                    applied_at=row["applied_at"] or datetime.now().isoformat(timespec="seconds"))
        return row["status"]

    def get(self, vid: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM vacancies WHERE id=?", (vid,)).fetchone()

    def applied_today(self) -> int:
        today = date.today().isoformat()
        return self.conn.execute(
            "SELECT COUNT(*) FROM vacancies WHERE status IN ('applied', 'unconfirmed') AND applied_at >= ?", (today,)
        ).fetchone()[0]

    def tg_link(self, vid: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM tg_links WHERE vacancy_id=?", (vid,)).fetchone()

    def add_tg_link(self, vid: int, bot: str, start_param: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO tg_links (vacancy_id, bot, start_param, found_at) VALUES (?, ?, ?, ?)",
                          (vid, bot, start_param, datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()

    def tg_start_param(self, bot: str) -> str | None:
        """start-код ссылки, если бот по ней ещё не запускался."""
        r = self.conn.execute("SELECT start_param FROM tg_links WHERE bot=? AND status='pending' ORDER BY found_at",
                              (bot,)).fetchone()
        return r[0] if r else None

    def tg_links_started(self, bot: str) -> None:
        self.conn.execute("UPDATE tg_links SET status='started', started_at=? WHERE bot=? AND status='pending'",
                          (datetime.now().isoformat(timespec="seconds"), bot))
        self.conn.commit()

    def tg_link_bots(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT bot FROM tg_links")]

    def tg_dialog(self, bot: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM tg_dialogs WHERE bot=?", (bot,)).fetchone()

    def save_tg_dialog(self, bot: str, **fields: Any) -> None:
        fields["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.conn.execute("INSERT OR IGNORE INTO tg_dialogs (bot) VALUES (?)", (bot,))
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE tg_dialogs SET {sets} WHERE bot=?", (*fields.values(), bot))
        self.conn.commit()

    def tg_surveys(self, bot: str) -> dict[str, str]:
        """Название вакансии (в нижнем регистре) → статус опроса."""
        return {r[0]: r[1] for r in self.conn.execute("SELECT title, status FROM tg_surveys WHERE bot=?", (bot,))}

    def save_tg_survey(self, bot: str, title: str, status: str, vacancy_id: int | None = None) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute("INSERT OR IGNORE INTO tg_surveys (bot, title, vacancy_id, started_at) VALUES (?, ?, ?, ?)",
                          (bot, title, vacancy_id, now))
        self.conn.execute("UPDATE tg_surveys SET status=?, finished_at=? WHERE bot=? AND title=?",
                          (status, now if status == "done" else None, bot, title))
        self.conn.commit()

    def stats(self) -> Iterable[tuple[str, int]]:
        return self.conn.execute(
            "SELECT status, COUNT(*) FROM vacancies GROUP BY status ORDER BY 2 DESC"
        ).fetchall()


def _enc(v: Any) -> Any:
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return int(v)
    return v


def dec_list(s: str | None) -> list[str]:
    return json.loads(s) if s else []
