#!/usr/bin/env python3
"""One-time, reversible cutoff mutation for Christopher's durable inbox.

The script is deliberately standalone so the authorised operator can inspect
and run it without deploying or restarting Hermes.  It only changes rows that
are still ``pending`` and whose sequence is at or below an explicit cutoff.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


ARTIFACT_TYPE = "tgg_ingress_cutoff_before_image"
ARTIFACT_VERSION = 1
RUNS_TABLE = "ingress_cutoff_runs"
ROWS_TABLE = "ingress_cutoff_run_rows"


class CutoffError(RuntimeError):
    """Refusal raised when the mutation cannot be proven safe."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(path: Path, *, read_only: bool) -> sqlite3.Connection:
    if not path.is_file():
        raise CutoffError(f"inbox database is missing: {path}")
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=FULL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextlib.contextmanager
def _consumer_stopped_lock(path: Path):
    """Acquire the consumer singleton lock or refuse a live race."""
    if not path.is_file():
        raise CutoffError(f"consumer lock file is missing or not regular: {path}")
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CutoffError(
                f"consumer is running and holds its singleton lock: {path}"
            ) from exc
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _require_inbox_schema(conn: sqlite3.Connection) -> None:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingress_events'"
    ).fetchone()
    if table is None:
        raise CutoffError("database has no ingress_events table")
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(ingress_events)").fetchall()
    }
    required = {"seq", "status", "retention_state", "updated_at"}
    missing = sorted(required - columns)
    if missing:
        raise CutoffError(
            "ingress_events is missing required columns: " + ",".join(missing)
        )
    integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        raise CutoffError(f"inbox integrity_check failed: {integrity}")


def _status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        str(row["status"]): int(row["n"])
        for row in conn.execute(
            "SELECT status,COUNT(*) AS n FROM ingress_events "
            "GROUP BY status ORDER BY status"
        )
    }


def _pending_retention_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        str(row["retention_state"]): int(row["n"])
        for row in conn.execute(
            "SELECT retention_state,COUNT(*) AS n FROM ingress_events "
            "WHERE status='pending' GROUP BY retention_state "
            "ORDER BY retention_state"
        )
    }


def _work_selection_counts(
    conn: sqlite3.Connection, cutoff_seq: int
) -> dict[str, int | None]:
    row = conn.execute(
        "SELECT COUNT(*) AS total,"
        "COALESCE(SUM(CASE WHEN seq<=? THEN 1 ELSE 0 END),0) AS at_or_before,"
        "COALESCE(SUM(CASE WHEN seq>? THEN 1 ELSE 0 END),0) AS after_cutoff,"
        "MIN(seq) AS min_seq,MAX(seq) AS max_seq "
        "FROM ingress_events WHERE status='pending' "
        "AND retention_state IN ('complete','bypassed')",
        (cutoff_seq, cutoff_seq),
    ).fetchone()
    return {
        "total": int(row["total"]),
        "at_or_before_cutoff": int(row["at_or_before"]),
        "after_cutoff": int(row["after_cutoff"]),
        "min_seq": int(row["min_seq"]) if row["min_seq"] is not None else None,
        "max_seq": int(row["max_seq"]) if row["max_seq"] is not None else None,
    }


def _selected_rows(
    conn: sqlite3.Connection, cutoff_seq: int
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM ingress_events "
            "WHERE status='pending' AND seq<=? ORDER BY seq",
            (cutoff_seq,),
        ).fetchall()
    ]


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> str:
    path = path.resolve()
    if path.exists():
        raise CutoffError(f"before-image already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return hashlib.sha256(encoded).hexdigest()


def _artifact_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan(inbox: Path, cutoff_seq: int) -> dict[str, Any]:
    conn = _connect(inbox, read_only=True)
    try:
        _require_inbox_schema(conn)
        rows = _selected_rows(conn, cutoff_seq)
        return {
            "mode": "plan",
            "inbox": str(inbox.resolve()),
            "cutoff_seq": cutoff_seq,
            "selected_count": len(rows),
            "selected_seq_min": int(rows[0]["seq"]) if rows else None,
            "selected_seq_max": int(rows[-1]["seq"]) if rows else None,
            "selected_retention_counts": dict(
                sorted(Counter(str(row["retention_state"]) for row in rows).items())
            ),
            "status_counts": _status_counts(conn),
            "pending_retention_counts": _pending_retention_counts(conn),
            "work_selection": _work_selection_counts(conn, cutoff_seq),
            "processing_at_or_before_cutoff": int(
                conn.execute(
                    "SELECT COUNT(*) FROM ingress_events "
                    "WHERE status='processing' AND seq<=?",
                    (cutoff_seq,),
                ).fetchone()[0]
            ),
        }
    finally:
        conn.close()


def apply_cutoff(
    inbox: Path,
    cutoff_seq: int,
    *,
    run_id: str,
    provenance: str,
    before_image: Path,
    consumer_lock_file: Path,
    expected_selected_count: int,
) -> dict[str, Any]:
    if not run_id.strip():
        raise CutoffError("run_id must be non-empty")
    if not provenance.strip():
        raise CutoffError("provenance must be non-empty")
    mutation_at = _utc_now()
    with _consumer_stopped_lock(consumer_lock_file):
        conn = _connect(inbox, read_only=False)
        artifact_written = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            _require_inbox_schema(conn)
            processing = int(
                conn.execute(
                    "SELECT COUNT(*) FROM ingress_events "
                    "WHERE status='processing' AND seq<=?",
                    (cutoff_seq,),
                ).fetchone()[0]
            )
            if processing:
                raise CutoffError(
                    "cutoff refuses historical processing rows: "
                    f"count={processing}; reconcile them before applying"
                )
            rows = _selected_rows(conn, cutoff_seq)
            if not rows:
                raise CutoffError(
                    "cutoff selected zero pending rows; refusing a no-op mutation"
                )
            if len(rows) != expected_selected_count:
                raise CutoffError(
                    "cutoff selected-count mismatch: "
                    f"expected={expected_selected_count} actual={len(rows)}"
                )
            before_status = _status_counts(conn)
            before_pending_retention = _pending_retention_counts(conn)
            before_work_selection = _work_selection_counts(conn, cutoff_seq)
            held_before = int(
                conn.execute(
                    "SELECT COUNT(*) FROM ingress_events WHERE retention_state='held'"
                ).fetchone()[0]
            )
            retention_before = {
                int(row["seq"]): str(row["retention_state"]) for row in rows
            }
            held_selected = sum(
                1 for row in rows if str(row["retention_state"]) == "held"
            )
            artifact = {
                "artifact_type": ARTIFACT_TYPE,
                "artifact_version": ARTIFACT_VERSION,
                "created_at": mutation_at,
                "inbox": str(inbox.resolve()),
                "run_id": run_id,
                "provenance": provenance,
                "cutoff_seq": cutoff_seq,
                "selected_count": len(rows),
                "selected_seq_min": int(rows[0]["seq"]),
                "selected_seq_max": int(rows[-1]["seq"]),
                "held_selected_count": held_selected,
                "status_counts_before": before_status,
                "pending_retention_counts_before": before_pending_retention,
                "work_selection_before": before_work_selection,
                "mutation_contract": {
                    "predicate": "status='pending' AND seq<=cutoff_seq",
                    "writes": ["status='skipped'", f"updated_at='{mutation_at}'"],
                    "unchanged": [
                        "retention_state",
                        "raw_json",
                        "message_id",
                        "chat_id",
                        "pa_turn_id",
                        "last_error",
                    ],
                },
                "rows": rows,
            }
            artifact_sha = _atomic_write_json(before_image, artifact)
            artifact_written = True
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {RUNS_TABLE} (
                    run_id TEXT PRIMARY KEY,
                    cutoff_seq INTEGER NOT NULL,
                    provenance TEXT NOT NULL,
                    before_image_path TEXT NOT NULL,
                    before_image_sha256 TEXT NOT NULL,
                    selected_count INTEGER NOT NULL,
                    applied_at TEXT NOT NULL,
                    reverted_at TEXT
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {ROWS_TABLE} (
                    run_id TEXT NOT NULL REFERENCES {RUNS_TABLE}(run_id),
                    seq INTEGER NOT NULL,
                    status_before TEXT NOT NULL,
                    updated_at_before TEXT NOT NULL,
                    PRIMARY KEY(run_id,seq)
                )
                """
            )
            conn.execute(
                f"INSERT INTO {RUNS_TABLE}("
                "run_id,cutoff_seq,provenance,before_image_path,"
                "before_image_sha256,selected_count,applied_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    run_id,
                    cutoff_seq,
                    provenance,
                    str(before_image.resolve()),
                    artifact_sha,
                    len(rows),
                    mutation_at,
                ),
            )
            conn.executemany(
                f"INSERT INTO {ROWS_TABLE}("
                "run_id,seq,status_before,updated_at_before) VALUES(?,?,?,?)",
                [
                    (
                        run_id,
                        int(row["seq"]),
                        str(row["status"]),
                        str(row["updated_at"]),
                    )
                    for row in rows
                ],
            )
            changed = conn.execute(
                "UPDATE ingress_events SET status='skipped',updated_at=? "
                "WHERE status='pending' AND seq<=?",
                (mutation_at, cutoff_seq),
            ).rowcount
            if changed != len(rows):
                raise CutoffError(
                    f"cutoff CAS mismatch: selected={len(rows)} changed={changed}"
                )
            after_status = _status_counts(conn)
            after_pending_retention = _pending_retention_counts(conn)
            after_work_selection = _work_selection_counts(conn, cutoff_seq)
            if after_work_selection["at_or_before_cutoff"] != 0:
                raise CutoffError(
                    "post-mutation work selection still includes cutoff rows"
                )
            if (
                after_status.get("pending", 0)
                != before_status.get("pending", 0) - changed
            ):
                raise CutoffError("pending count did not fall by the selected count")
            if (
                after_status.get("skipped", 0)
                != before_status.get("skipped", 0) + changed
            ):
                raise CutoffError("skipped count did not rise by the selected count")
            held_after = int(
                conn.execute(
                    "SELECT COUNT(*) FROM ingress_events WHERE retention_state='held'"
                ).fetchone()[0]
            )
            if held_after != held_before:
                raise CutoffError("cutoff changed the held retention population")
            retention_after = {
                int(row["seq"]): str(row["retention_state"])
                for row in conn.execute(
                    "SELECT seq,retention_state FROM ingress_events "
                    f"WHERE seq IN (SELECT seq FROM {ROWS_TABLE} "
                    "WHERE run_id=?)",
                    (run_id,),
                )
            }
            retention_mutations = sum(
                retention_after.get(seq) != state
                for seq, state in retention_before.items()
            )
            if retention_mutations:
                raise CutoffError(
                    "cutoff changed selected retention_state values: "
                    f"count={retention_mutations}"
                )
            conn.commit()
            return {
                "mode": "apply",
                "run_id": run_id,
                "provenance": provenance,
                "cutoff_seq": cutoff_seq,
                "selected_count": len(rows),
                "held_selected_count": held_selected,
                "before_image": str(before_image.resolve()),
                "before_image_sha256": artifact_sha,
                "status_counts_before": before_status,
                "status_counts_after": after_status,
                "pending_retention_counts_before": before_pending_retention,
                "pending_retention_counts_after": after_pending_retention,
                "work_selection_before": before_work_selection,
                "work_selection_after": after_work_selection,
                "retention_state_mutations": retention_mutations,
                "consumer_lock_file": str(consumer_lock_file.resolve()),
            }
        except Exception:
            conn.rollback()
            if artifact_written:
                before_image.unlink(missing_ok=True)
            raise
        finally:
            conn.close()


def restore_cutoff(
    inbox: Path,
    before_image: Path,
    *,
    confirm_run_id: str,
    consumer_lock_file: Path,
) -> dict[str, Any]:
    artifact = json.loads(before_image.read_text(encoding="utf-8"))
    if artifact.get("artifact_type") != ARTIFACT_TYPE:
        raise CutoffError("before-image artifact type is not recognized")
    if int(artifact.get("artifact_version", 0)) != ARTIFACT_VERSION:
        raise CutoffError("before-image artifact version is not supported")
    run_id = str(artifact.get("run_id", ""))
    if confirm_run_id != run_id:
        raise CutoffError("confirm_run_id does not match the before-image run_id")
    if Path(str(artifact.get("inbox", ""))).resolve() != inbox.resolve():
        raise CutoffError("before-image was created for a different inbox path")
    rows = artifact.get("rows")
    if not isinstance(rows, list) or len(rows) != int(
        artifact.get("selected_count", -1)
    ):
        raise CutoffError("before-image row denominator mismatch")
    mutation_at = str(artifact.get("created_at", ""))
    restored_at = _utc_now()
    with _consumer_stopped_lock(consumer_lock_file):
        conn = _connect(inbox, read_only=False)
        try:
            conn.execute("BEGIN IMMEDIATE")
            _require_inbox_schema(conn)
            run = conn.execute(
                f"SELECT * FROM {RUNS_TABLE} WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise CutoffError(
                    f"cutoff run is not recorded in the inbox: {run_id}"
                )
            if int(run["cutoff_seq"]) != int(artifact["cutoff_seq"]):
                raise CutoffError("before-image cutoff does not match the audit row")
            if str(run["before_image_sha256"]) != _artifact_sha256(before_image):
                raise CutoffError(
                    "before-image SHA-256 does not match the inbox audit row"
                )
            if run["reverted_at"] is not None:
                raise CutoffError(f"cutoff run is already restored: {run_id}")
            changed = 0
            for row in rows:
                result = conn.execute(
                    "UPDATE ingress_events SET status=?,updated_at=? "
                    "WHERE seq=? AND status='skipped' AND updated_at=?",
                    (
                        str(row["status"]),
                        str(row["updated_at"]),
                        int(row["seq"]),
                        mutation_at,
                    ),
                )
                changed += int(result.rowcount)
            if changed != len(rows):
                raise CutoffError(
                    f"restore CAS mismatch: expected={len(rows)} changed={changed}"
                )
            conn.execute(
                f"UPDATE {RUNS_TABLE} SET reverted_at=? WHERE run_id=?",
                (restored_at, run_id),
            )
            conn.commit()
            return {
                "mode": "restore",
                "run_id": run_id,
                "restored_count": changed,
                "restored_at": restored_at,
                "status_counts_after": _status_counts(conn),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def restore_from_audit(
    inbox: Path, *, run_id: str, consumer_lock_file: Path
) -> dict[str, Any]:
    """Restore changed fields from the durable in-database audit tables."""
    restored_at = _utc_now()
    with _consumer_stopped_lock(consumer_lock_file):
        conn = _connect(inbox, read_only=False)
        try:
            conn.execute("BEGIN IMMEDIATE")
            _require_inbox_schema(conn)
            run = conn.execute(
                f"SELECT * FROM {RUNS_TABLE} WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise CutoffError(
                    f"cutoff run is not recorded in the inbox: {run_id}"
                )
            if run["reverted_at"] is not None:
                raise CutoffError(f"cutoff run is already restored: {run_id}")
            rows = conn.execute(
                f"SELECT seq,status_before,updated_at_before FROM {ROWS_TABLE} "
                "WHERE run_id=? ORDER BY seq",
                (run_id,),
            ).fetchall()
            if len(rows) != int(run["selected_count"]):
                raise CutoffError("audit-table restore row denominator mismatch")
            changed = 0
            for row in rows:
                result = conn.execute(
                    "UPDATE ingress_events SET status=?,updated_at=? "
                    "WHERE seq=? AND status='skipped' AND updated_at=?",
                    (
                        str(row["status_before"]),
                        str(row["updated_at_before"]),
                        int(row["seq"]),
                        str(run["applied_at"]),
                    ),
                )
                changed += int(result.rowcount)
            if changed != len(rows):
                raise CutoffError(
                    f"audit restore CAS mismatch: expected={len(rows)} "
                    f"changed={changed}"
                )
            conn.execute(
                f"UPDATE {RUNS_TABLE} SET reverted_at=? WHERE run_id=?",
                (restored_at, run_id),
            )
            conn.commit()
            return {
                "mode": "restore-audit",
                "run_id": run_id,
                "restored_count": changed,
                "restored_at": restored_at,
                "status_counts_after": _status_counts(conn),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan, apply, or restore Christopher's one-time pending inbox cutoff."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan", help="Read-only cutoff preview.")
    plan_parser.add_argument("--inbox", type=Path, required=True)
    plan_parser.add_argument("--cutoff-seq", type=_positive_int, required=True)

    apply_parser = subparsers.add_parser(
        "apply", help="Write before-image, audit provenance, then apply cutoff."
    )
    apply_parser.add_argument("--inbox", type=Path, required=True)
    apply_parser.add_argument("--cutoff-seq", type=_positive_int, required=True)
    apply_parser.add_argument("--run-id", required=True)
    apply_parser.add_argument("--provenance", required=True)
    apply_parser.add_argument("--before-image", type=Path, required=True)
    apply_parser.add_argument("--consumer-lock-file", type=Path, required=True)
    apply_parser.add_argument(
        "--expect-selected-count", type=_positive_int, required=True
    )
    apply_parser.add_argument(
        "--confirm-apply",
        action="store_true",
        help="Required explicit acknowledgement that this mutates the inbox.",
    )

    restore_parser = subparsers.add_parser(
        "restore", help="CAS-restore rows from an apply before-image."
    )
    restore_parser.add_argument("--inbox", type=Path, required=True)
    restore_parser.add_argument("--before-image", type=Path, required=True)
    restore_parser.add_argument("--confirm-run-id", required=True)
    restore_parser.add_argument("--consumer-lock-file", type=Path, required=True)
    restore_parser.add_argument(
        "--confirm-restore",
        action="store_true",
        help="Required explicit acknowledgement that this mutates the inbox.",
    )
    audit_parser = subparsers.add_parser(
        "restore-audit",
        help="CAS-restore using the in-database audit if the file is unavailable.",
    )
    audit_parser.add_argument("--inbox", type=Path, required=True)
    audit_parser.add_argument("--run-id", required=True)
    audit_parser.add_argument("--consumer-lock-file", type=Path, required=True)
    audit_parser.add_argument(
        "--confirm-restore",
        action="store_true",
        help="Required explicit acknowledgement that this mutates the inbox.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            result = plan(args.inbox, args.cutoff_seq)
        elif args.command == "apply":
            if not args.confirm_apply:
                raise CutoffError("apply requires --confirm-apply")
            result = apply_cutoff(
                args.inbox,
                args.cutoff_seq,
                run_id=args.run_id,
                provenance=args.provenance,
                before_image=args.before_image,
                consumer_lock_file=args.consumer_lock_file,
                expected_selected_count=args.expect_selected_count,
            )
        elif args.command == "restore":
            if not args.confirm_restore:
                raise CutoffError("restore requires --confirm-restore")
            result = restore_cutoff(
                args.inbox,
                args.before_image,
                confirm_run_id=args.confirm_run_id,
                consumer_lock_file=args.consumer_lock_file,
            )
        else:
            if not args.confirm_restore:
                raise CutoffError("restore-audit requires --confirm-restore")
            result = restore_from_audit(
                args.inbox,
                run_id=args.run_id,
                consumer_lock_file=args.consumer_lock_file,
            )
    except (
        CutoffError,
        json.JSONDecodeError,
        OSError,
        sqlite3.Error,
        TypeError,
        KeyError,
        ValueError,
    ) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"ok": True, "data": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
