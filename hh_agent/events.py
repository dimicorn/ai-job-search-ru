"""Структурированный журнал событий (logs/events.jsonl) — материал для обучения в конце сессии."""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from .config import Config

SESSION = f"{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}"


def log_event(cfg: Config, kind: str, **data: Any) -> None:
    path = cfg.path("logs/events.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "session": SESSION, "kind": kind, **data}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def read_events(cfg: Config) -> list[dict[str, Any]]:
    path = cfg.path("logs/events.jsonl")
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
