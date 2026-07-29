"""Tests for the workflow worker tool adapter."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


TOOL_NAMES = {
    "wf_context",
    "wf_advance",
    "wf_propose",
    "wf_review",
    "wf_exception",
    "wf_signal",
}


@pytest.fixture
def workflow_worker(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "workflow-worker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "")

    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="synthetic workflow task",
            assignee="workflow-worker",
        )
        conn.execute(
            "UPDATE tasks SET workflow_template_id = ?, current_step_key = ? WHERE id = ?",
            ("synthetic@1", "start", task_id),
        )
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    return task_id


def _definitions(monkeypatch):
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    definitions = registry.get_definitions(
        set(resolve_toolset("kanban")), quiet=True
    )
    return {
        entry["function"]["name"]
        for entry in definitions
        if entry.get("function", {}).get("name", "").startswith("wf_")
    }


def test_discovery_registers_exactly_six_workflow_tools():
    from tools.registry import discover_builtin_tools, registry

    discover_builtin_tools()
    names = {
        name
        for name in registry.get_tool_names_for_toolset("kanban")
        if name.startswith("wf_")
    }
    assert names == TOOL_NAMES
    assert all(
        registry.get_entry(name).toolset == "kanban" for name in TOOL_NAMES
    )


def test_workflow_tools_hidden_without_workflow_task(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert _definitions(monkeypatch).isdisjoint(TOOL_NAMES)


def test_workflow_tools_visible_for_workflow_task(monkeypatch, workflow_worker):
    assert _definitions(monkeypatch) == TOOL_NAMES


def test_handlers_default_to_owned_task_and_call_engine(monkeypatch, workflow_worker):
    from tools import wf_tools as wt

    calls = []

    def context(conn, task_id):
        calls.append(("context", task_id))
        return {"task_id": task_id, "step": "start"}

    def propose(conn, task_id, *, action, payload):
        calls.append(("propose", task_id, action, payload))
        return {"approval_id": "a1"}

    def review(conn, task_id, *, reason, options):
        calls.append(("review", task_id, reason, options))
        return {"review_id": "r1"}

    def exception(conn, task_id, *, reason):
        calls.append(("exception", task_id, reason))
        return {"state": "exception"}

    monkeypatch.setattr(wt.wf_engine, "context", context, raising=False)
    monkeypatch.setattr(wt.wf_engine, "propose", propose, raising=False)
    monkeypatch.setattr(wt.wf_engine, "review", review, raising=False)
    monkeypatch.setattr(wt.wf_engine, "exception", exception, raising=False)

    assert json.loads(wt._handle_context({}))["ok"] is True
    assert json.loads(wt._handle_propose({
        "action": "send_notice", "payload": {"to": "ops"},
    }))["ok"] is True
    assert json.loads(wt._handle_review({
        "reason": "two candidates", "options": [{"id": "a"}],
    }))["ok"] is True
    assert json.loads(wt._handle_exception({"reason": "upstream unavailable"}))["ok"] is True

    assert calls == [
        ("context", workflow_worker),
        ("propose", workflow_worker, "send_notice", {"to": "ops"}),
        ("review", workflow_worker, "two candidates", [{"id": "a"}]),
        ("exception", workflow_worker, "upstream unavailable"),
    ]


def test_advance_ledgers_then_advances_with_event_id(monkeypatch, workflow_worker):
    from tools import wf_tools as wt

    calls = []

    def ingest(conn, **kwargs):
        calls.append(("ingest", kwargs))
        return 17

    def advance(conn, task_id, *, to_step, event_id):
        calls.append(("advance", task_id, to_step, event_id))
        return {"step": to_step}

    monkeypatch.setattr(wt.wf_engine, "ingest_event", ingest)
    monkeypatch.setattr(wt.wf_engine, "advance", advance)
    result = json.loads(wt._handle_advance({
        "to_step": "next", "evidence": {"ref": "synthetic"},
    }))

    assert result == {"ok": True, "event_id": 17, "result": {"step": "next"}}
    assert calls[0][0] == "ingest"
    assert calls[0][1]["source"] == "worker"
    assert calls[0][1]["event_type"] == "wf_advance"
    assert calls[0][1]["payload"] == {"ref": "synthetic"}
    assert calls[0][1]["corr"] is None
    assert calls[0][1]["external_id"].startswith("worker-wf-advance-")
    assert calls[1] == ("advance", workflow_worker, "next", 17)


def test_foreign_task_is_refused_before_engine_call(monkeypatch, workflow_worker, tmp_path):
    from hermes_cli import kanban_db as kb
    from tools import wf_tools as wt

    conn = kb.connect()
    try:
        foreign = kb.create_task(conn, title="foreign", assignee="other")
        conn.execute(
            "UPDATE tasks SET workflow_template_id = ? WHERE id = ?",
            ("synthetic@1", foreign),
        )
    finally:
        conn.close()

    called = []
    monkeypatch.setattr(
        wt.wf_engine, "context", lambda *args, **kwargs: called.append(True),
        raising=False,
    )
    result = json.loads(wt._handle_context({"task_id": foreign}))
    assert "tool_error" in result
    assert called == []


def test_signal_uses_metadata_and_reports_duplicate(monkeypatch, workflow_worker):
    from tools import wf_tools as wt

    calls = []

    def ingest(conn, **kwargs):
        calls.append(kwargs)
        return 23 if len(calls) == 1 else None

    monkeypatch.setattr(wt.wf_engine, "ingest_event", ingest)
    payload = {
        "metadata": {"external_id": "mail-1", "event_type": "mail.received"},
        "body_ref": "synthetic://body",
    }
    args = {"source": "email", "payload": payload, "corr": {"ref": "R1"}}

    first = json.loads(wt._handle_signal(args))
    second = json.loads(wt._handle_signal(args))
    assert first == {"ok": True, "event_id": 23, "duplicate": False}
    assert second == {"ok": True, "event_id": None, "duplicate": True}
    assert calls[0] == {
        "source": "email",
        "external_id": "mail-1",
        "payload": payload,
        "corr": {"ref": "R1"},
        "event_type": "mail.received",
    }


def test_invalid_payloads_fail_closed(monkeypatch, workflow_worker):
    from tools import wf_tools as wt

    called = []
    monkeypatch.setattr(
        wt.wf_engine, "ingest_event", lambda *args, **kwargs: called.append(True),
    )
    cases = [
        wt._handle_advance({"to_step": "next", "evidence": []}),
        wt._handle_propose({"action": "send", "payload": []}),
        wt._handle_review({"reason": " ", "options": []}),
        wt._handle_review({"reason": "why", "options": {}}),
        wt._handle_exception({"reason": ""}),
        wt._handle_signal({"source": " ", "payload": {}, "corr": {}}),
        wt._handle_signal({"source": "email", "payload": [], "corr": {}}),
        wt._handle_signal({"source": "email", "payload": {}, "corr": []}),
    ]
    assert all("tool_error" in json.loads(result) for result in cases)
    assert called == []


def test_source_contains_no_raw_transition_or_query_code():
    source = Path(__file__).parents[2].joinpath("tools", "wf_tools.py").read_text()
    for forbidden in ("complete_task", "block_task", "unblock_task", "SELECT", "INSERT", "UPDATE"):
        assert forbidden not in source
