"""Общая консоль всех модулей. Одна на процесс, поэтому строки параллельных потоков autopilot
не склеиваются. В рабочих потоках каждая строка — со временем и именем потока, а заголовок (rule)
печатается обычной строкой.

console.sequential() — работа идёт параллельно, а вывод блоками по порядку (в autopilot: hh.ru, потом
Хабр Карьера, потом Telegram): строки текущего блока печатаются сразу, следующих — копятся и выходят
целиком (с исходным временем), когда закончится предыдущий блок (section_done)."""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime

from rich.console import Console


def _worker() -> str | None:
    t = threading.current_thread()
    return None if t is threading.main_thread() else t.name


class _Console(Console):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._seq_lock = threading.RLock()
        self._order: list[tuple[str, str]] = []  # (блок, заголовок) по порядку вывода
        self._index: dict[str, int] = {}
        self._section_of: Callable[[str], str | None] | None = None
        self._current = 0
        self._done: set[str] = set()
        self._buffers: dict[str, list[tuple[tuple, dict]]] = {}

    def print(self, *objects, **kwargs) -> None:
        name = _worker()
        if name and objects and isinstance(objects[0], str):
            tag = (f"{datetime.now():%H:%M:%S} {name} " if kwargs.get("markup") is False
                   else f"[dim]{datetime.now():%H:%M:%S}[/] [bold]{name}[/] ")
            objects = (tag + objects[0], *objects[1:])
        with self._seq_lock:
            section = self._section_of(name) if name and self._section_of else None
            if self._index.get(section, -1) > self._current:  # блок ещё не дошёл до очереди — копим
                self._buffers[section].append((objects, kwargs))
                return
            super().print(*objects, **kwargs)

    def rule(self, title: str = "", **kwargs) -> None:
        if _worker():
            self.print(f"── {title} ──" if title else "──")
        else:
            super().rule(title, **kwargs)

    @contextmanager
    def sequential(self, order: list[tuple[str, str]], section_of: Callable[[str], str | None]) -> Iterator[None]:
        """order — [(блок, заголовок)]; section_of — блок по имени потока (None — печатать сразу)."""
        with self._seq_lock:
            self._order, self._section_of, self._current, self._done = list(order), section_of, 0, set()
            self._index = {key: i for i, (key, _) in enumerate(order)}
            self._buffers = {key: [] for key, _ in order}
            if order:
                super().rule(f"[bold]{order[0][1]}")
        try:
            yield
        finally:
            with self._seq_lock:
                self._done = {key for key, _ in self._order}
                self._advance()
                self._order, self._section_of, self._buffers, self._index = [], None, {}, {}

    def section_done(self, key: str) -> None:
        """Блок закончился: вывести накопленное следующими блоками и печатать их дальше сразу."""
        with self._seq_lock:
            self._done.add(key)
            self._advance()

    def _advance(self) -> None:
        while self._current < len(self._order) and self._order[self._current][0] in self._done:
            self._current += 1
            if self._current < len(self._order):
                key, title = self._order[self._current]
                super().rule(f"[bold]{title}")
                for objects, kwargs in self._buffers.pop(key, []):
                    super().print(*objects, **kwargs)


console = _Console()
