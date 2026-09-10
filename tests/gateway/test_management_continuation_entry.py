"""Exercise the actual stdin entry against isolated retained stores."""
import json
import os
import subprocess
import sys
from contextlib import closing

import yaml

from gateway.durable_jsonl_consumer import DurableInbox, initialize_cursor
from hermes_state import SessionDB


def test_real_entry_reads_retained_inbox_and_queues_once(tmp_path):
    source = tmp_path / "fixture.jsonl"
    source.write_text(json.dumps({"messageId": "original", "chatId": "management@g.us",
        "senderId": "fixture-user", "body": "Investigate SK/JOB/2609/156",
        "isGroup": True, "timestamp": 1789000000}) + "\n")
    cursor = tmp_path / "cursor.json"
    initialize_cursor(source, cursor, position="start")
    inbox_path = tmp_path / "inbox.db"
    inbox = DurableInbox(inbox_path)
    assert inbox.stage_from_source(source, cursor) == 1
    state_path = tmp_path / "state.db"
    with closing(SessionDB(db_path=state_path)):
        pass
    list_id = "case-list-20260910000000-abcdef1234"
    directory = tmp_path / "lists" / list_id
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps({
        "contract": "tgg-per-case-whatsapp-list/v1", "list_id": list_id,
        "management_request_id": "request", "items": [{"input_index": 1, "job_no": "SK/JOB/2609/156"}],
    }))
    config = {"enabled": True, "token_env": "TEST_CONTINUATION_TOKEN", "agent_id": "agent",
        "from_peer": "owner", "to_peer": "management", "management_chat_id": "management@g.us",
        "coordinator_receipt_root": str(tmp_path), "inbox_db": str(inbox_path), "state_db": str(state_path)}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"pa": {"management_continuation": config}}))
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "TEST_CONTINUATION_TOKEN": "fixture-secret"}
    packet = {"credential": "fixture-secret", "request": {"original_message_id": "original",
        "management_request_id": "request", "list_id": list_id}}
    def invoke(value):
        return subprocess.run([sys.executable, "-m", "gateway.management_continuation_entry"],
            input=json.dumps(value), text=True, capture_output=True, env=env, timeout=20)
    first = invoke(packet)
    assert first.returncode == 0, first.stdout + first.stderr
    repeated = invoke(packet)
    assert repeated.returncode == 0
    assert json.loads(first.stdout) == json.loads(repeated.stdout)
    refused = invoke({**packet, "credential": "wrong"})
    assert refused.returncode == 1
    assert json.loads(refused.stdout)["error"] == "CONTINUATION_UNAUTHORIZED"
    assert "fixture-secret" not in first.stdout + first.stderr + refused.stdout + refused.stderr
    with closing(SessionDB(db_path=state_path)) as db:
        assert len(db.list_pending_session_mailbox()) == 1
    assert len(inbox.message_id_selection(["original"])) == 1
