"""Acceptance coverage for the synthetic P5a workflow template."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from hermes_cli import kanban_db, wf_engine


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/workflow/synthetic_allied_like.yaml"
ENGINE_PLANE = (
    ROOT / "hermes_cli/wf_engine.py",
    ROOT / "hermes_cli/wf_watcher.py",
    ROOT / "tools/wf_tools.py",
)
TENANT_NOUNS = (
    "shipment_job",
    "booking_ref",
    "container_no",
    "job_no",
    "pickup_advice",
    "vgm_reply",
)


def _load_template() -> dict:
    document = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    assert set(document) == {"workflow"}
    template = document["workflow"]
    assert isinstance(template, dict)
    return template


def _wait(step: dict, kind: str) -> dict:
    waits = [wait for wait in step.get("waits", []) if wait["kind"] == kind]
    assert len(waits) == 1
    return waits[0]


def test_p5a_template_registers_exact_frozen_shape(tmp_path):
    template = _load_template()

    assert template["id"] == "synthetic-freight-loop"
    assert template["entity"] == "shipment_job"
    assert template["correlation_keys"] == ["container_no", "job_no", "booking_ref"]
    assert template["disambiguators"] == ["customer", "vessel", "direction"]
    assert template["create_on"] == [
        {"type": "trucking_instruction", "brief": "synthetic_mail_ingest"}
    ]

    steps = template["steps"]
    assert [step["key"] for step in steps] == [
        "ingest",
        "await_pickup",
        "create_job",
        "await_container",
        "notify_customer",
        "await_vgm",
        "await_gatein",
        "invoice",
        "done",
    ]
    assert all("mode" in step for step in steps)
    assert {step["mode"] for step in steps} <= {"auto", "manual", "propose"}
    assert {step["key"]: step["mode"] for step in steps} == {
        "ingest": "propose",
        "await_pickup": "auto",
        "create_job": "propose",
        "await_container": "manual",
        "notify_customer": "propose",
        "await_vgm": "auto",
        "await_gatein": "auto",
        "invoice": "propose",
        "done": "auto",
    }

    by_key = {step["key"]: step for step in steps}
    assert by_key["ingest"]["advance_to"] == "await_pickup"
    assert by_key["create_job"]["actions"] == ["job_create", "sheet_log"]
    assert by_key["create_job"]["advance_to"] == "await_container"
    assert by_key["notify_customer"]["actions"] == ["customer_email"]
    assert by_key["notify_customer"]["advance_to"] == "await_vgm"
    assert by_key["invoice"]["actions"] == [
        "charges_add",
        "invoice_generate",
        "invoice_email",
    ]
    assert by_key["invoice"]["advance_to"] == "done"

    pickup_event = _wait(by_key["await_pickup"], "event")
    assert pickup_event == {
        "kind": "event",
        "types": ["pickup_advice"],
        "schema": "pickup_advice_v1",
        "advance_to": "create_job",
    }
    pickup_timer = _wait(by_key["await_pickup"], "timer")
    assert pickup_timer == {
        "kind": "timer",
        "after": "72h",
        "action": "chase",
        "max_fires": 3,
        "then": "escalate",
    }

    container_event = _wait(by_key["await_container"], "event")
    assert container_event == {
        "kind": "event",
        "types": ["container_assigned"],
        "schema": "container_assigned_v1",
        "advance_to": "notify_customer",
    }

    vgm_event = _wait(by_key["await_vgm"], "event")
    assert vgm_event == {
        "kind": "event",
        "types": ["vgm_reply"],
        "schema": "vgm_reply_v1",
        "advance_to": "await_gatein",
    }
    vgm_timer = _wait(by_key["await_vgm"], "timer")
    assert vgm_timer == {
        "kind": "timer",
        "after": "24h",
        "action": "chase",
        "max_fires": 2,
        "then": "escalate",
    }

    gatein_event = _wait(by_key["await_gatein"], "event")
    assert gatein_event == {
        "kind": "event",
        "types": ["gate_in"],
        "schema": "gate_in_v1",
        "advance_to": "invoice",
    }
    assert by_key["done"] == {"key": "done", "mode": "auto"}

    conn = kanban_db.connect(tmp_path / "kanban.sqlite")
    try:
        template_id, version = wf_engine.register_template(conn, template)
        assert (template_id, version) == ("synthetic-freight-loop@1", 1)
        stored = conn.execute(
            "SELECT spec FROM wf_template WHERE slug = ? AND version = ?",
            ("synthetic-freight-loop", 1),
        ).fetchone()
        assert stored is not None
        assert json.loads(stored["spec"]) == template
    finally:
        conn.close()


def test_engine_plane_remains_tenant_neutral():
    for path in ENGINE_PLANE:
        source = path.read_text(encoding="utf-8")
        for noun in TENANT_NOUNS:
            assert noun not in source, f"tenant noun {noun!r} leaked into {path}"
