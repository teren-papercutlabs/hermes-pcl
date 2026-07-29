"""Workflow engine storage seam and template registrar.

The engine tables are an additive layer beside the kanban task chassis. This
wave exposes the frozen engine API and implements template registration;
runtime event handling is added by later workflow-engine phases.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
import sqlite3

from hermes_cli import kanban_db


@dataclass(frozen=True)
class MatchResult:
    """Result of deterministic event matching."""

    kind: str


@dataclass(frozen=True)
class ApplyResult:
    """Result of applying an event to an instance."""

    applied: bool = False


@dataclass(frozen=True)
class SweepResult:
    """Summary of a workflow intake sweep."""

    processed: int = 0


def register_template(conn: sqlite3.Connection, spec: dict) -> tuple[str, int]:
    """Register a canonical template spec and return its id and version.

    ``conn`` is owned by the caller. The transaction is deliberately routed
    through the kanban database's shared writer so registration participates
    in the same SQLite locking policy as the rest of the board.
    """
    try:
        slug = spec["id"]
    except (KeyError, TypeError):
        raise ValueError("template spec id is required") from None
    if not isinstance(slug, str) or not slug.strip():
        raise ValueError("template spec id is required")

    canonical_spec = json.dumps(
        spec,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    content_hash = hashlib.sha256(canonical_spec.encode("utf-8")).hexdigest()

    with kanban_db.write_txn(conn):
        newest = conn.execute(
            """
            SELECT template_id, version, content_hash
            FROM wf_template
            WHERE slug = ?
            ORDER BY version DESC
            LIMIT 1
            """,
            (slug,),
        ).fetchone()
        if newest is not None and newest[2] == content_hash:
            return newest[0], int(newest[1])

        version = 1 if newest is None else int(newest[1]) + 1
        template_id = f"{slug}@{version}"
        conn.execute(
            """
            INSERT INTO wf_template (
                template_id, slug, version, content_hash, spec, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (template_id, slug, version, content_hash, canonical_spec, int(time.time())),
        )
        return template_id, version


def create_instance(
    conn,
    *,
    template_id,
    entity_key,
    corr: dict,
    vars: dict | None,
    source_event_id: int | None,
) -> str:
    """Create a workflow instance (implemented in a later engine phase)."""
    raise NotImplementedError


def ingest_event(
    conn,
    *,
    source,
    external_id,
    payload: dict | None,
    corr: dict | None,
    event_type: str | None,
) -> int | None:
    """Record an inbound workflow event (implemented in a later phase)."""
    raise NotImplementedError


def match_event(conn, event_id) -> MatchResult:
    """Match an event to an instance (implemented in a later phase)."""
    raise NotImplementedError


def apply_event(conn, event_id, task_id, *, expected_step: str) -> ApplyResult:
    """Apply an event transition (implemented in a later phase)."""
    raise NotImplementedError


def park(conn, task_id, step_key, waits: list[dict]) -> None:
    """Park an instance and arm its waits (implemented in a later phase)."""
    raise NotImplementedError


def advance(conn, task_id, *, to_step, event_id) -> None:
    """Advance an instance (implemented in a later phase)."""
    raise NotImplementedError


def fire_due_timers(conn, now: int) -> list[int]:
    """Emit due timer events (implemented in a later phase)."""
    raise NotImplementedError


def sweep(conn, now: int) -> SweepResult:
    """Re-drive workflow intake (implemented in a later phase)."""
    raise NotImplementedError


__all__ = [
    "ApplyResult",
    "MatchResult",
    "SweepResult",
    "advance",
    "apply_event",
    "create_instance",
    "fire_due_timers",
    "ingest_event",
    "match_event",
    "park",
    "register_template",
    "sweep",
]
