"""Workflow dashboard API.

The dashboard is a read model over the workflow tables in the kanban
database.  It deliberately exposes summaries rather than workflow payloads:
the worker and engine own the sensitive material.  Mutations are delegated to
``hermes_cli.wf_engine`` so the dashboard cannot create a second state machine.

The enclosing dashboard mounts this router below ``/api/plugins/workflow``
after applying its normal session-token middleware.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from hermes_cli import kanban_db, wf_engine

router = APIRouter()

DEFAULT_WORKFLOW_BOARD = "workflow"
_BLOCKED_STATES = frozenset({"parked", "pending_approval", "needs_review", "exception"})
_APPROVAL_DECISIONS = frozenset({"approved", "edited_approved", "rejected"})


def _resolve_board(board: str | None) -> str:
    """Return the workflow board, rejecting an unknown override."""

    if board is None or not board.strip():
        return DEFAULT_WORKFLOW_BOARD
    try:
        normalized = kanban_db._normalize_board_slug(board)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not normalized:
        return DEFAULT_WORKFLOW_BOARD
    if not kanban_db.board_exists(normalized):
        raise HTTPException(status_code=404, detail=f"board {normalized!r} does not exist")
    return normalized


def _conn(board: str | None = None) -> sqlite3.Connection:
    return kanban_db.connect(board=_resolve_board(board))


def _json_object(value: str | None, *, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _workflow_steps(spec_text: str | None) -> list[dict[str, Any]]:
    spec = _json_object(spec_text, default={})
    if not isinstance(spec, dict):
        return []
    workflow = spec.get("workflow")
    if isinstance(workflow, dict):
        spec = workflow
    steps = spec.get("steps")
    if not isinstance(steps, list):
        return []
    result: list[dict[str, Any]] = []
    for position, raw_step in enumerate(steps):
        if not isinstance(raw_step, dict):
            continue
        key = raw_step.get("key")
        if not isinstance(key, str) or not key:
            continue
        result.append({
            "key": key,
            "label": str(raw_step.get("label") or raw_step.get("name") or key),
            "order": position,
        })
    return result


def _state_badges(state: str) -> list[str]:
    """Return stable UI badge names, with state as the primary badge."""

    badges: list[str] = []
    if state in _BLOCKED_STATES:
        badges.append(state)
    elif state:
        badges.append(state)
    return badges


def _instance_identity(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "task_id": row["task_id"],
        "entity_key": row["entity_key"],
        "template_id": row["template_id"],
        "template_version": int(row["template_version"]),
        "current_step": row["current_step_key"],
        "state": row["state"],
        "parked_since": row["parked_since"],
    }


def _safe_instance(row: sqlite3.Row) -> dict[str, Any]:
    value = _instance_identity(row)
    value["badges"] = _state_badges(str(row["state"]))
    return value


def _payload_summary(value: str | None) -> dict[str, Any]:
    """Describe approval payload shape without returning any values."""

    payload = _json_object(value, default=None)
    if isinstance(payload, dict):
        return {
            "kind": "object",
            "field_count": len(payload),
        }
    if isinstance(payload, list):
        return {"kind": "array", "item_count": len(payload)}
    if payload is None:
        return {"kind": "unknown"}
    return {"kind": type(payload).__name__}


def _card_query(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Read all cards and their current-stage start in one collection query."""

    return conn.execute(
        """
        SELECT i.task_id, i.entity_key, i.template_id, i.template_version,
               i.state, i.parked_since,
               t.current_step_key, t.created_at,
               stage.applied_at AS stage_started_at
          FROM wf_instance AS i
          JOIN tasks AS t ON t.id = i.task_id
          LEFT JOIN (
              SELECT task_id, to_step, MAX(applied_at) AS applied_at
                FROM wf_transition
               GROUP BY task_id, to_step
          ) AS stage
            ON stage.task_id = i.task_id
           AND stage.to_step = t.current_step_key
         WHERE t.status != 'archived'
         ORDER BY i.template_id, t.current_step_key, i.task_id
        """
    ).fetchall()


def _template_query(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT slug, version, spec
          FROM wf_template
         ORDER BY slug, version
        """
    ).fetchall()


def _board_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    now = int(time.time())
    template_rows = _template_query(conn)
    templates: list[dict[str, Any]] = []
    stages: dict[str, dict[str, Any]] = {}

    for row in template_rows:
        template_id = f"{row['slug']}@{int(row['version'])}"
        template_stages = _workflow_steps(row["spec"])
        templates.append({
            "template_id": template_id,
            "template_version": int(row["version"]),
            "stages": template_stages,
        })
        for stage in template_stages:
            # A shared step key is one column across template versions.  Keep
            # the first declared order and label, making the board deterministic
            # when multiple workflow families are installed.
            stages.setdefault(stage["key"], stage)

    rows = _card_query(conn)
    cards: list[dict[str, Any]] = []
    cards_by_stage: dict[str, list[dict[str, Any]]] = {}
    stage_counts: dict[str, int] = {key: 0 for key in stages}
    for row in rows:
        stage_started_at = row["stage_started_at"] or row["created_at"]
        elapsed = max(0, now - int(stage_started_at)) if stage_started_at is not None else None
        card = _safe_instance(row)
        card["time_in_stage"] = elapsed
        card["stage_started_at"] = stage_started_at
        cards.append(card)
        stage = str(row["current_step_key"] or "")
        cards_by_stage.setdefault(stage, []).append(card)
        stage_counts[stage] = stage_counts.get(stage, 0) + 1

    # Preserve template order, then expose a deterministic fallback column for
    # a malformed/forward-compatible instance step not present in a template.
    ordered_stages = sorted(stages.values(), key=lambda item: (item["order"], item["key"]))
    for key in sorted(set(cards_by_stage) - set(stages)):
        ordered_stages.append({"key": key, "label": key, "order": len(ordered_stages)})
        stage_counts.setdefault(key, 0)

    columns = []
    for stage in ordered_stages:
        key = stage["key"]
        columns.append({
            "key": key,
            "label": stage["label"],
            "order": stage["order"],
            "count": stage_counts.get(key, 0),
            "cards": cards_by_stage.get(key, []),
        })
    return {
        "board": DEFAULT_WORKFLOW_BOARD,
        "templates": templates,
        "columns": columns,
        "cards": cards,
        "stage_counts": stage_counts,
    }


def _raise_engine_error(exc: Exception) -> None:
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, (ValueError, TypeError)):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, (wf_engine.WorkflowConflictError, wf_engine.WorkflowInvariantError)):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise exc


def _require_object(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    return body


def _required_text(body: dict[str, Any], field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail=f"{field} must be a non-empty string")
    return value.strip()


async def _request_object(request: Request) -> dict[str, Any]:
    """Parse JSON manually so malformed action bodies return stable 400s."""

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="request body must be valid JSON") from exc
    return _require_object(body)


def _path_id(value: str, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be an integer") from exc
    if parsed < 1:
        raise HTTPException(status_code=400, detail=f"{field} must be positive")
    return parsed


@router.get("/board")
def get_board(board: str | None = Query(default=None)) -> dict[str, Any]:
    conn = _conn(board)
    try:
        payload = _board_payload(conn)
        payload["board"] = _resolve_board(board)
        return payload
    finally:
        conn.close()


@router.get("/instances/{task_id}/timeline")
def get_timeline(task_id: str, board: str | None = Query(default=None)) -> dict[str, Any]:
    conn = _conn(board)
    try:
        instance = conn.execute(
            """
            SELECT i.task_id, i.entity_key, i.template_id, i.template_version,
                   i.state, i.parked_since, t.current_step_key
              FROM wf_instance AS i
              JOIN tasks AS t ON t.id = i.task_id
             WHERE i.task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if instance is None:
            raise HTTPException(status_code=404, detail=f"unknown workflow instance: {task_id}")
        transitions = conn.execute(
            """
            SELECT tr.task_id, tr.step_key, tr.to_step, tr.event_id, tr.applied_at,
                   e.id, e.source, e.event_type, e.status, e.match_method,
                   e.created_at, e.applied_at AS event_applied_at
              FROM wf_transition AS tr
              JOIN wf_event AS e ON e.id = tr.event_id
             WHERE tr.task_id = ?
             ORDER BY tr.applied_at, tr.event_id
            """,
            (task_id,),
        ).fetchall()
        return {
            "instance": _safe_instance(instance),
            "transitions": [
                {
                    "task_id": row["task_id"],
                    "step_key": row["step_key"],
                    "to_step": row["to_step"],
                    "event_id": int(row["event_id"]),
                    "applied_at": row["applied_at"],
                    "event": {
                        "event_id": int(row["id"]),
                        "source": row["source"],
                        "event_type": row["event_type"],
                        "status": row["status"],
                        "match_method": row["match_method"],
                        "created_at": row["created_at"],
                        "applied_at": row["event_applied_at"],
                    },
                }
                for row in transitions
            ],
        }
    finally:
        conn.close()


def _candidate_ids(conn: sqlite3.Connection, event_id: int) -> tuple[str, ...]:
    try:
        return tuple(wf_engine._review_candidate_ids(
            conn,
            conn.execute("SELECT * FROM wf_event WHERE id = ?", (event_id,)).fetchone(),
        ))
    except (AttributeError, KeyError, TypeError, ValueError):
        return ()


def _candidate_summaries(conn: sqlite3.Connection, task_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    if not task_ids:
        return []
    placeholders = ",".join("?" for _ in task_ids)
    rows = conn.execute(
        f"""
        SELECT i.task_id, i.entity_key, i.template_id, i.template_version,
               i.state, i.parked_since, t.current_step_key
          FROM wf_instance AS i
          JOIN tasks AS t ON t.id = i.task_id
         WHERE i.task_id IN ({placeholders})
         ORDER BY i.task_id
        """,
        task_ids,
    ).fetchall()
    return [_safe_instance(row) for row in rows]


@router.get("/actions")
def get_actions(board: str | None = Query(default=None)) -> dict[str, Any]:
    conn = _conn(board)
    try:
        approvals = conn.execute(
            """
            SELECT a.id, a.task_id, a.step_key, a.action, a.payload, a.status,
                   a.created_at, i.entity_key, i.template_id, i.template_version,
                   i.state, i.parked_since, t.current_step_key
              FROM wf_approval AS a
              JOIN wf_instance AS i ON i.task_id = a.task_id
              JOIN tasks AS t ON t.id = a.task_id
             WHERE a.status = 'pending' AND t.status != 'archived'
             ORDER BY a.created_at, a.id
            """
        ).fetchall()
        events = conn.execute(
            """
            SELECT e.id, e.source, e.event_type, e.status, e.match_method,
                   e.created_at, e.applied_at
              FROM wf_event AS e
             WHERE e.status IN ('needs_review', 'ambiguous')
             ORDER BY e.created_at, e.id
            """
        ).fetchall()
        approval_items = []
        for row in approvals:
            approval_items.append({
                "approval_id": int(row["id"]),
                "task_id": row["task_id"],
                "entity_key": row["entity_key"],
                "template_id": row["template_id"],
                "template_version": int(row["template_version"]),
                "step_key": row["step_key"],
                "action": row["action"],
                "status": row["status"],
                "created_at": row["created_at"],
                "payload_summary": _payload_summary(row["payload"]),
            })
        event_items = []
        for row in events:
            event_id = int(row["id"])
            candidate_ids = _candidate_ids(conn, event_id)
            event_items.append({
                "event_id": event_id,
                "source": row["source"],
                "event_type": row["event_type"],
                "status": row["status"],
                "created_at": row["created_at"],
                "candidates": _candidate_summaries(conn, candidate_ids),
            })
        return {"approvals": approval_items, "events": event_items}
    finally:
        conn.close()


@router.post("/action/events/{event_id}/resolve")
async def resolve_event_action(event_id: str, request: Request, board: str | None = Query(default=None)) -> dict[str, Any]:
    event_id_int = _path_id(event_id, "event_id")
    body = await _request_object(request)
    conn = _conn(board)
    try:
        task_id = body.get("task_id")
        if task_id is not None and (not isinstance(task_id, str) or not task_id.strip()):
            raise HTTPException(status_code=400, detail="task_id must be a string or null")
        decided_by = _required_text(body, "decided_by")
        try:
            result = wf_engine.resolve_event(
                conn,
                event_id_int,
                task_id.strip() if isinstance(task_id, str) else None,
                decided_by=decided_by,
            )
        except Exception as exc:
            _raise_engine_error(exc)
        return {
            "ok": True,
            "event_id": event_id_int,
            "task_id": result.task_id,
            "kind": result.kind,
            "match_method": result.match_method,
            "reason": result.reason,
            "decided_by": decided_by,
        }
    finally:
        conn.close()


@router.post("/action/approvals/{approval_id}")
async def decide_approval_action(approval_id: str, request: Request, board: str | None = Query(default=None)) -> dict[str, Any]:
    approval_id_int = _path_id(approval_id, "approval_id")
    body = await _request_object(request)
    conn = _conn(board)
    try:
        decision = _required_text(body, "decision")
        if decision not in _APPROVAL_DECISIONS:
            raise HTTPException(status_code=400, detail="invalid approval decision")
        decided_by = _required_text(body, "decided_by")
        token = _required_text(body, "token")
        payload = body.get("payload")
        if "payload" in body and payload is not None and not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="payload must be an object or null")
        try:
            event_id = wf_engine.decide_approval(
                conn,
                approval_id_int,
                token,
                decision,
                decided_by=decided_by,
                payload=payload,
            )
        except Exception as exc:
            _raise_engine_error(exc)
        if event_id is None:
            raise HTTPException(status_code=409, detail="approval is already decided or token is stale")
        return {"ok": True, "approval_id": approval_id_int, "event_id": event_id}
    finally:
        conn.close()
