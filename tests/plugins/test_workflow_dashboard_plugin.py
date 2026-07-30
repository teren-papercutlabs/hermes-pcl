"""Acceptance tests for the workflow dashboard API plugin."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import shutil
import subprocess
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
    assert [stage["key"] for stage in data["templates"][0]["steps"]] == ["received", "review", "done"]
    assert data["templates"][0]["steps"][0]["advance_to"] == "review"
    assert data["templates"][0]["steps"][1]["waits"][0]["advance_to"] == "done"
    card_ids = {card["task_id"] for card in data["cards"]}
    assert task_id in card_ids and len(card_ids) == 2
    assert data["templates"][0]["stage_counts"]["received"] == 2
    assert data["templates"][0]["columns"][0]["count"] == 2
    assert all("current_step_key" in card for card in data["cards"])
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
    assert timeline["transitions"][-1]["event"]["event_type"] == "reply"
    assert timeline["transitions"][-1]["event"]["applied_at"] is not None
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
    assert data["approvals"][0]["payload"]["body"] == "private body"
    assert "cap-token" not in json.dumps(data)
    assert '"secret"' not in json.dumps(data)
    assert any(item["event_id"] == event_id for item in data["events"])
    review = next(item for item in data["events"] if item["event_id"] == event_id)
    assert review["event_summary"]["event_type"] is None
    assert review["event_summary"]["payload_shape"]["kind"] == "object"
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
        called["kwargs"] = kwargs
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
        called["kwargs"] = kwargs
        return original(*args, **kwargs)

    monkeypatch.setattr(module.wf_engine, "resolve_event", wrapped)
    response = client.post(
        f"/api/plugins/workflow/action/events/{event_id}/resolve",
        json={"task_id": task_id, "decided_by": "tester"},
    )
    assert response.status_code == 409  # not a deterministic candidate
    assert called["yes"] is True
    assert called["kwargs"]["decided_by"] == "tester"


def test_mutations_ignore_board_query_parameter(workflow_env):
    client, conn, _module = workflow_env
    template_id, _ = wf_engine.register_template(conn, _spec())
    _task_id, approval_id, token = _approval(conn, template_id, "thread-fixed-board")
    response = client.post(
        f"/api/plugins/workflow/action/approvals/{approval_id}?board=other",
        json={"decision": "approved", "decided_by": "tester", "token": token},
    )
    assert response.status_code == 200, response.text


def test_board_counts_are_scoped_to_each_template(workflow_env):
    client, conn, _module = workflow_env
    first_template, _ = wf_engine.register_template(conn, _spec())
    second_template, _ = wf_engine.register_template(conn, {
        "id": "other-flow",
        "steps": [
            {"key": "received", "label": "Other received", "advance_to": "done"},
            {"key": "done", "label": "Other done"},
        ],
    })
    _instance(conn, first_template, "first-template")
    _instance(conn, second_template, "second-template")

    board = client.get("/api/plugins/workflow/board").json()
    templates = {item["template_id"]: item for item in board["templates"]}
    assert templates["mail-flow@1"]["stage_counts"]["received"] == 1
    assert templates["other-flow@1"]["stage_counts"]["received"] == 1
    assert board["template_stage_counts"]["mail-flow@1"]["received"] == 1
    assert board["template_stage_counts"]["other-flow@1"]["received"] == 1


def _run_bundle_contract(board: dict, actions: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the dashboard bundle contract")
    bundle = Path(__file__).resolve().parents[2] / "plugins/workflow/dashboard/dist/index.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
const window = {
  __HERMES_PLUGIN_SDK__: {
    React: { createElement() { return null; } },
    hooks: {}, components: {}, flow: {}, utils: {},
  },
  __HERMES_PLUGINS__: { register() {} },
};
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), { window });
const helpers = window.__HERMES_WORKFLOW_DASHBOARD__;
process.stdout.write(JSON.stringify(helpers.dashboardContractModel(JSON.parse(process.argv[2]), JSON.parse(process.argv[3]))));
"""
    result = subprocess.run(
        [node, "-e", script, str(bundle), json.dumps(board), json.dumps(actions)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_real_api_json_executes_bundle_board_graph_and_action_contract(workflow_env):
    client, conn, _module = workflow_env
    template_id, _ = wf_engine.register_template(conn, _spec())
    _instance(conn, template_id, "thread-contract")
    _task_id, _approval_id, _token = _approval(conn, template_id, "thread-contract-approval")
    event_id = wf_engine.ingest_event(
        conn,
        source="email",
        external_id="contract-review",
        payload={"body": "must not render", "kind": "reply"},
        corr={"thread": "thread-contract"},
        event_type="reply",
    )
    conn.execute("UPDATE wf_event SET status = 'needs_review' WHERE id = ?", (event_id,))

    board = client.get("/api/plugins/workflow/board").json()
    actions = client.get("/api/plugins/workflow/actions").json()
    rendered = _run_bundle_contract(board, actions)
    assert rendered["templates"][0]["columns"]
    assert sum(column["count"] for column in rendered["templates"][0]["columns"]) > 0
    assert {edge["source"] + "->" + edge["target"] for edge in rendered["templates"][0]["edges"]} == {
        "received->review",
        "review->done",
    }
    assert rendered["approvals"][0]["payload"]["body"] == "private body"
    assert rendered["events"][0]["event_summary"]["correlation_values"] == {"thread": "thread-contract"}
    assert "must not render" not in json.dumps(rendered)


def test_bundle_uses_api_stage_shape_timestamp_nesting_and_operator_identity():
    source = (Path(__file__).resolve().parents[2] / "plugins/workflow/dashboard/dist/index.js").read_text()
    assert "function templateRenderModel" in source
    assert "var steps = asArray(item.steps)" in source
    assert "advance_to" in source and "waits" in source
    assert "function dateFromTimestamp" in source
    assert "numeric * 1000" in source
    assert "var event = row.event || {}" in source
    assert "Operator identity" in source
    assert 'decided_by: "dashboard"' not in source
    assert "item.stages" not in source
