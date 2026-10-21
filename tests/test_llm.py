import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from hh_agent import llm


class Ping(BaseModel):
    ok: bool


class Nested(BaseModel):
    items: list[Ping]


NOW = datetime(2026, 10, 20, 15, 37)
CODEX_LIMIT = ("You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
               "https://chatgpt.com/settings/usage to purchase more credits or try again at 4:37 PM.")


@pytest.mark.parametrize("message, expected", [
    (CODEX_LIMIT, datetime(2026, 10, 20, 16, 37)),
    ("try again at 3:10 PM.", datetime(2026, 10, 21, 15, 10)),  # уже прошло — значит, завтра
    ("Try again at Oct 21st, 2026 9:05 AM.", datetime(2026, 10, 21, 9, 5)),
    ("Please try again in 2 hours 15 minutes.", datetime(2026, 10, 20, 17, 52)),
    ("try again later", None),
    ("rate limit exceeded", None),
])
def test_reset_at(message, expected):
    assert llm.reset_at(message, NOW) == expected


def test_schema_is_strict_at_every_level():
    schema = llm._schema(Nested)
    assert schema["additionalProperties"] is False
    assert all(d.get("additionalProperties") is False for d in schema["$defs"].values())


@pytest.mark.parametrize("text", ['{"ok": true}', '```json\n{"ok": true}\n```', 'Вот ответ: {"ok": true} — готово'])
def test_parse_strips_wrapping(text):
    assert llm._parse(text, Ping).ok is True


def test_parse_rejects_wrong_schema():
    with pytest.raises(RuntimeError, match="не по схеме"):
        llm._parse('{"other": 1}', Ping)


def _backends(monkeypatch, codex_error: str = CODEX_LIMIT):
    calls = []

    def fake(cfg, b, system, user, model, purpose):
        calls.append(b)
        if b == "codex":
            raise llm.LimitReached(f"codex: {codex_error}")
        return Ping(ok=True)

    monkeypatch.setattr(llm, "ask_backend", fake)
    real = llm.reset_at  # время сброса считаем от NOW, а не от настоящего «сейчас»
    monkeypatch.setattr(llm, "reset_at", lambda msg, now=None: real(msg, NOW))
    return calls


def test_fallback_until_reset_time_and_across_runs(cfg, monkeypatch):
    cfg.analyst.backend, cfg.analyst.fallback_backend = "codex", "claude_code"
    calls = _backends(monkeypatch)
    monkeypatch.setattr(llm.time, "time", lambda: NOW.timestamp())

    assert llm.ask_json(cfg, "s", "u", Ping).ok
    assert llm.ask_json(cfg, "s", "u", Ping).ok
    assert calls == ["codex", "claude_code", "claude_code"]  # после лимита — сразу запасной
    saved = json.loads(cfg.path(llm.LIMITS_FILE).read_text())
    assert saved == {"codex": datetime(2026, 10, 20, 16, 37).timestamp()}

    llm._limited = None  # новый запуск: время сброса читается из файла
    llm.ask_json(cfg, "s", "u", Ping)
    assert calls[-1] == "claude_code"

    monkeypatch.setattr(llm.time, "time", lambda: datetime(2026, 10, 20, 16, 40).timestamp())
    llm.ask_json(cfg, "s", "u", Ping)  # после сброса — снова основной
    assert calls[-2:] == ["codex", "claude_code"]


def test_fallback_without_reset_time_lasts_until_run_ends(cfg, monkeypatch):
    cfg.analyst.backend, cfg.analyst.fallback_backend = "codex", "claude_code"
    calls = _backends(monkeypatch, codex_error="quota exceeded")
    llm.ask_json(cfg, "s", "u", Ping)
    llm.ask_json(cfg, "s", "u", Ping)
    assert calls == ["codex", "claude_code", "claude_code"]
    assert not cfg.path(llm.LIMITS_FILE).exists() or json.loads(cfg.path(llm.LIMITS_FILE).read_text()) == {}


def test_no_fallback_raises(cfg, monkeypatch):
    cfg.analyst.backend, cfg.analyst.fallback_backend = "codex", None
    _backends(monkeypatch)
    with pytest.raises(llm.LimitReached):
        llm.ask_json(cfg, "s", "u", Ping)


def test_fallback_settings_apply_only_to_fallback(cfg):
    cfg.analyst.model = "main-model"
    cfg.analyst.fallback_settings = {"model": "fb-model", "base_url": "http://fb/v1"}
    fb = llm.fallback_cfg(cfg).analyst
    assert (fb.model, fb.base_url) == ("fb-model", "http://fb/v1")
    assert (cfg.analyst.model, cfg.analyst.base_url) == ("main-model", None)


def _codex_run(monkeypatch, stdout: str, stderr: str = ""):
    monkeypatch.setattr(llm.subprocess, "run",
                        lambda cmd, **kw: SimpleNamespace(stdout=stdout, stderr=stderr, returncode=1))


def test_codex_usage_limit_is_limit_reached_with_reset_time(cfg, monkeypatch):
    event = {"type": "turn.failed", "error": {"message": CODEX_LIMIT}}
    _codex_run(monkeypatch, json.dumps(event) + "\n", stderr="WARN unrelated warning\n")
    with pytest.raises(llm.LimitReached) as e:
        llm.run_codex("prompt", Ping, cfg=cfg)
    assert "try again at 4:37 PM" in str(e.value)


def test_codex_usage_field_is_not_a_limit(cfg, monkeypatch):
    # в каждом обычном turn.completed есть поле "usage" — это не исчерпание лимита
    _codex_run(monkeypatch, '{"type":"turn.completed","usage":{"input_tokens":1}}\n', stderr="boom")
    with pytest.raises(RuntimeError) as e:
        llm.run_codex("prompt", Ping, cfg=cfg)
    assert not isinstance(e.value, llm.LimitReached)


def test_codex_words_from_vacancy_are_not_errors(cfg, monkeypatch):
    # «login» и «rate limit» в пересказе вакансии — не «нет входа» и не исчерпанный лимит
    events = [{"type": "item.completed", "item": {"type": "agent_message", "text": "SSO login, rate limiting в API"}},
              {"type": "turn.failed", "error": {"message": "stream disconnected before completion"}}]
    _codex_run(monkeypatch, "\n".join(json.dumps(e) for e in events) + "\n")
    with pytest.raises(RuntimeError) as e:
        llm.run_codex("prompt", Ping, cfg=cfg)
    assert not isinstance(e.value, llm.LimitReached)


def test_codex_not_logged_in(cfg, monkeypatch):
    _codex_run(monkeypatch, "", stderr="Error: Not logged in. Run `codex login`.")
    with pytest.raises(SystemExit, match="codex login"):
        llm.run_codex("prompt", Ping, cfg=cfg)


def test_api_key_from_env_file_with_export(cfg, monkeypatch):
    monkeypatch.delenv("HH_TEST_KEY", raising=False)
    cfg.analyst.api_key_env = "HH_TEST_KEY"
    cfg.path(".env").write_text("# ключи\nexport HH_TEST_KEY='sk-123'\n", encoding="utf-8")
    assert llm.api_key(cfg) == "sk-123"
