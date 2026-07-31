"""Atomic workflow state transitions on top of the kanban chassis.

The workflow tables are deliberately separate from kanban's task lifecycle.
This module owns the workflow rows and drives task status changes through the
existing :mod:`kanban_db` transition functions.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from hermes_cli import kanban_db


class WorkflowInvariantError(RuntimeError):
    """Raised when a workflow instance and its task disagree about state."""


class WorkflowConflictError(RuntimeError):
    """Raised when a workflow stage CAS loses a concurrent update."""


@dataclass(frozen=True)
class MatchResult:
    """Result of deterministic event matching."""

    kind: str
    task_id: str | None = None
    event_id: int | None = None
    match_method: str | None = None
    reason: str | None = None
    candidate_task_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApplyResult:
    """Result of applying an event to an instance."""

    kind: str = "applied"
    task_id: str | None = None
    event_id: int | None = None
    to_step: str | None = None
    reason: str | None = None

    @property
    def applied(self) -> bool:
        """Compatibility convenience for callers of the initial API stub."""

        return self.kind == "applied"


@dataclass(frozen=True)
class SweepResult:
    """Summary of a workflow intake sweep."""

    processed: int = 0
    event_ids: tuple[int, ...] = ()
    matched_ids: tuple[int, ...] = ()
    created_ids: tuple[int, ...] = ()
    buffered_ids: tuple[int, ...] = ()
    unmatched_ids: tuple[int, ...] = ()
    ambiguous_ids: tuple[int, ...] = ()
    superseded_ids: tuple[int, ...] = ()
    needs_review_ids: tuple[int, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        """Return stable per-class counts for callers that do not need ids."""

        return {
            "processed": self.processed,
            "matched": len(self.matched_ids),
            "created": len(self.created_ids),
            "buffered": len(self.buffered_ids),
            "unmatched": len(self.unmatched_ids),
            "ambiguous": len(self.ambiguous_ids),
            "superseded": len(self.superseded_ids),
            "needs_review": len(self.needs_review_ids),
        }


def _now() -> int:
    return int(time.time())


DONE_RETENTION_SECONDS = 14 * 24 * 60 * 60
_TERMINAL_EVENT_STATUSES = frozenset(
    {
        "matched",
        "applied",
        "ambiguous",
        "superseded",
        "needs_review",
        "routed_out",
    }
)
_MATCH_KINDS = frozenset(
    {"matched", "create", "buffered", "unmatched", "ambiguous", "superseded", "needs_review"}
)


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _load_json(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value)


def _template_parts(template_id: str) -> tuple[str, int]:
    if not isinstance(template_id, str) or "@" not in template_id:
        raise ValueError(f"invalid workflow template id: {template_id!r}")
    slug, separator, version = template_id.rpartition("@")
    if not separator or not slug or not version.isdigit():
        raise ValueError(f"invalid workflow template id: {template_id!r}")
    return slug, int(version)


def _load_template(conn: sqlite3.Connection, template_id: str) -> dict:
    slug, version = _template_parts(template_id)
    row = conn.execute(
        "SELECT slug, version, spec FROM wf_template WHERE slug = ? AND version = ?",
        (slug, version),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown workflow template: {template_id}")
    spec = _load_json(row["spec"], None)
    if not isinstance(spec, dict):
        raise ValueError(f"workflow template {template_id} is not an object")
    return spec


def _live_step_mode(
    conn: sqlite3.Connection,
    template_id: str,
    step_key: str,
    pinned_step: dict,
) -> dict:
    """Overlay only the live posture dial onto a pinned structural step."""

    slug, pinned_version = _template_parts(template_id)
    latest = conn.execute(
        """
        SELECT version, spec
          FROM wf_template
         WHERE slug = ?
         ORDER BY version DESC
         LIMIT 1
        """,
        (slug,),
    ).fetchone()
    if latest is None or int(latest["version"]) == pinned_version:
        return pinned_step

    latest_spec = _load_json(latest["spec"], None)
    if not isinstance(latest_spec, dict):
        return pinned_step
    try:
        latest_step = _step(latest_spec, step_key)
    except KeyError:
        # A stable step key is required for a posture overlay. If a later
        # family version removes it, the instance keeps its pinned structure
        # and its pinned mode rather than borrowing a different step.
        return pinned_step

    if "mode" not in latest_step:
        return pinned_step
    live_step = dict(pinned_step)
    live_step["mode"] = latest_step["mode"]
    return live_step


def _workflow_spec(spec: dict) -> dict:
    nested = spec.get("workflow")
    if isinstance(nested, dict):
        # The engine accepts either the stored workflow block or a complete
        # constitution fragment. The registrar still hashes the original.
        return nested
    return spec


def _steps(spec: dict) -> list[dict]:
    steps = _workflow_spec(spec).get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("workflow template must define a non-empty steps list")
    if any(not isinstance(step, dict) or not step.get("key") for step in steps):
        raise ValueError("workflow steps require non-empty key values")
    return steps


def _step(spec: dict, key: str) -> dict:
    for candidate in _steps(spec):
        if candidate["key"] == key:
            return candidate
    raise KeyError(f"unknown workflow step: {key}")


def _first_step(spec: dict) -> dict:
    return _steps(spec)[0]


def _terminal_step(step: dict) -> bool:
    return not any(
        step.get(name)
        for name in ("turn", "actions", "waits", "advance_to")
    )


def _wait_only_step(step: dict) -> bool:
    return bool(step.get("waits")) and not step.get("turn") and not step.get("actions")


def _task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown workflow task: {task_id}")
    return row


def _instance(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM wf_instance WHERE task_id = ?", (task_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown workflow instance: {task_id}")
    return row


def _assert_expected_turn(
    task: sqlite3.Row,
    *,
    expected_step: str | None = None,
    expected_run_id: int | None = None,
) -> None:
    """Reject a stale worker attempting to settle a successor stage run."""

    if expected_step is not None and task["current_step_key"] != expected_step:
        raise WorkflowConflictError(
            f"stale workflow step: expected {expected_step!r}, "
            f"found {task['current_step_key']!r}"
        )
    if expected_run_id is not None:
        current_run_id = task["current_run_id"]
        if (
            task["status"] != "running"
            or current_run_id is None
            or int(current_run_id) != int(expected_run_id)
        ):
            raise WorkflowConflictError(
                f"stale workflow run: expected {expected_run_id}, "
                f"found {current_run_id}"
            )


def _cas_step(
    conn: sqlite3.Connection,
    task_id: str,
    expected_step: str,
    next_step: str,
) -> bool:
    """CAS the reserved task step, including the no-op same-step case."""

    changed = conn.execute(
        """
        UPDATE tasks
           SET current_step_key = ?
         WHERE id = ? AND current_step_key = ?
        """,
        (next_step, task_id, expected_step),
    )
    return changed.rowcount == 1


def _assert_invariant(conn: sqlite3.Connection, task_id: str) -> None:
    """Assert the frozen task-status/workflow-state equivalences."""

    row = conn.execute(
        """
        SELECT t.status AS task_status, i.state AS instance_state
          FROM tasks t
          JOIN wf_instance i ON i.task_id = t.id
         WHERE t.id = ?
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise WorkflowInvariantError(f"missing task or workflow instance: {task_id}")

    status = row["task_status"]
    state = row["instance_state"]
    blocked_states = {"parked", "pending_approval", "needs_review", "exception"}
    valid = (
        (status == "blocked") == (state in blocked_states)
        and (status in {"ready", "running"}) == (state == "advancing")
        and (status == "done") == (state == "done")
        and status in {"blocked", "ready", "running", "done"}
    )
    if not valid:
        raise WorkflowInvariantError(
            f"workflow invariant mismatch for {task_id}: "
            f"task.status={status!r}, wf_instance.state={state!r}"
        )


def register_template(conn: sqlite3.Connection, spec: dict) -> tuple[str, int]:
    """Register a canonical template spec and return its id and version."""

    try:
        slug = spec["id"]
    except (KeyError, TypeError):
        raise ValueError("template spec id is required") from None
    if not isinstance(slug, str) or not slug.strip():
        raise ValueError("template spec id is required")

    # Validate wait contracts before persisting the immutable template.  A
    # malformed timer must fail at registration, not much later inside the
    # gateway watcher after an instance has parked on it.
    workflow = _workflow_spec(spec)
    for step in _steps(spec) if workflow.get("steps") is not None else ():
        waits = step.get("waits") or []
        if not isinstance(waits, list):
            raise ValueError("workflow step waits must be a list")
        for wait in waits:
            if not isinstance(wait, dict):
                raise ValueError("workflow waits must be objects")
            kind, _types, _schema, after, _action = _wait_fields(wait)
            if kind == "timer":
                _timer_at(after, 0)

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
            SELECT slug, version, content_hash
              FROM wf_template
             WHERE slug = ?
             ORDER BY version DESC
             LIMIT 1
            """,
            (slug,),
        ).fetchone()
        if newest is not None and newest[2] == content_hash:
            return f"{slug}@{int(newest[1])}", int(newest[1])

        version = 1 if newest is None else int(newest[1]) + 1
        template_id = f"{slug}@{version}"
        conn.execute(
            """
            INSERT INTO wf_template (slug, version, content_hash, spec, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (slug, version, content_hash, canonical_spec, _now()),
        )
        return template_id, version


def _block_if_needed(conn: sqlite3.Connection, task_id: str, reason: str) -> None:
    before = _task(conn, task_id)
    blocked = kanban_db.block_task(conn, task_id, reason=reason)
    if not blocked and before["status"] != "blocked":
        raise WorkflowConflictError(f"cannot block workflow task: {task_id}")


def _unblock(conn: sqlite3.Connection, task_id: str) -> None:
    before = _task(conn, task_id)
    unblocked = kanban_db.unblock_task(conn, task_id)
    if not unblocked and before["status"] not in {"ready", "running"}:
        raise WorkflowConflictError(f"cannot unblock workflow task: {task_id}")


def create_instance(
    conn,
    *,
    template_id,
    entity_key,
    corr: dict,
    vars: dict | None,
    source_event_id: int | None,
) -> str:
    """Create or retrieve one workflow instance for ``entity_key``."""

    with kanban_db.write_txn(conn):
        existing = conn.execute(
            "SELECT task_id FROM wf_instance WHERE entity_key = ?",
            (entity_key,),
        ).fetchone()
        if existing is not None:
            task_id = existing["task_id"]
            if source_event_id is not None:
                conn.execute(
                    """
                    UPDATE wf_event
                       SET matched_task_id = ?, status = 'matched',
                           match_method = COALESCE(match_method, 'deterministic')
                     WHERE id = ?
                    """,
                    (task_id, int(source_event_id)),
                )
            _assert_invariant(conn, task_id)
            return task_id

        spec = _load_template(conn, template_id)
        first = _first_step(spec)
        first_key = str(first["key"])
        created_task_id = kanban_db.create_task(
            conn,
            title=str(entity_key),
            workspace_kind="scratch",
            idempotency_key=str(entity_key),
            skills=["workflow-worker"],
        )

        # The entity UNIQUE is the authoritative gate. The task helper's
        # idempotency key is still set for chassis-level lookup, but it is not
        # relied upon for concurrent instance creation.
        existing = conn.execute(
            "SELECT task_id FROM wf_instance WHERE entity_key = ?",
            (entity_key,),
        ).fetchone()
        if existing is not None and existing["task_id"] != created_task_id:
            task_id = existing["task_id"]
            if source_event_id is not None:
                conn.execute(
                    "UPDATE wf_event SET matched_task_id = ?, status = 'matched' "
                    "WHERE id = ?",
                    (task_id, int(source_event_id)),
                )
            _assert_invariant(conn, task_id)
            return task_id

        task_id = created_task_id
        conn.execute(
            """
            UPDATE tasks
               SET workflow_template_id = ?, current_step_key = ?
             WHERE id = ?
            """,
            (template_id, first_key, task_id),
        )
        # A new instance enters its first stage as an advancing/ready task.
        # Wait-only entry stages are parked below through the same park path
        # used by worker-stage transitions, so their wait rows are armed too.
        state = "advancing"
        if _terminal_step(first):
            state = "done"
        conn.execute(
            """
            INSERT INTO wf_instance (
                task_id, entity_key, template_id, template_version,
                corr, vars, state, parked_since
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                str(entity_key),
                template_id,
                _template_parts(template_id)[1],
                _json(corr) or "{}",
                _json(vars if vars is not None else {}),
                state,
                None,
            ),
        )
        if source_event_id is not None:
            conn.execute(
                """
                UPDATE wf_event
                   SET matched_task_id = ?, status = 'matched',
                       match_method = COALESCE(match_method, 'deterministic')
                 WHERE id = ?
                """,
                (task_id, int(source_event_id)),
            )

        if state == "done":
            if not kanban_db.complete_task(conn, task_id):
                raise WorkflowConflictError(f"cannot complete workflow task: {task_id}")
        elif _wait_only_step(first):
            park(conn, task_id, first_key, list(first.get("waits") or []))
        _assert_invariant(conn, task_id)
        _rematch_held_events(
            conn,
            task_id=task_id,
            now=_now(),
            exclude_event_id=source_event_id,
        )
        return task_id


def ingest_event(
    conn,
    *,
    source,
    external_id,
    payload: dict | None,
    corr: dict | None,
    event_type: str | None,
) -> int | None:
    """Ledger one inbound event; exact source/external id redelivery is a no-op."""

    with kanban_db.write_txn(conn):
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO wf_event (
                source, external_id, event_type, payload, corr, status, created_at
            ) VALUES (?, ?, ?, ?, ?, 'received', ?)
            """,
            (
                str(source),
                external_id,
                event_type,
                _json(payload),
                _json(corr),
                _now(),
            ),
        )
        if cursor.rowcount == 0:
            return None
        return int(cursor.lastrowid)


def _wait_fields(wait: dict) -> tuple[str, list[str] | None, str | None, int | None, str | None]:
    allowed = {
        "kind", "types", "schema", "after", "action", "max_fires", "then",
        "advance_to",
    }
    unknown = set(wait) - allowed
    if unknown:
        raise ValueError(f"unknown workflow wait fields: {sorted(unknown)}")
    kind = wait.get("kind")
    if kind not in {"event", "timer"}:
        raise ValueError(f"workflow wait kind must be event or timer: {kind!r}")
    types = wait.get("types")
    if types is not None:
        if isinstance(types, str):
            types = [types]
        if not isinstance(types, list) or any(not isinstance(item, str) for item in types):
            raise ValueError("workflow wait types must be a list of strings")
        types = list(types)
    schema = wait.get("schema")
    if schema is not None and not isinstance(schema, str):
        raise ValueError("workflow wait schema must be a string")
    action = wait.get("action")
    if action is not None and not isinstance(action, str):
        raise ValueError("workflow timer action must be a string")
    if kind == "timer" and wait.get("max_fires") is not None:
        try:
            max_fires = int(wait["max_fires"])
        except (TypeError, ValueError) as exc:
            raise ValueError("workflow timer max_fires must be an integer") from exc
        if max_fires < 1:
            raise ValueError("workflow timer max_fires must be at least one")
        if action != "chase" and max_fires > 1:
            raise ValueError("repeating workflow timers must use action='chase'")
    return kind, types, schema, wait.get("after"), action


def _duration(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("timer duration must be numeric or a duration string")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip().lower()
        units = (("days", 86400), ("day", 86400), ("hours", 3600), ("hour", 3600),
                 ("h", 3600), ("minutes", 60), ("minute", 60), ("m", 60),
                 ("seconds", 1), ("second", 1), ("s", 1))
        for suffix, multiplier in units:
            if text.endswith(suffix):
                return int(float(text[:-len(suffix)].strip()) * multiplier)
    raise ValueError(f"invalid timer duration: {value!r}")


def _timer_at(after: Any, now: int) -> int:
    if after is None:
        raise ValueError("timer wait requires after")
    return now + _duration(after)


def _invalidate_waits(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute(
        """
        UPDATE wf_wait
           SET status = 'superseded', resume_token = NULL
         WHERE task_id = ? AND status = 'armed'
        """,
        (task_id,),
    )


def _invalidate_approvals(conn: sqlite3.Connection, task_id: str) -> None:
    """Remove every still-live approval capability for a task.

    Approval links are bearer capabilities just like wait links.  Keeping one
    alive after a stage has moved would let an old screen act on a successor
    stage, so expiry is deliberately broad rather than tied to one approval.
    """

    conn.execute(
        """
        UPDATE wf_approval
           SET status = 'expired', resume_token = ?
         WHERE task_id = ? AND status = 'pending' AND resume_token IS NOT NULL
        """,
        (secrets.token_urlsafe(32), task_id),
    )


def park(conn, task_id, step_key, waits: list[dict]) -> None:
    """Atomically replace the current waits and block the task."""

    if not isinstance(waits, list):
        raise ValueError("waits must be a list")
    with kanban_db.write_txn(conn):
        task = _task(conn, task_id)
        _instance(conn, task_id)
        if not _cas_step(conn, task_id, step_key, step_key):
            raise WorkflowConflictError(
                f"workflow stage changed while parking {task_id}: {step_key}"
            )
        _invalidate_waits(conn, task_id)
        created_at = _now()
        for wait in waits:
            if not isinstance(wait, dict):
                raise ValueError("workflow waits must be objects")
            kind, types, schema, after, action = _wait_fields(wait)
            is_timer = kind == "timer"
            conn.execute(
                """
                INSERT INTO wf_wait (
                    task_id, step_key, kind, event_types, schema_ref,
                    timer_at, timer_action, fires_used, resume_token,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 'armed', ?)
                """,
                (
                    task_id,
                    step_key,
                    kind,
                    _json(types) if not is_timer else None,
                    schema if not is_timer else None,
                    _timer_at(after, created_at) if is_timer else None,
                    action if is_timer else None,
                    secrets.token_urlsafe(32),
                    created_at,
                ),
            )
        conn.execute(
            "UPDATE wf_instance SET state = 'parked', parked_since = ? WHERE task_id = ?",
            (created_at, task_id),
        )
        _block_if_needed(conn, task_id, f"workflow parked at {step_key}")
        _assert_invariant(conn, task_id)


def _event_wait(
    conn: sqlite3.Connection,
    task_id: str,
    step_key: str,
    event: sqlite3.Row,
    spec: dict,
) -> tuple[sqlite3.Row, dict] | None:
    event_type = event["event_type"]
    source = event["source"]
    step = _step(spec, step_key)
    declared = step.get("waits") or []
    rows = conn.execute(
        """
        SELECT * FROM wf_wait
         WHERE task_id = ? AND step_key = ? AND status = 'armed'
         ORDER BY id
        """,
        (task_id, step_key),
    ).fetchall()
    for row in rows:
        for wait in declared:
            if not isinstance(wait, dict):
                continue
            kind, types, schema, _after, action = _wait_fields(wait)
            if kind != row["kind"]:
                continue
            if kind == "event":
                row_types = _load_json(row["event_types"], []) or []
                if event_type not in row_types or (types and event_type not in types):
                    continue
            else:
                if source != "timer":
                    continue
                if action and event_type not in {None, action}:
                    continue
            if schema is not None and row["schema_ref"] != schema:
                continue
            if wait.get("advance_to") is None and not step.get("advance_to"):
                raise ValueError(f"workflow wait has no advance_to: {step_key}")
            return row, wait
    return None


def apply_event(conn, event_id, task_id, *, expected_step: str) -> ApplyResult:
    """Apply one compatible event with stage-CAS serialization."""

    with kanban_db.write_txn(conn):
        # The first stateful operation is the stage CAS. A stale caller must
        # return to correlation and must never turn a race into an error.
        if not _cas_step(conn, task_id, expected_step, expected_step):
            return ApplyResult(
                kind="re_correlate",
                task_id=task_id,
                event_id=int(event_id),
                reason="current step changed",
            )

        event = conn.execute(
            "SELECT * FROM wf_event WHERE id = ?", (int(event_id),)
        ).fetchone()
        if event is None:
            raise KeyError(f"unknown workflow event: {event_id}")
        instance = _instance(conn, task_id)
        spec = _load_template(conn, instance["template_id"])
        prior = conn.execute(
            """
            SELECT 1 FROM wf_transition
             WHERE task_id = ? AND step_key = ? AND event_id = ?
            """,
            (task_id, expected_step, int(event_id)),
        ).fetchone()
        if prior is not None:
            return ApplyResult(
                kind="duplicate", task_id=task_id, event_id=int(event_id)
            )

        matched = _event_wait(conn, task_id, expected_step, event, spec)
        if matched is None:
            return ApplyResult(
                kind="unmatched", task_id=task_id, event_id=int(event_id),
                reason="no compatible armed wait",
            )
        wait_row, wait_spec = matched
        if not _typed_payload_is_valid(event, wait_row["schema_ref"]):
            _record_match(
                conn,
                int(event_id),
                "needs_review",
                task_id,
                method=event["match_method"] or "deterministic",
                reason="payload is not an object for the declared typed wait",
            )
            return ApplyResult(
                kind="needs_review",
                task_id=task_id,
                event_id=int(event_id),
                reason="payload is not an object for the declared typed wait",
            )
        step_spec = _step(spec, expected_step)
        to_step = wait_spec.get("advance_to") or step_spec.get("advance_to")
        if not to_step:
            raise ValueError(f"workflow event wait has no target: {expected_step}")
        _step(spec, str(to_step))

        inserted = conn.execute(
            """
            INSERT OR IGNORE INTO wf_transition
                (task_id, step_key, to_step, event_id, applied_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, expected_step, str(to_step), int(event_id), _now()),
        )
        if inserted.rowcount == 0:
            return ApplyResult(
                kind="duplicate", task_id=task_id, event_id=int(event_id)
            )

        payload = _load_json(event["payload"], {})
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ValueError("workflow event payload must be an object")
        values = _load_json(instance["vars"], {}) or {}
        if not isinstance(values, dict):
            raise ValueError("workflow instance vars must be an object")
        values.update(payload)

        conn.execute(
            """
            UPDATE wf_wait
               SET status = CASE WHEN id = ? THEN 'satisfied' ELSE 'superseded' END,
                   resume_token = NULL
             WHERE task_id = ? AND step_key = ? AND status = 'armed'
            """,
            (int(wait_row["id"]), task_id, expected_step),
        )
        _invalidate_approvals(conn, task_id)
        conn.execute(
            """
            UPDATE wf_event
               SET status = 'applied', matched_task_id = ?,
                   match_method = COALESCE(match_method, 'deterministic'),
                   applied_at = ?
             WHERE id = ?
            """,
            (task_id, _now(), int(event_id)),
        )
        conn.execute(
            """
            UPDATE wf_instance
               SET state = 'advancing', parked_since = NULL, vars = ?
             WHERE task_id = ?
            """,
            (_json(values), task_id),
        )
        if not _cas_step(conn, task_id, expected_step, str(to_step)):
            raise WorkflowConflictError(f"workflow stage changed during apply: {task_id}")
        _unblock(conn, task_id)
        _assert_invariant(conn, task_id)
        return ApplyResult(
            kind="applied", task_id=task_id, event_id=int(event_id),
            to_step=str(to_step),
        )


def advance(conn, task_id, *, to_step, event_id) -> None:
    """Commit a transition through the frozen engine API.

    Correlator and coordinator callers may use this primitive to apply a
    template-valid transition.  Worker turns use :func:`advance_stage_turn`,
    which additionally fences the run and enforces its declared target.
    """

    _advance(
        conn,
        task_id,
        to_step=to_step,
        event_id=event_id,
        expected_step=None,
        expected_run_id=None,
        enforce_declared_target=False,
    )


def advance_stage_turn(
    conn,
    task_id,
    *,
    to_step,
    event_id,
    expected_step: str,
    expected_run_id: int,
) -> None:
    """Commit a fenced worker-stage transition to its declared target."""

    _advance(
        conn,
        task_id,
        to_step=to_step,
        event_id=event_id,
        expected_step=expected_step,
        expected_run_id=expected_run_id,
        enforce_declared_target=True,
    )


def _advance(
    conn,
    task_id,
    *,
    to_step,
    event_id,
    expected_step: str | None,
    expected_run_id: int | None,
    enforce_declared_target: bool,
) -> None:
    with kanban_db.write_txn(conn):
        task = _task(conn, task_id)
        _assert_expected_turn(
            task,
            expected_step=expected_step,
            expected_run_id=expected_run_id,
        )
        instance = _instance(conn, task_id)
        leaving_parked_state = instance["state"] in {"parked", "pending_approval"}
        spec = _load_template(conn, instance["template_id"])
        current_step = task["current_step_key"]
        if not current_step:
            raise WorkflowConflictError(f"workflow task has no current step: {task_id}")
        current_spec = _step(spec, str(current_step))
        declared_target = current_spec.get("advance_to")
        if enforce_declared_target and str(to_step) != str(declared_target):
            raise WorkflowConflictError(
                f"workflow step {current_step!r} may advance only to "
                f"{declared_target!r}, not {to_step!r}"
            )
        next_step = _step(spec, str(to_step))
        duplicate = conn.execute(
            """
            SELECT 1 FROM wf_transition
             WHERE task_id = ? AND event_id = ? AND to_step = ?
            """,
            (task_id, int(event_id), str(to_step)),
        ).fetchone()
        if duplicate is not None:
            return
        if leaving_parked_state:
            _invalidate_waits(conn, task_id)
            _invalidate_approvals(conn, task_id)
        if not _cas_step(conn, task_id, str(current_step), str(to_step)):
            raise WorkflowConflictError(f"workflow stage changed: {task_id}")
        conn.execute(
            """
            INSERT INTO wf_transition (task_id, step_key, to_step, event_id, applied_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, str(current_step), str(to_step), int(event_id), _now()),
        )
        if _terminal_step(next_step):
            _invalidate_waits(conn, task_id)
            conn.execute(
                "UPDATE wf_instance SET state = 'done', parked_since = NULL WHERE task_id = ?",
                (task_id,),
            )
            if not kanban_db.complete_task(conn, task_id):
                raise WorkflowConflictError(f"cannot complete workflow task: {task_id}")
        elif _wait_only_step(next_step):
            conn.execute(
                "UPDATE wf_instance SET state = 'advancing', parked_since = NULL WHERE task_id = ?",
                (task_id,),
            )
            # park() deliberately composes through the same nested savepoint
            # path as all other kanban transitions.
            park(conn, task_id, str(to_step), list(next_step.get("waits") or []))
        else:
            conn.execute(
                "UPDATE wf_instance SET state = 'advancing', parked_since = NULL WHERE task_id = ?",
                (task_id,),
            )
            if task["status"] == "running":
                if not kanban_db.complete_stage_turn(
                    conn,
                    task_id,
                    summary=f"workflow stage {current_step} advanced to {to_step}",
                ):
                    raise WorkflowConflictError(
                        f"cannot close workflow stage turn: {task_id}"
                    )
            elif task["status"] == "blocked":
                _unblock(conn, task_id)
            elif task["status"] != "ready":
                raise WorkflowConflictError(
                    f"cannot advance workflow task from status {task['status']!r}"
                )
        _assert_invariant(conn, task_id)
    _rematch_held_events(
        conn,
        task_id=task_id,
        now=_now(),
        exclude_event_id=event_id,
    )


def _stage_event(conn, task_id: str, step_key: str) -> sqlite3.Row | None:
    """Return the structured event that caused the current stage turn.

    A transition is the strongest link because it binds the event to the
    stage being entered.  The first stage has no transition, so it falls back
    to the newest event matched to the instance (normally ``create_on``).
    """

    row = conn.execute(
        """
        SELECT e.*
          FROM wf_transition tr
          JOIN wf_event e ON e.id = tr.event_id
         WHERE tr.task_id = ? AND tr.to_step = ?
         ORDER BY tr.applied_at DESC, e.id DESC
         LIMIT 1
        """,
        (task_id, step_key),
    ).fetchone()
    if row is not None:
        return row
    return conn.execute(
        """
        SELECT *
          FROM wf_event
         WHERE matched_task_id = ?
         ORDER BY id DESC
         LIMIT 1
        """,
        (task_id,),
    ).fetchone()


def context(conn, task_id: str) -> dict:
    """Return the step-scoped, tenant-neutral context for one stage turn.

    The worker receives only the current structural step, live posture dial,
    accumulated structured variables, declared correlation values, and the
    structured event that opened this turn.  The full template and raw inbound
    bodies never enter the worker prompt.
    """

    task = _task(conn, task_id)
    instance = _instance(conn, task_id)
    spec = _load_template(conn, instance["template_id"])
    step = _step(spec, task["current_step_key"])
    step = _live_step_mode(
        conn,
        instance["template_id"],
        task["current_step_key"],
        step,
    )
    event = _stage_event(conn, task_id, str(task["current_step_key"]))
    event_context = None
    if event is not None:
        event_context = {
            "id": int(event["id"]),
            "source": event["source"],
            "event_type": event["event_type"],
            "payload": _load_json(event["payload"], {}),
            "corr": _load_json(event["corr"], {}),
        }
    return {
        "task_id": task_id,
        "template_id": instance["template_id"],
        "entity_key": instance["entity_key"],
        "corr": _load_json(instance["corr"], {}),
        "vars": _load_json(instance["vars"], {}),
        "state": instance["state"],
        "step": step,
        "event": event_context,
    }


def correlation(conn, task_id: str) -> dict:
    """Return the authoritative correlation scope for one workflow instance."""

    return _load_json(_instance(conn, task_id)["corr"], {})


def propose(
    conn,
    task_id: str,
    action: str,
    payload: dict,
    *,
    expected_step: str | None = None,
    expected_run_id: int | None = None,
) -> int:
    """Persist a complete approval proposal and park the instance."""

    with kanban_db.write_txn(conn):
        task = _task(conn, task_id)
        _assert_expected_turn(
            task,
            expected_step=expected_step,
            expected_run_id=expected_run_id,
        )
        instance = _instance(conn, task_id)
        token = secrets.token_urlsafe(32)
        approval = conn.execute(
            """
            INSERT INTO wf_approval (
                task_id, step_key, action, payload, status, resume_token, created_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                task_id,
                task["current_step_key"],
                str(action),
                _json(payload) if isinstance(payload, dict) else str(payload),
                token,
                _now(),
            ),
        )
        conn.execute(
            "UPDATE wf_instance SET state = 'pending_approval', parked_since = ? WHERE task_id = ?",
            (_now(), task_id),
        )
        _block_if_needed(conn, task_id, f"workflow proposal pending at {task['current_step_key']}")
        _assert_invariant(conn, task_id)
        return int(approval.lastrowid)


def review(
    conn,
    task_id: str,
    reason: str,
    options: Any = None,
    *,
    expected_step: str | None = None,
    expected_run_id: int | None = None,
) -> None:
    """Move a workflow instance into the human review state."""

    del options  # Reserved for the later adapter; no schema column is added.
    with kanban_db.write_txn(conn):
        task = _task(conn, task_id)
        _assert_expected_turn(
            task,
            expected_step=expected_step,
            expected_run_id=expected_run_id,
        )
        _instance(conn, task_id)
        conn.execute(
            "UPDATE wf_instance SET state = 'needs_review', parked_since = ? WHERE task_id = ?",
            (_now(), task_id),
        )
        _block_if_needed(conn, task_id, str(reason))
        _assert_invariant(conn, task_id)


def exception(
    conn,
    task_id: str,
    reason: str,
    *,
    expected_step: str | None = None,
    expected_run_id: int | None = None,
) -> None:
    """Move a workflow instance into the resumable exception state."""

    with kanban_db.write_txn(conn):
        task = _task(conn, task_id)
        _assert_expected_turn(
            task,
            expected_step=expected_step,
            expected_run_id=expected_run_id,
        )
        _instance(conn, task_id)
        conn.execute(
            "UPDATE wf_instance SET state = 'exception', parked_since = ? WHERE task_id = ?",
            (_now(), task_id),
        )
        _block_if_needed(conn, task_id, str(reason))
        _assert_invariant(conn, task_id)


def _extraction_schema(extraction_brief: Any) -> str | None:
    if isinstance(extraction_brief, str):
        return extraction_brief
    if isinstance(extraction_brief, dict):
        schema = extraction_brief.get("schema", extraction_brief.get("schema_ref"))
        if schema is None or isinstance(schema, str):
            return schema
    raise ValueError("extraction brief must name a schema")


def _validate_extraction(
    validator_or_registry: Any,
    schema_ref: str | None,
    payload: dict,
) -> bool:
    """Run an injected schema validator without giving schema ownership to us."""

    try:
        if callable(validator_or_registry):
            # Direct validators own the schema name and receive the declared
            # two-argument form. An internal TypeError must fail closed; it
            # must never be reinterpreted as an arity mismatch.
            verdict = validator_or_registry(schema_ref, payload)
        else:
            getter = getattr(validator_or_registry, "get", None)
            validator = getter(schema_ref) if callable(getter) else None
            if not callable(validator):
                return False
            # Registries are keyed by schema_ref and store payload-only
            # validators.
            verdict = validator(payload)
    except Exception:
        return False
    return verdict is True


def extract_event(
    conn,
    event_id: int,
    extraction_brief: dict | str,
    extractor,
    schema_validator,
) -> MatchResult:
    """Extract typed fields from a ledgered event, then deterministically match.

    The extractor is intentionally given only the event record and the
    template-declared brief.  Candidate instances are never part of this
    boundary, and this function does not construct an argv or model prompt.
    """

    # A model call must never sit inside BEGIN IMMEDIATE.  Snapshot the raw
    # event, call the bounded extractor unlocked, then CAS the result into the
    # ledger. Exact redelivery/concurrent classifiers therefore remain no-ops.
    event = conn.execute("SELECT * FROM wf_event WHERE id = ?", (int(event_id),)).fetchone()
    if event is None:
        raise KeyError(f"unknown workflow event: {event_id}")
    if event["status"] in _TERMINAL_EVENT_STATUSES:
        return _result_for_terminal_event(event)
    try:
        schema_ref = _extraction_schema(extraction_brief)
        event_view = {
            "id": int(event["id"]), "source": event["source"],
            "external_id": event["external_id"], "event_type": event["event_type"],
            "payload": _load_json(event["payload"], None), "corr": _load_json(event["corr"], None),
        }
        extracted = extractor(extraction_brief, event_view)
        if not isinstance(extracted, dict):
            raise ValueError("extractor result is not an object")
        payload = extracted.get("payload")
        corr = extracted.get("corr")
        event_type = extracted.get("event_type", event["event_type"])
        if not isinstance(payload, dict) or not isinstance(corr, dict):
            raise ValueError("extractor result requires object payload and corr")
        if event_type is not None and not isinstance(event_type, str):
            raise ValueError("extractor event_type must be a string or null")
        if not _validate_extraction(schema_validator, schema_ref, payload):
            raise ValueError("extracted payload did not validate")
    except Exception as exc:
        with kanban_db.write_txn(conn):
            current = conn.execute("SELECT * FROM wf_event WHERE id = ?", (int(event_id),)).fetchone()
            if current is None:
                raise KeyError(f"unknown workflow event: {event_id}")
            if current["status"] in _TERMINAL_EVENT_STATUSES:
                return _result_for_terminal_event(current)
            return _record_match(
                conn,
                int(event_id),
                "needs_review",
                reason=f"extraction failed: {exc}",
            )

    with kanban_db.write_txn(conn):
        current = conn.execute("SELECT * FROM wf_event WHERE id = ?", (int(event_id),)).fetchone()
        if current is None:
            raise KeyError(f"unknown workflow event: {event_id}")
        if current["status"] in _TERMINAL_EVENT_STATUSES:
            return _result_for_terminal_event(current)
        updated = conn.execute(
            """
            UPDATE wf_event
               SET event_type = ?, payload = ?, corr = ?, status = 'classified'
             WHERE id = ? AND status = 'received'
            """,
            (event_type, _json(payload), _json(corr), int(event_id)),
        ).rowcount
        if updated != 1:
            current = conn.execute("SELECT * FROM wf_event WHERE id = ?", (int(event_id),)).fetchone()
            return _result_for_terminal_event(current)
    return match_event(conn, int(event_id))


def _review_candidate_ids(conn: sqlite3.Connection, event: sqlite3.Row) -> tuple[str, ...]:
    """Recompute the only task ids a human is allowed to select."""

    ids: set[str] = set()
    for hit in _candidate_key_hits(conn, event):
        task_id = str(hit["instance"]["task_id"])
        position = _match_position(conn, event, hit, _now())
        if position in {"current", "future", "past", "done"}:
            ids.add(task_id)
    matched_task_id = event["matched_task_id"]
    if matched_task_id and matched_task_id in ids:
        ids.add(str(matched_task_id))
    return tuple(sorted(ids))


def resolve_event(
    conn,
    event_id: int,
    task_id: str | None = None,
    *,
    selected_task_id: str | None = None,
    decided_by: str | None = None,
) -> MatchResult:
    """Resolve an ambiguous/review event with a candidate or an explicit neither."""

    if selected_task_id is not None:
        if task_id is not None and task_id != selected_task_id:
            raise ValueError("conflicting human-selected task ids")
        task_id = selected_task_id
    actor = str(decided_by).strip() if decided_by is not None else "human"
    if not actor:
        actor = "human"
    with kanban_db.write_txn(conn):
        event = conn.execute("SELECT * FROM wf_event WHERE id = ?", (int(event_id),)).fetchone()
        if event is None:
            raise KeyError(f"unknown workflow event: {event_id}")
        if event["status"] not in {"ambiguous", "needs_review"}:
            raise WorkflowConflictError("workflow event is not awaiting human resolution")
        candidates = _review_candidate_ids(conn, event)
        resolution_external_id = f"event:{int(event_id)}:{task_id if task_id is not None else 'neither'}"
        resolution_id = ingest_event(
            conn,
            source="human_resolution",
            external_id=resolution_external_id,
            event_type="human_resolution",
            payload={
                "event_id": int(event_id),
                "decision": "match" if task_id else "neither",
                "decided_by": actor,
            },
            corr={"event_id": int(event_id), "task_id": task_id} if task_id else {"event_id": int(event_id)},
        )
        if resolution_id is None:
            raise WorkflowConflictError("human resolution was already recorded")
        if task_id is None:
            return _record_match(
                conn, int(event_id), "unmatched", method="human",
                reason="human selected neither candidate", status_override="routed_out",
            )
        if task_id not in candidates:
            raise WorkflowConflictError("human-selected task is not a deterministic candidate")

        task = _task(conn, task_id)
        instance = _instance(conn, task_id)
        spec = _load_template(conn, instance["template_id"])
        current_step = str(task["current_step_key"])
        selected_hit = next(
            (
                hit
                for hit in _candidate_key_hits(conn, event)
                if str(hit["instance"]["task_id"]) == task_id
            ),
            None,
        )
        position = (
            _match_position(conn, event, selected_hit, _now())
            if selected_hit is not None
            else None
        )
        if position == "future":
            return _record_match(
                conn,
                int(event_id),
                "buffered",
                task_id,
                method="human",
                reason="human-selected event belongs to a future step",
                candidates=candidates,
            )

        _record_match(conn, int(event_id), "matched", task_id, method="human")
        can_apply = _current_compatible(conn, task_id, current_step, event, spec)
        # Applying retains the original event as the transition cause. The
        # human resolution and application must commit in this same outer
        # transaction, so an application failure cannot strand matched state.
        if can_apply:
            applied = apply_event(conn, int(event_id), task_id, expected_step=current_step)
            if applied.kind != "applied":
                raise WorkflowConflictError(
                    f"human resolution could not apply event: {applied.kind}"
                )
            return MatchResult("matched", task_id, int(event_id), "human")
        return MatchResult("matched", task_id, int(event_id), "human", candidate_task_ids=candidates)


def resolve_human_review(
    conn,
    event_id: int,
    task_id: str | None = None,
    *,
    decided_by: str | None = None,
) -> MatchResult:
    """Compatibility spelling for :func:`resolve_event`."""

    return resolve_event(conn, event_id, task_id, decided_by=decided_by)


def _json_pointer(part: str) -> str:
    return part.replace("~", "~0").replace("/", "~1")


def _json_diff(before: Any, after: Any, path: str = "") -> list[dict]:
    """Produce a small, canonical JSON-Patch-style difference."""

    if isinstance(before, dict) and isinstance(after, dict):
        result: list[dict] = []
        for key in sorted(set(before) | set(after)):
            child = f"{path}/{_json_pointer(str(key))}"
            if key not in before:
                result.append({"op": "add", "path": child, "value": after[key]})
            elif key not in after:
                result.append({"op": "remove", "path": child})
            else:
                result.extend(_json_diff(before[key], after[key], child))
        return result
    if before != after:
        return [{"op": "replace", "path": path or "/", "value": after}]
    return []


def _approval_target(step: dict, decision: str) -> str | None:
    """Read declared approval edges while retaining a terse template form."""

    if decision == "rejected":
        keys = ("reject_to", "rejected_to", "on_rejected")
    else:
        keys = ("approve_to", "approved_to", "on_approved", "advance_to")
    nested = step.get("approval")
    sources = (step, nested) if isinstance(nested, dict) else (step,)
    for source in sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def decide_approval(
    conn,
    approval_id: int,
    resume_token: str,
    decision: str,
    *,
    decided_by: str,
    payload: dict | None = None,
) -> int | None:
    """Consume one approval capability and deterministically continue its step.

    ``None`` is the benign replay/stale-token result.  A successful call
    returns the durable approval event id.
    """

    if decision not in {"approved", "edited_approved", "rejected"}:
        raise ValueError("approval decision must be approved, edited_approved, or rejected")
    with kanban_db.write_txn(conn):
        approval = conn.execute("SELECT * FROM wf_approval WHERE id = ?", (int(approval_id),)).fetchone()
        if approval is None:
            raise KeyError(f"unknown workflow approval: {approval_id}")
        stored_token = approval["resume_token"]
        try:
            token_matches = (
                isinstance(stored_token, str)
                and isinstance(resume_token, str)
                and hmac.compare_digest(stored_token, resume_token)
            )
        except (TypeError, ValueError):
            token_matches = False
        if approval["status"] != "pending" or not token_matches:
            return None
        original_payload = _load_json(approval["payload"], None)
        if not isinstance(original_payload, dict):
            raise WorkflowConflictError("stored approval payload is not an object")
        if decision == "edited_approved":
            if not isinstance(payload, dict):
                raise ValueError("edited approval requires an object payload")
            approved_payload = payload
            decision_diff = _json(_json_diff(original_payload, approved_payload))
        else:
            if payload is not None:
                raise ValueError("only edited approval may provide a payload")
            approved_payload = original_payload
            decision_diff = None
        updated = conn.execute(
            """
            UPDATE wf_approval
               SET status = ?, decided_by = ?, decided_at = ?, decision_diff = ?, resume_token = ?
             WHERE id = ? AND status = 'pending' AND resume_token = ?
            """,
            (
                decision, str(decided_by), _now(), decision_diff,
                secrets.token_urlsafe(32), int(approval_id), resume_token,
            ),
        )
        if updated.rowcount != 1:
            return None
        event_id = ingest_event(
            conn,
            source="approval",
            external_id=f"approval:{int(approval_id)}:{resume_token}",
            event_type=decision,
            payload={"approval_id": int(approval_id), "decision": decision, "payload": approved_payload},
            corr={"task_id": approval["task_id"], "step_key": approval["step_key"]},
        )
        if event_id is None:
            raise WorkflowConflictError("approval decision event already exists")
        if decision != "rejected":
            conn.execute(
                """
                INSERT INTO wf_outbox (task_id, action, payload, status, created_at)
                VALUES (?, ?, ?, 'pending', ?)
                """,
                (approval["task_id"], approval["action"], _json(approved_payload), _now()),
            )
        instance = _instance(conn, approval["task_id"])
        spec = _load_template(conn, instance["template_id"])
        step = _step(spec, approval["step_key"])
        target = _approval_target(step, decision)
        if target is None:
            if decision == "rejected":
                _invalidate_waits(conn, approval["task_id"])
                _invalidate_approvals(conn, approval["task_id"])
                exception(conn, approval["task_id"], "approval rejected")
                return int(event_id)
            raise WorkflowConflictError("approval step has no declared approved edge")
        _step(spec, target)
        _advance(
            conn,
            approval["task_id"],
            to_step=target,
            event_id=int(event_id),
            expected_step=approval["step_key"],
            expected_run_id=None,
            enforce_declared_target=False,
        )
        return int(event_id)


def resolve_approval(conn, approval_id: int, resume_token: str, decision: str, **kwargs) -> int | None:
    """Compatibility spelling for :func:`decide_approval`."""

    return decide_approval(conn, approval_id, resume_token, decision, **kwargs)


def _event_data(event: sqlite3.Row) -> tuple[dict, dict]:
    """Return the structured correlation and payload portions of an event."""

    try:
        corr = _load_json(event["corr"], {})
    except (TypeError, json.JSONDecodeError):
        corr = {}
    try:
        payload = _load_json(event["payload"], {})
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(corr, dict):
        corr = {}
    if not isinstance(payload, dict):
        payload = {}
    return corr, payload


def _typed_payload_is_valid(event: sqlite3.Row, schema_ref: str | None) -> bool:
    """Validate the deterministic structural floor for a declared payload.

    Schema names are tenant-owned references, so the engine deliberately does
    not infer their field-level shape.  It can and must still reject a value
    which cannot be the object payload every typed event wait consumes.
    """

    if schema_ref is None:
        return True
    try:
        payload = _load_json(event["payload"], None)
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict)


def _read_value(values: dict, field: Any) -> tuple[bool, Any]:
    """Read a declared field, accepting direct and dotted object paths."""

    if not isinstance(field, str) or not field:
        return False, None
    if field in values:
        return True, values[field]
    current: Any = values
    for part in field.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _usable(value: Any) -> bool:
    return value is not None and value != ""


def _declared_fields(spec: dict, name: str) -> list[str]:
    """Normalize ordered template fields without adding template semantics."""

    values = _workflow_spec(spec).get(name) or []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    fields: list[str] = []
    for value in values:
        if isinstance(value, str) and value:
            fields.append(value)
        elif isinstance(value, dict):
            field = value.get("field", value.get("key", value.get("name")))
            if isinstance(field, str) and field:
                fields.append(field)
    return fields


def _correlation_fields(spec: dict) -> list[str]:
    return _declared_fields(spec, "correlation_keys")


def _disambiguator_fields(spec: dict) -> list[str]:
    return _declared_fields(spec, "disambiguators")


def _event_value(corr: dict, payload: dict, field: str) -> tuple[bool, Any]:
    """Correlation is authoritative, with payload as the declared fallback."""

    found, value = _read_value(corr, field)
    if found and _usable(value):
        return True, value
    found, value = _read_value(payload, field)
    return found and _usable(value), value


def _wait_types(wait: dict) -> tuple[str, ...]:
    types = wait.get("types") or []
    if isinstance(types, str):
        return (types,)
    if isinstance(types, list):
        return tuple(value for value in types if isinstance(value, str))
    return ()


def _event_matches_wait(event: sqlite3.Row, wait: dict) -> bool:
    kind = wait.get("kind")
    if kind == "event":
        return event["source"] != "timer" and event["event_type"] in _wait_types(wait)
    if kind == "timer":
        if event["source"] != "timer":
            return False
        action = wait.get("action")
        return action in (None, event["event_type"])
    return False


def _declared_event_types(spec: dict) -> set[str]:
    declared: set[str] = set()
    for step in _steps(spec):
        for wait in step.get("waits") or []:
            if not isinstance(wait, dict):
                continue
            if wait.get("kind") == "event":
                declared.update(_wait_types(wait))
            elif wait.get("kind") == "timer" and isinstance(wait.get("action"), str):
                declared.add(wait["action"])
    return declared


def _declared_wait_step_indexes(spec: dict, event: sqlite3.Row) -> list[int]:
    indexes: list[int] = []
    for index, step in enumerate(_steps(spec)):
        if any(
            isinstance(wait, dict) and _event_matches_wait(event, wait)
            for wait in (step.get("waits") or [])
        ):
            indexes.append(index)
    return indexes


def _current_compatible(
    conn: sqlite3.Connection,
    task_id: str,
    step_key: str,
    event: sqlite3.Row,
    spec: dict,
) -> bool:
    """Return whether the current step has an armed compatible wait."""

    rows = conn.execute(
        "SELECT * FROM wf_wait WHERE task_id = ? AND step_key = ? AND status = 'armed' ORDER BY id",
        (task_id, step_key),
    ).fetchall()
    waits = [wait for wait in (_step(spec, step_key).get("waits") or []) if isinstance(wait, dict)]
    for row in rows:
        for wait in waits:
            if wait.get("kind") != row["kind"]:
                continue
            if wait.get("kind") == "event":
                row_types = _load_json(row["event_types"], []) or []
                if event["event_type"] not in row_types or event["event_type"] not in _wait_types(wait):
                    continue
            elif event["source"] != "timer":
                continue
            if wait.get("kind") == "timer" and wait.get("action") not in (None, event["event_type"]):
                continue
            if wait.get("schema") is not None and row["schema_ref"] != wait.get("schema"):
                continue
            return True
    return False


def _candidate_key_hits(
    conn: sqlite3.Connection,
    event: sqlite3.Row,
    *,
    include_done: bool = True,
) -> list[dict]:
    """Find candidates using each template's strongest declared key only."""

    corr, payload = _event_data(event)
    rows = conn.execute(
        "SELECT * FROM wf_instance ORDER BY task_id"
    ).fetchall()
    hits: list[dict] = []
    for instance in rows:
        if not include_done and instance["state"] == "done":
            continue
        spec = _load_template(conn, instance["template_id"])
        instance_corr = _load_json(instance["corr"], {}) or {}
        if not isinstance(instance_corr, dict):
            instance_corr = {}
        for rank, field in enumerate(_correlation_fields(spec)):
            event_has_value, event_value = _event_value(corr, payload, field)
            instance_has_value, instance_value = _read_value(instance_corr, field)
            if event_has_value and instance_has_value and event_value == instance_value:
                hits.append(
                    {
                        "instance": instance,
                        "spec": spec,
                        "rank": rank,
                        "field": field,
                    }
                )
                break

    if not hits:
        return []
    strongest = min(hit["rank"] for hit in hits)
    # The key rank is part of the template contract. A weaker key must never
    # rescue a candidate after a stronger key produced a hit.
    return [hit for hit in hits if hit["rank"] == strongest]


def _create_on_types(spec: dict) -> set[str]:
    values = _workflow_spec(spec).get("create_on") or []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return set()
    result: set[str] = set()
    for value in values:
        if isinstance(value, str):
            result.add(value)
        elif isinstance(value, dict):
            event_type = value.get("event_type", value.get("type"))
            if isinstance(event_type, str):
                result.add(event_type)
    return result


def _is_create_event(spec: dict, event_type: str | None) -> bool:
    return event_type is not None and event_type in _create_on_types(spec)


def _template_id_from_event(corr: dict, payload: dict) -> str | None:
    for values in (corr, payload):
        for field in ("template_id", "workflow_template_id"):
            found, value = _read_value(values, field)
            if found and isinstance(value, str) and value:
                return value
    return None


def _entity_key_for_event(spec: dict, event: sqlite3.Row) -> str:
    corr, payload = _event_data(event)
    entity = _workflow_spec(spec).get("entity")
    if isinstance(entity, dict):
        entity = entity.get("key", entity.get("field", entity.get("name")))
    if isinstance(entity, str):
        found, value = _event_value(corr, payload, entity)
        if found:
            return str(value)
    for field in _correlation_fields(spec):
        found, value = _event_value(corr, payload, field)
        if found:
            return str(value)
    found, value = _event_value(corr, payload, "entity_key")
    if found:
        return str(value)
    return str(event["external_id"] or f"event-{event['id']}")


def _all_template_rows(conn: sqlite3.Connection) -> list[tuple[str, dict]]:
    rows = conn.execute(
        "SELECT slug, version, spec FROM wf_template ORDER BY slug, version"
    ).fetchall()
    result: list[tuple[str, dict]] = []
    for row in rows:
        template_id = f"{row['slug']}@{int(row['version'])}"
        spec = _load_json(row["spec"], None)
        if isinstance(spec, dict):
            result.append((template_id, spec))
    return result


def _event_has_any_declared_key(conn: sqlite3.Connection, event: sqlite3.Row) -> bool:
    corr, payload = _event_data(event)
    for _template_id, spec in _all_template_rows(conn):
        for field in _correlation_fields(spec):
            found, value = _event_value(corr, payload, field)
            if found and _usable(value):
                return True
    return False


def _event_is_declared(conn: sqlite3.Connection, event: sqlite3.Row) -> bool:
    for _template_id, spec in _all_template_rows(conn):
        if event["event_type"] in _declared_event_types(spec):
            return True
        if _is_create_event(spec, event["event_type"]):
            return True
    return False


def _record_match(
    conn: sqlite3.Connection,
    event_id: int,
    kind: str,
    task_id: str | None = None,
    *,
    method: str = "deterministic",
    reason: str | None = None,
    candidates: tuple[str, ...] = (),
    status_override: str | None = None,
) -> MatchResult:
    if kind not in _MATCH_KINDS:
        raise ValueError(f"invalid workflow match kind: {kind}")
    status = status_override or ("matched" if kind in {"matched", "create"} else kind)
    conn.execute(
        """
        UPDATE wf_event
           SET status = ?, matched_task_id = ?, match_method = ?
         WHERE id = ?
        """,
        (status, task_id, method, int(event_id)),
    )
    return MatchResult(
        kind=kind,
        task_id=task_id,
        event_id=int(event_id),
        match_method=method,
        reason=reason,
        candidate_task_ids=tuple(candidates),
    )


def _result_for_terminal_event(event: sqlite3.Row) -> MatchResult:
    status = event["status"]
    task_id = event["matched_task_id"]
    method = event["match_method"]
    if status == "matched":
        kind = "create" if method == "deterministic:create" else "matched"
    elif status == "applied":
        kind = "matched"
    elif status == "routed_out":
        kind = "unmatched"
    else:
        kind = status
    return MatchResult(
        kind=kind,
        task_id=task_id,
        event_id=int(event["id"]),
        match_method=method,
    )


def _disambiguate(
    event: sqlite3.Row,
    candidates: list[dict],
) -> list[dict]:
    if len(candidates) <= 1:
        return candidates
    corr, payload = _event_data(event)
    fields: list[str] = []
    for candidate in candidates:
        for field in _disambiguator_fields(candidate["spec"]):
            if field not in fields:
                fields.append(field)
    narrowed = list(candidates)
    for field in fields:
        found, event_value = _event_value(corr, payload, field)
        if not found:
            continue
        matching: list[dict] = []
        for candidate in narrowed:
            if field not in _disambiguator_fields(candidate["spec"]):
                matching.append(candidate)
                continue
            vars_value = _load_json(candidate["instance"]["vars"], {}) or {}
            if not isinstance(vars_value, dict):
                continue
            has_value, value = _read_value(vars_value, field)
            if has_value and value == event_value:
                matching.append(candidate)
        if matching:
            narrowed = matching
        else:
            # A declared field that contradicts every candidate cannot select
            # one. Keep the ambiguity visible instead of guessing.
            return []
        if len(narrowed) == 1:
            break
    return narrowed


def _contradicts_recorded_vars(event: sqlite3.Row, instance: sqlite3.Row) -> bool:
    corr, payload = _event_data(event)
    values = _load_json(instance["vars"], {}) or {}
    if not isinstance(values, dict):
        return False
    for source in (corr, payload):
        for field, event_value in source.items():
            if field not in values:
                continue
            if values[field] != event_value:
                return True
    return False


def _mutation_event(event: sqlite3.Row) -> bool:
    corr, payload = _event_data(event)
    merged = {**corr, **payload}
    for marker in ("mutation", "mutates", "is_mutation"):
        if merged.get(marker) is True:
            return True
    value = merged.get("operation", merged.get("action"))
    if isinstance(value, str) and value.lower() in {"update", "mutate", "delete", "cancel"}:
        return True
    event_type = event["event_type"]
    return isinstance(event_type, str) and event_type.lower() in {
        "update", "updated", "mutation", "mutated", "delete", "deleted", "cancel", "cancelled",
    }


def _done_within_retention(conn: sqlite3.Connection, task_id: str, now: int) -> bool:
    row = conn.execute(
        "SELECT completed_at FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None or row["completed_at"] is None:
        return False
    return int(row["completed_at"]) >= int(now) - DONE_RETENTION_SECONDS


def _match_position(
    conn: sqlite3.Connection,
    event: sqlite3.Row,
    candidate: dict,
    now: int,
) -> str | None:
    """Return current, future, past, or None for a keyed candidate."""

    instance = candidate["instance"]
    spec = candidate["spec"]
    current_key = instance["task_id"] and _task(conn, instance["task_id"])["current_step_key"]
    if not current_key:
        return None
    try:
        current_index = next(i for i, step in enumerate(_steps(spec)) if step["key"] == current_key)
    except StopIteration:
        return None
    if instance["state"] != "done" and _current_compatible(
        conn, instance["task_id"], str(current_key), event, spec
    ):
        return "current"
    indexes = _declared_wait_step_indexes(spec, event)
    if not indexes:
        return None
    if instance["state"] == "done":
        return "done"
    if any(index > current_index for index in indexes):
        return "future"
    if any(index < current_index for index in indexes):
        return "past"
    return None


def _rematch_held_events(
    conn: sqlite3.Connection,
    *,
    task_id: str | None = None,
    now: int | None = None,
    exclude_event_id: int | None = None,
) -> None:
    """Bounded, non-applying re-match used by instance creation and advance."""

    now = _now() if now is None else int(now)
    cutoff = now - DONE_RETENTION_SECONDS
    params: list[Any] = [cutoff]
    query = (
        "SELECT * FROM wf_event WHERE created_at >= ? "
        "AND status IN ('unmatched', 'buffered')"
    )
    if exclude_event_id is not None:
        query += " AND id != ?"
        params.append(int(exclude_event_id))
    query += " ORDER BY id"
    for event in conn.execute(query, params).fetchall():
        # A held create event is retried by the intake/sweep path, not from
        # another create, which keeps instance creation non-recursive.
        if any(
            _is_create_event(spec, event["event_type"])
            for _template_id, spec in _all_template_rows(conn)
        ):
            continue
        if task_id is not None:
            hits = _candidate_key_hits(conn, event)
            if not any(hit["instance"]["task_id"] == task_id for hit in hits):
                continue
        match_event(conn, int(event["id"]))


def match_event(conn, event_id) -> MatchResult:
    """Classify one event without applying it to a workflow instance."""

    with kanban_db.write_txn(conn):
        event = conn.execute(
            "SELECT * FROM wf_event WHERE id = ?", (int(event_id),)
        ).fetchone()
        if event is None:
            raise KeyError(f"unknown workflow event: {event_id}")
        if event["status"] in _TERMINAL_EVENT_STATUSES:
            return _result_for_terminal_event(event)

        now = _now()
        hits = _candidate_key_hits(conn, event)
        # A create event only births an instance when no usable live candidate
        # exists. Done rows inside retention are a late-event record, except
        # for a create signal, which is a new-instance check.
        create_templates: list[tuple[str, dict]] = []
        event_template_id = None
        corr, payload = _event_data(event)
        event_template_id = _template_id_from_event(corr, payload)
        for template_id, spec in _all_template_rows(conn):
            if not _is_create_event(spec, event["event_type"]):
                continue
            if event_template_id is None or event_template_id == template_id:
                create_templates.append((template_id, spec))

        live_hits = [
            hit
            for hit in hits
            if hit["instance"]["state"] != "done"
            or _done_within_retention(conn, hit["instance"]["task_id"], now)
        ]
        create_only_done = bool(hits) and not any(
            hit["instance"]["state"] != "done" for hit in hits
        )
        if (not live_hits or create_only_done) and create_templates:
            if len(create_templates) > 1:
                return _record_match(
                    conn,
                    int(event_id),
                    "ambiguous",
                    candidates=tuple(template_id for template_id, _spec in create_templates),
                    reason="multiple create templates accept the event",
                )
            template_id, spec = create_templates[0]
            entity_key = _entity_key_for_event(spec, event)
            existing_entity = conn.execute(
                "SELECT task_id FROM wf_instance WHERE entity_key = ?",
                (entity_key,),
            ).fetchone()
            if existing_entity is not None:
                entity_key = f"{entity_key}#{int(event_id)}"
            task_id = create_instance(
                conn,
                template_id=template_id,
                entity_key=entity_key,
                corr=corr,
                vars=payload,
                source_event_id=int(event_id),
            )
            return _record_match(
                conn,
                int(event_id),
                "create",
                task_id,
                method="deterministic:create",
                reason="event type declared in create_on",
            )

        if not hits:
            declared_key = _event_has_any_declared_key(conn, event)
            declared_event = _event_is_declared(conn, event)
            status = "unmatched"
            if not declared_key and not declared_event:
                status = "routed_out"
            return _record_match(
                conn,
                int(event_id),
                "unmatched",
                method="deterministic",
                reason=(
                    "event type is outside registered declarations"
                    if status == "routed_out"
                    else "no instance matched declared correlation values"
                ),
                status_override=status,
            )

        # Current-step compatibility is the only path that can yield a
        # matched event. A weaker correlation key never reaches this branch
        # after a stronger key produced any hit.
        current = []
        for hit in live_hits:
            task_id = hit["instance"]["task_id"]
            task = _task(conn, task_id)
            if _match_position(conn, event, hit, now) == "current":
                current.append({**hit, "task": task})
        if current:
            narrowed = _disambiguate(event, current)
            if len(narrowed) == 1:
                task_id = narrowed[0]["instance"]["task_id"]
                matched_wait = _event_wait(
                    conn,
                    task_id,
                    str(narrowed[0]["task"]["current_step_key"]),
                    event,
                    narrowed[0]["spec"],
                )
                if matched_wait is not None and not _typed_payload_is_valid(
                    event, matched_wait[0]["schema_ref"]
                ):
                    return _record_match(
                        conn,
                        int(event_id),
                        "needs_review",
                        task_id,
                        reason="payload is not an object for the declared typed wait",
                    )
                return _record_match(conn, int(event_id), "matched", task_id)
            return _record_match(
                conn,
                int(event_id),
                "ambiguous",
                candidates=tuple(item["instance"]["task_id"] for item in current),
                reason="more than one compatible current-step candidate",
            )

        positioned: list[tuple[dict, str]] = []
        for hit in live_hits:
            position = _match_position(conn, event, hit, now)
            if position in {"future", "past", "done"}:
                positioned.append((hit, position))

        if len(positioned) > 1:
            return _record_match(
                conn,
                int(event_id),
                "ambiguous",
                candidates=tuple(item["instance"]["task_id"] for item, _ in positioned),
                reason="more than one non-current candidate remains",
            )
        if len(positioned) == 1:
            hit, position = positioned[0]
            task_id = hit["instance"]["task_id"]
            if position == "future":
                return _record_match(
                    conn,
                    int(event_id),
                    "buffered",
                    task_id,
                    reason="event is declared by a future template step",
                )
            if position == "done":
                if _contradicts_recorded_vars(event, hit["instance"]) or _mutation_event(event):
                    return _record_match(
                        conn,
                        int(event_id),
                        "needs_review",
                        task_id,
                        reason="late event contradicts or mutates recorded state",
                    )
                return _record_match(
                    conn,
                    int(event_id),
                    "superseded",
                    task_id,
                    reason="late event correlated within done retention",
                )
            if _contradicts_recorded_vars(event, hit["instance"]):
                return _record_match(
                    conn,
                    int(event_id),
                    "needs_review",
                    task_id,
                    reason="past event contradicts recorded state",
                )
            return _record_match(
                conn,
                int(event_id),
                "superseded",
                task_id,
                reason="event belongs to a past template step",
            )

        # A keyed candidate with no compatible declared step is retained as an
        # unmatched event. It is still visible and can be reconsidered by the
        # bounded sweep if the instance advances later.
        return _record_match(
            conn,
            int(event_id),
            "unmatched",
            method="deterministic",
            reason="correlation matched but no compatible template step exists",
        )


def fire_due_timers(conn, now: int) -> list[int]:
    """Insert one deterministic synthetic event for each due armed timer."""

    created: list[int] = []
    with kanban_db.write_txn(conn):
        rows = conn.execute(
            """
            SELECT w.*, i.template_id
              FROM wf_wait w
              JOIN wf_instance i ON i.task_id = w.task_id
             WHERE w.kind = 'timer'
               AND w.status = 'armed'
               AND w.timer_at IS NOT NULL
               AND w.timer_at <= ?
             ORDER BY w.id
            """,
            (int(now),),
        ).fetchall()
        for wait in rows:
            spec = _load_template(conn, wait["template_id"])
            timer_specs = [
                item
                for item in (_step(spec, wait["step_key"]).get("waits") or [])
                if isinstance(item, dict) and item.get("kind") == "timer"
            ]
            timer_spec = next(
                (
                    item
                    for item in timer_specs
                    if item.get("action") == wait["timer_action"]
                ),
                timer_specs[0] if timer_specs else {},
            )
            limit = timer_spec.get("max_fires", 1)
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                limit = 1
            if limit < 1 or int(wait["fires_used"] or 0) >= limit:
                continue

            next_fire = int(wait["fires_used"] or 0) + 1
            external_id = f"wf:{wait['task_id']}:{wait['step_key']}:{wait['id']}:{next_fire}"
            existing = conn.execute(
                "SELECT id FROM wf_event WHERE source = 'timer' AND external_id = ?",
                (external_id,),
            ).fetchone()
            if existing is not None:
                continue
            event_id = ingest_event(
                conn,
                source="timer",
                external_id=external_id,
                payload={
                    "wait_id": int(wait["id"]),
                    "fire": next_fire,
                    "action": wait["timer_action"],
                    "task_id": wait["task_id"],
                    "step_key": wait["step_key"],
                },
                corr={"task_id": wait["task_id"], "step_key": wait["step_key"]},
                event_type=wait["timer_action"],
            )
            if event_id is None:
                continue
            next_timer_at = wait["timer_at"]
            if wait["timer_action"] == "chase" and next_fire < limit:
                # Chase timers are the one repeating timer class.  Re-arm the
                # same engine-owned row after each fire; the external id's
                # monotonically increasing fire number is the durable dedupe
                # key across gateway restarts.
                cadence = max(1, _duration(timer_spec["after"]))
                next_timer_at = int(now) + cadence
            updated = conn.execute(
                """
                UPDATE wf_wait
                   SET fires_used = fires_used + 1,
                       timer_at = ?
                 WHERE id = ? AND status = 'armed' AND fires_used = ?
                """,
                (
                    next_timer_at,
                    int(wait["id"]),
                    int(wait["fires_used"] or 0),
                ),
            )
            if updated.rowcount != 1:
                # Keep the inserted event and wait counter atomic.  A caller
                # must never observe a durable received event whose fire
                # number was not claimed by the wait row.
                raise WorkflowConflictError(
                    f"workflow timer fire CAS lost for wait {wait['id']}"
                )
            created.append(int(event_id))
    return created


def process_timer_event(conn, event_id: int) -> ApplyResult:
    """Apply one watcher-created timer event.

    Ordinary timer waits take the same CAS-protected transition path as any
    structured event.  Chase timers instead enqueue a tenant-neutral chase
    action while leaving the instance parked; reaching ``max_fires`` moves
    the instance to the resumable exception path and emits a kanban blocked
    event so existing notify subscriptions can surface the escalation.
    """

    with kanban_db.write_txn(conn):
        event = conn.execute(
            "SELECT * FROM wf_event WHERE id = ?", (int(event_id),)
        ).fetchone()
        if event is None:
            raise KeyError(f"unknown workflow event: {event_id}")
        if event["source"] != "timer":
            raise ValueError(f"workflow event {event_id} is not a timer event")
        if event["status"] in _TERMINAL_EVENT_STATUSES:
            return ApplyResult(
                kind="duplicate",
                task_id=event["matched_task_id"],
                event_id=int(event_id),
            )
        payload = _load_json(event["payload"], {}) or {}
        if not isinstance(payload, dict) or payload.get("wait_id") is None:
            raise ValueError(f"timer event {event_id} has no wait_id")
        wait = conn.execute(
            """
            SELECT w.*, i.template_id
              FROM wf_wait w
              JOIN wf_instance i ON i.task_id = w.task_id
             WHERE w.id = ?
            """,
            (int(payload["wait_id"]),),
        ).fetchone()
        if wait is None:
            _record_match(
                conn,
                int(event_id),
                "superseded",
                reason="timer wait no longer exists",
            )
            return ApplyResult(kind="superseded", event_id=int(event_id))
        task = _task(conn, wait["task_id"])
        if wait["status"] != "armed" or task["current_step_key"] != wait["step_key"]:
            _record_match(
                conn,
                int(event_id),
                "superseded",
                wait["task_id"],
                reason="timer lost the event/advance race",
            )
            return ApplyResult(
                kind="superseded",
                task_id=wait["task_id"],
                event_id=int(event_id),
            )

        spec = _load_template(conn, wait["template_id"])
        timer_specs = [
            item
            for item in (_step(spec, wait["step_key"]).get("waits") or [])
            if isinstance(item, dict) and item.get("kind") == "timer"
        ]
        timer_spec = next(
            (
                item
                for item in timer_specs
                if item.get("action") == wait["timer_action"]
            ),
            timer_specs[0] if timer_specs else {},
        )
        action = str(wait["timer_action"] or "")
        if action != "chase":
            if action in {"escalate", "deadline"}:
                reason = f"workflow {action} timer fired at {wait['step_key']}"
                conn.execute(
                    "UPDATE wf_wait SET status = 'satisfied', resume_token = NULL WHERE id = ?",
                    (int(wait["id"]),),
                )
                conn.execute(
                    """
                    UPDATE wf_event
                       SET status = 'applied', matched_task_id = ?,
                           match_method = 'deterministic:timer', applied_at = ?
                     WHERE id = ?
                    """,
                    (wait["task_id"], _now(), int(event_id)),
                )
                conn.execute(
                    "UPDATE wf_instance SET state = 'exception', parked_since = ? WHERE task_id = ?",
                    (_now(), wait["task_id"]),
                )
                # The task is already blocked while parked, so block_task()
                # cannot emit a second visible event.  Record the escalation
                # explicitly for the existing notifier cursor.
                kanban_db._append_event(
                    conn,
                    wait["task_id"],
                    "blocked",
                    {"reason": reason, "source": "workflow_timer"},
                )
                _assert_invariant(conn, wait["task_id"])
                return ApplyResult(
                    kind="exception",
                    task_id=wait["task_id"],
                    event_id=int(event_id),
                    reason=reason,
                )
            # Transition timers use the same atomic apply path.  The nested
            # transaction is a savepoint under the outer write transaction.
            return apply_event(
                conn,
                int(event_id),
                wait["task_id"],
                expected_step=wait["step_key"],
            )

        try:
            limit = max(1, int(timer_spec.get("max_fires", 1)))
        except (TypeError, ValueError):
            limit = 1
        fire_number = int(payload.get("fire") or wait["fires_used"] or 0)
        conn.execute(
            """
            INSERT INTO wf_outbox (task_id, action, payload, status, attempts, created_at)
            SELECT ?, 'chase', ?, 'pending', 0, ?
             WHERE NOT EXISTS (
                SELECT 1 FROM wf_outbox
                 WHERE task_id = ? AND action = 'chase'
                   AND json_extract(payload, '$.event_id') = ?
             )
            """,
            (
                wait["task_id"],
                _json(
                    {
                        "event_id": int(event_id),
                        "wait_id": int(wait["id"]),
                        "step_key": wait["step_key"],
                        "fire": fire_number,
                    }
                ),
                _now(),
                wait["task_id"],
                int(event_id),
            ),
        )
        conn.execute(
            """
            UPDATE wf_event
               SET status = 'applied', matched_task_id = ?,
                   match_method = 'deterministic:timer', applied_at = ?
             WHERE id = ?
            """,
            (wait["task_id"], _now(), int(event_id)),
        )
        if fire_number >= limit and timer_spec.get("then", "escalate") == "escalate":
            reason = (
                f"workflow chase cap reached at {wait['step_key']} "
                f"({fire_number}/{limit})"
            )
            conn.execute(
                "UPDATE wf_wait SET status = 'satisfied', resume_token = NULL WHERE id = ?",
                (int(wait["id"]),),
            )
            conn.execute(
                "UPDATE wf_instance SET state = 'exception', parked_since = ? WHERE task_id = ?",
                (_now(), wait["task_id"]),
            )
            kanban_db._append_event(
                conn,
                wait["task_id"],
                "blocked",
                {"reason": reason, "source": "workflow_chase_cap"},
            )
            _assert_invariant(conn, wait["task_id"])
            return ApplyResult(
                kind="exception",
                task_id=wait["task_id"],
                event_id=int(event_id),
                reason=reason,
            )
        _assert_invariant(conn, wait["task_id"])
        return ApplyResult(
            kind="chase",
            task_id=wait["task_id"],
            event_id=int(event_id),
        )


def sweep(conn, now: int) -> SweepResult:
    """Re-drive bounded intake rows without applying any workflow event."""

    cutoff = int(now) - DONE_RETENTION_SECONDS
    event_ids = [
        int(row["id"])
        for row in conn.execute(
            """
            SELECT id
              FROM wf_event
             WHERE created_at >= ?
               AND source != 'timer'
               AND status IN ('received', 'classified', 'unmatched', 'buffered')
             ORDER BY id
            """,
            (cutoff,),
        ).fetchall()
    ]
    # The active re-match window is deliberately bounded.  Once an event has
    # been held as an unmatched declared event beyond that window, keep it
    # visible by promoting it rather than attempting an unbounded re-match.
    aged_rows = conn.execute(
        """
        SELECT *
          FROM wf_event
         WHERE created_at < ?
           AND status = 'unmatched'
         ORDER BY id
        """,
        (cutoff,),
    ).fetchall()
    buckets: dict[str, list[int]] = {
        "matched": [],
        "create": [],
        "buffered": [],
        "unmatched": [],
        "ambiguous": [],
        "superseded": [],
        "needs_review": [],
    }
    with kanban_db.write_txn(conn):
        for event in aged_rows:
            # routed_out is terminal and absent from this query.  A declared
            # type alone is not enough: the dead-letter path is for events
            # that also carry a usable template-declared correlation value.
            if not (
                _event_is_declared(conn, event)
                and _event_has_any_declared_key(conn, event)
            ):
                continue
            event_id = int(event["id"])
            _record_match(
                conn,
                event_id,
                "needs_review",
                reason="declared unmatched event exceeded the intake window",
            )
            event_ids.append(event_id)
            buckets["needs_review"].append(event_id)
    for event_id in event_ids:
        if event_id in buckets["needs_review"]:
            continue
        result = match_event(conn, event_id)
        if result.kind in buckets:
            buckets[result.kind].append(event_id)
    return SweepResult(
        processed=len(event_ids),
        event_ids=tuple(event_ids),
        matched_ids=tuple(buckets["matched"]),
        created_ids=tuple(buckets["create"]),
        buffered_ids=tuple(buckets["buffered"]),
        unmatched_ids=tuple(buckets["unmatched"]),
        ambiguous_ids=tuple(buckets["ambiguous"]),
        superseded_ids=tuple(buckets["superseded"]),
        needs_review_ids=tuple(buckets["needs_review"]),
    )


__all__ = [
    "ApplyResult",
    "MatchResult",
    "SweepResult",
    "WorkflowConflictError",
    "WorkflowInvariantError",
    "_assert_invariant",
    "advance",
    "apply_event",
    "context",
    "create_instance",
    "decide_approval",
    "exception",
    "extract_event",
    "fire_due_timers",
    "ingest_event",
    "match_event",
    "park",
    "process_timer_event",
    "propose",
    "register_template",
    "review",
    "resolve_approval",
    "resolve_event",
    "resolve_human_review",
    "sweep",
]
