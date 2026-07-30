"""Acceptance tests for the workflow dashboard API plugin."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db, wf_engine


def _load_plugin():
    path = Path(__file__).resolve().parents[2] / "plugins/workflow/dashboard/plugin_api.py"
    name = f"workflow_dashboard_test_{id(path)}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def workflow_env(tmp_path, monkeypatch):
    db_path = tmp_path / "workflow.sqlite"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    conn = kanban_db.connect(db_path)
    module = _load_plugin()
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/workflow")
    client = TestClient(app)
    yield client, conn, module
    conn.close()


def _spec():
    return {
        "id": "mail-flow",
        "correlation_keys": ["thread"],
        "steps": [
            {"key": "received", "label": "Received", "advance_to": "review"},
            {"key": "review", "label": "Review", "waits": [{"kind": "event", "types": ["reply"], "advance_to": "done"}]},
            {"key": "done", "label": "Done"},
        ],
    }


def _instance(conn, template_id, entity, *, state=None):
    task_id = wf_engine.create_instance(
        conn,
        template_id=template_id,
        entity_key=entity,
        corr={"thread": entity},
        vars={},
        source_event_id=None,
    )
    if state == "review":
        event_id = wf_engine.ingest_event(conn, source="test", external_id=f"open-{entity}", payload={}, corr={}, event_type="open")
        wf_engine.advance(conn, task_id, to_step="review", event_id=event_id)
    return task_id


def _approval(conn, template_id, entity):
    task_id = wf_engine.create_instance(
        conn, template_id=template_id, entity_key=entity, corr={"thread": entity}, vars={}, source_event_id=None,
    )
    approval_id = wf_engine.propose(conn, task_id, "send_email", {"body": "private body", "secret": "cap-token"})
    token = conn.execute("SELECT resume_token FROM wf_approval WHERE id = ?", (approval_id,)).fetchone()[0]
    return task_id, approval_id, token


def test_board_is_collection_bound_and_excludes_archived(workflow_env, monkeypatch):
    client, conn, module = workflow_env
    template_id, _ = wf_engine.register_template(conn, _spec())
    task_id = _instance(conn, template_id, "thread-1")
    wf_engine.create_instance(conn, template_id=template_id, entity_key="thread-2", corr={"thread": "thread-2"}, vars={}, source_event_id=None)
    archived = wf_engine.create_instance(conn, template_id=template_id, entity_key="thread-3", corr={"thread": "thread-3"}, vars={}, source_event_id=None)
    conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (archived,))

    traces: list[str] = []
    original_connect = module.kanban_db.connect

    def traced_connect(*args, **kwargs):
        db = original_connect(*args, **kwargs)
        db.set_trace_callback(traces.append)
        return db

    monkeypatch.setattr(module.kanban_db, "connect", traced_connect)
    response = client.get("/api/plugins/workflow/board")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["board"] == "workflow"
    assert [stage["key"] for stage in data["templates"][0]["stages"]] == ["received", "review", "done"]
    card_ids = {card["task_id"] for card in data["cards"]}
    assert task_id in card_ids and len(card_ids) == 2
    assert data["stage_counts"]["received"] == 2
    assert sum("SELECT i.task_id" in statement for statement in traces) == 1


def test_timeline_is_ordered_and_does_not_expose_event_payload(workflow_env):
    client, conn, _module = workflow_env
    template_id, _ = wf_engine.register_template(conn, _spec())
    task_id = _instance(conn, template_id, "thread-timeline", state="review")
    event_id = wf_engine.ingest_event(
        conn,
        source="email",
        external_id="raw-email",
        payload={"body": "raw email body", "body_file": "/private/body", "token": "secret"},
        corr={"thread": "thread-timeline"},
        event_type="reply",
    )
    wf_engine.apply_event(conn, event_id, task_id, expected_step="review")

    response = client.get(f"/api/plugins/workflow/instances/{task_id}/timeline")
    assert response.status_code == 200
    timeline = response.json()
    assert timeline["instance"]["task_id"] == task_id
    assert timeline["transitions"][-1]["event_id"] == event_id
    assert [row["applied_at"] for row in timeline["transitions"]] == sorted(
        row["applied_at"] for row in timeline["transitions"]
    )
    serialized = json.dumps(timeline)
    assert "raw email body" not in serialized
    assert "/private/body" not in serialized
    assert "secret" not in serialized
    assert "payload" not in serialized


def test_actions_return_safe_approval_and_candidate_summaries(workflow_env):
    client, conn, _module = workflow_env
    template_id, _ = wf_engine.register_template(conn, _spec())
    task_id, approval_id, token = _approval(conn, template_id, "thread-approval")
    event_id = wf_engine.ingest_event(
        conn, source="email", external_id="needs-review", payload={"body": "private"}, corr={}, event_type=None,
    )
    conn.execute("UPDATE wf_event SET status = 'needs_review' WHERE id = ?", (event_id,))

    data = client.get("/api/plugins/workflow/actions").json()
    assert data["approvals"][0]["approval_id"] == approval_id
    assert "resume_token" not in json.dumps(data)
    assert "private body" not in json.dumps(data)
    assert "cap-token" not in json.dumps(data)
    assert '"secret"' not in json.dumps(data)
    assert any(item["event_id"] == event_id for item in data["events"])
    assert task_id
    assert token


def test_actions_delegate_and_replay_is_conflict_without_second_outbox(workflow_env, monkeypatch):
    client, conn, module = workflow_env
    template_id, _ = wf_engine.register_template(conn, _spec())
    task_id, approval_id, token = _approval(conn, template_id, "thread-approval-route")
    called = {}
    original = module.wf_engine.decide_approval

    def wrapped(*args, **kwargs):
        called["yes"] = True
        return original(*args, **kwargs)

    monkeypatch.setattr(module.wf_engine, "decide_approval", wrapped)
    response = client.post(
        f"/api/plugins/workflow/action/approvals/{approval_id}",
        json={"decision": "approved", "decided_by": "tester", "token": token},
    )
    assert response.status_code == 200, response.text
    assert called["yes"] is True
    assert "token" not in response.text
    assert conn.execute("SELECT COUNT(*) FROM wf_outbox WHERE task_id = ?", (task_id,)).fetchone()[0] == 1

    replay = client.post(
        f"/api/plugins/workflow/action/approvals/{approval_id}",
        json={"decision": "approved", "decided_by": "tester", "token": token},
    )
    assert replay.status_code == 409
    assert conn.execute("SELECT COUNT(*) FROM wf_outbox WHERE task_id = ?", (task_id,)).fetchone()[0] == 1


def test_action_validation_not_found_and_resolve_delegation(workflow_env, monkeypatch):
    client, conn, module = workflow_env
    assert client.post("/api/plugins/workflow/action/approvals/999", json={}).status_code == 400
    assert client.post(
        "/api/plugins/workflow/action/approvals/999",
        json={"decision": "wat", "decided_by": "tester", "token": "x"},
    ).status_code == 400
    assert client.post(
        "/api/plugins/workflow/action/events/999/resolve",
        json={"task_id": None, "decided_by": "tester"},
    ).status_code == 404

    template_id, _ = wf_engine.register_template(conn, _spec())
    task_id = _instance(conn, template_id, "thread-resolve")
    event_id = wf_engine.ingest_event(conn, source="email", external_id="resolve", payload={}, corr={}, event_type="unmatched")
    conn.execute("UPDATE wf_event SET status = 'needs_review' WHERE id = ?", (event_id,))
    called = {}
    original = module.wf_engine.resolve_event

    def wrapped(*args, **kwargs):
        called["yes"] = True
        return original(*args, **kwargs)

    monkeypatch.setattr(module.wf_engine, "resolve_event", wrapped)
    response = client.post(
        f"/api/plugins/workflow/action/events/{event_id}/resolve",
        json={"task_id": task_id, "decided_by": "tester"},
    )
    assert response.status_code == 409  # not a deterministic candidate
    assert called["yes"] is True
