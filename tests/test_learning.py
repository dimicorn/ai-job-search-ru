"""Подстройка по итогам сессии (learning.learn): правила по событиям журнала."""
from hh_agent import learning
from hh_agent.events import log_event


def test_calm_sessions_shorten_pauses(cfg):
    store = learning.Store(cfg)
    store.tuning["pace_multiplier"] = 1.5
    store.data["calm_streak"] = learning.CALM_SESSIONS - 1
    store.save()
    log_event(cfg, "apply", vid=1, mode="auto", status="applied")
    learning.learn(cfg)
    assert learning.Store(cfg).tuning["pace_multiplier"] == round(1.5 / 1.1, 2)


def test_captcha_lengthens_pauses(cfg):
    log_event(cfg, "captcha", where="apply")
    learning.learn(cfg)
    assert learning.Store(cfg).tuning["pace_multiplier"] == 1.25


def test_dry_run_does_not_reset_calm_streak(cfg):
    store = learning.Store(cfg)
    store.data["calm_streak"] = 2
    store.save()
    log_event(cfg, "apply", vid=1, mode="dry_run", status="dry_run")
    learning.learn(cfg)
    assert learning.Store(cfg).data["calm_streak"] == 2
