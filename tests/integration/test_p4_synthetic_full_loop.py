"""Credential-free P4 integration proof through email, workflow, and portal APIs."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.config import PlatformConfig
from gateway.platforms.email import EmailAdapter
from hermes_cli import kanban_db, wf_engine


FIXED_NOW = 2_000_000_000


def _load_dashboard_api(run: int):
    path = Path(__file__).resolve().parents[2] / "plugins/workflow/dashboard/plugin_api.py"
    name = f"p4_synthetic_dashboard_{run}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    module.time.time = lambda: FIXED_NOW
    return module


def _mail_spec() -> dict:
    return {
        "id": "p4-mail",
        "correlation_keys": ["case"],
        "steps": [
            {"key": "received", "label": "Received", "advance_to": "waiting"},
            {
                "key": "waiting",
                "label": "Waiting",
                "waits": [
                    {
                        "kind": "event",
                        "types": ["reply"],
                        "schema": "mail-reply",
                        "advance_to": "done",
                    }
                ],
            },
            {"key": "done", "label": "Done"},
        ],
    }


def _approval_spec() -> dict:
    return {
        "id": "p4-approval",
        "steps": [
            {
                "key": "draft",
                "label": "Draft",
                "advance_to": "sent",
                "reject_to": "rejected",
            },
            {"key": "sent", "label": "Sent"},
            {"key": "rejected", "label": "Rejected"},
        ],
    }


def _instance(conn, template_id: str, entity: str, corr: dict) -> str:
    return wf_engine.create_instance(
        conn,
        template_id=template_id,
        entity_key=entity,
        corr=corr,
        vars={},
        source_event_id=None,
    )


def _at_waiting(conn, task_id: str) -> None:
    event_id = wf_engine.ingest_event(
        conn,
        source="synthetic_setup",
        external_id=f"open:{task_id}",
        payload={},
        corr={},
        event_type="open",
    )
    assert event_id is not None
    wf_engine.advance(conn, task_id, to_step="waiting", event_id=event_id)


def _email(message_id: str, subject: str, body: str) -> dict:
    return {
        "uid": message_id.encode(),
        "sender_addr": "operator@example.test",
        "sender_name": "Synthetic Operator",
        "subject": subject,
        "message_id": message_id,
        "in_reply_to": "",
        "references": "",
        "body": body,
        "attachments": [],
        "date": "Tue, 1 Jan 2030 00:00:00 +0000",
    }


class _SyntheticSMTP:
    sent: list = []

    def __init__(self, *_args, **_kwargs):
        pass

    def starttls(self, **_kwargs):
        return None

    def login(self, *_args, **_kwargs):
        return None

    def send_message(self, message):
        self.sent.append(message)

    def quit(self):
        return None

    def close(self):
        return None


def _normalized_board(board: dict) -> dict:
    return {
        "columns": [
            {
                "key": column["key"],
                "count": column["count"],
                "entities": sorted(card["entity_key"] for card in column["cards"]),
            }
            for column in board["columns"]
        ],
        "stage_counts": board["stage_counts"],
        "badges": sorted(
            (card["entity_key"], tuple(card["badges"]))
            for card in board["cards"]
        ),
    }


def _run_full_loop(home: Path, run: int, monkeypatch) -> dict:
    home.mkdir()
    db_path = home / "workflow.sqlite"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("EMAIL_ADDRESS", "hermes@example.test")
    monkeypatch.setenv("EMAIL_PASSWORD", "synthetic-only")
    monkeypatch.setenv("EMAIL_IMAP_HOST", "imap.invalid")
    monkeypatch.setenv("EMAIL_SMTP_HOST", "smtp.invalid")
    monkeypatch.setenv("EMAIL_ALLOWED_USERS", "operator@example.test")

    dashboard = _load_dashboard_api(run)
    app = FastAPI()
    app.include_router(dashboard.router, prefix="/api/plugins/workflow")
    client = TestClient(app)
    conn = kanban_db.connect(db_path)
    ledger: dict[str, int] = {}

    def ledger_email(envelope: dict) -> None:
        event_id = wf_engine.ingest_event(
            conn,
            source="email",
            external_id=envelope["external_id"],
            payload=envelope,
            corr={},
            event_type=None,
        )
        assert event_id is not None
        ledger[envelope["external_id"]] = event_id

    adapter = EmailAdapter(
        PlatformConfig(enabled=True, extra={"workflow_ingress_callback": ledger_email})
    )
    adapter.handle_message = AsyncMock()

    try:
        with patch("gateway.platforms.email.get_hermes_home", return_value=home):
            mail_template, _ = wf_engine.register_template(conn, _mail_spec())
            approval_template, _ = wf_engine.register_template(conn, _approval_spec())

            valid_task = _instance(conn, mail_template, "case-valid", {"case": "valid"})
            _at_waiting(conn, valid_task)
            asyncio.run(adapter._dispatch_message(_email(
                "<valid@example.test>",
                "Valid reply",
                "CASE=valid; RESULT=accepted",
            )))
            valid_event = ledger["<valid@example.test>"]
            ledger_row = conn.execute(
                "SELECT payload, status FROM wf_event WHERE id = ?",
                (valid_event,),
            ).fetchone()
            body_ref = Path(json.loads(ledger_row["payload"])["body_ref"])
            assert ledger_row["status"] == "received"
            assert body_ref.read_text(encoding="utf-8") == "CASE=valid; RESULT=accepted"

            extraction_brief = {
                "schema": "mail-reply",
                "instruction": "Read the body reference and return reply/case fields.",
            }

            def extract_valid(brief: dict, event: dict) -> dict:
                assert brief == extraction_brief
                # The extraction boundary sees the ledger event, not candidate instances.
                assert "candidates" not in event
                text = Path(event["payload"]["body_ref"]).read_text(encoding="utf-8")
                assert text == "CASE=valid; RESULT=accepted"
                return {
                    "event_type": "reply",
                    "payload": {"result": "accepted"},
                    "corr": {"case": "valid"},
                }

            matched = wf_engine.extract_event(
                conn,
                valid_event,
                extraction_brief,
                extract_valid,
                {"mail-reply": lambda payload: payload == {"result": "accepted"}},
            )
            assert matched.kind == "matched"
            assert matched.task_id == valid_task
            applied = wf_engine.apply_event(
                conn,
                valid_event,
                valid_task,
                expected_step="waiting",
            )
            assert applied.kind == "applied"

            asyncio.run(adapter._dispatch_message(_email(
                "<unparseable@example.test>",
                "Broken reply",
                "this is not a typed workflow reply",
            )))
            bad_event = ledger["<unparseable@example.test>"]
            bad = wf_engine.extract_event(
                conn,
                bad_event,
                extraction_brief,
                lambda *_: {"payload": "not-an-object", "corr": {}},
                {"mail-reply": lambda _payload: True},
            )
            assert bad.kind == "needs_review"
            actions = client.get("/api/plugins/workflow/actions")
            assert actions.status_code == 200, actions.text
            assert any(
                item["event_id"] == bad_event and item["status"] == "needs_review"
                for item in actions.json()["events"]
            )

            ambiguous_a = _instance(conn, mail_template, "case-shared-a", {"case": "shared"})
            ambiguous_b = _instance(conn, mail_template, "case-shared-b", {"case": "shared"})
            _at_waiting(conn, ambiguous_a)
            _at_waiting(conn, ambiguous_b)
            asyncio.run(adapter._dispatch_message(_email(
                "<ambiguous@example.test>",
                "Shared reply",
                "CASE=shared; RESULT=accepted",
            )))
            ambiguous_event = ledger["<ambiguous@example.test>"]
            ambiguous = wf_engine.extract_event(
                conn,
                ambiguous_event,
                extraction_brief,
                lambda *_: {
                    "event_type": "reply",
                    "payload": {"result": "accepted"},
                    "corr": {"case": "shared"},
                },
                {"mail-reply": lambda payload: payload == {"result": "accepted"}},
            )
            assert ambiguous.kind == "ambiguous"
            actions = client.get("/api/plugins/workflow/actions").json()
            action = next(item for item in actions["events"] if item["event_id"] == ambiguous_event)
            assert sorted(item["entity_key"] for item in action["candidates"]) == [
                "case-shared-a",
                "case-shared-b",
            ]
            picked = client.post(
                f"/api/plugins/workflow/action/events/{ambiguous_event}/resolve",
                json={"task_id": ambiguous_b, "decided_by": "synthetic-reviewer"},
            )
            assert picked.status_code == 200, picked.text
            assert picked.json()["match_method"] == "human"
            assert tuple(conn.execute(
                "SELECT status, matched_task_id, match_method FROM wf_event WHERE id = ?",
                (ambiguous_event,),
            ).fetchone()) == ("applied", ambiguous_b, "human")

            approved_task = _instance(conn, approval_template, "approval-plain", {})
            approved_id = wf_engine.propose(
                conn,
                approved_task,
                "send_email",
                {"to": "recipient@example.test", "body": "original"},
            )
            approved_token = conn.execute(
                "SELECT resume_token FROM wf_approval WHERE id = ?",
                (approved_id,),
            ).fetchone()[0]
            approved = client.post(
                f"/api/plugins/workflow/action/approvals/{approved_id}",
                json={
                    "decision": "approved",
                    "decided_by": "synthetic-reviewer",
                    "token": approved_token,
                },
            )
            assert approved.status_code == 200, approved.text
            assert conn.execute(
                "SELECT COUNT(*) FROM wf_outbox WHERE task_id = ?",
                (approved_task,),
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT resume_token FROM wf_approval WHERE id = ?",
                (approved_id,),
            ).fetchone()[0] != approved_token
            replay = client.post(
                f"/api/plugins/workflow/action/approvals/{approved_id}",
                json={
                    "decision": "approved",
                    "decided_by": "synthetic-reviewer",
                    "token": approved_token,
                },
            )
            assert replay.status_code == 409

            edited_task = _instance(conn, approval_template, "approval-edited", {})
            edited_id = wf_engine.propose(
                conn,
                edited_task,
                "send_email",
                {"to": "recipient@example.test", "body": "original"},
            )
            edited_token = conn.execute(
                "SELECT resume_token FROM wf_approval WHERE id = ?",
                (edited_id,),
            ).fetchone()[0]
            edited_payload = {"to": "recipient@example.test", "body": "edited"}
            edited = client.post(
                f"/api/plugins/workflow/action/approvals/{edited_id}",
                json={
                    "decision": "edited_approved",
                    "decided_by": "synthetic-reviewer",
                    "token": edited_token,
                    "payload": edited_payload,
                },
            )
            assert edited.status_code == 200, edited.text
            edited_row = conn.execute(
                "SELECT decision_diff FROM wf_approval WHERE id = ?",
                (edited_id,),
            ).fetchone()
            assert edited_row["decision_diff"] == (
                '[{"op":"replace","path":"/body","value":"edited"}]'
            )
            assert tuple(conn.execute(
                "SELECT payload, status FROM wf_outbox WHERE task_id = ?",
                (edited_task,),
            ).fetchone()) == ('{"body":"edited","to":"recipient@example.test"}', "queued")

            # Leave one card in each UI badge state so the response shape proves
            # the actual card/badge contract consumed by board and graph views.
            pending_task = _instance(conn, approval_template, "badge-pending", {})
            wf_engine.propose(conn, pending_task, "send_email", {"body": "pending"})
            review_task = _instance(conn, approval_template, "badge-review", {})
            wf_engine.review(conn, review_task, "synthetic review")
            exception_task = _instance(conn, approval_template, "badge-exception", {})
            wf_engine.exception(conn, exception_task, "synthetic exception")

            asyncio.run(adapter._dispatch_message(_email(
                "<thread-a@example.test>",
                "Thread A",
                "first same-sender message",
            )))
            asyncio.run(adapter._dispatch_message(_email(
                "<thread-b@example.test>",
                "Thread B",
                "second same-sender message",
            )))
            _SyntheticSMTP.sent = []
            with patch("gateway.platforms.email.smtplib.SMTP", _SyntheticSMTP):
                adapter._send_email(
                    "operator@example.test",
                    "reply B first",
                    "<thread-b@example.test>",
                )
                adapter._send_email(
                    "operator@example.test",
                    "reply A second",
                    "<thread-a@example.test>",
                )
            assert [
                message["In-Reply-To"] for message in _SyntheticSMTP.sent
            ] == ["<thread-b@example.test>", "<thread-a@example.test>"]
            assert [
                message["Subject"] for message in _SyntheticSMTP.sent
            ] == ["Re: Thread B", "Re: Thread A"]

            board_response = client.get("/api/plugins/workflow/board")
            assert board_response.status_code == 200, board_response.text
            board = board_response.json()
            assert all(
                column["count"] == board["stage_counts"][column["key"]]
                for column in board["columns"]
            )
            badge_map = {
                card["entity_key"]: card["badges"]
                for card in board["cards"]
            }
            assert badge_map["case-shared-a"] == ["parked"]
            assert badge_map["badge-pending"] == ["pending_approval"]
            assert badge_map["badge-review"] == ["needs_review"]
            assert badge_map["badge-exception"] == ["exception"]

            timeline = client.get(
                f"/api/plugins/workflow/instances/{valid_task}/timeline"
            )
            assert timeline.status_code == 200, timeline.text
            transitions = timeline.json()["transitions"]
            assert [row["to_step"] for row in transitions] == ["waiting", "done"]
            assert transitions[-1]["event"]["match_method"] == "deterministic"

            return {
                "valid_event": tuple(conn.execute(
                    "SELECT status, match_method FROM wf_event WHERE id = ?",
                    (valid_event,),
                ).fetchone()),
                "bad_status": conn.execute(
                    "SELECT status FROM wf_event WHERE id = ?",
                    (bad_event,),
                ).fetchone()[0],
                "ambiguous_status": tuple(conn.execute(
                    "SELECT status, match_method FROM wf_event WHERE id = ?",
                    (ambiguous_event,),
                ).fetchone()),
                "approval_outbox": conn.execute(
                    "SELECT COUNT(*) FROM wf_outbox"
                ).fetchone()[0],
                "threads": [
                    message["In-Reply-To"] for message in _SyntheticSMTP.sent
                ],
                "board": _normalized_board(board),
                "timeline": [
                    (row["to_step"], row["event"]["event_type"], row["event"]["match_method"])
                    for row in transitions
                ],
            }
    finally:
        conn.close()


def test_p4_synthetic_full_loop_is_repeatable(tmp_path, monkeypatch):
    """Run the complete integration twice from empty, isolated Hermes homes."""

    first = _run_full_loop(tmp_path / "run-1", 1, monkeypatch)
    second = _run_full_loop(tmp_path / "run-2", 2, monkeypatch)
    assert first == second
