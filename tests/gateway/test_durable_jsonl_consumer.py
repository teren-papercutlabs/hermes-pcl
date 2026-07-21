import argparse
import asyncio
import json
import sqlite3
from types import SimpleNamespace
from pathlib import Path

import pytest
import yaml

from gateway import durable_jsonl_consumer as consumer


def _message(message_id: str, chat_id: str = "test-group@g.us") -> dict:
    return {
        "messageId": message_id,
        "chatId": chat_id,
        "senderId": "fixture-user",
        "senderName": "Fixture User",
        "chatName": "Fixture Chat",
        "isGroup": True,
        "body": "fixture message",
        "hasMedia": False,
        "mediaType": None,
        "mediaUrls": [],
        "timestamp": 100,
        "fromMe": False,
    }


def _write_jsonl(path: Path, values: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(value) + "\n" for value in values),
        encoding="utf-8",
    )


def test_stage_is_durable_before_cursor_and_idempotent(tmp_path):
    source = tmp_path / "events.jsonl"
    cursor_path = tmp_path / "cursor.json"
    inbox = consumer.DurableInbox(tmp_path / "inbox.db")
    values = [_message("m1"), _message("m2")]
    _write_jsonl(source, values)
    first_line_end = len((json.dumps(values[0]) + "\n").encode())

    consumer.initialize_cursor(source, cursor_path, position="start")
    assert inbox.stage_from_source(source, cursor_path, max_records=1) == 1
    assert consumer.SourceCursor.from_path(cursor_path).offset == first_line_end
    assert inbox.counts() == {"pending": 1}

    assert inbox.stage_from_source(source, cursor_path, max_records=10) == 1
    assert inbox.counts() == {"pending": 2}

    # Simulate a crash after the DB commit but before cursor replacement by
    # rewinding the cursor. The unique message id absorbs the re-read.
    raw = json.loads(cursor_path.read_text())
    raw["offset"] = 0
    consumer._atomic_write_json(cursor_path, raw)
    assert inbox.stage_from_source(source, cursor_path, max_records=10) == 2
    assert inbox.counts() == {"pending": 2}
    assert consumer.SourceCursor.from_path(cursor_path).offset == source.stat().st_size


def test_partial_line_never_advances_cursor(tmp_path):
    source = tmp_path / "events.jsonl"
    cursor_path = tmp_path / "cursor.json"
    inbox = consumer.DurableInbox(tmp_path / "inbox.db")
    source.write_text(json.dumps(_message("partial")), encoding="utf-8")
    consumer.initialize_cursor(source, cursor_path, position="start")

    assert inbox.stage_from_source(source, cursor_path) == 0
    assert consumer.SourceCursor.from_path(cursor_path).offset == 0
    with source.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert inbox.stage_from_source(source, cursor_path) == 1


def test_source_rotation_and_truncation_fail_closed(tmp_path):
    source = tmp_path / "events.jsonl"
    cursor_path = tmp_path / "cursor.json"
    inbox = consumer.DurableInbox(tmp_path / "inbox.db")
    _write_jsonl(source, [_message("m1")])
    consumer.initialize_cursor(source, cursor_path, position="end")

    replacement = tmp_path / "replacement.jsonl"
    _write_jsonl(replacement, [_message("m2")])
    replacement.replace(source)
    with pytest.raises(consumer.ConsumerError, match="inode changed"):
        inbox.stage_from_source(source, cursor_path)


def test_singleton_guard_rejects_second_holder(tmp_path):
    lock_path = tmp_path / "consumer.lock"
    with consumer.SingletonLock(lock_path):
        with pytest.raises(consumer.ConsumerError, match="singleton"):
            with consumer.SingletonLock(lock_path):
                pass


def test_disabled_once_does_not_require_or_open_source(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("pa:\n  enabled: false\n", encoding="utf-8")
    gate = tmp_path / "processing-gate.json"
    gate.write_text(
        json.dumps({"version": 1, "enabled": False, "generation": 0}),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        config=str(config),
        source=str(tmp_path / "does-not-exist.jsonl"),
        cursor=str(tmp_path / "does-not-exist.cursor"),
        inbox=str(tmp_path / "inbox.db"),
        status_file=str(tmp_path / "status.json"),
        lock_file=str(tmp_path / "consumer.lock"),
        state_db=str(tmp_path / "state.db"),
        processing_gate=str(gate),
        once=True,
        poll_seconds=0.01,
        max_records=10,
    )

    assert asyncio.run(consumer.run_consumer(args)) == 0
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["state"] == "standby"
    assert status["processing_enabled"] is False
    assert status["config_enabled"] is False
    assert status["gate_enabled"] is False
    assert status["source_opened"] is False
    assert status["cursor_advanced"] is False
    assert not Path(args.cursor).exists()


def test_root_gate_blocks_accidentally_enabled_config_without_opening_source(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("pa:\n  enabled: true\n", encoding="utf-8")
    gate = tmp_path / "processing-gate.json"
    gate.write_text(
        json.dumps({"version": 1, "enabled": False, "generation": 0}),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        config=str(config),
        source=str(tmp_path / "does-not-exist.jsonl"),
        cursor=str(tmp_path / "does-not-exist.cursor"),
        inbox=str(tmp_path / "inbox.db"),
        status_file=str(tmp_path / "status.json"),
        lock_file=str(tmp_path / "consumer.lock"),
        state_db=str(tmp_path / "state.db"),
        processing_gate=str(gate),
        once=True,
        poll_seconds=0.01,
        max_records=10,
    )

    assert asyncio.run(consumer.run_consumer(args)) == 0
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["processing_enabled"] is False
    assert status["config_enabled"] is True
    assert status["gate_enabled"] is False
    assert status["source_opened"] is False
    assert status["cursor_advanced"] is False
    assert not Path(args.cursor).exists()


def test_fixture_mode_uses_inbox_path_and_marks_completed(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump({
            "model": {
                "provider": "openai-direct-primary",
                "default": "gpt-5.4-mini",
            },
            "pa": {"enabled": True},
        }),
        encoding="utf-8",
    )
    source = tmp_path / "fixture.jsonl"
    _write_jsonl(source, [_message("fixture-1")])

    async def fake_process(records, **kwargs):
        assert [record.message_id for record in records] == ["fixture-1"]
        return {
            "turn_id": "pa-turn-fixture",
            "provider": "openai-direct-primary",
            "model": "gpt-5.4-mini",
            "processed": 1,
            "outbound_captured": 1,
            "blocked_commands": 0,
        }

    monkeypatch.setattr(consumer, "process_replay_records", fake_process)
    args = argparse.Namespace(
        test_root=str(tmp_path),
        source=str(source),
        cursor=str(tmp_path / "cursor.json"),
        inbox=str(tmp_path / "inbox.db"),
        config=str(config),
        state_db=str(tmp_path / "state.db"),
        report=str(tmp_path / "report.json"),
        run_id="fixture-run",
        max_records=10,
    )
    assert asyncio.run(consumer.run_fixture(args)) == 0
    assert consumer.DurableInbox(tmp_path / "inbox.db").counts() == {"completed": 1}
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["ok"] is True
    assert report["result"]["turn_id"] == "pa-turn-fixture"


def test_fixture_mode_rejects_path_outside_test_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    config = root / "config.yaml"
    config.write_text("pa:\n  enabled: true\n", encoding="utf-8")
    outside = tmp_path / "outside.jsonl"
    _write_jsonl(outside, [_message("m1")])
    args = argparse.Namespace(
        test_root=str(root),
        source=str(outside),
        cursor=str(root / "cursor.json"),
        inbox=str(root / "inbox.db"),
        config=str(config),
        state_db=str(root / "state.db"),
        report=str(root / "report.json"),
        run_id="fixture-run",
        max_records=10,
    )
    with pytest.raises(consumer.ConsumerError, match="escapes test root"):
        asyncio.run(consumer.run_fixture(args))


def test_bridge_wrapper_is_normalized_without_client_content_in_metadata():
    item = _message("wrapped")
    assert consumer._bridge_item({"event": item}) == item
    with pytest.raises(consumer.ConsumerError, match="messageId/chatId"):
        consumer._bridge_item({"event": {"type": "connection"}})


def _enabled_consumer_args(tmp_path: Path, messages: list[dict]) -> argparse.Namespace:
    constitution = tmp_path / "constitution.yaml"
    constitution.write_text(
        yaml.safe_dump({
            "selectors": [{
                "job_type": "tgg_management",
                "match": {
                    "source.platform": "whatsapp",
                    "source.chat_id": "management@g.us",
                },
            }],
        }),
        encoding="utf-8",
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump({
            "model": {"provider": "openai-direct-primary", "default": "fixture"},
            "pa": {"enabled": True, "constitution_path": str(constitution)},
        }),
        encoding="utf-8",
    )
    source = tmp_path / "events.jsonl"
    _write_jsonl(source, messages)
    cursor = tmp_path / "cursor.json"
    consumer.initialize_cursor(source, cursor, position="start")
    gate = tmp_path / "processing-gate.json"
    gate.write_text(json.dumps({
        "version": 1,
        "enabled": True,
        "generation": 3,
        "changed_at": "2026-07-21T00:00:00+00:00",
    }), encoding="utf-8")
    return argparse.Namespace(
        config=str(config), source=str(source), cursor=str(cursor),
        inbox=str(tmp_path / "inbox.db"), status_file=str(tmp_path / "status.json"),
        lock_file=str(tmp_path / "consumer.lock"), state_db=str(tmp_path / "state.db"),
        processing_gate=str(gate), once=True, poll_seconds=0.001, max_records=100,
        site_concurrency=4, chat_batch_size=25,
    )


def test_pending_chat_batches_are_fifo_and_split_management_capacity(tmp_path):
    inbox = consumer.DurableInbox(tmp_path / "inbox.db")
    source = tmp_path / "events.jsonl"
    values = [
        _message("site-a-1", "site-a@g.us"),
        _message("mgmt-1", "management@g.us"),
        _message("site-a-2", "site-a@g.us"),
        _message("site-b-1", "site-b@g.us"),
    ]
    _write_jsonl(source, values)
    cursor = tmp_path / "cursor.json"
    consumer.initialize_cursor(source, cursor, position="start")
    assert inbox.stage_from_source(source, cursor, max_records=10) == 4

    management, site = inbox.pending_chat_batches(
        batch_size=2, priority_chats={"management@g.us"}
    )
    assert [(chat, [r.message_id for r in rows]) for chat, rows in management] == [
        ("management@g.us", ["mgmt-1"])
    ]
    assert [(chat, [r.message_id for r in rows]) for chat, rows in site] == [
        ("site-a@g.us", ["site-a-1", "site-a-2"]),
        ("site-b@g.us", ["site-b-1"]),
    ]


def test_startup_reconciles_successful_turn_refs_and_requeues_only_unmatched(tmp_path):
    inbox = consumer.DurableInbox(tmp_path / "inbox.db")
    source = tmp_path / "events.jsonl"
    _write_jsonl(source, [_message("done-ref"), _message("retry-ref")])
    cursor = tmp_path / "cursor.json"
    consumer.initialize_cursor(source, cursor, position="start")
    inbox.stage_from_source(source, cursor, max_records=10)
    records = inbox.pending(limit=10)
    inbox.claim(records)

    state_db = tmp_path / "state.db"
    conn = sqlite3.connect(state_db)
    conn.execute(
        "CREATE TABLE pa_turns(turn_id TEXT, message_refs_json TEXT, "
        "turn_status TEXT, error_json TEXT, completed_at REAL)"
    )
    conn.execute(
        "INSERT INTO pa_turns VALUES(?,?,?,?,?)",
        ("turn-done", json.dumps(["done-ref"]), "completed", None, 1.0),
    )
    conn.commit()
    conn.close()

    before = inbox.total()
    assert inbox.reconcile_orphan_processing(state_db) == {
        "completed": 1, "requeued": 1, "total": 2,
    }
    assert inbox.total() == before
    with inbox.connect() as conn:
        rows = {
            row["message_id"]: (row["status"], row["pa_turn_id"])
            for row in conn.execute(
                "SELECT message_id,status,pa_turn_id FROM ingress_events"
            )
        }
    assert rows == {
        "done-ref": ("completed", "turn-done"),
        "retry-ref": ("pending", None),
    }


def test_conservation_high_water_hard_aborts_on_row_deletion(tmp_path):
    inbox = consumer.DurableInbox(tmp_path / "inbox.db")
    source = tmp_path / "events.jsonl"
    _write_jsonl(source, [_message("keep-1"), _message("keep-2")])
    cursor = tmp_path / "cursor.json"
    consumer.initialize_cursor(source, cursor, position="start")
    inbox.stage_from_source(source, cursor, max_records=10)
    assert inbox.assert_and_record_conservation() == 2
    with inbox.connect() as conn:
        conn.execute("DELETE FROM ingress_events WHERE message_id='keep-2'")
    with pytest.raises(consumer.ConsumerError, match="conservation hard-abort"):
        inbox.assert_and_record_conservation()


@pytest.mark.asyncio
async def test_seq_3030_management_lane_runs_during_999_site_shape(
    tmp_path, monkeypatch
):
    # Match the incident's seq exactly without creating unrelated fixture rows.
    messages = [
        _message(f"site-a-{index}", "site-a@g.us")
        if index % 2 == 0 else _message(f"site-b-{index}", "site-b@g.us")
        for index in range(999)
    ]
    messages.append(_message("management-seq-3030", "management@g.us"))
    for message in messages:
        message["timestamp"] = 1784630163.917
    args = _enabled_consumer_args(tmp_path, messages)
    seeded = consumer.DurableInbox(Path(args.inbox))
    with seeded.connect() as conn:
        conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('ingress_events',2030)")

    order: list[str] = []
    site_a_started = asyncio.Event()
    site_b_started = asyncio.Event()
    management_done = asyncio.Event()

    async def fake_process(records, **kwargs):
        chat = records[0].chat_id
        if chat == "site-a@g.us":
            site_a_started.set()
            await asyncio.wait_for(management_done.wait(), timeout=2)
            order.append("slow-site-complete")
        elif chat == "site-b@g.us":
            site_b_started.set()
            order.append("other-site-complete")
        else:
            await asyncio.wait_for(site_a_started.wait(), timeout=2)
            await asyncio.wait_for(site_b_started.wait(), timeout=2)
            order.append("management-complete")
            management_done.set()
        return {
            "processed": len(records),
            "submitted_message_ids": [r.message_id for r in records],
            "handled": [{
                "message_ids": [r.message_id for r in records],
                "turn_id": f"turn-{chat}",
            }],
            "captured_outbound": [],
        }

    monkeypatch.setattr(consumer, "process_live_records", fake_process)
    monkeypatch.setattr(consumer, "_new_gateway_runner", lambda: object())
    monkeypatch.delenv("TGG_DEMO_MANAGEMENT_ONLY", raising=False)

    assert await consumer.run_consumer(args) == 0
    assert order.index("management-complete") < order.index("slow-site-complete")
    inbox = consumer.DurableInbox(Path(args.inbox))
    assert inbox.total() == 1000
    with inbox.connect() as conn:
        management = conn.execute(
            "SELECT seq,status,pa_turn_id FROM ingress_events WHERE message_id=?",
            ("management-seq-3030",),
        ).fetchone()
    assert management["seq"] == 3030
    assert management["status"] == "completed"
    assert inbox.counts() == {"completed": 51, "pending": 949}
    status = json.loads(Path(args.status_file).read_text())
    assert status["scheduler_mode"] == "per-chat-parallel"
    assert status["site_concurrency"] == 4
    assert status["state_total"] == 1000


@pytest.mark.asyncio
async def test_cancelling_active_chat_requeues_without_double_processing(
    tmp_path, monkeypatch
):
    inbox = consumer.DurableInbox(tmp_path / "inbox.db")
    source = tmp_path / "events.jsonl"
    _write_jsonl(source, [_message("interrupt-me", "site@g.us")])
    cursor = tmp_path / "cursor.json"
    consumer.initialize_cursor(source, cursor, position="start")
    inbox.stage_from_source(source, cursor)
    records = inbox.pending(limit=1)
    entered = asyncio.Event()

    async def never_finishes(records, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(consumer, "process_live_records", never_finishes)
    config = tmp_path / "config.yaml"
    config.write_text("pa:\n  enabled: true\n", encoding="utf-8")
    task = asyncio.create_task(consumer._process_claimed_chat_batch(
        inbox, records, config_path=config, state_db=tmp_path / "state.db",
        gate_changed_at="2026-07-21T00:00:00+00:00", runner=object(),
    ))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert inbox.counts() == {"pending": 1}
    assert inbox.pending(limit=1)[0].message_id == "interrupt-me"


@pytest.mark.asyncio
async def test_management_reuses_stable_persistent_chat_namespace(tmp_path, monkeypatch):
    args = _enabled_consumer_args(tmp_path, [])
    plans = []

    class Runner:
        async def replay(self, plan):
            plans.append(plan)
            return SimpleNamespace(processed=1, outbound=[])

    monkeypatch.setenv("TGG_PERSISTENT_CHAT_SESSION_SCOPE", "management")
    record = consumer.InboxRecord(
        seq=1, message_id="management-1", chat_id="management@g.us",
        start_offset=0, end_offset=1, raw=_message("management-1", "management@g.us"),
    )
    await consumer.process_live_records(
        [record], config_path=Path(args.config), state_db=Path(args.state_db), runner=Runner()
    )
    record_2 = consumer.InboxRecord(
        seq=2, message_id="management-2", chat_id="management@g.us",
        start_offset=1, end_offset=2, raw=_message("management-2", "management@g.us"),
    )
    await consumer.process_live_records(
        [record_2], config_path=Path(args.config), state_db=Path(args.state_db), runner=Runner()
    )
    assert [plan.replay_namespace for plan in plans] == [
        "agent:live-drain:persistent-chat",
        "agent:live-drain:persistent-chat",
    ]
    assert all({message["chatId"] for message in plan.messages} == {"management@g.us"} for plan in plans)
