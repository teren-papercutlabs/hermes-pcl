"""Continuation enqueue must not create duplicate turns or reset old ones."""
import pytest
from contextlib import closing

from hermes_state import SessionDB


def test_repeat_survives_delivery_and_database_reopen(tmp_path):
    path = tmp_path / "state.db"
    args = dict(agent_id="agent", from_session_name="owner", to_session_name="management",
                body="Continue retained list", source_message_id="original",
                idempotency_key="original:list:followup")
    with closing(SessionDB(db_path=path)) as db:
        first = db.create_session_mailbox_message(**args)
        assert db.create_session_mailbox_message(**args) == first
        assert db.claim_session_mailbox(first["id"])
        claimed = db.create_session_mailbox_message(**args)
        assert claimed["id"] == first["id"]
        assert claimed["status"] != "pending"
        db.complete_session_mailbox(first["id"], to_session_key="retained", to_session_id="resolved")
    with closing(SessionDB(db_path=path)) as db:
        repeated = db.create_session_mailbox_message(**args)
        assert repeated["id"] == first["id"]
        assert repeated["status"] == "delivered"
        assert db.list_pending_session_mailbox() == []
        with pytest.raises(ValueError, match="MAILBOX_IDEMPOTENCY_CONFLICT"):
            db.create_session_mailbox_message(**{**args, "body": "Different list"})
        assert db.get_session_mailbox_message(first["id"])["body"] == args["body"]


def test_unkeyed_messages_keep_existing_independent_behavior(tmp_path):
    with closing(SessionDB(db_path=tmp_path / "state.db")) as db:
        args = dict(agent_id="agent", from_session_name="a", to_session_name="b", body="hello")
        assert db.create_session_mailbox_message(**args)["id"] != db.create_session_mailbox_message(**args)["id"]
