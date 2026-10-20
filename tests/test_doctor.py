import shutil
from pathlib import Path

from hh_agent import doctor, recruiters

ROOT = Path(__file__).resolve().parent.parent


def _statuses(d):
    return {name: status for status, name, _ in d.results}


def test_doctor_without_config_fails(tmp_path):
    assert doctor.run(tmp_path / "config.yaml") == 1


def test_doctor_on_fresh_install(cfg, monkeypatch):
    """Свежая установка: конфиг из образца, резюме-шаблон, пустые факты — ошибка и подсказки."""
    shutil.copy(ROOT / "resume.example.md", cfg.path("resume.example.md"))
    shutil.copy(ROOT / "resume.example.md", cfg.path("resume.md"))
    monkeypatch.setattr(doctor.shutil, "which", lambda prog: f"/usr/bin/{prog}")
    monkeypatch.setattr(doctor, "_run", lambda cmd, timeout=20: (0, "Logged in using ChatGPT"))
    d = doctor.Doctor()
    assert doctor._config(d, cfg.base_dir / "config.yaml") is not None
    doctor._llm(d, cfg)
    doctor._telegram(d, cfg)
    st = _statuses(d)
    assert st["resume.md"] == doctor.FAIL  # шаблон — надо скачать или заполнить
    assert st["questions.facts"] == doctor.WARN  # в образце только подсказки-комментарии
    assert st["основная: codex"] == doctor.OK
    assert st["запасная LLM"] == doctor.WARN  # в образце запасной нет
    assert st["выключено"] == doctor.OK  # Telegram по умолчанию выключен


def test_doctor_reports_missing_llm_login(cfg, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda prog: None)
    cfg.analyst.fallback_backend = "openai"
    cfg.analyst.fallback_settings = {"base_url": "https://example.com/v1", "model": "m", "api_key_env": "NO_SUCH_KEY_ENV"}
    monkeypatch.delenv("NO_SUCH_KEY_ENV", raising=False)
    d = doctor.Doctor()
    doctor._llm(d, cfg)
    st = _statuses(d)
    assert st["основная: codex"] == doctor.FAIL and st["запасная: openai"] == doctor.FAIL


def test_telegram_session_lock_is_exclusive(cfg):
    first = recruiters.session_lock(cfg)
    assert first is not None
    assert recruiters.session_lock(cfg) is None  # второй запуск сессию не получит
    first.close()
    again = recruiters.session_lock(cfg)
    assert again is not None
    again.close()
