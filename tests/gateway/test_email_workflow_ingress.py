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


def test_embedded_watcher_extracts_ingressed_email_before_matching(
    isolated_email_env, monkeypatch
):
    """Consumer path: gateway ingress -> embedded tick -> engine outcome."""
    from hermes_cli import wf_engine

    runner = _runner()
    conn = kanban_db.connect(board="workflow")
    template = {
        "id": "email-ingress-flow",
        "entity": "case",
        "correlation_keys": ["case"],
        "disambiguators": [],
        "create_on": [],
        "email_extraction": {
            "schema": "email-reply-v1",
            "instruction": "Classify the persisted email into a typed reply.",
        },
        "steps": [
            {"key": "start", "advance_to": "waiting"},
            {
                "key": "waiting",
                "waits": [{
                    "kind": "event", "types": ["reply"],
                    "schema": "email-reply-v1", "advance_to": "done",
                }],
            },
            {"key": "done"},
        ],
    }
    try:
        template_id, _ = wf_engine.register_template(conn, template)
        task_id = wf_engine.create_instance(
            conn, template_id=template_id, entity_key="case-live",
            corr={"case": "live"}, vars={}, source_event_id=None,
        )
        setup = wf_engine.ingest_event(
            conn, source="synthetic", external_id="setup", payload={}, corr={}, event_type="setup",
        )
        assert setup is not None
        wf_engine.advance(conn, task_id, to_step="waiting", event_id=setup)
    finally:
        conn.close()

    isolated_email_env.mkdir(parents=True, exist_ok=True)
    body_path = isolated_email_env / "mail-body.txt"
    body_path.write_text("CASE=live", encoding="utf-8")
    runner._ingest_email_workflow_event(
        {"source": "email", "external_id": "<live@example.test>", "body_ref": str(body_path)}
    )
    observed = []

    def extract(brief, event):
        observed.append((brief, event))
        assert brief == template["email_extraction"]
        assert "candidates" not in event
        return {
            "event_type": "reply", "payload": {"result": "accepted"},
            "corr": {"case": "live"},
        }

    runner._extract_workflow_email = extract
    runner._validate_workflow_email_payload = (  # type: ignore[method-assign]
        lambda schema, payload: schema == "email-reply-v1" and payload == {"result": "accepted"}
    )
    monkeypatch.setattr(kanban_db, "list_boards", lambda include_archived=False: [{"slug": "workflow"}])
    runner._running = True
    real_sleep = asyncio.sleep

    async def stop_after_tick(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", stop_after_tick)
    asyncio.run(runner._wf_watcher(interval=1))

    conn = kanban_db.connect(board="workflow")
    try:
        row = conn.execute(
            "SELECT event_type, status, matched_task_id FROM wf_event WHERE source = 'email'"
        ).fetchone()
        assert tuple(row) == ("reply", "applied", task_id)
        assert conn.execute(
            "SELECT current_step_key FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0] == "done"
    finally:
        conn.close()
    assert len(observed) == 1


def test_gateway_email_extractor_uses_configured_auxiliary_runtime(
    isolated_email_env, monkeypatch
):
    """The live extractor uses the host auxiliary slot, never a second client."""
    from agent import auxiliary_client

    body_path = isolated_email_env / "workflow" / "ingress" / "email" / "bodies" / "body.txt"
    body_path.parent.mkdir(parents=True, exist_ok=True)
    body_path.write_text("typed input", encoding="utf-8")
    calls = {}

    class Completions:
        def create(self, **kwargs):
            calls.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps({
                        "event_type": "reply", "payload": {"result": "ok"}, "corr": {"case": "one"},
                    }))
                )]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr(
        auxiliary_client, "get_text_auxiliary_client", lambda task: (client, "host-aux-model")
    )
    monkeypatch.setattr(auxiliary_client, "get_auxiliary_extra_body", lambda: {})
    monkeypatch.setattr(auxiliary_client, "auxiliary_max_tokens_param", lambda n: {"max_tokens": n})

    extracted = _runner()._extract_workflow_email(
        {"schema": "email-reply-v1", "instruction": "return a typed reply"},
        {
            "payload": {
                "body_ref": str(body_path), "external_id": "<one@example.test>",
                "sender": "user@example.test", "subject": "status",
            }
        },
    )
    assert extracted == {"event_type": "reply", "payload": {"result": "ok"}, "corr": {"case": "one"}}
    assert calls["model"] == "host-aux-model"
    assert calls["response_format"] == {"type": "json_object"}
    assert calls["max_tokens"] == 800
    assert "return a typed reply" in calls["messages"][1]["content"]
    assert "candidates" not in calls["messages"][1]["content"]
