import asyncio
import hashlib
import json
from contextlib import closing
from types import SimpleNamespace

import pytest

from gateway import durable_jsonl_consumer as durable
from gateway import management_continuation_consumer as continuation
from gateway.replay import ReplayPlan, replay_context
from hermes_state import SessionDB


def _config(tmp_path, *, chat="management@g.us"):
    path = tmp_path / "config.yaml"
    path.write_text(
        "model:\n  provider: fixture\n  default: fixture-model\n"
        "pa:\n  enabled: true\n  management_continuation:\n"
        "    enabled: true\n    agent_id: agent\n    from_peer: owner\n"
        f"    to_peer: management\n    management_chat_id: {chat}\n"
        "    token_env: CONTINUATION_TEST_TOKEN\n"
        f"    coordinator_receipt_root: {tmp_path}\n",
        encoding="utf-8",
    )
    return path


def _envelope(tmp_path, *, original="original", request="request"):
    list_id = "case-list-20260910000000-abcdef1234"
    manifest = {
        "contract": "tgg-per-case-whatsapp-list/v1", "list_id": list_id,
        "management_request_id": request,
        "items": [{"job_no": "SK/JOB/2609/156"}],
    }
    path = tmp_path / "lists" / list_id / "manifest.json"
    path.parent.mkdir(parents=True)
    raw = json.dumps(manifest).encode()
    path.write_bytes(raw)
    return {
        "contract": "management-list-continuation/v1", "original_message_id": original,
        "management_request_id": request, "list_id": list_id,
        "chat_id": "management@g.us", "jobs": ["SK/JOB/2609/156"],
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _original_record():
    return durable.InboxRecord(
        seq=1, message_id="original", chat_id="management@g.us", start_offset=0, end_offset=1,
        raw={"messageId": "original", "chatId": "management@g.us", "isGroup": True,
             "senderId": "requester", "senderName": "Requester", "body": "Investigate",
             "timestamp": 1},
    )


def test_typed_claim_reserves_mailbox_and_terminalizes_prior_process_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTINUATION_TEST_TOKEN", "token")
    config_path = _config(tmp_path)
    config = continuation.load_management_continuation_config(
        config_path, inbox_db=tmp_path / "inbox.db", state_db=tmp_path / "state.db"
    )
    envelope = _envelope(tmp_path)
    with closing(SessionDB(db_path=tmp_path / "state.db")) as db:
        for index in range(25):
            db.create_session_mailbox_message(
                agent_id="agent", from_session_name="owner", to_session_name="management",
                body=f"ordinary mailbox body {index}", source_message_id=f"ordinary-{index}",
            )
        invalid = db.create_session_mailbox_message(
            agent_id="agent", from_session_name="owner", to_session_name="management",
            body="{not-json", source_message_id="ordinary-invalid",
        )
        row = db.create_session_mailbox_message(
            agent_id="agent", from_session_name="owner", to_session_name="management",
            body=json.dumps(envelope), source_message_id="original",
        )
        selected = continuation.next_pending_continuation(db, config)
        assert selected is not None
        assert selected[0]["id"] == row["id"]
        assert selected[1].original_message_id == "original"
        generic_rows = db.list_pending_session_mailbox(
            agent_id="agent", limit=25,
            exclude_body_contracts=("management-list-continuation/v1",),
        )
        assert len(generic_rows) == 25
        assert db.claim_session_mailbox(
            invalid["id"], exclude_body_contracts=("management-list-continuation/v1",)
        )
        # An earlier generic read of this row cannot take the reserved
        # contract because claim repeats the exclusion inside its transaction.
        assert not db.claim_session_mailbox(
            row["id"], exclude_body_contracts=("management-list-continuation/v1",)
        )
        assert db.claim_session_mailbox(
            row["id"], include_body_contract="management-list-continuation/v1"
        )
    source = tmp_path / "capture.jsonl"
    source.write_text("", encoding="utf-8")
    gate = tmp_path / "gate.json"
    gate.write_text(json.dumps({"version": 1, "enabled": False, "generation": 0}), encoding="utf-8")
    args = SimpleNamespace(
        config=str(config_path), source=str(source), cursor=str(tmp_path / "cursor.json"),
        inbox=str(tmp_path / "inbox.db"), status_file=str(tmp_path / "status.json"),
        processing_gate=str(gate), state_db=str(tmp_path / "state.db"), case_db=str(tmp_path / "case.db"),
        source_before_image_dir=str(tmp_path / "before"), lock_file=str(tmp_path / "consumer.lock"),
        activity_lock_file=None, site_concurrency=1, chat_batch_size=1, retention_batch_size=1,
        source_projection_batch_size=1, poll_seconds=0.01, max_records=1, once=True,
    )
    assert asyncio.run(durable.run_consumer(args)) == 0
    with closing(SessionDB(db_path=tmp_path / "state.db")) as db:
        assert db.get_session_mailbox_message(row["id"])["status"] == "failed"
        assert continuation.failed_continuation_status(db, config) == {
            "failed_count": 1,
            "latest_error": "CONTINUATION_PRIOR_PROCESS_OUTCOME_UNKNOWN",
        }
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["management_continuation"] == {
        "failed_count": 1,
        "latest_error": "CONTINUATION_PRIOR_PROCESS_OUTCOME_UNKNOWN",
    }


def test_trusted_replay_plan_binds_internal_event_to_retained_namespace():
    namespace = "agent:live-drain:persistent-chat:fixture:fixture-model"
    retained = namespace + ":whatsapp:group:management@g.us:requester"
    plan = ReplayPlan(
        replay_namespace=namespace,
        session_key_override=retained,
    )
    with replay_context(plan) as context:
        assert context.namespace_session_key("agent:main:whatsapp:group:management@g.us:system@internal") == retained


def test_adapter_internal_provenance_comes_from_runtime_not_payload(monkeypatch):
    from gateway.config import PlatformConfig
    from gateway.platforms.whatsapp import WhatsAppAdapter
    monkeypatch.delenv("WHATSAPP_GROUP_POLICY", raising=False)
    monkeypatch.delenv("WHATSAPP_GROUP_ALLOWED_USERS", raising=False)
    adapter = WhatsAppAdapter(PlatformConfig(enabled=True, extra={"group_policy": "open"}))
    message = {"messageId": "internal-one", "chatId": "management@g.us", "isGroup": True,
               "senderId": "system@internal", "body": "Continue retained request", "internal": True}
    plain = asyncio.run(adapter._build_message_event(message, bypass_require_mention=True))
    assert plain is not None and plain.internal is False
    with replay_context(ReplayPlan(messages=(message,), internal_message_ids=("internal-one",))):
        trusted = asyncio.run(adapter._build_message_event(message, bypass_require_mention=True))
    assert trusted is not None and trusted.internal is True


def test_completed_internal_turn_delivers_against_original_without_forging_it(
    tmp_path, monkeypatch
):
    asyncio.run(_completed_internal_turn_delivers(tmp_path, monkeypatch))


async def _completed_internal_turn_delivers(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTINUATION_TEST_TOKEN", "token")
    config_path = _config(tmp_path)
    config = continuation.load_management_continuation_config(
        config_path, inbox_db=tmp_path / "inbox.db", state_db=tmp_path / "state.db"
    )
    envelope = _envelope(tmp_path)
    inbox = SimpleNamespace(message_id_selection=lambda _: [_original_record()])
    received = {}

    async def fake_process(records, **kwargs):
        event = kwargs["replay_messages"][0]
        received["event"] = event
        received["session_key_override"] = kwargs["replay_session_key_override"]
        return {"provider_errors": [], "handled": [{
            "message_ids": [event["messageId"]], "turn_id": "completed-turn",
        }], "captured_outbound": [{"kind": "send"}]}

    monkeypatch.setattr(durable, "process_live_records", fake_process)
    monkeypatch.setattr(continuation, "_retained_live_management_session", lambda **_: ("bound", "session"))
    monkeypatch.setattr("gateway.management_continuation.require_complete_list", lambda _: {"complete": True})
    def deliver(_inbox, **kwargs):
        received["delivery"] = kwargs
        return {"delivered": 1, "undelivered": 0, "suppressed": 0, "duplicate": 0}
    monkeypatch.setattr(durable, "deliver_management_replies", deliver)

    with closing(SessionDB(db_path=tmp_path / "state.db")) as db:
        created = db.create_session_mailbox_message(
            agent_id="agent", from_session_name="owner", to_session_name="management",
            body=json.dumps(envelope), source_message_id="original",
        )
        row = db.get_session_mailbox_message(created["id"])
        assert db.claim_session_mailbox(created["id"])
        parsed = continuation._typed_envelope(row)
        await continuation.process_claimed_management_continuation(
            row=row, envelope=parsed, session_db=db, inbox=inbox, config=config,
            config_path=config_path, state_db=tmp_path / "state.db",
            gate_changed_at="2026-01-01T00:00:00Z", runner=object(),
        )
        assert db.get_session_mailbox_message(created["id"])["status"] == "delivered"
    assert received["event"]["messageId"].startswith("management-continuation:")
    assert received["event"]["senderId"] == "system@internal"
    assert received["session_key_override"] == "bound"
    assert received["event"]["quotedMessageId"] == "original"
    assert received["delivery"]["handled_groups"] == [{
        "message_ids": [received["event"]["messageId"]], "turn_id": "completed-turn"
    }]
    assert received["delivery"]["continuation"]["original_message_id"] == "original"


def test_unknown_model_outcome_is_failed_not_requeued(tmp_path, monkeypatch):
    asyncio.run(_unknown_model_outcome_is_failed(tmp_path, monkeypatch))


def test_retained_session_follows_real_compression_lineage(tmp_path):
    with closing(SessionDB(db_path=tmp_path / "state.db")) as db:
        db.create_session("before", source="whatsapp")
        db.end_session("before", end_reason="compression")
        db.create_session("after", source="whatsapp", parent_session_id="before")
        store = SimpleNamespace(_db=db, _ensure_loaded=lambda: None,
                                _entries={"key": SimpleNamespace(session_id="after")})
        assert continuation._same_retained_lineage(runner=SimpleNamespace(session_store=store),
            session_key="key", before_session_id="before", after_session_id="after")


async def _unknown_model_outcome_is_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTINUATION_TEST_TOKEN", "token")
    config_path = _config(tmp_path)
    config = continuation.load_management_continuation_config(
        config_path, inbox_db=tmp_path / "inbox.db", state_db=tmp_path / "state.db"
    )
    envelope = _envelope(tmp_path)
    inbox = SimpleNamespace(message_id_selection=lambda _: [_original_record()])

    async def unknown(*_args, **_kwargs):
        return {"provider_errors": ["provider disconnected"], "handled": [], "captured_outbound": []}
    monkeypatch.setattr(durable, "process_live_records", unknown)
    monkeypatch.setattr(continuation, "_retained_live_management_session", lambda **_: ("bound", "session"))

    with closing(SessionDB(db_path=tmp_path / "state.db")) as db:
        created = db.create_session_mailbox_message(
            agent_id="agent", from_session_name="owner", to_session_name="management",
            body=json.dumps(envelope), source_message_id="original",
        )
        row = db.get_session_mailbox_message(created["id"])
        assert db.claim_session_mailbox(created["id"])
        with pytest.raises(continuation.ManagementContinuationError, match="MODEL_OUTCOME_UNKNOWN"):
            await continuation.process_claimed_management_continuation(
                row=row, envelope=continuation._typed_envelope(row), session_db=db,
                inbox=inbox, config=config, config_path=config_path,
                state_db=tmp_path / "state.db", gate_changed_at="2026-01-01T00:00:00Z",
                runner=object(),
            )
        assert db.get_session_mailbox_message(created["id"])["status"] == "failed"
