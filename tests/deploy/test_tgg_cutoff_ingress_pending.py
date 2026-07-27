import importlib.util
import fcntl
import json
import sqlite3
from pathlib import Path

from gateway.durable_jsonl_consumer import DurableInbox


SCRIPT = (
    Path(__file__).parents[2]
    / "deploy/tgg/christopher/scripts/cutoff_ingress_pending.py"
)
SPEC = importlib.util.spec_from_file_location("cutoff_ingress_pending", SCRIPT)
assert SPEC and SPEC.loader
cutoff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cutoff)


def _inbox(path: Path) -> Path:
    (path.parent / "consumer.lock").touch()
    inbox = DurableInbox(path)
    with inbox.connect() as conn:
        for seq in range(1, 8):
            retention = "held" if seq == 2 else (
                "bypassed" if seq in {3, 6} else "complete"
            )
            conn.execute(
                "INSERT INTO ingress_events("
                "seq,message_id,chat_id,source_device,source_inode,"
                "start_offset,end_offset,raw_json,status,retention_state,"
                "created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    seq,
                    f"m-{seq}",
                    f"chat-{seq % 2}",
                    1,
                    1,
                    seq,
                    seq + 1,
                    json.dumps({"messageId": f"m-{seq}", "chatId": f"chat-{seq % 2}"}),
                    "pending" if seq != 4 else "completed",
                    retention,
                    f"2026-07-27T00:00:0{seq}+00:00",
                    f"2026-07-27T00:00:0{seq}+00:00",
                ),
            )
    return path


def _status_rows(path: Path):
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT seq,status,retention_state,updated_at "
            "FROM ingress_events ORDER BY seq"
        ).fetchall()


def test_plan_is_read_only(tmp_path):
    inbox = _inbox(tmp_path / "inbox.db")
    before = _status_rows(inbox)

    result = cutoff.plan(inbox, 5)

    assert result["selected_count"] == 4
    assert result["selected_retention_counts"] == {
        "bypassed": 1,
        "complete": 2,
        "held": 1,
    }
    assert _status_rows(inbox) == before
    with sqlite3.connect(inbox) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name LIKE 'ingress_cutoff_%'"
        ).fetchone()[0] == 0


def test_apply_proves_post_cutoff_selection_and_preserves_held_state(tmp_path):
    inbox = _inbox(tmp_path / "inbox.db")
    before_image = tmp_path / "before.json"

    result = cutoff.apply_cutoff(
        inbox,
        5,
        run_id="tgg-d1-test",
        provenance="WB:227c5ed9 copy test",
        before_image=before_image,
        consumer_lock_file=tmp_path / "consumer.lock",
        expected_selected_count=4,
    )

    assert result["selected_count"] == 4
    assert result["held_selected_count"] == 1
    assert result["retention_state_mutations"] == 0
    assert result["status_counts_before"] == {"completed": 1, "pending": 6}
    assert result["status_counts_after"] == {
        "completed": 1,
        "pending": 2,
        "skipped": 4,
    }
    assert result["work_selection_after"] == {
        "total": 2,
        "at_or_before_cutoff": 0,
        "after_cutoff": 2,
        "min_seq": 6,
        "max_seq": 7,
    }
    artifact = json.loads(before_image.read_text(encoding="utf-8"))
    assert artifact["selected_count"] == len(artifact["rows"]) == 4
    assert [row["seq"] for row in artifact["rows"]] == [1, 2, 3, 5]
    assert all("raw_json" in row for row in artifact["rows"])
    assert artifact["provenance"] == "WB:227c5ed9 copy test"
    with sqlite3.connect(inbox) as conn:
        assert conn.execute(
            "SELECT retention_state FROM ingress_events WHERE seq=2"
        ).fetchone()[0] == "held"
        audit = conn.execute(
            "SELECT cutoff_seq,provenance,selected_count "
            "FROM ingress_cutoff_runs WHERE run_id='tgg-d1-test'"
        ).fetchone()
        assert audit == (5, "WB:227c5ed9 copy test", 4)
    durable = DurableInbox(inbox, read_only=True)
    management, site = durable.pending_chat_batches(batch_size=25)
    selected = [record.seq for _, batch in management + site for record in batch]
    assert selected == [6, 7]


def test_restore_uses_before_image_and_cas(tmp_path):
    inbox = _inbox(tmp_path / "inbox.db")
    before = _status_rows(inbox)
    before_image = tmp_path / "before.json"
    cutoff.apply_cutoff(
        inbox,
        5,
        run_id="restore-me",
        provenance="WB:227c5ed9 restore test",
        before_image=before_image,
        consumer_lock_file=tmp_path / "consumer.lock",
        expected_selected_count=4,
    )

    result = cutoff.restore_cutoff(
        inbox,
        before_image,
        confirm_run_id="restore-me",
        consumer_lock_file=tmp_path / "consumer.lock",
    )

    assert result["restored_count"] == 4
    assert _status_rows(inbox) == before
    with sqlite3.connect(inbox) as conn:
        assert conn.execute(
            "SELECT reverted_at IS NOT NULL FROM ingress_cutoff_runs "
            "WHERE run_id='restore-me'"
        ).fetchone()[0] == 1


def test_apply_refuses_without_selected_pending_rows(tmp_path):
    inbox = _inbox(tmp_path / "inbox.db")
    with sqlite3.connect(inbox) as conn:
        conn.execute("UPDATE ingress_events SET status='completed'")
    before_image = tmp_path / "before.json"

    try:
        cutoff.apply_cutoff(
            inbox,
            7,
            run_id="no-op",
            provenance="WB:227c5ed9 no-op test",
            before_image=before_image,
            consumer_lock_file=tmp_path / "consumer.lock",
            expected_selected_count=4,
        )
    except cutoff.CutoffError as exc:
        assert "selected zero pending rows" in str(exc)
    else:
        raise AssertionError("zero-row cutoff did not refuse")
    assert not before_image.exists()


def test_apply_refuses_while_consumer_lock_is_held(tmp_path):
    inbox = _inbox(tmp_path / "inbox.db")
    lock_path = tmp_path / "consumer.lock"
    lock = lock_path.open("a+", encoding="utf-8")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        try:
            cutoff.apply_cutoff(
                inbox,
                5,
                run_id="live-consumer",
                provenance="WB:227c5ed9 lock test",
                before_image=tmp_path / "before.json",
                consumer_lock_file=lock_path,
                expected_selected_count=4,
            )
        except cutoff.CutoffError as exc:
            assert "consumer is running" in str(exc)
        else:
            raise AssertionError("cutoff did not refuse the live consumer lock")
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def test_apply_refuses_historical_processing_rows(tmp_path):
    inbox = _inbox(tmp_path / "inbox.db")
    with sqlite3.connect(inbox) as conn:
        conn.execute("UPDATE ingress_events SET status='processing' WHERE seq=1")

    try:
        cutoff.apply_cutoff(
            inbox,
            5,
            run_id="processing-row",
            provenance="WB:227c5ed9 processing test",
            before_image=tmp_path / "before.json",
            consumer_lock_file=tmp_path / "consumer.lock",
            expected_selected_count=3,
        )
    except cutoff.CutoffError as exc:
        assert "historical processing rows" in str(exc)
    else:
        raise AssertionError("cutoff did not refuse a historical processing row")


def test_apply_refuses_missing_lock_and_stale_plan_count(tmp_path):
    inbox = _inbox(tmp_path / "inbox.db")
    missing_lock = tmp_path / "mistyped.lock"
    try:
        cutoff.apply_cutoff(
            inbox,
            5,
            run_id="missing-lock",
            provenance="WB:227c5ed9 lock-path test",
            before_image=tmp_path / "missing-lock-before.json",
            consumer_lock_file=missing_lock,
            expected_selected_count=4,
        )
    except cutoff.CutoffError as exc:
        assert "lock file is missing" in str(exc)
    else:
        raise AssertionError("cutoff accepted a nonexistent consumer lock")
    assert not missing_lock.exists()

    try:
        cutoff.apply_cutoff(
            inbox,
            5,
            run_id="stale-plan",
            provenance="WB:227c5ed9 count test",
            before_image=tmp_path / "stale-plan-before.json",
            consumer_lock_file=tmp_path / "consumer.lock",
            expected_selected_count=5,
        )
    except cutoff.CutoffError as exc:
        assert "selected-count mismatch" in str(exc)
    else:
        raise AssertionError("cutoff accepted a stale plan count")


def test_apply_refuses_existing_before_image_without_mutation(tmp_path):
    inbox = _inbox(tmp_path / "inbox.db")
    before = _status_rows(inbox)
    image = tmp_path / "before.json"
    image.write_text("do not replace", encoding="utf-8")

    try:
        cutoff.apply_cutoff(
            inbox,
            5,
            run_id="existing-image",
            provenance="WB:227c5ed9 artifact test",
            before_image=image,
            consumer_lock_file=tmp_path / "consumer.lock",
            expected_selected_count=4,
        )
    except cutoff.CutoffError as exc:
        assert "before-image already exists" in str(exc)
    else:
        raise AssertionError("cutoff replaced an existing before-image")
    assert image.read_text(encoding="utf-8") == "do not replace"
    assert _status_rows(inbox) == before


def test_audit_table_restore_survives_missing_before_image(tmp_path):
    inbox = _inbox(tmp_path / "inbox.db")
    before = _status_rows(inbox)
    before_image = tmp_path / "before.json"
    lock_path = tmp_path / "consumer.lock"
    cutoff.apply_cutoff(
        inbox,
        5,
        run_id="audit-restore",
        provenance="WB:227c5ed9 audit restore test",
        before_image=before_image,
        consumer_lock_file=lock_path,
        expected_selected_count=4,
    )
    before_image.unlink()

    result = cutoff.restore_from_audit(
        inbox, run_id="audit-restore", consumer_lock_file=lock_path
    )

    assert result["restored_count"] == 4
    assert _status_rows(inbox) == before
