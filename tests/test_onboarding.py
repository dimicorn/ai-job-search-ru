"""Установка без ручной работы: резюме с hh.ru (hh-agent resume) и частые факты в мастере setup."""
import shutil
from pathlib import Path

import pytest
import yaml

from hh_agent import resume
from hh_agent.cli import cmd_resume, resume_is_template
from hh_agent.wizard import fill_facts

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def md():
    return resume.to_markdown((FIX / "hh_resume_export.html").read_text(encoding="utf-8"))


def test_resume_keeps_sections_and_useful_header(md):
    assert md.startswith("# ML-инженер\n")
    assert [line for line in md.splitlines() if line.startswith("## ")] == [
        "## Желаемая должность и зарплата", "## Опыт работы — 4 года", "## Образование", "## Навыки"]
    assert "- Проживает: Казань" in md
    assert "- Гражданство: Россия, есть разрешение на работу: Россия, Казахстан" in md
    assert "- Не готов к переезду, готов к командировкам" in md
    for kept in ("ООО «Пример»", "Январь 2022 — настоящее время", "• Детекция дефектов (YOLOv8)", "Лаборант-исследователь",
                 "КФУ, прикладная математика, 2020", "PyTorch", "Английский — B2", "150 000 ₽",
                 "220 000 000 кадров", "бот для Telegram (Telethon)"):  # числа и слово Telegram — не контакты
        assert kept in md, kept


def test_resume_drops_name_and_contacts(md):
    for private in ("Иванов", "Иван Иванович", "1996", "+7", "1234567", "example.com", "@ivan", "telegram",
                    "предпочитаемый", "Резюме обновлено", "<", "\n\n\n"):
        assert private not in md, private


def test_resume_rejects_other_pages():
    with pytest.raises(ValueError):
        resume.to_markdown("<html><body><p>Войдите на сайт</p></body></html>")


def test_resume_command_keeps_filled_resume(cfg):
    shutil.copy(ROOT / "resume.example.md", cfg.path("resume.example.md"))
    assert resume_is_template(cfg)  # resume.md ещё нет
    shutil.copy(ROOT / "resume.example.md", cfg.path("resume.md"))
    assert resume_is_template(cfg)  # копия образца — тоже шаблон
    cfg.path("resume.md").write_text("# Моё резюме\n", encoding="utf-8")
    assert not resume_is_template(cfg)
    with pytest.raises(SystemExit, match="--force"):
        cmd_resume(cfg, force=False)  # заполненный не перезаписываем, браузер не открывается
    assert cfg.path("resume.md").read_text(encoding="utf-8") == "# Моё резюме\n"


def test_fill_facts_from_template():
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    answers = iter(["150 000 ₽ на руки", "", "удалённо, полная занятость"])
    out = fill_facts(text, ask=lambda q: next(answers, ""))
    facts = yaml.safe_load(out)["questions"]["facts"]
    assert "Ожидаемая зарплата: 150 000 ₽ на руки" in facts
    assert "# Город, готовность к переезду и командировкам:" in facts  # пропущенное остаётся подсказкой
    assert "Формат работы и занятость: удалённо, полная занятость" in facts
    assert {k: v for k, v in yaml.safe_load(out).items() if k != "questions"} == \
           {k: v for k, v in yaml.safe_load(text).items() if k != "questions"}  # остальное не тронуто


def test_fill_facts_keeps_filled_facts_and_survives_eof():
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    filled = fill_facts(text, ask=lambda q: "ответ")
    assert fill_facts(filled, ask=lambda q: pytest.fail("уже заполнено — не спрашиваем")) == filled

    def eof(q):
        raise EOFError
    assert fill_facts(text, ask=eof) == text  # без терминала — без изменений
