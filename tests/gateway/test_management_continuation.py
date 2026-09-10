"""Enqueue boundary only; model/session/delivery integration is separate."""
import json
from contextlib import closing
from types import SimpleNamespace

import pytest

from gateway.management_continuation import enqueue_continuation
from hermes_state import SessionDB


def test_authenticated_retained_list_enqueues_once(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_CONTINUATION_TOKEN", "isolated-test-token")
    request = {"original_message_id": "original", "management_request_id": "request",
               "list_id": "case-list-20260910000000-abcdef1234"}
    directory = tmp_path / "lists" / request["list_id"]
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps({
        "contract": "tgg-per-case-whatsapp-list/v1", "list_id": request["list_id"],
        "management_request_id": "request", "items": [{"input_index": 1, "job_no": "SK/JOB/2609/156"}],
    }))
    config = {"enabled": True, "token_env": "TEST_CONTINUATION_TOKEN", "agent_id": "agent",
              "from_peer": "owner", "to_peer": "management", "management_chat_id": "management",
              "coordinator_receipt_root": str(tmp_path)}
    # Inbox selection is the boundary's dependency; no capture is fabricated.
    inbox = SimpleNamespace(message_id_selection=lambda ids: [SimpleNamespace(chat_id="management")])
    with closing(SessionDB(db_path=tmp_path / "state.db")) as db:
        kwargs = dict(config=config, request=request, credential="isolated-test-token",
                      inbox=inbox, session_db=db)
        first = enqueue_continuation(**kwargs)
        assert enqueue_continuation(**kwargs) == first
        envelope = json.loads(db.get_session_mailbox_message(first["id"])["body"])
        assert envelope["jobs"] == ["SK/JOB/2609/156"]
        assert envelope["original_message_id"] == "original"
        with pytest.raises(ValueError, match="CONTINUATION_LIST_BINDING_INVALID"):
            enqueue_continuation(**{**kwargs, "request": {**request, "management_request_id": "other"}})
        assert len(db.list_pending_session_mailbox()) == 1


def test_unauthorized_request_cannot_read_retained_state():
    with pytest.raises(ValueError, match="CONTINUATION_UNAUTHORIZED"):
        enqueue_continuation(config={}, request={}, credential="wrong", inbox=None, session_db=None)


def test_failed_terminal_is_not_a_complete_answer(monkeypatch):
    from gateway.management_continuation import require_complete_list
    from tools.registry import registry
    envelope = {"list_id": "list", "management_request_id": "request", "jobs": ["SK/JOB/2609/156"]}
    state = {**envelope, "complete": True, "items": [{"job_no": envelope["jobs"][0],
             "status": "failed", "result": {"error": "submission failed"}}]}
    monkeypatch.setattr(registry, "get_entry", lambda _: SimpleNamespace(toolset="tgg-per-case-whatsapp-coordinator"))
    monkeypatch.setattr(registry, "dispatch", lambda *_: json.dumps({"ok": True, "list": state}))
    with pytest.raises(ValueError, match="CONTINUATION_LIST_NOT_SUBSTANTIVELY_COMPLETE"):
        require_complete_list(envelope)
    state["items"][0].update(status="completed", result={"disposition": "no_evidence", "reason": "No matching source"})
    assert require_complete_list(envelope)["complete"] is True


def test_followup_uses_existing_delivery_ledger_without_reusing_original_claim(tmp_path, monkeypatch):
    from gateway import durable_jsonl_consumer as consumer
    from urllib import request as http
    inbox = consumer.DurableInbox(tmp_path / "inbox.db")
    chat = "management@g.us"
    original = "original"
    record = consumer.InboxRecord(seq=1, message_id=original, chat_id=chat,
        start_offset=0, end_offset=1, raw={"messageId": original, "chatId": chat,
        "senderId": "client", "body": "Investigate these cases", "timestamp": 1789000000})
    monkeypatch.setattr(consumer, "_management_selector_chats", lambda _: {chat})
    sent = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self): return b'{"success":true,"messageId":"provider-confirmed"}'
    def send(request, timeout):
        sent.append(json.loads(request.data))
        return Response()
    monkeypatch.setattr(http, "urlopen", send)
    args = dict(inbox=inbox, config_path=tmp_path / "config.yaml",
        captured_outbound=[{"kind": "send", "args": [chat, "The requested results"], "kwargs": {}}],
        batch_records=[record], gate_changed_at="2026-01-01T00:00:00Z",
        handled_groups=[{"message_ids": [original], "turn_id": "completed-turn"}])
    assert consumer.deliver_management_replies(**args)["delivered"] == 1
    continuation = {"list_id": "case-list-20260910000000-abcdef1234", "original_message_id": original}
    assert consumer.deliver_management_replies(**args, continuation=continuation)["delivered"] == 1
    assert consumer.deliver_management_replies(**args, continuation=continuation)["duplicate"] == 1
    assert consumer.deliver_management_replies(**args)["duplicate"] == 1
    assert len(sent) == 2
    assert sent[1]["replyTo"] == {"messageId": original, "participant": "client", "body": "Investigate these cases"}
