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
import json
import os
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
INBOX_SCHEMA_VERSION = 1


class ConsumerError(RuntimeError):
    """Fail-closed consumer contract violation."""


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
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ingress_events_status_seq_idx
                    ON ingress_events(status, seq);
                """
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
                        start_offset,end_offset,raw_json,status,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,'pending',?,?)
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
                "FROM ingress_events WHERE status='pending' ORDER BY seq"
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

    def total(self) -> int:
        return sum(self.counts().values())

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
        error: str | None = None,
    ) -> None:
        if status not in {"delivered", "undelivered"}:
            raise ValueError(f"invalid reply delivery status {status!r}")
        with self.connect() as conn:
            conn.execute(
                "UPDATE reply_deliveries SET status=?, bridge_message_id=?, error=? "
                "WHERE delivery_key=?",
                (status, bridge_message_id, error, delivery_key),
            )


def processing_enabled(config_path: Path) -> bool:
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    pa = data.get("pa") if isinstance(data, dict) else None
    return bool(pa.get("enabled")) if isinstance(pa, dict) else False


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
    sends = [
        parsed
        for parsed in (_parse_captured_send(entry) for entry in captured_outbound)
        if parsed is not None
    ]
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
        delivery_key = f"{chat_id}::{anchor or 'no-anchor'}"
        if not inbox.claim_reply_delivery(
            delivery_key, chat_id=chat_id, reply_to_message_id=anchor
        ):
            summary["duplicate"] += 1
            continue
        body = json.dumps(
            {
                "chatId": chat_id,
                "message": send["content"],
                "replyTo": {"messageId": anchor},
            }
        ).encode()
        request = Request(
            f"{bridge_url}/send",
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
        handled_by_turn: dict[str, list[InboxRecord]] = {}
        skipped: list[InboxRecord] = []
        for record in records:
            turn_id = turn_for_message.get(record.message_id)
            if turn_id:
                handled_by_turn.setdefault(turn_id, []).append(record)
            else:
                # The native replay path consumed the row but legitimately
                # produced no turn (mention/debounce/dedup/non-content).
                skipped.append(record)
        for turn_id, handled in handled_by_turn.items():
            inbox.finish(handled, status="completed", pa_turn_id=turn_id)
        if skipped:
            inbox.finish(skipped, status="skipped")
    except asyncio.CancelledError:
        inbox.requeue(records, reason="graceful-cancellation")
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

    with SingletonLock(Path(args.lock_file).resolve()):
        recovery = inbox.reconcile_orphan_processing(state_db)
        expected_total = inbox.total()
        runner: Any | None = None
        tasks: dict[str, asyncio.Task[None]] = {}
        lanes: dict[str, str] = {}
        try:
            while True:
                done_chats = [chat_id for chat_id, task in tasks.items() if task.done()]
                for chat_id in done_chats:
                    task = tasks.pop(chat_id)
                    lanes.pop(chat_id, None)
                    await task

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
                            "site_concurrency": site_concurrency,
                            "chat_batch_size": chat_batch_size,
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

                if runner is None:
                    runner = _new_gateway_runner()

                _write_status(
                    status_path,
                    {
                        "state": "running",
                        "processing_enabled": True,
                        "config_enabled": True,
                        "gate_enabled": True,
                        "gate_generation": int(gate["generation"]),
                        "gate_change_run_id": gate.get("change_run_id"),
                        "pid": os.getpid(),
                        "source_opened": True,
                        "cursor_advanced": False,
                        "scheduler_mode": "per-chat-parallel",
                        "site_concurrency": site_concurrency,
                        "chat_batch_size": chat_batch_size,
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
                    active_site = sum(1 for lane in lanes.values() if lane == "site")
                    available_site = max(0, site_concurrency - active_site)
                    for chat_id, records in site_batches[:available_site]:
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
                        await asyncio.gather(*tasks.values())
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
                            "state": "running",
                            "processing_enabled": True,
                            "config_enabled": True,
                            "gate_enabled": True,
                            "gate_generation": int(gate["generation"]),
                            "gate_change_run_id": gate.get("change_run_id"),
                            "pid": os.getpid(),
                            "staged": after_stage - before_stage,
                            "scheduler_mode": "per-chat-parallel",
                            "site_concurrency": site_concurrency,
                            "chat_batch_size": chat_batch_size,
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
                        "state": "running",
                        "processing_enabled": True,
                        "config_enabled": True,
                        "gate_enabled": True,
                        "gate_generation": int(gate["generation"]),
                        "gate_change_run_id": gate.get("change_run_id"),
                        "pid": os.getpid(),
                        "staged": after_stage - before_stage,
                        "scheduler_mode": "per-chat-parallel",
                        "site_concurrency": site_concurrency,
                        "chat_batch_size": chat_batch_size,
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
