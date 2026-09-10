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
