from __future__ import annotations

import json
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Any, Iterable

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

-- Ссылки на ботов-рекрутеров из чатов hh: pending -> started
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
    updated_at   TEXT
);
"""

COLUMNS = {
    "search_name", "title", "company", "salary_from", "salary_to", "currency", "area", "url",
    "experience", "work_formats", "has_test", "letter_required", "description", "key_skills",
    "found_at", "status", "filter_reason", "score", "verdict", "reasons", "red_flags",
    "cover_letter", "applied_at", "error", "attempts",
}


class DB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(vacancies)")}
        if "attempts" not in cols:  # миграция базы, созданной до появления обучения
            self.conn.execute("ALTER TABLE vacancies ADD COLUMN attempts INTEGER DEFAULT 0")
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

    def tg_pending(self, bot: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM tg_links WHERE bot=? AND status='pending' ORDER BY found_at",
                                 (bot,)).fetchall()

    def tg_link_started(self, vid: int) -> None:
        self.conn.execute("UPDATE tg_links SET status='started', started_at=? WHERE vacancy_id=?",
                          (datetime.now().isoformat(timespec="seconds"), vid))
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
