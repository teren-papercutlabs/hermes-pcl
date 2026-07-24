"""Canonical PA message-store writer and retrieval surface.

The physical SQLite database is supplied by the deployment.  This module owns
message row creation regardless of which client database currently contains
the tables.  Callers outside this module may attach processing or retained
media metadata to an existing row, but must never create a message row.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo


class MessageStoreError(RuntimeError):
    """Base error for message-store operations."""


class MessageConflictError(MessageStoreError):
    """Raised when two feeds claim incompatible identities for one message."""

    def __init__(self, reason: str, event: "MessageEvent"):
        self.reason = reason
        self.event = event
        super().__init__(reason)


SOURCE_PRIORITY = {
    "legacy": 0,
    "whatsapp": 1,
    "whatsapp_export": 1,
    "export": 1,
    "history-sync": 2,
    "whatsapp-capture-v1": 2,
    "capture": 3,
}

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
TOKEN_RE = re.compile(r"[\w@.+-]+", re.UNICODE)


@dataclass(frozen=True)
class MessageEvent:
    message_id: str
    source_key: str
    source: str
    source_ref: str
    chat_id: str
    chat_name: str
    sender_id: str | None
    from_me: bool
    timestamp: int
    text: str
    message_kind: str
    has_media: bool
    media_refs: tuple[Any, ...]
    quoted_text: str
    reply_to_source_ref: str | None
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class WriteResult:
    action: str
    message_id: str
    source_key: str
    sources: tuple[str, ...]
    before_image: Mapping[str, Any] | None = None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return decoded if isinstance(decoded, list) else []
    return list(value) if isinstance(value, (list, tuple)) else []


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return dict(value) if isinstance(value, Mapping) else {}


def _bridge_item(value: Mapping[str, Any]) -> dict[str, Any]:
    candidates: list[Mapping[str, Any]] = [value]
    for key in ("normalized", "message", "event", "payload", "data"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            candidates.append(nested)
    for candidate in candidates:
        if candidate.get("messageId") and candidate.get("chatId"):
            return dict(candidate)
    raise MessageStoreError("record has no messageId/chatId item")


def _timestamp(value: Any) -> int:
    if isinstance(value, bool):
        raise MessageStoreError("boolean is not a message timestamp")
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        return int(number)
    text = str(value or "").strip()
    if not text:
        raise MessageStoreError("message timestamp is required")
    if text.replace(".", "", 1).isdigit():
        return _timestamp(float(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MessageStoreError(f"invalid message timestamp: {text!r}") from exc
    if parsed.tzinfo is None:
        raise MessageStoreError("naive message timestamp is forbidden")
    return int(parsed.timestamp())


def normalize_event(record: Mapping[str, Any], *, source: str) -> MessageEvent:
    """Normalize one capture/history record into the canonical writer contract."""
    if source not in SOURCE_PRIORITY:
        raise MessageStoreError(f"unsupported message source: {source}")
    item = _bridge_item(record)
    message_id = str(item.get("messageId") or "").strip()
    chat_id = str(item.get("chatId") or "").strip()
    if not message_id or not chat_id:
        raise MessageStoreError("message identity is incomplete")
    timestamp_value = next(
        (
            item.get(key)
            for key in ("timestamp", "ingressTimestamp", "ingress_ts", "receivedAt", "ts")
            if item.get(key) is not None
        ),
        None,
    )
    media = item.get("mediaUrls") or item.get("mediaPaths") or item.get("media") or []
    if isinstance(media, (str, bytes, Mapping)):
        media = [media]
    media_refs = tuple(media) if isinstance(media, Sequence) else ()
    media_type = str(item.get("mediaType") or item.get("messageKind") or "").strip()
    has_media = bool(item.get("hasMedia") or media_refs or media_type)
    kind = media_type or ("media" if has_media else "text")
    source_key = str(item.get("sourceKey") or f"{source}:{message_id}").strip()
    return MessageEvent(
        message_id=message_id,
        source_key=source_key,
        source=source,
        source_ref=str(item.get("sourceRef") or message_id).strip(),
        chat_id=chat_id,
        chat_name=str(item.get("chatName") or chat_id).strip(),
        sender_id=str(item.get("senderId") or "").strip() or None,
        from_me=bool(item.get("fromMe")),
        timestamp=_timestamp(timestamp_value),
        text=str(item.get("body") or item.get("text") or ""),
        message_kind=kind,
        has_media=has_media,
        media_refs=media_refs,
        quoted_text=str(item.get("quotedText") or ""),
        reply_to_source_ref=str(item.get("quotedMessageId") or "").strip() or None,
        raw=dict(record),
    )


def _is_image(row: Mapping[str, Any]) -> bool:
    if str(row.get("message_kind") or "").split("/", 1)[0].lower() == "image":
        return True
    for item in _json_list(row.get("media_refs")):
        value = item
        if isinstance(item, Mapping):
            mime = str(item.get("mime") or item.get("mimeType") or "")
            if mime.split("/", 1)[0].lower() == "image":
                return True
            value = (
                item.get("path")
                or item.get("filePath")
                or item.get("localPath")
                or item.get("ref")
                or ""
            )
        if Path(str(value)).suffix.lower() in IMAGE_SUFFIXES:
            return True
    return False


def _merge_media(existing: Any, incoming: Sequence[Any]) -> list[Any]:
    merged = _json_list(existing)
    seen = {_json(item) for item in merged}
    for item in incoming:
        key = _json(item)
        if key not in seen:
            merged.append(item)
            seen.add(key)
    return merged


class MessageStore:
    """SQLite-backed canonical message writer and FTS retrieval interface."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path).expanduser().resolve()

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=30)
            conn.execute("PRAGMA query_only=ON")
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS message_ledger (
                  message_id TEXT PRIMARY KEY,
                  source_key TEXT NOT NULL UNIQUE,
                  source TEXT NOT NULL DEFAULT 'legacy',
                  source_ref TEXT NOT NULL,
                  chat_jid TEXT NOT NULL,
                  chat_name TEXT NOT NULL,
                  job_type TEXT,
                  zone TEXT NOT NULL DEFAULT '',
                  channel_type TEXT NOT NULL DEFAULT 'whatsapp',
                  sender_id TEXT,
                  from_me INTEGER NOT NULL,
                  ts INTEGER NOT NULL,
                  sgt TEXT NOT NULL,
                  text TEXT NOT NULL DEFAULT '',
                  message_kind TEXT NOT NULL,
                  has_media INTEGER NOT NULL DEFAULT 0,
                  media_refs TEXT NOT NULL DEFAULT '[]',
                  quoted_text TEXT NOT NULL DEFAULT '',
                  reply_to_source_ref TEXT,
                  raw_json TEXT NOT NULL DEFAULT '{}',
                  turn_id TEXT,
                  turn_ids_json TEXT NOT NULL DEFAULT '[]',
                  processed_at REAL,
                  sources TEXT NOT NULL DEFAULT '[]',
                  source_flags TEXT NOT NULL DEFAULT '{}',
                  source_discrepancy TEXT,
                  in_scope INTEGER NOT NULL DEFAULT 1,
                  ledger_updated_at INTEGER NOT NULL,
                  description TEXT,
                  source_keys_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE INDEX IF NOT EXISTS message_ledger_chat_ts_idx
                  ON message_ledger(chat_jid, ts);
                CREATE INDEX IF NOT EXISTS message_ledger_ts_idx
                  ON message_ledger(ts);
                CREATE TABLE IF NOT EXISTS pa_message_aliases (
                  alias TEXT PRIMARY KEY,
                  message_id TEXT NOT NULL REFERENCES message_ledger(message_id)
                    ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS pa_message_aliases_message_idx
                  ON pa_message_aliases(message_id);
                CREATE TABLE IF NOT EXISTS pa_message_holds (
                  hold_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  source TEXT NOT NULL,
                  message_id TEXT,
                  chat_id TEXT,
                  record_sha256 TEXT NOT NULL,
                  error TEXT NOT NULL,
                  held_at INTEGER NOT NULL,
                  UNIQUE(source, record_sha256)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS pa_message_fts USING fts5(
                  message_id UNINDEXED, text, description, tokenize='unicode61'
                );
                """
            )
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(message_ledger)")
            }
            if "description" not in columns:
                conn.execute("ALTER TABLE message_ledger ADD COLUMN description TEXT")
            if "source_keys_json" not in columns:
                conn.execute(
                    "ALTER TABLE message_ledger ADD COLUMN source_keys_json "
                    "TEXT NOT NULL DEFAULT '[]'"
                )
            conn.execute("DELETE FROM pa_message_fts")
            conn.execute(
                "INSERT INTO pa_message_fts(message_id,text,description) "
                "SELECT message_id,text,coalesce(description,'') FROM message_ledger"
            )
            conn.execute(
                "INSERT OR IGNORE INTO pa_message_aliases(alias,message_id) "
                "SELECT source_key,message_id FROM message_ledger"
            )
            conn.execute(
                "INSERT OR IGNORE INTO pa_message_aliases(alias,message_id) "
                "SELECT message_id,message_id FROM message_ledger"
            )

    def assert_ready(self) -> None:
        """Fail before consumption when the explicit cutover migration is absent."""
        required_tables = {
            "message_ledger",
            "pa_message_aliases",
            "pa_message_holds",
            "pa_message_fts",
        }
        with self.connect(read_only=True) as conn:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                )
            }
            missing = sorted(required_tables - tables)
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(message_ledger)")
            } if "message_ledger" in tables else set()
        missing_columns = sorted({"description", "source_keys_json"} - columns)
        if missing or missing_columns:
            raise MessageStoreError(
                "PA_MESSAGE_STORE_NOT_INITIALIZED: "
                f"missing_tables={missing} missing_columns={missing_columns}"
            )

    def hold_record(
        self, record: Mapping[str, Any], *, source: str, error: Exception
    ) -> WriteResult:
        """Quarantine one bad ingress record without blocking later messages."""
        try:
            item = _bridge_item(record)
        except MessageStoreError:
            item = {}
        message_id = str(item.get("messageId") or "").strip()
        chat_id = str(item.get("chatId") or "").strip()
        digest = hashlib.sha256(_json(record).encode()).hexdigest()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO pa_message_holds(
                  source,message_id,chat_id,record_sha256,error,held_at
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(source,record_sha256) DO UPDATE SET
                  error=excluded.error,held_at=excluded.held_at
                """,
                (
                    source,
                    message_id or None,
                    chat_id or None,
                    digest,
                    f"{type(error).__name__}: {error}"[:2000],
                    int(time.time()),
                ),
            )
        return WriteResult(
            action="held",
            message_id=message_id or f"held:{digest[:24]}",
            source_key="",
            sources=(source,),
        )

    def record_or_hold(
        self, record: Mapping[str, Any], *, source: str
    ) -> WriteResult:
        """Record one event or durably hold an event-level validation conflict."""
        try:
            return self.record_message(record, source=source)
        except MessageStoreError as exc:
            return self.hold_record(record, source=source, error=exc)

    def _row_for_event(
        self, conn: sqlite3.Connection, event: MessageEvent
    ) -> sqlite3.Row | None:
        rows = conn.execute(
            """
            SELECT DISTINCT l.*
              FROM message_ledger l
              LEFT JOIN pa_message_aliases a ON a.message_id=l.message_id
             WHERE l.message_id=? OR l.source_key=? OR a.alias IN (?,?,?)
            """,
            (
                event.message_id,
                event.source_key,
                event.message_id,
                event.source_key,
                event.source_ref,
            ),
        ).fetchall()
        if len(rows) > 1:
            raise MessageConflictError("aliases resolve to multiple messages", event)
        return rows[0] if rows else None

    @staticmethod
    def _sources(row: Mapping[str, Any] | None) -> list[str]:
        values = _json_list(row["sources"]) if row else []
        return [str(value) for value in values if str(value)]

    @staticmethod
    def _priority(source: str) -> int:
        return SOURCE_PRIORITY.get(source, 0)

    def _refresh_fts(
        self, conn: sqlite3.Connection, message_id: str
    ) -> None:
        conn.execute("DELETE FROM pa_message_fts WHERE message_id=?", (message_id,))
        conn.execute(
            "INSERT INTO pa_message_fts(message_id,text,description) "
            "SELECT message_id,text,coalesce(description,'') "
            "FROM message_ledger WHERE message_id=?",
            (message_id,),
        )

    def record_message(
        self,
        record: Mapping[str, Any] | MessageEvent,
        *,
        source: str | None = None,
    ) -> WriteResult:
        """Create/reconcile one row. Capture facts outrank history/export facts."""
        event = (
            record
            if isinstance(record, MessageEvent)
            else normalize_event(record, source=str(source or ""))
        )
        now = int(time.time())
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = self._row_for_event(conn, event)
            before = dict(existing) if existing else None
            if existing and str(existing["chat_jid"]) != event.chat_id:
                raise MessageConflictError("message identity crosses chats", event)
            prior_sources = self._sources(existing)
            sources = sorted(set(prior_sources) | {event.source})
            source_flags = _json_object(existing["source_flags"]) if existing else {}
            source_flags.update({value: True for value in sources})
            aliases = sorted(
                set(_json_list(existing["source_keys_json"]) if existing else [])
                | {event.message_id, event.source_key, event.source_ref}
            )
            media = _merge_media(
                existing["media_refs"] if existing else "[]", event.media_refs
            )
            incoming_wins = (
                existing is None
                or self._priority(event.source)
                >= self._priority(str(existing["source"]))
            )
            if existing is None:
                canonical_message_id = event.message_id
                conn.execute(
                    """
                    INSERT INTO message_ledger(
                      message_id,source_key,source,source_ref,chat_jid,chat_name,
                      sender_id,from_me,ts,sgt,text,message_kind,has_media,
                      media_refs,quoted_text,reply_to_source_ref,raw_json,sources,
                      source_flags,in_scope,ledger_updated_at,source_keys_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        canonical_message_id,
                        event.source_key,
                        event.source,
                        event.source_ref,
                        event.chat_id,
                        event.chat_name,
                        event.sender_id,
                        int(event.from_me),
                        event.timestamp,
                        datetime.fromtimestamp(
                            event.timestamp, tz=ZoneInfo("Asia/Singapore")
                        ).isoformat(),
                        event.text,
                        event.message_kind,
                        int(event.has_media or bool(media)),
                        _json(media),
                        event.quoted_text,
                        event.reply_to_source_ref,
                        _json(event.raw),
                        _json(sources),
                        _json(source_flags),
                        1,
                        now,
                        _json(aliases),
                    ),
                )
                action = "created"
            else:
                canonical_message_id = str(existing["message_id"])
                prior_raw = _json_object(existing["raw_json"])
                incoming_raw = dict(event.raw)
                merged_raw = {**prior_raw, **incoming_raw}
                if "importProvenance" in prior_raw:
                    merged_raw["importProvenance"] = prior_raw["importProvenance"]
                values = {
                    # source_key is a durable external join key. New feed keys
                    # become aliases; they never rewrite the canonical key.
                    "source_key": existing["source_key"],
                    "source": event.source if incoming_wins else existing["source"],
                    "source_ref": event.source_ref if incoming_wins else existing["source_ref"],
                    "chat_name": event.chat_name if incoming_wins else existing["chat_name"],
                    "sender_id": (
                        event.sender_id
                        if incoming_wins and event.sender_id
                        else existing["sender_id"]
                    ),
                    "from_me": int(event.from_me) if incoming_wins else existing["from_me"],
                    "ts": event.timestamp if incoming_wins else existing["ts"],
                    "text": (
                        event.text
                        if incoming_wins and event.text.strip()
                        else existing["text"]
                    ),
                    "message_kind": (
                        event.message_kind if incoming_wins else existing["message_kind"]
                    ),
                    "quoted_text": (
                        event.quoted_text if incoming_wins else existing["quoted_text"]
                    ),
                    "reply_to_source_ref": (
                        event.reply_to_source_ref
                        if incoming_wins
                        else existing["reply_to_source_ref"]
                    ),
                    "raw_json": _json(merged_raw) if incoming_wins else existing["raw_json"],
                }
                conn.execute(
                    """
                    UPDATE message_ledger SET
                      source_key=:source_key,source=:source,source_ref=:source_ref,
                      chat_name=:chat_name,sender_id=:sender_id,from_me=:from_me,
                      ts=:ts,sgt=:sgt,text=:text,message_kind=:message_kind,
                      has_media=:has_media,media_refs=:media_refs,
                      quoted_text=:quoted_text,
                      reply_to_source_ref=:reply_to_source_ref,raw_json=:raw_json,
                      sources=:sources,source_flags=:source_flags,
                      ledger_updated_at=:updated_at,source_keys_json=:aliases
                    WHERE message_id=:message_id
                    """,
                    {
                        **values,
                        "sgt": datetime.fromtimestamp(
                            int(values["ts"]), tz=ZoneInfo("Asia/Singapore")
                        ).isoformat(),
                        "has_media": int(event.has_media or bool(media)),
                        "media_refs": _json(media),
                        "sources": _json(sources),
                        "source_flags": _json(source_flags),
                        "updated_at": now,
                        "aliases": _json(aliases),
                        "message_id": canonical_message_id,
                    },
                )
                action = "repaired" if incoming_wins else "folded"
            for alias in aliases:
                owner = conn.execute(
                    "SELECT message_id FROM pa_message_aliases WHERE alias=?", (alias,)
                ).fetchone()
                if owner and str(owner["message_id"]) != canonical_message_id:
                    raise MessageConflictError("alias belongs to another message", event)
                conn.execute(
                    "INSERT OR IGNORE INTO pa_message_aliases(alias,message_id) VALUES(?,?)",
                    (alias, canonical_message_id),
                )
            self._refresh_fts(conn, canonical_message_id)
            return WriteResult(
                action=action,
                message_id=canonical_message_id,
                source_key=event.source_key,
                sources=tuple(sources),
                before_image=before,
            )

    def set_description_once(self, message_id: str, description: str) -> bool:
        """Persist a photo description exactly once and refresh its FTS row."""
        clean = " ".join(str(description).split()).strip()
        if not clean:
            raise MessageStoreError("empty photo description is forbidden")
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE message_ledger SET description=?,ledger_updated_at=? "
                "WHERE message_id=? AND coalesce(trim(description),'')=''",
                (clean, int(time.time()), message_id),
            )
            if cursor.rowcount:
                self._refresh_fts(conn, message_id)
                return True
            return False

    def pending_image_descriptions(
        self, *, eager_only: bool = False, limit: int = 20
    ) -> list[dict[str, Any]]:
        where = "AND source='capture'" if eager_only else ""
        requested = max(1, min(int(limit), 100))
        with self.connect(read_only=True) as conn:
            rows = conn.execute(
                "SELECT message_id,message_kind,media_refs,source "
                "FROM message_ledger "
                "WHERE has_media=1 AND coalesce(trim(description),'')='' "
                f"{where} ORDER BY ts LIMIT ?",
                (requested * 10,),
            ).fetchall()
        images = [
            {
                "message_id": str(row["message_id"]),
                "media_refs": _json_list(row["media_refs"]),
                "source": str(row["source"]),
            }
            for row in rows
            if _is_image(dict(row))
        ]
        return images[:requested]

    def image_description_candidate(
        self, message_id: str
    ) -> dict[str, Any] | None:
        """Return internal media refs for one undescribed image.

        This internal method is consumed only by the descriptor. Retrieval
        results deliberately omit media paths and raw payloads.
        """
        with self.connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT message_id,message_kind,media_refs,source "
                "FROM message_ledger WHERE message_id=? "
                "AND has_media=1 AND coalesce(trim(description),'')=''",
                (message_id,),
            ).fetchone()
        if not row or not _is_image(dict(row)):
            return None
        return {
            "message_id": str(row["message_id"]),
            "media_refs": _json_list(row["media_refs"]),
            "source": str(row["source"]),
        }

    @staticmethod
    def first_local_image(row: Mapping[str, Any]) -> str | None:
        for item in row.get("media_refs") or []:
            value = item
            if isinstance(item, Mapping):
                value = (
                    item.get("path")
                    or item.get("filePath")
                    or item.get("localPath")
                    or item.get("ref")
                    or ""
                )
            path = Path(str(value)).expanduser()
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                return str(path)
        return None

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = TOKEN_RE.findall(query)
        if not tokens:
            raise MessageStoreError("search query has no searchable terms")
        return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)

    def search(
        self,
        query: str,
        *,
        chat: str | None = None,
        sender: str | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        clauses = ["pa_message_fts MATCH ?"]
        params: list[Any] = [self._fts_query(query)]
        if chat:
            clauses.append("l.chat_jid=?")
            params.append(chat)
        if sender:
            clauses.append("l.sender_id=?")
            params.append(sender)
        if from_ts is not None:
            clauses.append("l.ts>=?")
            params.append(int(from_ts))
        if to_ts is not None:
            clauses.append("l.ts<=?")
            params.append(int(to_ts))
        params.append(max(1, min(int(limit), 50)))
        with self.connect(read_only=True) as conn:
            rows = conn.execute(
                """
                SELECT l.message_id,l.chat_jid,l.chat_name,l.sender_id,l.from_me,
                       l.ts,l.sgt,l.text,l.message_kind,l.has_media,l.description,
                       l.sources,bm25(pa_message_fts) AS score
                  FROM pa_message_fts
                  JOIN message_ledger l ON l.message_id=pa_message_fts.message_id
                 WHERE """
                + " AND ".join(clauses)
                + " ORDER BY score,l.ts LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def context(self, message_id: str, *, window: int = 3) -> list[dict[str, Any]]:
        with self.connect(read_only=True) as conn:
            anchor = conn.execute(
                """
                SELECT l.message_id,l.chat_jid,l.ts
                  FROM message_ledger l
                  LEFT JOIN pa_message_aliases a ON a.message_id=l.message_id
                 WHERE l.message_id=? OR l.source_key=? OR a.alias=?
                 LIMIT 1
                """,
                (message_id, message_id, message_id),
            ).fetchone()
            if not anchor:
                return []
            before = conn.execute(
                """
                SELECT message_id,chat_jid,chat_name,sender_id,from_me,ts,sgt,text,
                       message_kind,has_media,description,sources
                  FROM message_ledger
                 WHERE chat_jid=? AND (ts<? OR (ts=? AND message_id<?))
                 ORDER BY ts DESC,message_id DESC LIMIT ?
                """,
                (
                    anchor["chat_jid"],
                    anchor["ts"],
                    anchor["ts"],
                    anchor["message_id"],
                    max(0, min(int(window), 20)),
                ),
            ).fetchall()
            after = conn.execute(
                """
                SELECT message_id,chat_jid,chat_name,sender_id,from_me,ts,sgt,text,
                       message_kind,has_media,description,sources
                  FROM message_ledger
                 WHERE chat_jid=? AND (ts>? OR (ts=? AND message_id>=?))
                 ORDER BY ts,message_id LIMIT ?
                """,
                (
                    anchor["chat_jid"],
                    anchor["ts"],
                    anchor["ts"],
                    anchor["message_id"],
                    max(1, min(int(window), 20) + 1),
                ),
            ).fetchall()
        return [dict(row) for row in reversed(before)] + [dict(row) for row in after]

    def verification_report(self) -> dict[str, Any]:
        with self.connect(read_only=True) as conn:
            scalar = lambda sql: int(conn.execute(sql).fetchone()[0])
            return {
                "rows": scalar("SELECT COUNT(*) FROM message_ledger"),
                "fts_rows": scalar("SELECT COUNT(*) FROM pa_message_fts"),
                "duplicate_message_ids": scalar(
                    "SELECT COUNT(*) FROM (SELECT message_id FROM message_ledger "
                    "GROUP BY message_id HAVING COUNT(*)>1)"
                ),
                "duplicate_source_keys": scalar(
                    "SELECT COUNT(*) FROM (SELECT source_key FROM message_ledger "
                    "GROUP BY source_key HAVING COUNT(*)>1)"
                ),
                "held_records": scalar("SELECT COUNT(*) FROM pa_message_holds"),
                "undescribed_images": len(self.pending_image_descriptions(limit=100)),
                "integrity_check": str(
                    conn.execute("PRAGMA integrity_check").fetchone()[0]
                ),
            }


def backfill_jsonl(
    store: MessageStore,
    path: Path | str,
    *,
    source: str,
    before_image_sink: Callable[[Mapping[str, Any]], None] | None = None,
    held_sink: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, int]:
    """Replay one JSONL feed through the same canonical writer."""
    counts = {"read": 0, "created": 0, "repaired": 0, "folded": 0, "held": 0}
    with Path(path).expanduser().open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            counts["read"] += 1
            try:
                record = json.loads(line)
                if not isinstance(record, Mapping):
                    raise MessageStoreError("JSONL row is not an object")
                result = store.record_message(record, source=source)
                counts[result.action] += 1
                if result.before_image is not None and before_image_sink:
                    before_image_sink(
                        {
                            "feed": source,
                            "line": line_number,
                            "message_id": result.message_id,
                            "before": result.before_image,
                        }
                    )
            except (json.JSONDecodeError, MessageStoreError, sqlite3.IntegrityError) as exc:
                counts["held"] += 1
                if held_sink:
                    held_sink(
                        {
                            "feed": source,
                            "line": line_number,
                            "error": f"{type(exc).__name__}: {exc}",
                            "record_sha256": hashlib.sha256(line.encode()).hexdigest(),
                        }
                    )
    return counts


async def describe_pending_images(
    store: MessageStore,
    descriptor: Callable[[str], Any],
    *,
    eager_only: bool,
    limit: int = 20,
) -> dict[str, Any]:
    """Describe pending photos once, leaving failures retryable.

    The descriptor receives a validated local image path and may be sync or
    async. No image bytes are returned or persisted in the description layer.
    """
    report: dict[str, Any] = {"selected": 0, "described": 0, "deferred": 0, "errors": []}
    for row in store.pending_image_descriptions(
        eager_only=eager_only, limit=limit
    ):
        report["selected"] += 1
        image_path = store.first_local_image(row)
        if not image_path:
            report["deferred"] += 1
            continue
        try:
            value = descriptor(image_path)
            if inspect.isawaitable(value):
                value = await value
            if store.set_description_once(row["message_id"], str(value)):
                report["described"] += 1
        except Exception as exc:
            report["errors"].append(
                {
                    "message_id": row["message_id"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return report
