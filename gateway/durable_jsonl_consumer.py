"""Crash-safe JSONL ingress for externally captured messaging events.

The bridge's HTTP ``/messages`` route is deliberately not used here.  That
route drains an unauthenticated, in-memory, capped queue.  This consumer reads
the bridge's append-first durable JSONL store, stages every complete record in
its own SQLite inbox, and advances a committed cursor only after the inbox
transaction commits.

Production processing requires two independent declarations: ``pa.enabled``
in the consumer's Hermes config and the root-owned processing gate passed to
the daemon.  Unless both are true, the daemon does not open the source JSONL
and does not advance either the source cursor or inbox state.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml


CURSOR_VERSION = 1
INBOX_SCHEMA_VERSION = 2


class ConsumerError(RuntimeError):
    """Fail-closed consumer contract violation."""


class MediaRetentionError(ConsumerError):
    """Retryable failure before a claimed event reaches the model."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    data = (json.dumps(dict(payload), sort_keys=True, indent=2) + "\n").encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


@dataclass(frozen=True)
class SourceCursor:
    version: int
    source_path: str
    source_device: int
    source_inode: int
    initial_offset: int
    offset: int
    initialized_at: str
    updated_at: str
    last_message_id: str | None = None

    @classmethod
    def from_path(cls, path: Path) -> "SourceCursor":
        raw = json.loads(path.read_text(encoding="utf-8"))
        cursor = cls(
            version=int(raw["version"]),
            source_path=str(raw["source_path"]),
            source_device=int(raw["source_device"]),
            source_inode=int(raw["source_inode"]),
            initial_offset=int(raw.get("initial_offset", raw["offset"])),
            offset=int(raw["offset"]),
            initialized_at=str(raw["initialized_at"]),
            updated_at=str(raw["updated_at"]),
            last_message_id=(
                str(raw["last_message_id"]) if raw.get("last_message_id") else None
            ),
        )
        if cursor.version != CURSOR_VERSION:
            raise ConsumerError(
                f"unsupported cursor version {cursor.version}; expected {CURSOR_VERSION}"
            )
        if cursor.offset < 0 or cursor.initial_offset < 0:
            raise ConsumerError("cursor offsets cannot be negative")
        if cursor.offset < cursor.initial_offset:
            raise ConsumerError("cursor offset cannot precede its initial boundary")
        return cursor


def initialize_cursor(
    source: Path, cursor_path: Path, *, position: str
) -> SourceCursor:
    source = source.resolve()
    if position not in {"start", "end"}:
        raise ConsumerError("initial cursor position must be start or end")
    stat = source.stat()
    now = _utc_now()
    initial_offset = int(stat.st_size if position == "end" else 0)
    cursor = SourceCursor(
        version=CURSOR_VERSION,
        source_path=str(source),
        source_device=int(stat.st_dev),
        source_inode=int(stat.st_ino),
        initial_offset=initial_offset,
        offset=initial_offset,
        initialized_at=now,
        updated_at=now,
        last_message_id=None,
    )
    if cursor_path.exists():
        existing = SourceCursor.from_path(cursor_path)
        if existing.source_path != cursor.source_path:
            raise ConsumerError(
                f"cursor already belongs to {existing.source_path}, not {cursor.source_path}"
            )
        return existing
    _atomic_write_json(cursor_path, asdict(cursor))
    return cursor


class SingletonLock:
    """Process singleton guard backed by a non-blocking flock."""

    def __init__(self, path: Path):
        self.path = path
        self._handle: Any = None

    def __enter__(self) -> "SingletonLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise ConsumerError(
                f"consumer singleton already holds {self.path}"
            ) from exc
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(f"{os.getpid()}\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


def _bridge_item(value: Any) -> dict[str, Any]:
    """Accept the durable bridge item or a shallow append-envelope wrapper."""
    if not isinstance(value, Mapping):
        raise ConsumerError("durable JSONL record is not an object")
    candidates = [value]
    # "normalized" is the live capture-bridge envelope (whatsapp_capture_event
    # records in events.jsonl nest the bridge item there); the flat shape and
    # the other wrapper keys cover fixtures and replay corpora. Missing
    # "normalized" put the live consumer in a poison-record crash loop on the
    # first real capture record at activation (2026-07-21, first-light).
    for key in ("normalized", "message", "event", "payload", "data"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            candidates.append(nested)
    for candidate in candidates:
        if candidate.get("messageId") and candidate.get("chatId"):
            return dict(candidate)
    raise ConsumerError("durable JSONL record has no bridge messageId/chatId item")


@dataclass(frozen=True)
class InboxRecord:
    seq: int
    message_id: str
    chat_id: str
    start_offset: int
    end_offset: int
    raw: dict[str, Any]


def _initial_retention_state(item: Mapping[str, Any]) -> str:
    """Classify obvious non-images without doing any retention I/O."""
    coarse = str(item.get("mediaType") or item.get("mimeType") or "")
    if coarse.split("/", 1)[0].strip().lower() == "image":
        return "pending"
    values = item.get("mediaUrls") or item.get("media") or item.get("mediaPaths") or []
    if isinstance(values, Mapping):
        values = [values]
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        for value in values:
            if not isinstance(value, Mapping):
                continue
            mime = str(
                value.get("mime") or value.get("mimeType")
                or value.get("contentType") or ""
            )
            if mime.split("/", 1)[0].strip().lower() == "image":
                return "pending"
    return "bypassed"


class DurableInbox:
    """Consumer-owned durable inbox and source cursor staging ledger."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ingress_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reply_deliveries (
                    delivery_key TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    reply_to_message_id TEXT,
                    status TEXT NOT NULL
                        CHECK (status IN ('delivered','undelivered')),
                    bridge_message_id TEXT,
                    provider_outcome TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ingress_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    chat_id TEXT NOT NULL,
                    source_device INTEGER NOT NULL,
                    source_inode INTEGER NOT NULL,
                    start_offset INTEGER NOT NULL,
                    end_offset INTEGER NOT NULL,
                    raw_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','processing','completed','skipped','failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    pa_turn_id TEXT,
                    last_error TEXT,
                    retained_media_count INTEGER NOT NULL DEFAULT 0,
                    retention_failures INTEGER NOT NULL DEFAULT 0,
                    retention_attempts INTEGER NOT NULL DEFAULT 0,
                    retention_state TEXT NOT NULL DEFAULT 'pending'
                        CHECK (retention_state IN ('pending','complete','bypassed','held')),
                    retention_last_error TEXT,
                    retention_updated_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ingress_events_status_seq_idx
                    ON ingress_events(status, seq);
                """
            )
            # Existing consumer DBs predate durable provider-outcome detail.
            reply_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(reply_deliveries)")
            }
            if "provider_outcome" not in reply_columns:
                conn.execute("ALTER TABLE reply_deliveries ADD COLUMN provider_outcome TEXT")
            ingress_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(ingress_events)")
            }
            if "retained_media_count" not in ingress_columns:
                conn.execute(
                    "ALTER TABLE ingress_events ADD COLUMN retained_media_count "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "retention_failures" not in ingress_columns:
                conn.execute(
                    "ALTER TABLE ingress_events ADD COLUMN retention_failures "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "retention_attempts" not in ingress_columns:
                conn.execute(
                    "ALTER TABLE ingress_events ADD COLUMN retention_attempts "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            retention_state_added = "retention_state" not in ingress_columns
            if retention_state_added:
                conn.execute(
                    "ALTER TABLE ingress_events ADD COLUMN retention_state "
                    "TEXT NOT NULL DEFAULT 'pending'"
                )
            if "retention_last_error" not in ingress_columns:
                conn.execute(
                    "ALTER TABLE ingress_events ADD COLUMN retention_last_error TEXT"
                )
            if "retention_updated_at" not in ingress_columns:
                conn.execute(
                    "ALTER TABLE ingress_events ADD COLUMN retention_updated_at TEXT"
                )
            if retention_state_added:
                rows = conn.execute(
                    "SELECT seq,raw_json,retained_media_count FROM ingress_events"
                ).fetchall()
                for row in rows:
                    if int(row["retained_media_count"] or 0) > 0:
                        conn.execute(
                            "UPDATE ingress_events SET retention_state='complete',"
                            "retention_updated_at=? WHERE seq=?",
                            (_utc_now(), int(row["seq"])),
                        )
                        continue
                    try:
                        item = _bridge_item(json.loads(row["raw_json"]))
                        state = _initial_retention_state(item)
                    except (ConsumerError, TypeError, ValueError):
                        state = "pending"
                    if state == "bypassed":
                        conn.execute(
                            "UPDATE ingress_events SET retention_state='bypassed',"
                            "retention_updated_at=? WHERE seq=?",
                            (_utc_now(), int(row["seq"])),
                        )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ingress_events_retention_queue_idx "
                "ON ingress_events(status,retention_state,seq)"
            )
            conn.execute(
                "INSERT INTO ingress_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(INBOX_SCHEMA_VERSION),),
            )

    def stage_from_source(
        self,
        source: Path,
        cursor_path: Path,
        *,
        max_records: int = 100,
    ) -> int:
        """Stage complete JSONL records, then advance the source cursor.

        DB commit happens before the cursor write.  A crash between them only
        re-reads already-unique message IDs; it cannot lose a record.
        """
        source = source.resolve()
        if not cursor_path.exists():
            raise ConsumerError("source cursor is missing; initialize it explicitly")
        cursor = SourceCursor.from_path(cursor_path)
        if cursor.source_path != str(source):
            raise ConsumerError(
                f"cursor source mismatch: {cursor.source_path} != {source}"
            )
        stat = source.stat()
        if (int(stat.st_dev), int(stat.st_ino)) != (
            cursor.source_device,
            cursor.source_inode,
        ):
            raise ConsumerError(
                "source JSONL inode changed; explicit rotation recovery required"
            )
        if cursor.offset > stat.st_size:
            raise ConsumerError("source JSONL truncated below committed cursor")

        staged: list[tuple[int, int, dict[str, Any]]] = []
        last_message_id = cursor.last_message_id
        with source.open("rb") as handle:
            handle.seek(cursor.offset)
            for _ in range(max(1, max_records)):
                start = handle.tell()
                line = handle.readline()
                if not line:
                    break
                end = handle.tell()
                if not line.endswith(b"\n"):
                    break
                try:
                    decoded = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ConsumerError(
                        f"invalid JSONL at byte {start}: {exc.msg}"
                    ) from exc
                item = _bridge_item(decoded)
                message_id = str(item["messageId"])
                staged.append((start, end, item))
                last_message_id = message_id

        if not staged:
            return 0

        now = _utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for start, end, item in staged:
                conn.execute(
                    """
                    INSERT INTO ingress_events(
                        message_id,chat_id,source_device,source_inode,
                        start_offset,end_offset,raw_json,status,retention_state,
                        retention_updated_at,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?)
                    ON CONFLICT(message_id) DO NOTHING
                    """,
                    (
                        str(item["messageId"]),
                        str(item["chatId"]),
                        cursor.source_device,
                        cursor.source_inode,
                        start,
                        end,
                        json.dumps(item, sort_keys=True, separators=(",", ":")),
                        _initial_retention_state(item),
                        now,
                        now,
                        now,
                    ),
                )
            conn.commit()

        updated = SourceCursor(
            version=cursor.version,
            source_path=cursor.source_path,
            source_device=cursor.source_device,
            source_inode=cursor.source_inode,
            initial_offset=cursor.initial_offset,
            offset=staged[-1][1],
            initialized_at=cursor.initialized_at,
            updated_at=now,
            last_message_id=last_message_id,
        )
        _atomic_write_json(cursor_path, asdict(updated))
        return len(staged)

    def pending(
        self,
        *,
        limit: int = 10,
        priority_chats: frozenset[str] | set[str] | None = None,
    ) -> list[InboxRecord]:
        """Pending records, priority chats first, then source order.

        Management chats jump the drain queue (2026-07-21): a fresh mgmt
        message must not wait behind a thousand-record site backlog for its
        reply. Per-chat ordering stays strictly by seq within each class, and
        chats are independent sessions, so cross-chat reordering is safe.
        """
        priority = sorted(priority_chats or ())
        placeholders = ",".join("?" for _ in priority)
        priority_clause = (
            f"(CASE WHEN chat_id IN ({placeholders}) THEN 0 ELSE 1 END), "
            if priority
            else ""
        )
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT seq,message_id,chat_id,start_offset,end_offset,raw_json "
                "FROM ingress_events WHERE status='pending' "
                f"ORDER BY {priority_clause}seq LIMIT ?",
                (*priority, max(1, limit)),
            ).fetchall()
        return [
            InboxRecord(
                seq=int(row["seq"]),
                message_id=str(row["message_id"]),
                chat_id=str(row["chat_id"]),
                start_offset=int(row["start_offset"]),
                end_offset=int(row["end_offset"]),
                raw=json.loads(row["raw_json"]),
            )
            for row in rows
        ]

    def pending_chat_batches(
        self,
        *,
        batch_size: int,
        priority_chats: frozenset[str] | set[str] | None = None,
        exclude_chats: frozenset[str] | set[str] | None = None,
    ) -> tuple[list[tuple[str, list[InboxRecord]]], list[tuple[str, list[InboxRecord]]]]:
        """Return FIFO batches grouped by chat, split into management/site lanes.

        A chat appears at most once.  Rows stay pending until the scheduler has
        capacity and claims that chat's batch, so no unrelated chat can own or
        strand them.  Ordering is oldest pending row per chat, then row seq.
        """
        size = max(1, int(batch_size))
        priority = set(priority_chats or ())
        excluded = set(exclude_chats or ())
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT seq,message_id,chat_id,start_offset,end_offset,raw_json "
                "FROM ingress_events WHERE status='pending' "
                "AND retention_state IN ('complete','bypassed') ORDER BY seq"
            ).fetchall()
        grouped: dict[str, list[InboxRecord]] = {}
        for row in rows:
            chat_id = str(row["chat_id"])
            if chat_id in excluded:
                continue
            batch = grouped.setdefault(chat_id, [])
            if len(batch) >= size:
                continue
            batch.append(
                InboxRecord(
                    seq=int(row["seq"]),
                    message_id=str(row["message_id"]),
                    chat_id=chat_id,
                    start_offset=int(row["start_offset"]),
                    end_offset=int(row["end_offset"]),
                    raw=json.loads(row["raw_json"]),
                )
            )
        ordered = sorted(grouped.items(), key=lambda item: item[1][0].seq)
        management = [item for item in ordered if item[0] in priority]
        site = [item for item in ordered if item[0] not in priority]
        return management, site

    def newest_pending_for_chats(
        self, chats: frozenset[str] | set[str]
    ) -> list[InboxRecord]:
        """Return one newest pending record from the selected chats."""
        selected = sorted(chats)
        if not selected:
            return []
        placeholders = ",".join("?" for _ in selected)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT seq,message_id,chat_id,start_offset,end_offset,raw_json "
                "FROM ingress_events WHERE status='pending' "
                f"AND chat_id IN ({placeholders}) ORDER BY seq DESC LIMIT 1",
                tuple(selected),
            ).fetchone()
        if row is None:
            return []
        return [
            InboxRecord(
                seq=int(row["seq"]),
                message_id=str(row["message_id"]),
                chat_id=str(row["chat_id"]),
                start_offset=int(row["start_offset"]),
                end_offset=int(row["end_offset"]),
                raw=json.loads(row["raw_json"]),
            )
        ]

    def claim(self, records: Sequence[InboxRecord]) -> None:
        if not records:
            return
        now = _utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for record in records:
                changed = conn.execute(
                    "UPDATE ingress_events SET status='processing', attempts=attempts+1, "
                    "updated_at=? WHERE seq=? AND status='pending'",
                    (now, record.seq),
                ).rowcount
                if changed != 1:
                    raise ConsumerError(f"inbox record {record.seq} was not pending")
            conn.commit()

    def finish(
        self,
        records: Sequence[InboxRecord],
        *,
        status: str,
        pa_turn_id: str | None = None,
        error: str | None = None,
    ) -> None:
        if status not in {"completed", "skipped", "failed"}:
            raise ConsumerError(f"invalid inbox finish status {status}")
        now = _utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for record in records:
                changed = conn.execute(
                    "UPDATE ingress_events SET status=?, pa_turn_id=?, last_error=?, "
                    "updated_at=? WHERE seq=? AND status='processing'",
                    (status, pa_turn_id, (error or "")[:2000] or None, now, record.seq),
                ).rowcount
                if changed != 1:
                    raise ConsumerError(
                        f"inbox record {record.seq} was not processing at finish"
                    )
            conn.commit()

    def requeue(self, records: Sequence[InboxRecord], *, reason: str) -> None:
        """Return an owned batch to pending after graceful interruption."""
        if not records:
            return
        now = _utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for record in records:
                changed = conn.execute(
                    "UPDATE ingress_events SET status='pending', last_error=?, updated_at=? "
                    "WHERE seq=? AND status='processing'",
                    ((reason or "interrupted")[:2000], now, record.seq),
                ).rowcount
                if changed != 1:
                    raise ConsumerError(
                        f"inbox record {record.seq} was not processing at requeue"
                    )
            conn.commit()

    def finish_processed_batch(
        self,
        records: Sequence[InboxRecord],
        *,
        turn_for_message: Mapping[str, str],
    ) -> None:
        """Atomically terminal every record in a processor-evidenced batch."""
        now = _utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for record in records:
                turn_id = turn_for_message.get(record.message_id)
                status = "completed" if turn_id else "skipped"
                changed = conn.execute(
                    "UPDATE ingress_events SET status=?,pa_turn_id=?,last_error=NULL,"
                    "updated_at=? WHERE seq=? AND status='processing'",
                    (status, turn_id, now, record.seq),
                ).rowcount
                if changed != 1:
                    raise ConsumerError(
                        f"inbox record {record.seq} was not processing at batch finish"
                    )
            conn.commit()

    def reconcile_orphan_processing(self, state_db: Path) -> dict[str, int]:
        """Recover prior-process claims using successful pa_turn evidence.

        Completed turn refs are authoritative.  A processing row referenced by
        such a turn becomes completed with that turn id; every other orphan is
        returned to pending.  Nothing is deleted and the row total must remain
        identical across the transaction.
        """
        turn_by_message: dict[str, str] = {}
        if state_db.exists():
            conn = sqlite3.connect(state_db)
            conn.row_factory = sqlite3.Row
            try:
                tables = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if "pa_turns" in tables:
                    rows = conn.execute(
                        "SELECT turn_id,message_refs_json FROM pa_turns "
                        "WHERE turn_status='completed' AND error_json IS NULL "
                        "ORDER BY completed_at"
                    ).fetchall()
                    for row in rows:
                        try:
                            refs = json.loads(row["message_refs_json"] or "[]")
                        except (TypeError, ValueError):
                            refs = []
                        for ref in refs:
                            if ref:
                                turn_by_message[str(ref)] = str(row["turn_id"])
            finally:
                conn.close()

        now = _utc_now()
        completed = 0
        requeued = 0
        with self.connect() as conn:
            before = int(conn.execute("SELECT COUNT(*) FROM ingress_events").fetchone()[0])
            processing = conn.execute(
                "SELECT seq,message_id FROM ingress_events WHERE status='processing'"
            ).fetchall()
            conn.execute("BEGIN IMMEDIATE")
            for row in processing:
                turn_id = turn_by_message.get(str(row["message_id"]))
                if turn_id:
                    conn.execute(
                        "UPDATE ingress_events SET status='completed',pa_turn_id=?,"
                        "last_error=NULL,updated_at=? WHERE seq=? AND status='processing'",
                        (turn_id, now, int(row["seq"])),
                    )
                    completed += 1
                else:
                    conn.execute(
                        "UPDATE ingress_events SET status='pending',"
                        "last_error='startup-orphan-requeued',updated_at=? "
                        "WHERE seq=? AND status='processing'",
                        (now, int(row["seq"])),
                    )
                    requeued += 1
            after = int(conn.execute("SELECT COUNT(*) FROM ingress_events").fetchone()[0])
            if after != before:
                raise ConsumerError(
                    f"inbox conservation failed during recovery: before={before} after={after}"
                )
            conn.commit()
        return {"completed": completed, "requeued": requeued, "total": completed + requeued}

    def counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT status,COUNT(*) AS n FROM ingress_events GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}

    def retention_candidates(self, *, limit: int) -> list[InboxRecord]:
        """Bounded pending-business work, with new rows ahead of retries."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT seq,message_id,chat_id,start_offset,end_offset,raw_json "
                "FROM ingress_events WHERE status='pending' "
                "AND retention_state IN ('pending','held') "
                "ORDER BY CASE retention_state WHEN 'pending' THEN 0 ELSE 1 END, "
                "COALESCE(retention_updated_at,created_at),seq LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [
            InboxRecord(
                seq=int(row["seq"]),
                message_id=str(row["message_id"]),
                chat_id=str(row["chat_id"]),
                start_offset=int(row["start_offset"]),
                end_offset=int(row["end_offset"]),
                raw=json.loads(row["raw_json"]),
            )
            for row in rows
        ]

    def retention_result(self, record: InboxRecord) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT retention_state,retained_media_count "
                "FROM ingress_events WHERE seq=?",
                (record.seq,),
            ).fetchone()
        if row is None:
            raise ConsumerError(f"inbox record {record.seq} disappeared")
        state = str(row["retention_state"])
        if state not in {"complete", "bypassed"}:
            return None
        return {
            "retained": int(row["retained_media_count"]),
            "bytes": 0,
            "operation": state == "complete",
            "durable": True,
        }

    def record_retention(
        self,
        record: InboxRecord,
        *,
        retained: int | None = None,
        bypassed: bool = False,
        error: str | None = None,
    ) -> None:
        now = _utc_now()
        with self.connect() as conn:
            if error is not None:
                conn.execute(
                    "UPDATE ingress_events SET retention_state='held',"
                    "retention_attempts=retention_attempts+1,"
                    "retention_failures=retention_failures+1,"
                    "retention_last_error=?,retention_updated_at=? "
                    "WHERE seq=? AND retention_state IN ('pending','held')",
                    ((error or "retention failed")[:2000], now, record.seq),
                )
                return
            state = "bypassed" if bypassed else "complete"
            changed = conn.execute(
                "UPDATE ingress_events SET retention_state=?,"
                "retained_media_count=?,retention_attempts=retention_attempts+1,"
                "retention_last_error=NULL,retention_updated_at=? "
                "WHERE seq=? AND retention_state IN ('pending','held')",
                (state, max(0, int(retained or 0)), now, record.seq),
            ).rowcount
            if changed == 0:
                row = conn.execute(
                    "SELECT retention_state FROM ingress_events WHERE seq=?",
                    (record.seq,),
                ).fetchone()
                if row is None or str(row[0]) not in {"complete", "bypassed"}:
                    raise ConsumerError(
                        f"inbox record {record.seq} could not record retention outcome"
                    )

    def retention_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(retained_media_count),0),"
                "COALESCE(SUM(retention_failures),0),"
                "COALESCE(SUM(retention_attempts),0),"
                "COALESCE(SUM(CASE WHEN status='pending' "
                "AND retention_state='pending' THEN 1 ELSE 0 END),0),"
                "COALESCE(SUM(CASE WHEN retention_state='complete' THEN 1 ELSE 0 END),0),"
                "COALESCE(SUM(CASE WHEN retention_state='bypassed' THEN 1 ELSE 0 END),0),"
                "COALESCE(SUM(CASE WHEN status IN ('pending','processing') "
                "AND retention_state='held' THEN 1 ELSE 0 END),0) "
                "FROM ingress_events"
            ).fetchone()
        return {
            "retention_total": int(row[0]),
            "retention_failures": int(row[1]),
            "retention_attempts": int(row[2]),
            "retention_pending": int(row[3]),
            "retention_complete": int(row[4]),
            "retention_bypassed": int(row[5]),
            "retention_held": int(row[6]),
        }

    def retention_last_error(self) -> str | None:
        """Newest unresolved retention hold, independent across chat lanes."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT retention_last_error FROM ingress_events "
                "WHERE status IN ('pending','processing') "
                "AND retention_state='held' "
                "ORDER BY retention_updated_at DESC, seq DESC LIMIT 1"
            ).fetchone()
        return str(row[0]) if row else None

    def total(self) -> int:
        return sum(self.counts().values())

    def assert_and_record_conservation(self) -> int:
        """Persist a monotonic row-total high-water mark; deletion hard-aborts."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = int(conn.execute("SELECT COUNT(*) FROM ingress_events").fetchone()[0])
            row = conn.execute(
                "SELECT value FROM ingress_meta WHERE key='state_total_high_water'"
            ).fetchone()
            previous = int(row[0]) if row else 0
            if current < previous:
                raise ConsumerError(
                    "inbox conservation hard-abort: "
                    f"recorded_total={previous} current_total={current}"
                )
            conn.execute(
                "INSERT INTO ingress_meta(key,value) VALUES('state_total_high_water',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(current),),
            )
            conn.commit()
        return current

    def oldest_processing_updated_at(self) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MIN(updated_at) FROM ingress_events WHERE status='processing'"
            ).fetchone()
        return str(row[0]) if row and row[0] else None

    def claim_reply_delivery(
        self, delivery_key: str, *, chat_id: str, reply_to_message_id: str | None
    ) -> bool:
        """Durably claim a reply delivery BEFORE the send (at-most-once).

        Returns True when this call claimed the key. A crash between send and
        the outcome update leaves the claim row behind, which permanently
        refuses a re-send — undelivered-and-loud over retry-into-spam.
        """
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO reply_deliveries(
                    delivery_key,chat_id,reply_to_message_id,status,error,created_at
                ) VALUES(?,?,?,'undelivered','claimed-in-flight',?)
                """,
                (delivery_key, chat_id, reply_to_message_id, _utc_now()),
            )
            return cursor.rowcount == 1

    def record_reply_delivery(
        self,
        delivery_key: str,
        *,
        status: str,
        bridge_message_id: str | None = None,
        provider_outcome: str | None = None,
        error: str | None = None,
    ) -> None:
        if status not in {"delivered", "undelivered"}:
            raise ValueError(f"invalid reply delivery status {status!r}")
        with self.connect() as conn:
            conn.execute(
                "UPDATE reply_deliveries SET status=?, bridge_message_id=?, "
                "provider_outcome=?, error=? "
                "WHERE delivery_key=?",
                (status, bridge_message_id, provider_outcome, error, delivery_key),
            )


def processing_enabled(config_path: Path) -> bool:
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    pa = data.get("pa") if isinstance(data, dict) else None
    return bool(pa.get("enabled")) if isinstance(pa, dict) else False


def _retention_config(config_path: Path) -> dict[str, Any] | None:
    """Return the opt-in generic media-retention contract.

    TGG supplies the values, but the consumer only understands roots and a
    configured business-operation name.  Client/case semantics stay behind
    that operation.
    """
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    pa = data.get("pa") if isinstance(data, Mapping) else None
    raw = pa.get("media_retention") if isinstance(pa, Mapping) else None
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
        return None
    root_value = str(raw.get("media_root") or raw.get("root") or "").strip()
    root = Path(root_value).expanduser()
    source_roots = raw.get("source_roots") or raw.get("allowed_source_roots") or []
    operation = str(raw.get("operation") or raw.get("retention_operation") or "")
    ref_prefix = str(raw.get("media_ref_prefix") or "/media").strip().rstrip("/")
    if not root_value:
        raise MediaRetentionError("media retention root is not configured")
    if isinstance(source_roots, (str, bytes)):
        source_roots = [source_roots]
    if not isinstance(source_roots, Sequence) or not source_roots:
        raise MediaRetentionError("media retention source_roots are not configured")
    if not operation:
        raise MediaRetentionError("media retention operation is not configured")
    if (
        ref_prefix != "/media"
        and (
            not ref_prefix.startswith("/media/")
            or any(part in {"", ".", ".."} for part in ref_prefix.split("/")[2:])
        )
    ):
        raise MediaRetentionError("media retention ref prefix is invalid")
    return {
        "root": root.resolve(),
        "source_roots": tuple(Path(str(p)).expanduser().resolve() for p in source_roots),
        "operation": operation,
        "ref_prefix": ref_prefix,
        "min_free_percent": float(raw.get("min_free_percent", 20)),
    }


def _media_root_metrics(
    config_path: Path, *, inspect: bool, count_root: bool = True
) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "media_root_count": 0,
        "media_root_bytes": 0,
        "media_volume_free_percent": None,
    }
    config = _retention_config(config_path)
    if config is None or not inspect:
        return metrics
    root: Path = config["root"]
    volume_path = root
    while not volume_path.exists() and volume_path != volume_path.parent:
        volume_path = volume_path.parent
    try:
        usage = shutil.disk_usage(volume_path)
    except OSError as exc:
        raise MediaRetentionError(f"media volume is not measurable: {exc}") from exc
    free_percent = (usage.free / usage.total * 100) if usage.total else 0.0
    metrics["media_volume_free_percent"] = round(free_percent, 3)
    if root.exists() and count_root:
        if not root.is_dir():
            raise MediaRetentionError("configured media root is not a directory")
        for directory, _, filenames in os.walk(root, followlinks=False):
            for filename in filenames:
                candidate = Path(directory) / filename
                if candidate.is_file():
                    metrics["media_root_count"] += 1
                    metrics["media_root_bytes"] += candidate.stat().st_size
    return metrics


def _retention_status(
    inbox: DurableInbox, config_path: Path, *, inspect_media: bool
) -> dict[str, Any]:
    return {
        **inbox.retention_counts(),
        **_media_root_metrics(config_path, inspect=inspect_media),
        "retention_hold": inbox.retention_last_error(),
    }


def _assert_media_headroom(config_path: Path, status: Mapping[str, Any]) -> None:
    config = _retention_config(config_path)
    if config is None:
        return
    free = status.get("media_volume_free_percent")
    if free is None:
        raise MediaRetentionError("media volume free space is unknown")
    if float(free) < float(config["min_free_percent"]):
        raise MediaRetentionError(
            "media volume free space below configured floor: "
            f"{float(free):.3f}% < {config['min_free_percent']:.3f}%"
        )


_IMAGE_SIGNATURES: tuple[tuple[str, str, bytes, int], ...] = (
    ("image/jpeg", "jpg", b"\xff\xd8\xff", 0),
    ("image/png", "png", b"\x89PNG\r\n\x1a\n", 0),
    ("image/gif", "gif", b"GIF8", 0),
    ("image/webp", "webp", b"WEBP", 8),
)


def _validated_image_type(path: Path, declared: str | None) -> tuple[str, str]:
    with path.open("rb") as handle:
        prefix = handle.read(16)
    detected = next(
        ((mime, ext) for mime, ext, signature, offset in _IMAGE_SIGNATURES
         if prefix[offset:offset + len(signature)] == signature),
        None,
    )
    if detected is None:
        raise MediaRetentionError(f"retention source is not a supported image: {path.name}")
    mime, ext = detected
    declared = str(declared or "").split(";", 1)[0].strip().lower()
    if declared and "/" in declared and declared != mime:
        # image/jpg is a widespread non-standard spelling for image/jpeg.
        if not (declared == "image/jpg" and mime == "image/jpeg"):
            raise MediaRetentionError(
                f"PROVENANCE_DIVERGENCE: declared MIME {declared} != {mime}"
            )
    return mime, ext


def _contained_existing_file(value: Any, roots: Sequence[Path]) -> Path:
    if isinstance(value, Mapping):
        value = value.get("path") or value.get("filePath") or value.get("localPath") or value.get("url")
    text = str(value or "")
    if text.startswith("file://"):
        text = text[7:]
    try:
        candidate = Path(text).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise MediaRetentionError(f"media source is unavailable: {exc}") from exc
    if not candidate.is_file() or not any(candidate.is_relative_to(root) for root in roots):
        raise MediaRetentionError("media source escapes configured roots or is not a file")
    return candidate


def _event_media(item: Mapping[str, Any]) -> list[tuple[Any, str | None]]:
    values = item.get("mediaUrls") or item.get("media") or item.get("mediaPaths") or []
    if isinstance(values, (str, bytes, Mapping)):
        values = [values]
    if not isinstance(values, Sequence):
        raise MediaRetentionError("event media collection is not a list")
    result: list[tuple[Any, str | None]] = []
    event_mime = item.get("mediaType") or item.get("mimeType")
    event_kind = str(event_mime or "").split("/", 1)[0].strip().lower()
    if event_kind != "image":
        return []
    for value in values:
        mime = (
            value.get("mime") or value.get("mimeType") or value.get("contentType")
            if isinstance(value, Mapping) else event_mime
        )
        declared = str(mime) if mime else None
        kind = str(declared or "").split("/", 1)[0].strip().lower()
        if kind != "image":
            continue
        result.append((value, declared))
    return result


def _converge_retained_media(
    config_path: Path, *, operation: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    from types import SimpleNamespace

    from agent.pa_constitution import resolve_context
    from tools.pa_business_tools import execute_business_operation, load_business_bridge_config

    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    resolved = resolve_context(
        config_data,
        {
            "source": {
                "platform": "whatsapp",
                "chat_id": str(payload.get("chat_jid") or ""),
            }
        },
    )
    if resolved is None:
        raise MediaRetentionError("media retention could not resolve client context")
    # Retention is pre-model ingress infrastructure, not a model-callable job
    # operation. Reuse the resolved tenant/auth/operation registry while
    # deliberately avoiding job-brief allow/deny scope (ops correctly denies
    # the model from invoking this internal write itself).
    internal_context = SimpleNamespace(
        constitution=resolved.constitution,
        job_brief=None,
        job_type="media_retention_internal",
    )
    bridge = load_business_bridge_config(config_data, pa_context=internal_context)
    result = execute_business_operation(bridge, operation=operation, payload=payload)
    data = result.get("data") if isinstance(result, Mapping) else None
    if (
        not isinstance(result, Mapping)
        or result.get("ok") is not True
        or not isinstance(data, Mapping)
        or "ledgerChanged" not in data
        or "observationsChanged" not in data
    ):
        raise MediaRetentionError(
            "media retention convergence returned an invalid Systems envelope"
        )
    return dict(result)


def _retain_record_media_impl(
    record: InboxRecord, *, config_path: Path
) -> dict[str, Any]:
    """Retain one event's images and converge its configured system ledger.

    Files land before the idempotent operation.  Therefore a crash after the
    rename or operation is safe to replay, while changed bytes/MIME at the
    same source ordinal fail closed.
    """
    config = _retention_config(config_path)
    if config is None:
        return {"retained": 0, "bytes": 0, "operation": False}
    item = _bridge_item(record.raw)
    media = _event_media(item)
    if not media:
        coarse_kind = str(
            item.get("mediaType") or item.get("mimeType") or ""
        ).split("/", 1)[0].strip().lower()
        if coarse_kind != "image":
            return {"retained": 0, "bytes": 0, "operation": False}
        if item.get("hasMedia") is True:
            raise MediaRetentionError(
                "mandatory inbound media has no resolvable capture path"
            )
        return {"retained": 0, "bytes": 0, "operation": False}
    chat_id = str(item.get("chatId") or record.chat_id)
    message_id = str(item.get("messageId") or record.message_id)
    if chat_id != record.chat_id or message_id != record.message_id:
        raise MediaRetentionError("PROVENANCE_DIVERGENCE: inbox/event identity mismatch")
    identity_digest = hashlib.sha256(
        (chat_id + "\0" + message_id).encode("utf-8")
    ).hexdigest()
    source_key = f"whatsapp-capture-v1:{identity_digest}"
    filename_prefix = hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:24]
    root: Path = config["root"]
    root.mkdir(parents=True, exist_ok=True, mode=0o750)
    _assert_media_headroom(
        config_path,
        _media_root_metrics(config_path, inspect=True, count_root=False),
    )
    retained: list[dict[str, Any]] = []
    total_bytes = 0
    for ordinal, (raw_path, declared_mime) in enumerate(media):
        source = _contained_existing_file(raw_path, config["source_roots"])
        mime, ext = _validated_image_type(source, declared_mime)
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        target = (root / f"{filename_prefix}_{ordinal}.{ext}").resolve()
        if not target.is_relative_to(root):
            raise MediaRetentionError("derived retention target escapes configured root")
        ordinal_candidates = list(root.glob(f"{filename_prefix}_{ordinal}.*"))
        if ordinal_candidates and target not in ordinal_candidates:
            raise MediaRetentionError(
                f"PROVENANCE_DIVERGENCE: retained ordinal {ordinal} MIME changed"
            )
        if target.exists():
            existing = target.read_bytes()
            existing_mime, _ = _validated_image_type(target, mime)
            if hashlib.sha256(existing).hexdigest() != digest or existing_mime != mime:
                raise MediaRetentionError(
                    f"PROVENANCE_DIVERGENCE: retained ordinal {ordinal} changed"
                )
        else:
            tmp = root / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, target)
                directory_fd = os.open(root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    tmp.unlink()
        total_bytes += len(content)
        retained.append({
            "source_key": source_key,
            "media_ordinal": ordinal,
            "digest": digest,
            "mime": mime,
            "ref": f"{config['ref_prefix']}/{target.name}",
        })

    try:
        _converge_retained_media(
            config_path,
            operation=config["operation"],
            payload={
                "message_id": message_id,
                "source_key": source_key,
                "chat_jid": chat_id,
                "media": retained,
            },
        )
    except Exception as exc:
        if isinstance(exc, MediaRetentionError):
            raise
        raise MediaRetentionError(f"media retention convergence failed: {exc}") from exc
    return {"retained": len(retained), "bytes": total_bytes, "operation": True}


def retain_record_media(record: InboxRecord, *, config_path: Path) -> dict[str, Any]:
    """Normalize fallible retention I/O to the retryable retention class."""
    try:
        return _retain_record_media_impl(record, config_path=config_path)
    except MediaRetentionError:
        raise
    except OSError as exc:
        raise MediaRetentionError(f"media retention I/O failed: {exc}") from exc


def ensure_record_media_retained(
    inbox: DurableInbox, record: InboxRecord, *, config_path: Path
) -> dict[str, Any]:
    """Retain once, with a durable result shared by capture and claim paths."""
    durable = inbox.retention_result(record)
    if durable is not None:
        return durable
    try:
        result = retain_record_media(record, config_path=config_path)
    except MediaRetentionError as exc:
        inbox.record_retention(record, error=f"media-retention-retry: {exc}")
        raise
    inbox.record_retention(
        record,
        retained=int(result["retained"]),
        bypassed=not bool(result.get("operation")),
    )
    return result


async def retain_pending_media(
    inbox: DurableInbox, *, config_path: Path, limit: int
) -> dict[str, int]:
    """Bounded capture-lane retention independent of business processing."""
    summary = {"examined": 0, "retained": 0, "bypassed": 0, "held": 0}
    for record in inbox.retention_candidates(limit=limit):
        summary["examined"] += 1
        try:
            result = await asyncio.to_thread(
                ensure_record_media_retained,
                inbox,
                record,
                config_path=config_path,
            )
        except MediaRetentionError as exc:
            summary["held"] += 1
            print(
                "media retention HELD/PENDING: "
                f"chat={record.chat_id} message={record.message_id} error={exc}",
                file=sys.stderr,
            )
            continue
        if result.get("operation"):
            summary["retained"] += int(result["retained"])
        else:
            summary["bypassed"] += 1
    return summary


def retain_claimed_media(records: Sequence[InboxRecord], *, config_path: Path) -> dict[str, int]:
    totals = {"retained": 0, "bytes": 0, "events": 0}
    for record in records:
        result = retain_record_media(record, config_path=config_path)
        totals["retained"] += int(result["retained"])
        totals["bytes"] += int(result["bytes"])
        totals["events"] += int(bool(result["retained"]))
    return totals


def processing_gate_state(gate_path: Path) -> dict[str, Any]:
    if not gate_path.is_file():
        raise ConsumerError(f"production processing gate is missing: {gate_path}")
    try:
        state = json.loads(gate_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ConsumerError(f"production processing gate is unreadable: {exc}") from exc
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ConsumerError("production processing gate must be a version-1 object")
    if state.get("enabled") not in {True, False}:
        raise ConsumerError("production processing gate enabled must be boolean")
    generation = state.get("generation")
    if not isinstance(generation, int) or generation < 0:
        raise ConsumerError(
            "production processing gate generation must be non-negative"
        )
    return state


def configured_engine(config_path: Path) -> tuple[str, str]:
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    model = data.get("model") if isinstance(data, dict) else None
    if not isinstance(model, dict):
        raise ConsumerError("config.model must be a mapping")
    provider = str(model.get("provider") or "")
    selected = str(model.get("default") or model.get("model") or "")
    if not provider or not selected:
        raise ConsumerError("config model provider/default is incomplete")
    return provider, selected


def _turn_row(
    state_db: Path,
    *,
    replay_run_id: str | None = None,
    started_after: float | None = None,
) -> sqlite3.Row | None:
    if not state_db.exists():
        return None
    conn = sqlite3.connect(state_db)
    conn.row_factory = sqlite3.Row
    try:
        if replay_run_id:
            return conn.execute(
                "SELECT * FROM pa_turns WHERE replay_run_id=? "
                "ORDER BY started_at DESC LIMIT 1",
                (replay_run_id,),
            ).fetchone()
        return conn.execute(
            "SELECT * FROM pa_turns WHERE started_at>=? "
            "ORDER BY started_at DESC LIMIT 1",
            (float(started_after or 0),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def _assert_completed_turn(
    row: sqlite3.Row | None,
    *,
    provider: str,
    model: str,
    require_response: bool,
) -> str:
    if row is None:
        raise ConsumerError("Hermes produced no pa_turn record")
    if str(row["turn_status"]) != "completed":
        raise ConsumerError(
            f"Hermes pa_turn {row['turn_id']} status={row['turn_status']}"
        )
    if str(row["provider"]) != provider or str(row["model"]) != model:
        raise ConsumerError(
            f"Hermes pa_turn engine mismatch: {row['provider']}/{row['model']}"
        )
    if require_response:
        envelope = json.loads(row["raw_turn_envelope_json"] or "{}")
        response = str(envelope.get("final_response") or "").strip()
        if not response:
            raise ConsumerError("Hermes pa_turn completed without a provider response")
    return str(row["turn_id"])


async def process_replay_records(
    records: Sequence[InboxRecord],
    *,
    config_path: Path,
    state_db: Path,
    run_id: str,
) -> dict[str, Any]:
    """Process fixture records through Hermes replay with outbound captured."""
    from gateway.config import load_gateway_config
    from gateway.replay import ReplayPlan
    from gateway.run import GatewayRunner

    provider, model = configured_engine(config_path)
    runner = GatewayRunner(load_gateway_config())
    result = await runner.replay(
        ReplayPlan(
            platform="whatsapp",
            messages=tuple(record.raw for record in records),
            run_id=run_id,
            attempt_id=f"attempt-{uuid.uuid4().hex[:12]}",
            delivery_mode="capture",
            bypass_require_mention=True,
            bypass_auth=True,
            source_path="fixture-only-durable-jsonl-consumer",
        )
    )
    row = _turn_row(state_db, replay_run_id=run_id)
    turn_id = _assert_completed_turn(
        row,
        provider=provider,
        model=model,
        require_response=True,
    )
    return {
        "turn_id": turn_id,
        "provider": provider,
        "model": model,
        "processed": int(result.processed),
        "outbound_captured": len(result.outbound),
        "blocked_commands": len(result.blocked_commands),
    }


def _turn_rows(state_db: Path, *, replay_run_id: str) -> list[sqlite3.Row]:
    if not state_db.exists():
        return []
    conn = sqlite3.connect(state_db)
    conn.row_factory = sqlite3.Row
    try:
        return list(
            conn.execute(
                "SELECT * FROM pa_turns WHERE replay_run_id=? ORDER BY started_at",
                (replay_run_id,),
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


async def process_live_records(
    records: Sequence[InboxRecord],
    *,
    config_path: Path,
    state_db: Path,
    runner: Any | None = None,
) -> dict[str, Any]:
    """Process live durable records through the replay orchestrator.

    This is the eval-proven machinery (turn bundling, passive-debounce parity,
    per-run turn attribution) rather than a hand-rolled per-message path:

    - ``delivery_mode="capture"``: agent responses are captured, never sent —
      outbound remains impossible from this path regardless of trust rung.
    - ``bypass_auth=True``: the observer/ingest scope is governed by the chat
      allowlist + constitution selectors, NOT per-user pairing. Site workers
      are unpaired by design; routing their group messages through the paired
      -user auth gate silently dropped every event and produced the
      2026-07-21 first-light no-turn crash loop.
    - Outcome classification (activation ruling): a message a turn cites is
      turn-produced (completed); a message the pipeline legitimately held —
      mention-gated, debounced into a later bundle, deduped, non-content —
      is consumed-no-turn (skipped), NOT a failure. Only genuine errors
      (turn failed, engine mismatch, orchestrator exception) raise and mark
      the batch failed.
    """
    from gateway.config import load_gateway_config
    from gateway.replay import ReplayPlan
    from gateway.run import GatewayRunner

    provider, model = configured_engine(config_path)
    # Production passes one long-lived runner into every per-chat task.  The
    # optional construction path preserves isolated callers and fixtures.
    runner = runner or GatewayRunner(load_gateway_config())
    run_id = f"live-drain-{uuid.uuid4().hex[:12]}"
    persistent_scope = os.environ.get(
        "TGG_PERSISTENT_CHAT_SESSION_SCOPE", "management"
    ).strip().lower()
    if persistent_scope not in {"off", "management", "all"}:
        raise ConsumerError(
            "TGG_PERSISTENT_CHAT_SESSION_SCOPE must be off, management, or all"
        )
    management_chats = _management_selector_chats(config_path)
    persistent_batch = persistent_scope == "all" or (
        persistent_scope == "management"
        and bool(records)
        and all(record.chat_id in management_chats for record in records)
    )
    replay_namespace = (
        "agent:live-drain:persistent-chat" if persistent_batch else None
    )
    result = await runner.replay(
        ReplayPlan(
            platform="whatsapp",
            messages=tuple(record.raw for record in records),
            run_id=run_id,
            attempt_id=f"attempt-{uuid.uuid4().hex[:12]}",
            delivery_mode="capture",
            bypass_require_mention=True,
            bypass_auth=True,
            live_business_writes=True,
            source_path="durable-jsonl-consumer-live",
            # Every chat is one ongoing conversation. The stable prefix plus
            # SessionStore's existing platform/chat suffix yields one session
            # per chat. Rollout scope is management-only for the demo; setting
            # TGG_PERSISTENT_CHAT_SESSION_SCOPE=all extends the same mechanism
            # to site chats after backlog/autocompact validation.
            replay_namespace=replay_namespace,
        )
    )
    handled: list[dict[str, Any]] = []
    for row in _turn_rows(state_db, replay_run_id=run_id):
        turn_id = _assert_completed_turn(
            row,
            provider=provider,
            model=model,
            require_response=False,
        )
        try:
            refs = json.loads(row["message_refs_json"] or "[]")
        except (TypeError, ValueError, KeyError, IndexError):
            refs = []
        ids = [str(ref) for ref in refs if ref]
        handled.append({"message_ids": ids, "turn_id": turn_id})
    return {
        "provider": provider,
        "model": model,
        "processed": int(result.processed or 0),
        "handled": handled,
        "captured_outbound": [dict(entry) for entry in result.outbound],
        "outbound_sent": 0,
        "submitted_message_ids": [record.message_id for record in records],
    }


def _management_selector_chats(config_path: Path) -> frozenset[str]:
    """WhatsApp chats bound to the tgg_management selector class.

    The reply-delivery decision keys on the SELECTOR of the inbound chat
    (teren 2026-07-21 12:00 ruling) — never on prose or model output.
    Ingest/site-selector chats are structurally absent from this set, so
    their captured responses can never deliver.
    """
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    pa = data.get("pa") if isinstance(data, dict) else None
    constitution_raw = str((pa or {}).get("constitution_path") or "")
    if not constitution_raw:
        return frozenset()
    constitution_path = Path(constitution_raw)
    if not constitution_path.is_file():
        return frozenset()
    constitution = yaml.safe_load(constitution_path.read_text(encoding="utf-8")) or {}
    chats: set[str] = set()
    for selector in constitution.get("selectors") or []:
        if not isinstance(selector, Mapping):
            continue
        match = selector.get("match") or {}
        if (
            selector.get("job_type") == "tgg_management"
            and match.get("source.platform") == "whatsapp"
            and match.get("source.chat_id")
        ):
            chats.add(str(match.get("source.chat_id")))
    return frozenset(chats)


def _parse_captured_send(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """Extract (chat_id, content, reply_to) from a captured adapter send.

    Only plain text ``send`` calls are deliverable; media/confirm/clarify
    kinds stay capture-only.
    """
    if not isinstance(entry, Mapping) or entry.get("kind") != "send":
        return None
    args = list(entry.get("args") or [])
    kwargs = dict(entry.get("kwargs") or {})
    chat_id = str(kwargs.get("chat_id") or (args[0] if len(args) > 0 else "") or "")
    content = str(kwargs.get("content") or (args[1] if len(args) > 1 else "") or "")
    reply_to = kwargs.get("reply_to") if kwargs.get("reply_to") else (
        args[2] if len(args) > 2 else None
    )
    if not chat_id or not content.strip():
        return None
    return {
        "chat_id": chat_id,
        "content": content,
        "reply_to": str(reply_to) if reply_to else None,
    }


def _parse_captured_media(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expand captured native image calls into one bounded item per file."""
    if not isinstance(entry, Mapping):
        return []
    kind = str(entry.get("kind") or "")
    if kind not in {"send_image_file", "send_multiple_images"}:
        return []
    args = list(entry.get("args") or [])
    kwargs = dict(entry.get("kwargs") or {})
    chat_id = str(kwargs.get("chat_id") or (args[0] if args else "") or "")
    reply_to = kwargs.get("reply_to")
    if reply_to is None and kind == "send_image_file" and len(args) > 3:
        reply_to = args[3]
    if not chat_id:
        return []
    if kind == "send_image_file":
        path = kwargs.get("image_path") or (args[1] if len(args) > 1 else None)
        caption = kwargs.get("caption") or (args[2] if len(args) > 2 else None)
        values: Sequence[Any] = [(path, caption)] if path else []
    else:
        values = kwargs.get("images") or (args[1] if len(args) > 1 else [])
        if isinstance(values, (str, bytes, Mapping)):
            values = [values]
    parsed: list[dict[str, Any]] = []
    for ordinal, value in enumerate(values if isinstance(values, Sequence) else []):
        if isinstance(value, Mapping):
            path = value.get("image_path") or value.get("path") or value.get("url")
            caption = value.get("caption") or value.get("alt_text")
        elif isinstance(value, (list, tuple)):
            path = value[0] if value else None
            caption = value[1] if len(value) > 1 else None
        else:
            path, caption = value, None
        text = str(path or "")
        if text.startswith("file://"):
            from urllib.parse import unquote
            text = unquote(text[7:])
        if text:
            parsed.append({
                "send_kind": "media",
                "chat_id": chat_id,
                "path": text,
                "caption": str(caption) if caption else None,
                "reply_to": str(reply_to) if reply_to else None,
                "ordinal": ordinal,
            })
    return parsed


def _timestamp_epoch_seconds(value: Any) -> float | None:
    """Normalize bridge/gate timestamp shapes to epoch seconds."""
    if isinstance(value, Mapping):
        value = value.get("low") or value.get("value") or value.get("seconds")
    if value is None or value == "":
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    # WhatsApp exports may use milliseconds while the replay corpus commonly
    # uses seconds.  Normalize before comparing with the ISO activation gate.
    if numeric >= 100_000_000_000:
        numeric /= 1000
    return numeric


def _fresh_management_chats(
    records: Sequence[InboxRecord],
    *,
    config_path: Path,
    gate_changed_at: str,
) -> frozenset[str]:
    """Return post-activation management chats in this exact processing pick."""
    gate_epoch = _timestamp_epoch_seconds(gate_changed_at)
    if gate_epoch is None:
        raise ConsumerError("processing gate changed_at is not a valid timestamp")
    management_chats = _management_selector_chats(config_path)
    fresh: set[str] = set()
    for record in records:
        if record.chat_id not in management_chats:
            continue
        try:
            item = _bridge_item(record.raw)
        except ConsumerError:
            continue
        timestamp = _timestamp_epoch_seconds(item.get("timestamp"))
        if timestamp is not None and timestamp >= gate_epoch:
            fresh.add(record.chat_id)
    return frozenset(fresh)


def _post_typing_presence(chat_id: str, presence: str) -> bool:
    """Best-effort presence update through the same local bridge."""
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    bridge_url = os.environ.get("TGG_REPLY_BRIDGE_URL", "http://127.0.0.1:3011").rstrip("/")
    request = Request(
        f"{bridge_url}/typing",
        data=json.dumps({"chatId": chat_id, "presence": presence}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            status_code = int(getattr(response, "status", 0) or 0)
            payload = json.loads(response.read() or b"{}")
        if status_code == 200 and payload.get("success") is True:
            return True
        print(
            "typing presence outcome NOT confirmed: "
            f"chat={chat_id} presence={presence} http={status_code} "
            f"payload={json.dumps(payload)[:200]}",
            file=sys.stderr,
        )
    except HTTPError as exc:
        print(
            "typing presence REFUSED/FAILED at bridge: "
            f"chat={chat_id} presence={presence} http={exc.code}",
            file=sys.stderr,
        )
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        print(
            f"typing presence FAILED: chat={chat_id} presence={presence} error={exc}",
            file=sys.stderr,
        )
    return False


@contextlib.asynccontextmanager
async def _management_typing_presence(
    records: Sequence[InboxRecord],
    *,
    config_path: Path,
    gate_changed_at: str,
    reassert_seconds: float = 20,
):
    """Keep fresh management chats composing until processing+delivery ends."""
    chats = _fresh_management_chats(
        records, config_path=config_path, gate_changed_at=gate_changed_at
    )
    if not chats:
        yield
        return
    for chat_id in chats:
        await asyncio.to_thread(_post_typing_presence, chat_id, "composing")
    stop = asyncio.Event()

    async def reassert() -> None:
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=reassert_seconds)
                return
            except asyncio.TimeoutError:
                for chat_id in chats:
                    await asyncio.to_thread(
                        _post_typing_presence, chat_id, "composing"
                    )

    task = asyncio.create_task(reassert())
    try:
        yield
    finally:
        stop.set()
        await task
        for chat_id in chats:
            await asyncio.to_thread(_post_typing_presence, chat_id, "paused")


def deliver_management_replies(
    inbox: DurableInbox,
    *,
    config_path: Path,
    captured_outbound: Sequence[Mapping[str, Any]],
    batch_records: Sequence[InboxRecord],
    gate_changed_at: str,
    handled_groups: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    """Deliver mgmt-selector responses through the rung-gated bridge.

    Contract (teren 2026-07-21 12:00 ruling):
    - delivery keys on the inbound chat's selector class: management only;
      site/ingest responses stay capture-only forever.
    - only anchors cited by completed turns from THIS processing result and
      whose ingress timestamp is at/after the current activation gate may
      deliver; backlog-context output remains captured but suppressed.
    - every send goes through the bridge POST /send — the 4-layer outbound
      stack (policy, authority, lease, guarded transport) is THE enforcement;
      no allowlist logic is duplicated here and a bridge refusal is final.
    - at-most-once per turn response: a durable claim precedes the send; a
      failure logs loudly and records undelivered — never retries.
    """
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError

    summary = {"delivered": 0, "undelivered": 0, "suppressed": 0, "duplicate": 0}
    sends: list[dict[str, Any]] = [
        parsed
        for parsed in (_parse_captured_send(entry) for entry in captured_outbound)
        if parsed is not None
    ]
    for entry in captured_outbound:
        sends.extend(_parse_captured_media(entry))
    if not sends:
        return summary
    gate_epoch = _timestamp_epoch_seconds(gate_changed_at)
    if gate_epoch is None:
        raise ConsumerError("processing gate changed_at is not a valid timestamp")
    management_chats = _management_selector_chats(config_path)
    bridge_url = os.environ.get("TGG_REPLY_BRIDGE_URL", "http://127.0.0.1:3011").rstrip("/")
    newest_message_by_chat: dict[str, str] = {}
    records_by_id: dict[str, InboxRecord] = {}
    for record in batch_records:
        newest_message_by_chat[record.chat_id] = record.message_id
        records_by_id[record.message_id] = record
    handled_message_ids = {
        str(message_id)
        for group in handled_groups
        for message_id in (group.get("message_ids") or [])
        if message_id
    }
    for send in sends:
        chat_id = send["chat_id"]
        if chat_id not in management_chats:
            summary["suppressed"] += 1
            continue
        anchor = send["reply_to"] or newest_message_by_chat.get(chat_id)
        anchor_record = records_by_id.get(str(anchor)) if anchor else None
        try:
            anchor_item = _bridge_item(anchor_record.raw) if anchor_record else None
        except ConsumerError:
            anchor_item = None
        anchor_epoch = _timestamp_epoch_seconds(
            anchor_item.get("timestamp") if anchor_item else None
        )
        if (
            not anchor
            or anchor not in handled_message_ids
            or anchor_record is None
            or anchor_record.chat_id != chat_id
            or anchor_epoch is None
            or anchor_epoch < gate_epoch
        ):
            summary["suppressed"] += 1
            continue
        # Cross-source contract: WhatsApp-native inbound identity only.
        # Export/backfill and live capture may render body/quote/media text
        # differently; content hashing would silently turn those rendering
        # differences into distinct sends.  Once a reply is claimed for an
        # inbound WA message, no second rendering/model response may send it
        # again.  ``anchor`` is the source-native messageId, not replay-1.
        if send.get("send_kind", "text") == "media":
            retention = _retention_config(config_path)
            if retention is None:
                summary["suppressed"] += 1
                continue
            try:
                media_path = Path(send["path"]).expanduser().resolve(strict=True)
                if not media_path.is_file() or not media_path.is_relative_to(retention["root"]):
                    raise MediaRetentionError("captured media path escapes retained-media root")
                media_mime, _ = _validated_image_type(media_path, None)
                media_identity = hashlib.sha256(media_path.read_bytes()).hexdigest()
            except (OSError, MediaRetentionError):
                summary["suppressed"] += 1
                continue
            delivery_key = (
                f"media::{chat_id}::{anchor}::{media_identity}::{send['ordinal']}"
            )
        else:
            delivery_key = f"{chat_id}::{anchor or 'no-anchor'}"
        if not inbox.claim_reply_delivery(
            delivery_key, chat_id=chat_id, reply_to_message_id=anchor
        ):
            summary["duplicate"] += 1
            continue
        body_payload: dict[str, Any] = {
                "chatId": chat_id,
                "replyTo": {"messageId": anchor},
        }
        endpoint = "send"
        if send.get("send_kind", "text") == "media":
            endpoint = "send-media"
            body_payload.update({
                "filePath": str(media_path),
                "mediaType": "image",
            })
            if send.get("caption"):
                body_payload["caption"] = send["caption"]
            # The bridge's media route currently consumes a scalar native id.
            body_payload["replyTo"] = anchor
        else:
            body_payload["message"] = send["content"]
        body = json.dumps(body_payload).encode()
        request = Request(
            f"{bridge_url}/{endpoint}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                status_code = int(getattr(response, "status", 0) or 0)
                payload = json.loads(response.read() or b"{}")
            # Strict success only (codex round): the bridge signals
            # indeterminate sends as HTTP 202 {"outcome":"unknown",
            # "retrySafe":false} — urllib does not raise for 202, and an
            # unknown outcome must NEVER be recorded delivered (nor retried:
            # the claim stays consumed).
            if status_code == 200 and payload.get("success") is True:
                inbox.record_reply_delivery(
                    delivery_key,
                    status="delivered",
                    bridge_message_id=str(payload.get("messageId") or ""),
                    provider_outcome=str(payload.get("outcome") or "delivered"),
                )
                summary["delivered"] += 1
            else:
                print(
                    "reply delivery outcome NOT confirmed: "
                    f"chat={chat_id} anchor={anchor} http={status_code} "
                    f"payload={json.dumps(payload)[:200]}",
                    file=sys.stderr,
                )
                inbox.record_reply_delivery(
                    delivery_key,
                    status="undelivered",
                    provider_outcome=str(payload.get("outcome") or "unconfirmed"),
                    error=f"http-{status_code}-unconfirmed: {json.dumps(payload)[:200]}",
                )
                summary["undelivered"] += 1
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode(errors="replace")[:300]
            except Exception:
                pass
            print(
                "reply delivery REFUSED/FAILED at bridge: "
                f"chat={chat_id} anchor={anchor} http={exc.code} detail={detail}",
                file=sys.stderr,
            )
            inbox.record_reply_delivery(
                delivery_key,
                status="undelivered",
                error=f"http-{exc.code}: {detail}",
            )
            summary["undelivered"] += 1
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            print(
                f"reply delivery FAILED (transport): chat={chat_id} anchor={anchor} error={exc}",
                file=sys.stderr,
            )
            inbox.record_reply_delivery(
                delivery_key, status="undelivered", error=str(exc)[:300]
            )
            summary["undelivered"] += 1
    return summary


def _write_status(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_write_json(path, {"version": 1, "updated_at": _utc_now(), **dict(payload)})


def _new_gateway_runner() -> Any:
    from gateway.config import load_gateway_config
    from gateway.run import GatewayRunner

    return GatewayRunner(load_gateway_config())


async def _process_claimed_chat_batch(
    inbox: DurableInbox,
    records: Sequence[InboxRecord],
    *,
    config_path: Path,
    state_db: Path,
    gate_changed_at: str,
    runner: Any,
) -> None:
    """Claim and process exactly one chat batch through the shared runner."""
    if not records:
        return
    chat_ids = {record.chat_id for record in records}
    if len(chat_ids) != 1:
        raise ConsumerError(f"scheduler produced a mixed-chat batch: {sorted(chat_ids)}")
    inbox.claim(records)
    try:
        # Capture-lane retention normally completed while the row was pending.
        # This claimed-chat path remains the idempotent safety net: a row can
        # never reach Hermes until its durable retention outcome is complete.
        for record in records:
            await asyncio.to_thread(
                ensure_record_media_retained,
                inbox,
                record,
                config_path=config_path,
            )
        async with _management_typing_presence(
            records,
            config_path=config_path,
            gate_changed_at=gate_changed_at,
        ):
            result = await process_live_records(
                records,
                config_path=config_path,
                state_db=state_db,
                runner=runner,
            )
        submitted = {str(value) for value in result.get("submitted_message_ids") or []}
        expected = {record.message_id for record in records}
        if submitted != expected:
            raise ConsumerError(
                "processor evidence did not conserve claimed messages: "
                f"claimed={len(expected)} evidenced={len(submitted)}"
            )
        turn_for_message: dict[str, str] = {}
        for group in result.get("handled") or []:
            turn_id = str(group.get("turn_id") or "")
            if not turn_id:
                continue
            for message_id in group.get("message_ids") or []:
                turn_for_message[str(message_id)] = turn_id
        unknown = set(turn_for_message) - expected
        if unknown:
            raise ConsumerError(
                f"turn evidence referenced messages outside claimed chat batch: {sorted(unknown)}"
            )
        # One transaction: a multi-turn batch can never land partially
        # terminal if the process exits between turn groups.
        inbox.finish_processed_batch(records, turn_for_message=turn_for_message)
    except asyncio.CancelledError:
        inbox.requeue(records, reason="graceful-cancellation")
        raise
    except MediaRetentionError as exc:
        inbox.requeue(records, reason=f"media-retention-retry: {exc}")
        raise
    except Exception as exc:
        inbox.finish(records, status="failed", error=str(exc))
        raise

    # Delivery follows durable ingest completion.  It cannot change inbox
    # terminal state, retry the batch, or consume site-lane capacity.
    try:
        delivery = deliver_management_replies(
            inbox,
            config_path=config_path,
            captured_outbound=result.get("captured_outbound") or [],
            batch_records=records,
            gate_changed_at=gate_changed_at,
            handled_groups=result.get("handled") or [],
        )
        if delivery.get("delivered") or delivery.get("undelivered"):
            print(f"reply deliveries: {delivery}", file=sys.stderr)
    except Exception as exc:
        print(
            f"reply delivery machinery FAILED (ingest unaffected): {exc}",
            file=sys.stderr,
        )


async def run_consumer(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    source = Path(args.source).resolve()
    cursor = Path(args.cursor).resolve()
    inbox = DurableInbox(Path(args.inbox).resolve())
    status_path = Path(args.status_file).resolve()
    gate_path = Path(args.processing_gate).resolve()
    state_db = Path(args.state_db).resolve()
    site_concurrency = max(1, int(getattr(args, "site_concurrency", 4)))
    chat_batch_size = max(1, int(getattr(args, "chat_batch_size", 25)))
    retention_batch_size = max(1, int(getattr(args, "retention_batch_size", 25)))

    with SingletonLock(Path(args.lock_file).resolve()):
        recovery = inbox.reconcile_orphan_processing(state_db)
        expected_total = inbox.assert_and_record_conservation()
        runner: Any | None = None
        tasks: dict[str, asyncio.Task[None]] = {}
        lanes: dict[str, str] = {}
        try:
            while True:
                done_chats = [chat_id for chat_id, task in tasks.items() if task.done()]
                for chat_id in done_chats:
                    task = tasks.pop(chat_id)
                    lanes.pop(chat_id, None)
                    try:
                        await task
                    except MediaRetentionError as exc:
                        # This chat's claimed rows are already pending again.
                        # Keep the other chat lanes and the daemon alive; the
                        # durable status exposes the hold until a retry clears it.
                        print(
                            f"media retention HELD/PENDING: chat={chat_id} error={exc}",
                            file=sys.stderr,
                        )

                config_enabled = processing_enabled(config_path)
                gate = processing_gate_state(gate_path)
                gate_enabled = gate["enabled"] is True
                if not (config_enabled and gate_enabled):
                    if tasks:
                        for task in tasks.values():
                            task.cancel()
                        await asyncio.gather(*tasks.values(), return_exceptions=True)
                        tasks.clear()
                        lanes.clear()
                    counts = inbox.counts()
                    if sum(counts.values()) != expected_total:
                        raise ConsumerError(
                            "inbox conservation failed in standby: "
                            f"expected={expected_total} actual={sum(counts.values())}"
                        )
                    _write_status(
                        status_path,
                        {
                            **_retention_status(
                                inbox, config_path, inspect_media=False
                            ),
                            "state": "standby",
                            "processing_enabled": False,
                            "config_enabled": config_enabled,
                            "gate_enabled": gate_enabled,
                            "gate_generation": int(gate["generation"]),
                            "gate_change_run_id": gate.get("change_run_id"),
                            "pid": os.getpid(),
                            "source_opened": False,
                            "cursor_advanced": False,
                            "scheduler_mode": "per-chat-parallel",
                            "claim_stale_seconds": 1800,
                            "site_concurrency": site_concurrency,
                            "chat_batch_size": chat_batch_size,
                            "retention_batch_size": retention_batch_size,
                            "active_management_chats": [],
                            "active_site_chats": [],
                            "oldest_active_claim": inbox.oldest_processing_updated_at(),
                            "state_total": expected_total,
                            "startup_recovery": recovery,
                            "inbox": counts,
                        },
                    )
                    if args.once:
                        return 0
                    await asyncio.sleep(args.poll_seconds)
                    continue

                retention_status = _retention_status(
                    inbox, config_path, inspect_media=True
                )
                try:
                    _assert_media_headroom(config_path, retention_status)
                except MediaRetentionError as exc:
                    _write_status(
                        status_path,
                        {
                            **retention_status,
                            "state": "held",
                            "processing_enabled": False,
                            "config_enabled": True,
                            "gate_enabled": True,
                            "gate_generation": int(gate["generation"]),
                            "pid": os.getpid(),
                            "source_opened": False,
                            "cursor_advanced": False,
                            "retention_hold": str(exc),
                            "inbox": inbox.counts(),
                        },
                    )
                    raise
                _write_status(
                    status_path,
                    {
                        **retention_status,
                        "state": (
                            "held-pending" if inbox.retention_last_error() else "running"
                        ),
                        "processing_enabled": True,
                        "config_enabled": True,
                        "gate_enabled": True,
                        "gate_generation": int(gate["generation"]),
                        "gate_change_run_id": gate.get("change_run_id"),
                        "pid": os.getpid(),
                        "source_opened": True,
                        "cursor_advanced": False,
                        "scheduler_mode": "per-chat-parallel",
                        "claim_stale_seconds": 1800,
                        "site_concurrency": site_concurrency,
                        "chat_batch_size": chat_batch_size,
                        "retention_batch_size": retention_batch_size,
                        "active_management_chats": sorted(
                            chat for chat, lane in lanes.items() if lane == "management"
                        ),
                        "active_site_chats": sorted(
                            chat for chat, lane in lanes.items() if lane == "site"
                        ),
                        "oldest_active_claim": inbox.oldest_processing_updated_at(),
                        "state_total": expected_total,
                        "startup_recovery": recovery,
                        "inbox": inbox.counts(),
                    },
                )

                before_stage = inbox.total()
                staged = inbox.stage_from_source(
                    source, cursor, max_records=args.max_records
                )
                while staged >= args.max_records:
                    staged = inbox.stage_from_source(
                        source, cursor, max_records=args.max_records
                    )
                after_stage = inbox.total()
                if after_stage < before_stage:
                    raise ConsumerError(
                        "inbox conservation failed during staging: "
                        f"before={before_stage} after={after_stage}"
                    )
                expected_total += after_stage - before_stage
                if inbox.assert_and_record_conservation() != expected_total:
                    raise ConsumerError(
                        "inbox conservation hard-abort after staging: "
                        f"expected={expected_total} actual={inbox.total()}"
                    )

                # Retention is a capture-lane concern, not a model/business
                # concern.  It runs before demo-pause lane selection and never
                # claims or terminals the business row.
                retention_cycle = await retain_pending_media(
                    inbox,
                    config_path=config_path,
                    limit=retention_batch_size,
                )

                try:
                    priority_chats = _management_selector_chats(config_path)
                except Exception:
                    priority_chats = frozenset()
                demo_management_only = os.environ.get(
                    "TGG_DEMO_MANAGEMENT_ONLY", ""
                ).strip().lower() in {"1", "true", "yes", "on"}
                management_batches, site_batches = inbox.pending_chat_batches(
                    batch_size=chat_batch_size,
                    priority_chats=priority_chats,
                    exclude_chats=set(tasks),
                )
                gate_changed_at = str(gate.get("changed_at") or "")
                active_site = sum(1 for lane in lanes.values() if lane == "site")
                available_site = max(0, site_concurrency - active_site)
                selected_site_batches = (
                    [] if demo_management_only else site_batches[:available_site]
                )
                if (management_batches or selected_site_batches) and runner is None:
                    runner = _new_gateway_runner()

                # Reserved management capacity: these tasks never acquire or
                # wait for a site slot.  One task per chat preserves FIFO.
                for chat_id, records in management_batches:
                    tasks[chat_id] = asyncio.create_task(
                        _process_claimed_chat_batch(
                            inbox,
                            records,
                            config_path=config_path,
                            state_db=state_db,
                            gate_changed_at=gate_changed_at,
                            runner=runner,
                        )
                    )
                    lanes[chat_id] = "management"

                if not demo_management_only:
                    for chat_id, records in selected_site_batches:
                        tasks[chat_id] = asyncio.create_task(
                            _process_claimed_chat_batch(
                                inbox,
                                records,
                                config_path=config_path,
                                state_db=state_db,
                                gate_changed_at=gate_changed_at,
                                runner=runner,
                            )
                        )
                        lanes[chat_id] = "site"

                if args.once:
                    if tasks:
                        outcomes = await asyncio.gather(
                            *tasks.values(), return_exceptions=True
                        )
                        for outcome in outcomes:
                            if isinstance(outcome, MediaRetentionError):
                                print(
                                    f"media retention HELD/PENDING: {outcome}",
                                    file=sys.stderr,
                                )
                            elif isinstance(outcome, BaseException):
                                raise outcome
                        tasks.clear()
                        lanes.clear()
                    counts = inbox.counts()
                    if sum(counts.values()) != expected_total:
                        raise ConsumerError(
                            "inbox conservation failed at once boundary: "
                            f"expected={expected_total} actual={sum(counts.values())}"
                        )
                    _write_status(
                        status_path,
                        {
                            **_retention_status(
                                inbox, config_path, inspect_media=True
                            ),
                            "state": (
                                "held-pending" if inbox.retention_last_error() else "running"
                            ),
                            "processing_enabled": True,
                            "config_enabled": True,
                            "gate_enabled": True,
                            "gate_generation": int(gate["generation"]),
                            "gate_change_run_id": gate.get("change_run_id"),
                            "pid": os.getpid(),
                            "staged": after_stage - before_stage,
                            "scheduler_mode": "per-chat-parallel",
                            "claim_stale_seconds": 1800,
                            "site_concurrency": site_concurrency,
                            "chat_batch_size": chat_batch_size,
                            "retention_batch_size": retention_batch_size,
                            "retention_cycle": retention_cycle,
                            "active_management_chats": [],
                            "active_site_chats": [],
                            "oldest_active_claim": inbox.oldest_processing_updated_at(),
                            "state_total": expected_total,
                            "startup_recovery": recovery,
                            "inbox": counts,
                        },
                    )
                    return 0

                counts = inbox.counts()
                if sum(counts.values()) != expected_total:
                    raise ConsumerError(
                        "inbox conservation failed: "
                        f"expected={expected_total} actual={sum(counts.values())}"
                    )
                _write_status(
                    status_path,
                    {
                        **_retention_status(
                            inbox, config_path, inspect_media=True
                        ),
                        "state": (
                            "held-pending" if inbox.retention_last_error() else "running"
                        ),
                        "processing_enabled": True,
                        "config_enabled": True,
                        "gate_enabled": True,
                        "gate_generation": int(gate["generation"]),
                        "gate_change_run_id": gate.get("change_run_id"),
                        "pid": os.getpid(),
                        "staged": after_stage - before_stage,
                        "scheduler_mode": "per-chat-parallel",
                        "claim_stale_seconds": 1800,
                        "site_concurrency": site_concurrency,
                        "chat_batch_size": chat_batch_size,
                        "retention_batch_size": retention_batch_size,
                        "retention_cycle": retention_cycle,
                        "active_management_chats": sorted(
                            chat for chat, lane in lanes.items() if lane == "management"
                        ),
                        "active_site_chats": sorted(
                            chat for chat, lane in lanes.items() if lane == "site"
                        ),
                        "oldest_active_claim": inbox.oldest_processing_updated_at(),
                        "state_total": expected_total,
                        "startup_recovery": recovery,
                        "inbox": counts,
                    },
                )
                await asyncio.sleep(args.poll_seconds)
        except asyncio.CancelledError:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            if inbox.total() != expected_total:
                raise ConsumerError(
                    "inbox conservation failed during graceful cancellation"
                )
            raise


async def run_fixture(args: argparse.Namespace) -> int:
    test_root = Path(args.test_root).resolve()
    source = Path(args.source).resolve()
    cursor = Path(args.cursor).resolve()
    inbox_path = Path(args.inbox).resolve()
    config_path = Path(args.config).resolve()
    state_db = Path(args.state_db).resolve()
    for path in (source, cursor, inbox_path, config_path, state_db):
        if path != test_root and test_root not in path.parents:
            raise ConsumerError(f"fixture path escapes test root: {path}")
    if "/var/lib/tgg-capture" in str(source):
        raise ConsumerError("fixture mode refuses the live capture store")
    if not processing_enabled(config_path):
        raise ConsumerError("fixture config must explicitly enable PA inside test root")

    inbox = DurableInbox(inbox_path)
    initialize_cursor(source, cursor, position="start")
    staged = inbox.stage_from_source(source, cursor, max_records=args.max_records)
    records = inbox.pending(limit=args.max_records)
    if not records:
        raise ConsumerError("fixture source staged no records")
    inbox.claim(records)
    try:
        result = await process_replay_records(
            records,
            config_path=config_path,
            state_db=state_db,
            run_id=args.run_id,
        )
        inbox.finish(records, status="completed", pa_turn_id=result["turn_id"])
    except Exception as exc:
        inbox.finish(records, status="failed", error=str(exc))
        raise
    report = {
        "ok": True,
        "mode": "fixture-only",
        "staged": staged,
        "inbox": inbox.counts(),
        "result": result,
    }
    _atomic_write_json(Path(args.report).resolve(), report)
    print(json.dumps(report, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init-cursor", help="Create the source cursor once")
    init.add_argument("--source", required=True)
    init.add_argument("--cursor", required=True)
    init.add_argument("--position", choices=("start", "end"), required=True)

    run = sub.add_parser("run", help="Run the production standby/consumer loop")
    run.add_argument("--source", required=True)
    run.add_argument("--cursor", required=True)
    run.add_argument("--inbox", required=True)
    run.add_argument("--config", required=True)
    run.add_argument("--state-db", required=True)
    run.add_argument("--processing-gate", required=True)
    run.add_argument("--lock-file", required=True)
    run.add_argument("--status-file", required=True)
    run.add_argument("--poll-seconds", type=float, default=2.0)
    run.add_argument("--max-records", type=int, default=100)
    run.add_argument("--site-concurrency", type=int, default=4)
    run.add_argument("--chat-batch-size", type=int, default=25)
    run.add_argument("--retention-batch-size", type=int, default=25)
    run.add_argument("--once", action="store_true")

    fixture = sub.add_parser(
        "fixture", help="Run the same ingress path against isolated fixture state"
    )
    fixture.add_argument("--test-root", required=True)
    fixture.add_argument("--source", required=True)
    fixture.add_argument("--cursor", required=True)
    fixture.add_argument("--inbox", required=True)
    fixture.add_argument("--config", required=True)
    fixture.add_argument("--state-db", required=True)
    fixture.add_argument("--report", required=True)
    fixture.add_argument("--run-id", required=True)
    fixture.add_argument("--max-records", type=int, default=10)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "init-cursor":
            cursor = initialize_cursor(
                Path(args.source), Path(args.cursor), position=args.position
            )
            print(json.dumps(asdict(cursor), sort_keys=True))
            return 0
        if args.command == "run":
            return asyncio.run(run_consumer(args))
        if args.command == "fixture":
            return asyncio.run(run_fixture(args))
        raise ConsumerError(f"unknown command {args.command}")
    except ConsumerError as exc:
        print(f"consumer error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
