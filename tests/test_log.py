import io
import threading
import time

from hh_agent.autopilot import _section
from hh_agent.log import _Console

SITES = ["hh", "habr"]
ORDER = [("hh", "hh.ru"), ("habr", "Хабр Карьера"), ("tg", "AI-рекрутеры в Telegram")]


def test_section_of_thread():
    assert _section("поиск hh.ru", SITES) == "hh"
    assert _section("Хабр Карьера", SITES) == "habr"
    assert _section("Telegram", SITES) == "tg"
    assert _section("LLM_0", SITES) == "hh"  # оценка — в первом блоке
    assert _section("что-то ещё", SITES) is None


def test_parallel_work_sequential_output():
    """Хабр и Telegram заканчивают раньше hh, но их строки выходят после блока hh.ru."""
    out = io.StringIO()
    console = _Console(file=out, width=120)  # capture() у rich — свой в каждом потоке, поэтому пишем в буфер

    def say(lines, gap):
        for line in lines:
            time.sleep(gap)
            console.print(line)

    threads = [threading.Thread(target=say, args=(["hh-1", "hh-2", "hh-3"], 0.05), name="hh.ru"),
               threading.Thread(target=say, args=(["habr-1", "habr-2"], 0.0), name="Хабр Карьера"),
               threading.Thread(target=say, args=(["tg-1"], 0.0), name="Telegram")]
    with console.sequential(ORDER, lambda name: _section(name, SITES)):
        for t in threads:
            t.start()
        for key, _ in ORDER:
            for t in threads:
                if _section(t.name, SITES) == key:
                    t.join()
            console.section_done(key)
    out = out.getvalue()
    marks = ["hh.ru", "hh-1", "hh-2", "hh-3", "Хабр Карьера", "habr-1", "habr-2", "AI-рекрутеры", "tg-1"]
    positions = [out.index(m) for m in marks]
    assert positions == sorted(positions), out
    assert "Telegram" in out.splitlines()[-1]  # строки потоков — с именем потока
