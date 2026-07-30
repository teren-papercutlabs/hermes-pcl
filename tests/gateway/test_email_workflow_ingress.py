"""End-to-end tests for GatewayRunner's email workflow ingress wiring."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.run import GatewayRunner
from hermes_cli import kanban_db


RAW_BODY = "Raw inbound body\nwith a second line."


@pytest.fixture
def isolated_email_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "workflow")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "workflow.db"))
    monkeypatch.setenv("EMAIL_ADDRESS", "hermes@test.com")
    monkeypatch.setenv("EMAIL_PASSWORD", "test-password")
    monkeypatch.setenv("EMAIL_IMAP_HOST", "imap.test.com")
    monkeypatch.setenv("EMAIL_SMTP_HOST", "smtp.test.com")
    monkeypatch.setenv("EMAIL_ALLOWED_USERS", "user@test.com")
    return hermes_home


def _runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    return runner


def _message() -> dict:
    return {
        "uid": b"20",
        "sender_addr": "user@test.com",
        "sender_name": "User",
        "subject": "Booking status",
        "message_id": "<workflow@test.com>",
        "in_reply_to": "<parent@test.com>",
        "references": "<root@test.com> <parent@test.com>",
        "body": RAW_BODY,
        "attachments": [],
        "date": "Wed, 30 Jul 2026 11:00:00 +0800",
    }


def _adapter() -> tuple[GatewayRunner, object]:
    runner = _runner()
    adapter = runner._create_adapter(
        Platform.EMAIL,
        PlatformConfig(enabled=True),
    )
    assert adapter is not None
    assert adapter._workflow_ingress_callback.__self__ is runner
    adapter.handle_message = AsyncMock()
    return runner, adapter


def _workflow_rows():
    conn = kanban_db.connect(board="workflow")
    try:
        return conn.execute(
            "SELECT source, external_id, event_type, corr, payload "
            "FROM wf_event ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_factory_dispatch_creates_one_body_by_reference_event(
    isolated_email_env,
):
    _runner_instance, adapter = _adapter()

    asyncio.run(adapter._dispatch_message(_message()))

    rows = _workflow_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "email"
    assert row["external_id"] == "<workflow@test.com>"
    assert row["event_type"] is None
    assert json.loads(row["corr"]) == {}

    payload = json.loads(row["payload"])
    assert payload["source"] == "email"
    assert payload["external_id"] == "<workflow@test.com>"
    assert "body_ref" in payload
    assert "body" not in payload
    assert RAW_BODY not in row["payload"]
    assert payload["body_ref"]
    with open(payload["body_ref"], encoding="utf-8") as body_file:
        assert body_file.read() == RAW_BODY

    adapter.handle_message.assert_awaited_once()


def test_factory_dispatch_exact_redelivery_is_deduplicated(
    isolated_email_env,
):
    _runner_instance, adapter = _adapter()
    message = _message()

    asyncio.run(adapter._dispatch_message(message))
    asyncio.run(adapter._dispatch_message(message))

    assert len(_workflow_rows()) == 1
    assert adapter.handle_message.await_count == 2


def test_factory_dispatch_stays_fail_closed_when_workflow_db_fails(
    isolated_email_env,
    monkeypatch,
):
    _runner_instance, adapter = _adapter()

    def fail_connect(*args, **kwargs):
        raise RuntimeError("workflow database unavailable")

    monkeypatch.setattr(kanban_db, "connect", fail_connect)

    with pytest.raises(RuntimeError, match="workflow database unavailable"):
        asyncio.run(adapter._dispatch_message(_message()))

    adapter.handle_message.assert_not_awaited()


def test_workflow_ingress_defaults_to_workflow_board_and_ignores_duplicate_result(
    isolated_email_env,
    monkeypatch,
):
    monkeypatch.delenv("HERMES_KANBAN_BOARD")
    runner = _runner()
    envelope = {
        "source": "email",
        "external_id": "<duplicate@test.com>",
        "body_ref": str(isolated_email_env / "body.txt"),
    }

    runner._ingest_email_workflow_event(envelope)
    runner._ingest_email_workflow_event(envelope)

    rows = _workflow_rows()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"]) == envelope
