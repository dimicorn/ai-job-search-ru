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
