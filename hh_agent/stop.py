"""Остановка по Ctrl+C или SIGTERM (kill, launchctl): без трейсбэков, с итогом запуска.

Первый сигнал выставляет флаг и прерывает главный поток (KeyboardInterrupt). Рабочие потоки autopilot
видят флаг: новых откликов и опросов не начинают, паузы обрывают, ошибки после остановки
не считают сбоями — вакансия, отклик на которую прервали, остаётся в очереди. Второй сигнал —
выйти сразу, не дожидаясь потоков.

Ctrl+C в терминале получает и драйвер Playwright: он сам закрывает браузер, поэтому текущий отклик
обрывается. SIGTERM приходит только агенту — текущий отклик он доделывает."""
from __future__ import annotations

import signal
import threading

_EVENT = threading.Event()
_signum = 0


class Stopped(Exception):
    """Запрошена остановка — выйти из текущего шага."""


def requested() -> bool:
    return _EVENT.is_set()


def wait(seconds: float) -> bool:
    """Пауза, которую обрывает остановка. True — остановка запрошена."""
    return _EVENT.wait(max(0.0, seconds))


def check() -> None:
    if _EVENT.is_set():
        raise Stopped


def reason() -> str:
    return "Ctrl+C" if _signum in (0, signal.SIGINT) else signal.Signals(_signum).name


def exit_code() -> int:
    return 128 + (_signum or signal.SIGINT)


def _handler(signum, frame) -> None:
    global _signum
    if not _EVENT.is_set():
        _signum = signum
        _EVENT.set()
    raise KeyboardInterrupt


def install() -> None:
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, _handler)
