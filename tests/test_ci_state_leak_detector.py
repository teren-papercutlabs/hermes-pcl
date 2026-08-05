"""Regression coverage for the process-state leak detector in conftest."""

from __future__ import annotations

import asyncio
import threading

import pytest


def _conftest_plugin(request):
    return next(
        plugin
        for plugin in request.config.pluginmanager.get_plugins()
        if getattr(plugin, "__file__", "").endswith("tests/conftest.py")
    )


def test_detector_names_every_leftover(request):
    conftest = _conftest_plugin(request)
    policy = object()
    stream = object()
    before = {
        "threads": {1: "MainThread"},
        "environment": {},
        "stdout": stream,
        "stderr": stream,
        "event_loop": (policy, None, None, None),
        "children": {},
        "cwd": "/before",
    }
    after = {
        "threads": {1: "MainThread", 2: "hermes-leaked-worker"},
        "environment": {"HERMES_EVAL_MODE": "1"},
        "stdout": object(),
        "stderr": object(),
        "event_loop": (object(), object(), False, None),
        "children": {42: "python"},
        "cwd": "/after",
    }

    assert conftest._process_state_leaks(before, after) == [
        "left thread hermes-leaked-worker running",
        "left HERMES_EVAL_MODE set",
        "replaced sys.stdout",
        "replaced sys.stderr",
        "replaced event loop policy",
        "left a different current event loop",
        "left child process python (pid 42) running",
        "changed cwd to /after",
    ]


def test_sanctioned_fixtures_restore_state(monkeypatch, capsys):
    monkeypatch.setenv("HERMES_EVAL_MODE", "1")
    print("captured")
    assert capsys.readouterr().out == "captured\n"


@pytest.mark.asyncio
async def test_pytest_asyncio_loop_is_sanctioned():
    assert asyncio.get_running_loop().is_running()


def test_timeout_handler_dumps_all_threads(monkeypatch, capsys, request):
    conftest = _conftest_plugin(request)
    calls = []
    monkeypatch.setattr(
        conftest.faulthandler,
        "dump_traceback",
        lambda **kwargs: calls.append(kwargs),
    )

    with pytest.raises(TimeoutError, match="30 second timeout"):
        conftest._timeout_handler(None, None)

    assert calls[0]["all_threads"] is True
    assert sorted(thread.name for thread in threading.enumerate())
    assert "live threads at test timeout:" in capsys.readouterr().err
