from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from gateway.pa_message_store import (
    MessageConflictError,
    MessageStore,
    backfill_jsonl,
    describe_pending_images,
)
from gateway.durable_jsonl_consumer import DurableInbox, initialize_cursor


def event(
    message_id: str,
    *,
    text: str,
    timestamp: int,
    chat: str = "ops@g.us",
    media: list[object] | None = None,
    history: bool = False,
) -> dict[str, object]:
    return {
        "messageId": message_id,
        "chatId": chat,
        "chatName": "Operations",
        "senderId": "worker@example",
        "fromMe": False,
        "timestamp": timestamp,
        "body": text,
        "hasMedia": bool(media),
        "mediaType": "image" if media else "",
        "mediaUrls": media or [],
        "historySync": history,
    }


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_same_writer_dedupes_and_capture_wins(tmp_path: Path) -> None:
    store = MessageStore(tmp_path / "tenant.db")
    store.initialize()
    history = event(
        "m-1",
        text="history body",
        timestamp=100,
        history=True,
    )
    capture = event("m-1", text="capture body exact", timestamp=101)

    assert store.record_message(history, source="history-sync").action == "created"
    assert store.record_message(history, source="history-sync").action == "repaired"
    result = store.record_message(capture, source="capture")
    assert result.action == "repaired"
    assert result.before_image is not None

    with store.connect(read_only=True) as conn:
        row = conn.execute("SELECT * FROM message_ledger").fetchone()
        assert row["text"] == "capture body exact"
        assert row["ts"] == 101
        assert row["source"] == "capture"
        assert row["source_key"] == "history-sync:m-1"
        assert json.loads(row["sources"]) == ["capture", "history-sync"]
        assert conn.execute("SELECT COUNT(*) FROM message_ledger").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM pa_message_aliases").fetchone()[0] == 3

    # Lower-priority history cannot overwrite captured facts.
    store.record_message(
        event("m-1", text="stale history", timestamp=99, history=True),
        source="history-sync",
    )
    store.record_message(
        event("m-1", text="", timestamp=102),
        source="capture",
    )
    assert store.search("capture body")[0]["message_id"] == "m-1"


def test_conflicting_chat_is_held_not_guessed(tmp_path: Path) -> None:
    store = MessageStore(tmp_path / "tenant.db")
    store.initialize()
    store.record_message(event("m-1", text="one", timestamp=100), source="capture")
    with pytest.raises(MessageConflictError, match="crosses chats"):
        store.record_message(
            event("m-1", text="two", timestamp=100, chat="other@g.us"),
            source="capture",
        )


@pytest.mark.asyncio
async def test_photo_description_is_stored_once_and_linked_to_search(
    tmp_path: Path,
) -> None:
    image = tmp_path / "damage.jpg"
    image.write_bytes(b"\xff\xd8\xff" + b"fixture")
    store = MessageStore(tmp_path / "tenant.db")
    store.initialize()
    store.record_message(
        event(
            "photo-1",
            text="[image received]",
            timestamp=200,
            media=[str(image)],
        ),
        source="capture",
    )
    calls: list[str] = []

    async def descriptor(path: str) -> str:
        calls.append(path)
        return "Cracked ceiling panel with exposed wiring."

    first = await describe_pending_images(
        store, descriptor, eager_only=True, limit=10
    )
    second = await describe_pending_images(
        store, descriptor, eager_only=True, limit=10
    )
    assert first == {
        "selected": 1,
        "described": 1,
        "deferred": 0,
        "errors": [],
    }
    assert second["selected"] == 0
    assert calls == [str(image)]
    rows = store.search("cracked wiring")
    assert [row["message_id"] for row in rows] == ["photo-1"]
    assert rows[0]["description"] == "Cracked ceiling panel with exposed wiring."
    assert "media_refs" not in rows[0]
    assert "raw_json" not in rows[0]


def test_bm25_relevance_and_context_filters(tmp_path: Path) -> None:
    store = MessageStore(tmp_path / "tenant.db")
    store.initialize()
    store.record_message(
        event(
            "m-1",
            text="urgent pump leak pump leak at riser",
            timestamp=100,
        ),
        source="capture",
    )
    store.record_message(
        event("m-2", text="pump inspection complete", timestamp=101),
        source="capture",
    )
    store.record_message(
        event("m-3", text="paint touch up complete", timestamp=102),
        source="capture",
    )
    results = store.search("pump leak", chat="ops@g.us")
    assert [row["message_id"] for row in results] == ["m-1"]
    assert results[0]["score"] < 0
    context = store.context("m-2", window=1)
    assert [row["message_id"] for row in context] == ["m-1", "m-2", "m-3"]


def test_two_feed_backfill_cli_is_resumable_and_capture_wins(tmp_path: Path) -> None:
    db = tmp_path / "tenant.db"
    MessageStore(db).initialize()
    capture = tmp_path / "events.jsonl"
    history = tmp_path / "history-sync.jsonl"
    write_jsonl(
        capture,
        [event("overlap", text="capture truth", timestamp=201)],
    )
    write_jsonl(
        history,
        [
            event(
                "overlap",
                text="history copy",
                timestamp=200,
                history=True,
            ),
            event(
                "deep",
                text="older archived pump note",
                timestamp=20,
                history=True,
            ),
        ],
    )
    run = tmp_path / "run"
    command = [
        sys.executable,
        "scripts/pa_message_store.py",
        "backfill",
        "--db",
        str(db),
        "--capture-jsonl",
        str(capture),
        "--history-jsonl",
        str(history),
        "--snapshot",
        str(run / "snapshot.db"),
        "--before-images",
        str(run / "before.jsonl"),
        "--held-conflicts",
        str(run / "held.jsonl"),
        "--report",
        str(run / "report.json"),
    ]
    completed = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[2],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["ok"] is True
    assert report["feeds"]["capture"]["created"] == 1
    assert report["feeds"]["history_sync"]["folded"] == 1
    assert report["feeds"]["history_sync"]["created"] == 1
    assert report["verification"]["rows"] == 2
    assert (run / "snapshot.db").is_file()
    assert MessageStore(db).search("capture truth")[0]["message_id"] == "overlap"
    assert MessageStore(db).search("older archived")[0]["message_id"] == "deep"


def test_initialize_migrates_existing_table_without_losing_rows(tmp_path: Path) -> None:
    db = tmp_path / "existing.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE message_ledger(
              message_id TEXT PRIMARY KEY,source_key TEXT UNIQUE,source TEXT,
              source_ref TEXT,chat_jid TEXT,chat_name TEXT,job_type TEXT,zone TEXT,
              channel_type TEXT,sender_id TEXT,from_me INTEGER,ts INTEGER,sgt TEXT,
              text TEXT,message_kind TEXT,has_media INTEGER,media_refs TEXT,
              quoted_text TEXT,reply_to_source_ref TEXT,raw_json TEXT,turn_id TEXT,
              turn_ids_json TEXT,processed_at REAL,sources TEXT,source_flags TEXT,
              source_discrepancy TEXT,in_scope INTEGER,ledger_updated_at INTEGER
            )
            """
        )
        conn.execute(
            """
            INSERT INTO message_ledger VALUES(
              'legacy','legacy-key','legacy','legacy','chat','Chat',NULL,'',
              'whatsapp',NULL,0,1,'','kept text','text',0,'[]','',NULL,'{}',
              NULL,'[]',NULL,'["legacy"]','{}',NULL,1,1
            )
            """
        )
    store = MessageStore(db)
    store.initialize()
    assert store.search("kept text")[0]["message_id"] == "legacy"
    with store.connect(read_only=True) as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(message_ledger)")
        }
        assert {"description", "source_keys_json"} <= columns


def test_capture_admission_calls_canonical_writer_before_cursor_advance(
    tmp_path: Path,
) -> None:
    source = tmp_path / "events.jsonl"
    cursor = tmp_path / "cursor.json"
    write_jsonl(
        source,
        [event("admitted", text="written at admission", timestamp=300)],
    )
    initialize_cursor(source, cursor, position="start")
    store = MessageStore(tmp_path / "tenant.db")
    store.initialize()
    inbox = DurableInbox(tmp_path / "inbox.db")

    assert (
        inbox.stage_from_source(
            source,
            cursor,
            max_records=10,
            message_store=store,
        )
        == 1
    )
    assert store.search("written admission")[0]["message_id"] == "admitted"
    assert inbox.total() == 1


def test_bad_live_record_is_held_and_does_not_block_later_admission(
    tmp_path: Path,
) -> None:
    source = tmp_path / "events.jsonl"
    cursor = tmp_path / "cursor.json"
    bad = event("bad-time", text="bad", timestamp=300)
    bad["timestamp"] = "not-a-time"
    write_jsonl(
        source,
        [bad, event("good-after", text="continues safely", timestamp=301)],
    )
    initialize_cursor(source, cursor, position="start")
    store = MessageStore(tmp_path / "tenant.db")
    store.initialize()
    store.assert_ready()
    inbox = DurableInbox(tmp_path / "inbox.db")
    admitted: list[str] = []

    assert inbox.stage_from_source(
        source,
        cursor,
        max_records=10,
        message_store=store,
        admitted_message_ids=admitted,
    ) == 2
    assert admitted == ["good-after"]
    assert store.search("continues safely")[0]["message_id"] == "good-after"
    with store.connect(read_only=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pa_message_holds").fetchone()[0] == 1
    with inbox.connect() as conn:
        statuses = {
            row["message_id"]: row["status"]
            for row in conn.execute(
                "SELECT message_id,status FROM ingress_events ORDER BY seq"
            )
        }
    assert statuses == {"bad-time": "skipped", "good-after": "pending"}
