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


def _png_bytes(payload: bytes = b"fixture") -> bytes:
    return b"\x89PNG\r\n\x1a\n" + payload


def _retention_config(tmp_path: Path, source_root: Path, media_root: Path) -> Path:
    path = tmp_path / "retention-config.yaml"
    path.write_text(yaml.safe_dump({
        "pa": {
            "enabled": True,
            "media_retention": {
                "enabled": True,
                "media_root": str(media_root),
                "media_ref_prefix": "/media/tgg/hermes",
                "source_roots": [str(source_root)],
                "operation": "tgg_media_retention",
            },
        },
    }), encoding="utf-8")
    return path


def test_media_retention_is_atomic_idempotent_and_provenance_bound(tmp_path, monkeypatch):
    source_root = tmp_path / "capture"
    source_root.mkdir()
    source = source_root / "photo.png"
    source.write_bytes(_png_bytes())
    media_root = tmp_path / "systems-media"
    config = _retention_config(tmp_path, source_root, media_root)
    calls: list[dict] = []
    monkeypatch.setattr(
        consumer, "_converge_retained_media",
        lambda config_path, **kwargs: calls.append(kwargs["payload"])
        or {
            "ok": True,
            "data": {"ledgerChanged": False, "observationsChanged": False},
        },
    )
    raw = _message("M-IMAGE", "management@g.us")
    raw.update({"hasMedia": True, "mediaType": "image/png", "mediaUrls": [str(source)]})
    record = consumer.InboxRecord(1, "M-IMAGE", "management@g.us", 0, 1, raw)

    first = consumer.retain_record_media(record, config_path=config)
    second = consumer.retain_record_media(record, config_path=config)
    assert first == second == {"retained": 1, "bytes": len(_png_bytes()), "operation": True}
    files = list(media_root.glob("*.png"))
    assert len(files) == 1
    assert files[0].read_bytes() == _png_bytes()
    assert calls[0] == calls[1]
    assert calls[0]["message_id"] == "M-IMAGE"
    assert calls[0]["media"][0]["media_ordinal"] == 0
    assert calls[0]["media"][0]["ref"] == f"/media/tgg/hermes/{files[0].name}"

    files[0].write_bytes(_png_bytes(b"changed"))
    with pytest.raises(consumer.MediaRetentionError, match="PROVENANCE_DIVERGENCE"):
        consumer.retain_record_media(record, config_path=config)


def test_media_retention_refuses_source_path_escape(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png_bytes())
    config = _retention_config(tmp_path, allowed, tmp_path / "retained")
    monkeypatch.setattr(consumer, "_converge_retained_media", lambda *a, **k: {})
    raw = _message("ESCAPE")
    raw.update(
        {"hasMedia": True, "mediaType": "image", "mediaUrls": [str(outside)]}
    )
    record = consumer.InboxRecord(1, "ESCAPE", "test-group@g.us", 0, 1, raw)
    with pytest.raises(consumer.MediaRetentionError, match="escapes configured roots"):
        consumer.retain_record_media(record, config_path=config)


def test_media_retention_normalizes_source_read_oserror(tmp_path, monkeypatch):
    capture = tmp_path / "capture"
    capture.mkdir()
    source = capture / "photo.png"
    source.write_bytes(_png_bytes())
    config = _retention_config(tmp_path, capture, tmp_path / "retained")
    raw = _message("SOURCE-RACE")
    raw.update(
        {"hasMedia": True, "mediaType": "image", "mediaUrls": [str(source)]}
    )
    record = consumer.InboxRecord(
        1, "SOURCE-RACE", "test-group@g.us", 0, 1, raw
    )
    original_read_bytes = Path.read_bytes

    def fail_source_read(path):
        if path == source:
            raise OSError("capture download disappeared")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_source_read)

    with pytest.raises(consumer.MediaRetentionError, match="retention I/O failed"):
        consumer.retain_record_media(record, config_path=config)


def test_production_normalized_video_is_not_retained(tmp_path, monkeypatch):
    capture = tmp_path / "capture"
    capture.mkdir()
    video = capture / "clip.mp4"
    video.write_bytes(b"not-an-image-video-fixture")
    retained = tmp_path / "retained"
    config = _retention_config(tmp_path, capture, retained)
    calls = []
    monkeypatch.setattr(
        consumer, "_converge_retained_media", lambda *a, **k: calls.append((a, k))
    )
    normalized = _message("VIDEO-1")
    normalized.update({
        "hasMedia": True, "mediaType": "video", "mediaUrls": [str(video)]
    })
    record = consumer.InboxRecord(
        1, "VIDEO-1", "test-group@g.us", 0, 1,
        {"type": "whatsapp_capture_event", "normalized": normalized},
    )
    assert consumer.retain_record_media(record, config_path=config) == {
        "retained": 0, "bytes": 0, "operation": False,
    }
    assert calls == []
    assert not retained.exists()


@pytest.mark.asyncio
async def test_pending_production_video_bypasses_retention_and_completes(
    tmp_path, monkeypatch
):
    args = _enabled_consumer_args(tmp_path, [])
    capture = tmp_path / "capture"
    capture.mkdir()
    video = capture / "pending.mp4"
    video.write_bytes(b"video-fixture")
    config = Path(args.config)
    data = yaml.safe_load(config.read_text())
    data["pa"]["media_retention"] = {
        "enabled": True,
        "media_root": str(tmp_path / "retained"),
        "source_roots": [str(capture)],
        "operation": "tgg_media_retention",
        "min_free_percent": 0,
    }
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    normalized = _message("VIDEO-PENDING", "site@g.us")
    normalized.update(
        {
            "hasMedia": True,
            "mediaType": "video",
            "mediaUrls": [str(video)],
        }
    )
    _write_jsonl(
        Path(args.source),
        [{"type": "whatsapp_capture_event", "normalized": normalized}],
    )

    async def fake_process(records, **kwargs):
        assert [record.message_id for record in records] == ["VIDEO-PENDING"]
        return {
            "submitted_message_ids": ["VIDEO-PENDING"],
            "handled": [
                {"message_ids": ["VIDEO-PENDING"], "turn_id": "turn-video"}
            ],
            "captured_outbound": [],
        }

    monkeypatch.setattr(consumer, "process_live_records", fake_process)
    monkeypatch.setattr(consumer, "_new_gateway_runner", lambda: object())

    assert await consumer.run_consumer(args) == 0
    inbox = consumer.DurableInbox(Path(args.inbox))
    assert inbox.counts() == {"completed": 1}
    assert inbox.retention_counts() == {
        "retention_total": 0,
        "retention_failures": 0,
        "retention_attempts": 0,
        "retention_pending": 0,
        "retention_complete": 0,
        "retention_bypassed": 1,
        "retention_held": 0,
    }


@pytest.mark.asyncio
async def test_production_normalized_missing_image_stays_pending_and_once_survives(
    tmp_path, monkeypatch
):
    args = _enabled_consumer_args(tmp_path, [])
    capture = tmp_path / "capture"
    capture.mkdir()
    retained = tmp_path / "retained"
    config = Path(args.config)
    data = yaml.safe_load(config.read_text())
    data["pa"]["media_retention"] = {
        "enabled": True, "media_root": str(retained),
        "source_roots": [str(capture)], "operation": "tgg_media_retention",
        "min_free_percent": 0,
    }
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    normalized = _message("MISSING-IMAGE", "management@g.us")
    normalized.update({
        "hasMedia": True, "mediaType": "image",
        "mediaUrls": [str(capture / "already-evicted.jpg")],
        "timestamp": 1784630163.917,
    })
    _write_jsonl(
        Path(args.source),
        [{"type": "whatsapp_capture_event", "normalized": normalized}],
    )
    assert await consumer.run_consumer(args) == 0
    inbox = consumer.DurableInbox(Path(args.inbox))
    assert inbox.counts() == {"pending": 1}
    assert inbox.retention_counts()["retention_failures"] == 1
    status = json.loads(Path(args.status_file).read_text())
    assert status["state"] == "held-pending"
    assert "media source is unavailable" in status["retention_hold"]


@pytest.mark.asyncio
async def test_one_chat_retention_hold_does_not_kill_other_chat(tmp_path, monkeypatch):
    args = _enabled_consumer_args(tmp_path, [])
    capture = tmp_path / "capture"
    capture.mkdir()
    config = Path(args.config)
    data = yaml.safe_load(config.read_text())
    data["pa"]["media_retention"] = {
        "enabled": True, "media_root": str(tmp_path / "retained"),
        "source_roots": [str(capture)], "operation": "tgg_media_retention",
        "min_free_percent": 0,
    }
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    missing = _message("MISSING", "management@g.us")
    missing.update({
        "hasMedia": True, "mediaType": "image",
        "mediaUrls": [str(capture / "gone.jpg")], "timestamp": 1784630163.917,
    })
    healthy = _message("HEALTHY", "site@g.us")
    _write_jsonl(Path(args.source), [
        {"type": "whatsapp_capture_event", "normalized": missing}, healthy,
    ])

    async def fake_process(records, **kwargs):
        assert [record.message_id for record in records] == ["HEALTHY"]
        return {
            "submitted_message_ids": ["HEALTHY"],
            "handled": [{"message_ids": ["HEALTHY"], "turn_id": "turn-healthy"}],
            "captured_outbound": [],
        }

    monkeypatch.setattr(consumer, "process_live_records", fake_process)
    monkeypatch.setattr(consumer, "_new_gateway_runner", lambda: object())
    assert await consumer.run_consumer(args) == 0
    inbox = consumer.DurableInbox(Path(args.inbox))
    assert inbox.counts() == {"completed": 1, "pending": 1}
    with inbox.connect() as conn:
        states = {
            row["message_id"]: row["status"]
            for row in conn.execute("SELECT message_id,status FROM ingress_events")
        }
    assert states == {"MISSING": "pending", "HEALTHY": "completed"}
    assert "media source is unavailable" in (inbox.retention_last_error() or "")


@pytest.mark.asyncio
async def test_demo_pause_retains_pending_site_image_without_runner_or_delivery(
    tmp_path, monkeypatch
):
    args = _enabled_consumer_args(tmp_path, [])
    capture = tmp_path / "capture"
    capture.mkdir()
    source = capture / "site-photo.png"
    source.write_bytes(_png_bytes())
    config = Path(args.config)
    data = yaml.safe_load(config.read_text())
    data["pa"]["media_retention"] = {
        "enabled": True,
        "media_root": str(tmp_path / "retained"),
        "source_roots": [str(capture)],
        "operation": "tgg_media_retention",
        "min_free_percent": 0,
    }
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    normalized = _message("PAUSED-SITE-IMAGE", "site@g.us")
    normalized.update({
        "hasMedia": True,
        "mediaType": "image/png",
        "mediaUrls": [str(source)],
        "timestamp": 1784630163.917,
    })
    _write_jsonl(
        Path(args.source),
        [{"type": "whatsapp_capture_event", "normalized": normalized}],
    )
    convergence = []
    monkeypatch.setattr(
        consumer,
        "_converge_retained_media",
        lambda config_path, **kwargs: convergence.append(kwargs["payload"])
        or {
            "ok": True,
            "data": {"ledgerChanged": True, "observationsChanged": True},
        },
    )
    monkeypatch.setattr(
        consumer,
        "_new_gateway_runner",
        lambda: pytest.fail("paused site retention constructed a model runner"),
    )
    monkeypatch.setattr(
        consumer,
        "deliver_management_replies",
        lambda *args, **kwargs: pytest.fail("paused site retention invoked delivery"),
    )
    monkeypatch.setenv("TGG_DEMO_MANAGEMENT_ONLY", "1")

    assert await consumer.run_consumer(args) == 0
    inbox = consumer.DurableInbox(Path(args.inbox))
    assert inbox.counts() == {"pending": 1}
    with inbox.connect() as conn:
        row = conn.execute(
            "SELECT status,pa_turn_id,retention_state,retained_media_count "
            "FROM ingress_events WHERE message_id='PAUSED-SITE-IMAGE'"
        ).fetchone()
    assert tuple(row) == ("pending", None, "complete", 1)
    assert len(list((tmp_path / "retained").glob("*.png"))) == 1
    assert len(convergence) == 1
    status = json.loads(Path(args.status_file).read_text())
    assert status["retention_cycle"] == {
        "examined": 1,
        "retained": 1,
        "bypassed": 0,
        "held": 0,
    }
    assert status["retention_total"] == 1
    assert status["retention_complete"] == 1
    assert status["inbox"] == {"pending": 1}


@pytest.mark.asyncio
async def test_demo_pause_non_image_bypasses_retention_without_runner(
    tmp_path, monkeypatch
):
    args = _enabled_consumer_args(tmp_path, [])
    normalized = _message("PAUSED-SITE-TEXT", "site@g.us")
    normalized["timestamp"] = 1784630163.917
    _write_jsonl(
        Path(args.source),
        [{"type": "whatsapp_capture_event", "normalized": normalized}],
    )
    monkeypatch.setenv("TGG_DEMO_MANAGEMENT_ONLY", "1")
    monkeypatch.setattr(
        consumer,
        "retain_record_media",
        lambda *args, **kwargs: pytest.fail("non-image entered retention I/O"),
    )
    monkeypatch.setattr(
        consumer,
        "_new_gateway_runner",
        lambda: pytest.fail("paused site text constructed a model runner"),
    )

    assert await consumer.run_consumer(args) == 0
    inbox = consumer.DurableInbox(Path(args.inbox))
    with inbox.connect() as conn:
        row = conn.execute(
            "SELECT status,retention_state,retention_attempts "
            "FROM ingress_events WHERE message_id='PAUSED-SITE-TEXT'"
        ).fetchone()
    assert tuple(row) == ("pending", "bypassed", 0)
    assert json.loads(Path(args.status_file).read_text())["retention_cycle"] == {
        "examined": 0,
        "retained": 0,
        "bypassed": 0,
        "held": 0,
    }


@pytest.mark.asyncio
async def test_demo_pause_retention_hold_preserves_management_lane_and_retries(
    tmp_path, monkeypatch
):
    args = _enabled_consumer_args(tmp_path, [])
    args.retention_batch_size = 1
    capture = tmp_path / "capture"
    capture.mkdir()
    config = Path(args.config)
    data = yaml.safe_load(config.read_text())
    data["pa"]["media_retention"] = {
        "enabled": True,
        "media_root": str(tmp_path / "retained"),
        "source_roots": [str(capture)],
        "operation": "tgg_media_retention",
        "min_free_percent": 0,
    }
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    missing = _message("PAUSED-MISSING", "site@g.us")
    missing.update({
        "hasMedia": True,
        "mediaType": "image/jpeg",
        "mediaUrls": [str(capture / "raced-away.jpg")],
        "timestamp": 1784630163.917,
    })
    management = _message("MANAGEMENT-LIVE", "management@g.us")
    management["timestamp"] = 1784630164.917
    _write_jsonl(Path(args.source), [
        {"type": "whatsapp_capture_event", "normalized": missing},
        {"type": "whatsapp_capture_event", "normalized": management},
    ])
    processed = []

    async def fake_process(records, **kwargs):
        processed.extend(record.message_id for record in records)
        return {
            "submitted_message_ids": [record.message_id for record in records],
            "handled": [{
                "message_ids": [record.message_id for record in records],
                "turn_id": "turn-management",
            }],
            "captured_outbound": [],
        }

    monkeypatch.setattr(consumer, "process_live_records", fake_process)
    monkeypatch.setattr(consumer, "_new_gateway_runner", lambda: object())
    monkeypatch.setenv("TGG_DEMO_MANAGEMENT_ONLY", "1")

    assert await consumer.run_consumer(args) == 0
    assert processed == ["MANAGEMENT-LIVE"]
    inbox = consumer.DurableInbox(Path(args.inbox))
    with inbox.connect() as conn:
        rows = {
            row["message_id"]: (
                row["status"], row["retention_state"], row["retention_failures"]
            )
            for row in conn.execute(
                "SELECT message_id,status,retention_state,retention_failures "
                "FROM ingress_events"
            )
        }
    assert rows == {
        "PAUSED-MISSING": ("pending", "held", 1),
        "MANAGEMENT-LIVE": ("completed", "bypassed", 0),
    }
    status = json.loads(Path(args.status_file).read_text())
    assert status["state"] == "held-pending"
    assert status["retention_held"] == 1
    assert "media source is unavailable" in status["retention_hold"]
    assert await consumer.run_consumer(args) == 0
    assert processed == ["MANAGEMENT-LIVE"]
    retry_counts = consumer.DurableInbox(Path(args.inbox)).retention_counts()
    assert retry_counts["retention_failures"] == 2
    assert retry_counts["retention_attempts"] == 2


@pytest.mark.asyncio
async def test_demo_pause_retention_rerun_is_counter_and_storage_idempotent(
    tmp_path, monkeypatch
):
    args = _enabled_consumer_args(tmp_path, [])
    capture = tmp_path / "capture"
    capture.mkdir()
    source = capture / "rerun.png"
    source.write_bytes(_png_bytes())
    config = Path(args.config)
    data = yaml.safe_load(config.read_text())
    data["pa"]["media_retention"] = {
        "enabled": True,
        "media_root": str(tmp_path / "retained"),
        "source_roots": [str(capture)],
        "operation": "tgg_media_retention",
        "min_free_percent": 0,
    }
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    normalized = _message("PAUSED-RERUN", "site@g.us")
    normalized.update({
        "hasMedia": True,
        "mediaType": "image/png",
        "mediaUrls": [str(source)],
        "timestamp": 1784630163.917,
    })
    _write_jsonl(
        Path(args.source),
        [{"type": "whatsapp_capture_event", "normalized": normalized}],
    )
    convergence = []
    monkeypatch.setattr(
        consumer,
        "_converge_retained_media",
        lambda config_path, **kwargs: convergence.append(kwargs["payload"])
        or {
            "ok": True,
            "data": {"ledgerChanged": False, "observationsChanged": False},
        },
    )
    monkeypatch.setenv("TGG_DEMO_MANAGEMENT_ONLY", "1")
    monkeypatch.setattr(
        consumer,
        "_new_gateway_runner",
        lambda: pytest.fail("paused site rerun constructed a model runner"),
    )

    assert await consumer.run_consumer(args) == 0
    first = consumer.DurableInbox(Path(args.inbox)).retention_counts()
    first_files = [path.name for path in (tmp_path / "retained").glob("*")]
    assert await consumer.run_consumer(args) == 0
    second = consumer.DurableInbox(Path(args.inbox)).retention_counts()
    second_files = [path.name for path in (tmp_path / "retained").glob("*")]
    assert first == second
    assert first["retention_total"] == 1
    assert first["retention_attempts"] == 1
    assert first_files == second_files
    assert len(second_files) == 1
    assert len(convergence) == 1


def test_retention_operation_resolves_from_real_overlay_registry(tmp_path, monkeypatch):
    canonical = Path("deploy/tgg/christopher/config.yaml")
    data = yaml.safe_load(canonical.read_text())
    data["pa"]["enabled"] = True
    data["pa"]["constitution_path"] = str(
        Path("deploy/tgg/christopher/christopher_tgg_constitution.yaml").resolve()
    )
    config = tmp_path / "actual-shape.yaml"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    observed = {}

    def fake_execute(bridge, operation, payload, **kwargs):
        observed.update(operation=operation, known=set(bridge.operations), payload=payload)
        return {
            "ok": True,
            "data": {"ledgerChanged": False, "observationsChanged": False},
        }

    monkeypatch.setattr("tools.pa_business_tools.execute_business_operation", fake_execute)
    result = consumer._converge_retained_media(
        config,
        operation="tgg_media_retention",
        payload={"chat_jid": "120363421424519051@g.us", "message_id": "M1"},
    )
    assert result == {
        "ok": True,
        "data": {"ledgerChanged": False, "observationsChanged": False},
    }
    assert observed["operation"] == "tgg_media_retention"
    assert "tgg_media_retention" in observed["known"]


@pytest.mark.parametrize(
    "response",
    [
        {"ok": False, "error": "rejected"},
        {"ok": True, "data": {}},
        {"ledgerChanged": False, "observationsChanged": False},
    ],
)
def test_retention_convergence_rejects_non_contract_systems_envelope(
    tmp_path, monkeypatch, response
):
    canonical = Path("deploy/tgg/christopher/config.yaml")
    data = yaml.safe_load(canonical.read_text())
    data["pa"]["enabled"] = True
    data["pa"]["constitution_path"] = str(
        Path("deploy/tgg/christopher/christopher_tgg_constitution.yaml").resolve()
    )
    config = tmp_path / "actual-shape.yaml"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setattr(
        "tools.pa_business_tools.execute_business_operation",
        lambda *args, **kwargs: response,
    )

    with pytest.raises(
        consumer.MediaRetentionError, match="invalid Systems envelope"
    ):
        consumer._converge_retained_media(
            config,
            operation="tgg_media_retention",
            payload={
                "chat_jid": "120363421424519051@g.us",
                "message_id": "M1",
            },
        )


@pytest.mark.asyncio
async def test_retention_failure_requeues_before_model(tmp_path, monkeypatch):
    inbox = consumer.DurableInbox(tmp_path / "inbox.db")
    source = tmp_path / "events.jsonl"
    _write_jsonl(source, [_message("retry-media")])
    cursor = tmp_path / "cursor.json"
    consumer.initialize_cursor(source, cursor, position="start")
    inbox.stage_from_source(source, cursor)
    records = inbox.pending(limit=1)
    with inbox.connect() as conn:
        conn.execute(
            "UPDATE ingress_events SET retention_state='pending' WHERE seq=?",
            (records[0].seq,),
        )
    config = tmp_path / "config.yaml"
    config.write_text("pa:\n  enabled: true\n", encoding="utf-8")
    model_called = False
    def fail_retention(*args, **kwargs):
        raise consumer.MediaRetentionError("systems unavailable")
    async def model(*args, **kwargs):
        nonlocal model_called
        model_called = True
    monkeypatch.setattr(consumer, "retain_record_media", fail_retention)
    monkeypatch.setattr(consumer, "process_live_records", model)
    with pytest.raises(consumer.MediaRetentionError, match="systems unavailable"):
        await consumer._process_claimed_chat_batch(
            inbox, records, config_path=config, state_db=tmp_path / "state.db",
            gate_changed_at="2026-07-21T00:00:00+00:00", runner=object(),
        )
    assert model_called is False
    assert inbox.counts() == {"pending": 1}
    assert inbox.retention_counts() == {
        "retention_total": 0,
        "retention_failures": 1,
        "retention_attempts": 1,
        "retention_pending": 0,
        "retention_complete": 0,
        "retention_bypassed": 0,
        "retention_held": 1,
    }


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


def test_inbox_v1_migration_classifies_existing_retention_queue(tmp_path):
    db = tmp_path / "v1-inbox.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE ingress_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE reply_deliveries (
            delivery_key TEXT PRIMARY KEY, chat_id TEXT NOT NULL,
            reply_to_message_id TEXT, status TEXT NOT NULL,
            bridge_message_id TEXT, error TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE ingress_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL UNIQUE, chat_id TEXT NOT NULL,
            source_device INTEGER NOT NULL, source_inode INTEGER NOT NULL,
            start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL,
            raw_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, pa_turn_id TEXT, last_error TEXT,
            retained_media_count INTEGER NOT NULL DEFAULT 0,
            retention_failures INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        """
    )
    image = _message("OLD-IMAGE", "site@g.us")
    image.update({
        "hasMedia": True,
        "mediaType": "image/png",
        "mediaUrls": [str(tmp_path / "old.png")],
    })
    text = _message("OLD-TEXT", "site@g.us")
    for item in (image, text):
        conn.execute(
            "INSERT INTO ingress_events(message_id,chat_id,source_device,source_inode,"
            "start_offset,end_offset,raw_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                item["messageId"], item["chatId"], 1, 1, 0, 1,
                json.dumps({"type": "whatsapp_capture_event", "normalized": item}),
                "2026-07-22T00:00:00+00:00", "2026-07-22T00:00:00+00:00",
            ),
        )
    conn.commit()
    conn.close()

    inbox = consumer.DurableInbox(db)
    with inbox.connect() as migrated:
        columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(ingress_events)")
        }
        states = {
            row["message_id"]: row["retention_state"]
            for row in migrated.execute(
                "SELECT message_id,retention_state FROM ingress_events"
            )
        }
        schema_version = migrated.execute(
            "SELECT value FROM ingress_meta WHERE key='schema_version'"
        ).fetchone()[0]
    assert {
        "retention_attempts", "retention_state", "retention_last_error",
        "retention_updated_at",
    } <= columns
    assert states == {"OLD-IMAGE": "pending", "OLD-TEXT": "bypassed"}
    assert schema_version == "2"


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
    assert {
        "retention_total", "retention_failures", "media_root_count",
        "media_root_bytes", "media_volume_free_percent",
    } <= set(status)
    assert not Path(args.cursor).exists()


def test_disabled_retention_does_not_touch_media_root(tmp_path):
    media_root = tmp_path / "must-not-exist"
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "pa": {
            "enabled": False,
            "media_retention": {
                "enabled": True, "media_root": str(media_root),
                "source_roots": [str(tmp_path / "capture")],
                "operation": "tgg_media_retention", "min_free_percent": 20,
            },
        },
    }), encoding="utf-8")
    gate = tmp_path / "gate.json"
    gate.write_text(json.dumps({"version": 1, "enabled": False, "generation": 0}))
    args = argparse.Namespace(
        config=str(config), source=str(tmp_path / "missing.jsonl"),
        cursor=str(tmp_path / "missing.cursor"), inbox=str(tmp_path / "inbox.db"),
        status_file=str(tmp_path / "status.json"), lock_file=str(tmp_path / "lock"),
        state_db=str(tmp_path / "state.db"), processing_gate=str(gate), once=True,
        poll_seconds=.01, max_records=10,
    )
    assert asyncio.run(consumer.run_consumer(args)) == 0
    assert not media_root.exists()
    status = json.loads(Path(args.status_file).read_text())
    assert status["media_root_count"] == 0
    assert status["media_root_bytes"] == 0
    assert status["media_volume_free_percent"] is None


@pytest.mark.asyncio
async def test_low_media_volume_holds_before_source_open(tmp_path, monkeypatch):
    args = _enabled_consumer_args(tmp_path, [_message("not-staged")])
    config = Path(args.config)
    data = yaml.safe_load(config.read_text())
    data["pa"]["media_retention"] = {
        "enabled": True, "media_root": str(tmp_path / "retained"),
        "source_roots": [str(tmp_path)], "operation": "tgg_media_retention",
        "min_free_percent": 20,
    }
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setattr(
        consumer.shutil, "disk_usage",
        lambda path: consumer.shutil._ntuple_diskusage(total=100, used=90, free=10),
    )
    with pytest.raises(consumer.MediaRetentionError, match="below configured floor"):
        await consumer.run_consumer(args)
    status = json.loads(Path(args.status_file).read_text())
    assert status["state"] == "held"
    assert status["source_opened"] is False
    assert status["media_volume_free_percent"] == 10.0
    assert consumer.SourceCursor.from_path(Path(args.cursor)).offset == 0


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
