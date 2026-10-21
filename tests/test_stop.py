"""Остановка по Ctrl+C и SIGTERM: флаг, паузы, пул запросов к LLM, ошибки потоков после остановки."""
import signal
import threading
import time

import pytest

from hh_agent import autopilot, stop
from hh_agent.analyst import _in_parallel


@pytest.fixture(autouse=True)
def _fresh_stop(monkeypatch):
    monkeypatch.setattr(stop, "_EVENT", threading.Event())
    monkeypatch.setattr(stop, "_signum", 0)


def test_first_signal_sets_flag_and_interrupts():
    with pytest.raises(KeyboardInterrupt):
        stop._handler(signal.SIGTERM, None)
    assert stop.requested() and stop.reason() == "SIGTERM" and stop.exit_code() == 143
    with pytest.raises(KeyboardInterrupt):  # второй — тоже прерывает, причина остаётся первой
        stop._handler(signal.SIGINT, None)
    assert stop.reason() == "SIGTERM"
    with pytest.raises(stop.Stopped):
        stop.check()


def test_sleep_ends_on_stop(cfg):
    stop._EVENT.set()
    t0 = time.monotonic()
    assert autopilot._sleep(600, "Пауза", cfg, dry_run=True)
    assert time.monotonic() - t0 < 1


def test_llm_pool_starts_nothing_after_stop(cfg):
    cfg.analyst.parallel = 1
    calls = []

    def fn(chunk):
        calls.append(chunk)
        stop._EVENT.set()
        return chunk

    assert list(_in_parallel(cfg, fn, [[1], [2], [3]])) == []
    assert calls == [[1]]


def test_thread_error_after_stop_is_not_a_failure(cfg):
    def boom(cfg):
        stop._EVENT.set()
        raise RuntimeError("Target page, context or browser has been closed")

    t = autopilot._thread(boom, cfg, name="hh.ru")
    t.start()
    t.join()
    assert t.daemon and not cfg.path("logs/events.jsonl").exists()
