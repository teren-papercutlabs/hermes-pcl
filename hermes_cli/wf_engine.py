"""Atomic workflow state transitions on top of the kanban chassis.

The workflow tables are deliberately separate from kanban's task lifecycle.
This module owns the workflow rows and drives task status changes through the
existing :mod:`kanban_db` transition functions.
"""

from __future__ import annotations

import hashlib
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


def _now() -> int:
    return int(time.time())


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
    """Commit a worker-stage transition and enter the next stage."""

    with kanban_db.write_txn(conn):
        task = _task(conn, task_id)
        instance = _instance(conn, task_id)
        spec = _load_template(conn, instance["template_id"])
        current_step = task["current_step_key"]
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
        if not current_step:
            raise WorkflowConflictError(f"workflow task has no current step: {task_id}")
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
            if task["status"] == "blocked":
                _unblock(conn, task_id)
            elif task["status"] not in {"ready", "running"}:
                raise WorkflowConflictError(
                    f"cannot advance workflow task from status {task['status']!r}"
                )
        _assert_invariant(conn, task_id)


def context(conn, task_id: str) -> dict:
    """Return the tenant-neutral worker context for one instance."""

    task = _task(conn, task_id)
    instance = _instance(conn, task_id)
    spec = _load_template(conn, instance["template_id"])
    step = _step(spec, task["current_step_key"])
    return {
        "task_id": task_id,
        "template_id": instance["template_id"],
        "entity_key": instance["entity_key"],
        "corr": _load_json(instance["corr"], {}),
        "vars": _load_json(instance["vars"], {}),
        "state": instance["state"],
        "step": step,
    }


def propose(conn, task_id: str, action: str, payload: dict) -> int:
    """Persist a complete approval proposal and park the instance."""

    with kanban_db.write_txn(conn):
        task = _task(conn, task_id)
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


def review(conn, task_id: str, reason: str, options: Any = None) -> None:
    """Move a workflow instance into the human review state."""

    del options  # Reserved for the later adapter; no schema column is added.
    with kanban_db.write_txn(conn):
        task = _task(conn, task_id)
        _instance(conn, task_id)
        conn.execute(
            "UPDATE wf_instance SET state = 'needs_review', parked_since = ? WHERE task_id = ?",
            (_now(), task_id),
        )
        _block_if_needed(conn, task_id, str(reason))
        _assert_invariant(conn, task_id)


def exception(conn, task_id: str, reason: str) -> None:
    """Move a workflow instance into the resumable exception state."""

    with kanban_db.write_txn(conn):
        task = _task(conn, task_id)
        _instance(conn, task_id)
        conn.execute(
            "UPDATE wf_instance SET state = 'exception', parked_since = ? WHERE task_id = ?",
            (_now(), task_id),
        )
        _block_if_needed(conn, task_id, str(reason))
        _assert_invariant(conn, task_id)


def match_event(conn, event_id) -> MatchResult:
    """Deterministic correlator implementation lands in the correlator unit."""

    raise NotImplementedError


def fire_due_timers(conn, now: int) -> list[int]:
    """Timer watcher implementation lands in the correlator unit."""

    raise NotImplementedError


def sweep(conn, now: int) -> SweepResult:
    """Intake sweep implementation lands in the correlator unit."""

    raise NotImplementedError


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
    "exception",
    "fire_due_timers",
    "ingest_event",
    "match_event",
    "park",
    "propose",
    "register_template",
    "review",
    "sweep",
]
