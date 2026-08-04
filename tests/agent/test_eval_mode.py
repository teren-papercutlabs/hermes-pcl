"""Eval mode: the switch that keeps a measurement run from changing its subject."""

import pytest

from agent.eval_mode import (
    EVAL_MODE_ENV,
    eval_mode_enabled,
    self_modification_allowed,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(EVAL_MODE_ENV, raising=False)


def test_default_is_off_so_production_is_untouched():
    assert eval_mode_enabled() is False
    assert eval_mode_enabled({"agent": {}}) is False
    assert self_modification_allowed() is True


@pytest.mark.parametrize("raw", ["1", "true", "on", "yes", "eval", True])
def test_environment_turns_it_on(monkeypatch, raw):
    monkeypatch.setenv(EVAL_MODE_ENV, str(raw))
    assert eval_mode_enabled() is True
    assert self_modification_allowed() is False


def test_config_turns_it_on_without_the_environment():
    assert eval_mode_enabled({"agent": {"eval_mode": True}}) is True
    assert eval_mode_enabled({"eval_mode": "yes"}) is True


def test_environment_wins_over_a_config_copied_from_a_live_home(monkeypatch):
    monkeypatch.setenv(EVAL_MODE_ENV, "1")
    assert eval_mode_enabled({"agent": {"eval_mode": False}}) is True


def test_unparseable_values_do_not_silently_enable_it(monkeypatch):
    monkeypatch.setenv(EVAL_MODE_ENV, "maybe")
    assert eval_mode_enabled() is False


def test_background_review_is_suppressed_in_eval_mode(monkeypatch):
    """The guard is on the SPAWN: silencing the summary would still let it write."""
    import run_agent

    monkeypatch.setenv(EVAL_MODE_ENV, "1")
    spawned = []
    monkeypatch.setattr(
        run_agent.threading,
        "Thread",
        lambda *a, **k: spawned.append((a, k)),
        raising=False,
    )
    agent = run_agent.AIAgent.__new__(run_agent.AIAgent)
    agent._spawn_background_review([], review_memory=True, review_skills=True)
    assert spawned == []
