"""Worker-facing workflow tools.

The registry is the only tool-facing seam in this module.  Workflow state is
resolved through the existing kanban helpers, while every workflow operation
is delegated to :mod:`hermes_cli.wf_engine`.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Callable, Optional

from hermes_cli import wf_engine
from tools import kanban_tools
from tools.registry import registry


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields}, ensure_ascii=False)


def _error(message: str) -> str:
    return json.dumps({"tool_error": str(message)}, ensure_ascii=False)


def _close(conn: Any) -> None:
    try:
        conn.close()
    except Exception:
        pass


def _check_workflow_task() -> bool:
    """Expose the tools only for a dispatcher task opted into a workflow."""
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    if not task_id:
        return False
    conn = None
    try:
        _kb, conn = kanban_tools._connect()
        task = _kb.get_task(conn, task_id)
        return bool(task and task.workflow_template_id)
    except Exception:
        return False
    finally:
        if conn is not None:
            _close(conn)


def _workflow_scope(task_id_arg: Optional[str]) -> tuple[Optional[tuple[str, Any]], Optional[str]]:
    """Resolve and validate the worker's workflow task."""
    task_id = kanban_tools._default_task_id(task_id_arg)
    if not task_id:
        return None, _error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )

    ownership_error = kanban_tools._enforce_worker_task_ownership(task_id)
    if ownership_error:
        try:
            ownership_message = json.loads(ownership_error).get(
                "error", ownership_error
            )
        except (TypeError, ValueError):
            ownership_message = ownership_error
        return None, _error(ownership_message)

    conn = None
    try:
        kb, conn = kanban_tools._connect()
        task = kb.get_task(conn, task_id)
        if task is None:
            _close(conn)
            return None, _error(f"task {task_id} not found")
        if not task.workflow_template_id:
            _close(conn)
            return None, _error(f"task {task_id} is not a workflow task")
        return (task_id, conn), None
    except Exception as exc:
        if conn is not None:
            _close(conn)
        return None, _error(f"workflow task lookup failed: {exc}")


def _with_workflow_task(
    task_id_arg: Optional[str],
    operation: str,
    callback: Callable[[str, Any], str],
) -> str:
    try:
        scope, error = _workflow_scope(task_id_arg)
    except Exception as exc:
        return _error(f"{operation} task validation failed: {exc}")
    if error:
        return error
    assert scope is not None
    task_id, conn = scope
    try:
        return callback(task_id, conn)
    except Exception as exc:
        return _error(f"{operation} failed: {exc}")
    finally:
        _close(conn)


def _engine_function(name: str) -> Callable[..., Any]:
    function = getattr(wf_engine, name, None)
    if not callable(function):
        raise RuntimeError(f"workflow engine function {name} is unavailable")
    return function


def _required_text(args: dict, name: str) -> tuple[Optional[str], Optional[str]]:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        return None, _error(f"{name} must be a non-empty string")
    return value.strip(), None


def _required_object(args: dict, name: str, *, non_empty: bool = False) -> tuple[Optional[dict], Optional[str]]:
    value = args.get(name)
    if not isinstance(value, dict) or (non_empty and not value):
        return None, _error(f"{name} must be a JSON object")
    return value, None


def _handle_context(args: dict, **_kw: Any) -> str:
    if not isinstance(args, dict):
        return _error("arguments must be a JSON object")

    def call(task_id: str, conn: Any) -> str:
        result = _engine_function("context")(conn, task_id)
        return _ok(result=result)

    return _with_workflow_task(args.get("task_id"), "wf_context", call)


def _handle_advance(args: dict, **_kw: Any) -> str:
    if not isinstance(args, dict):
        return _error("arguments must be a JSON object")
    to_step, error = _required_text(args, "to_step")
    if error:
        return error
    evidence, error = _required_object(args, "evidence")
    if error:
        return error

    def call(task_id: str, conn: Any) -> str:
        external_id = f"worker-wf-advance-{uuid.uuid4().hex}"
        event_id = _engine_function("ingest_event")(
            conn,
            source="worker",
            external_id=external_id,
            payload=evidence,
            corr=None,
            event_type="wf_advance",
        )
        if event_id is None:
            return _error("wf_advance event was already recorded")
        result = _engine_function("advance")(
            conn,
            task_id,
            to_step=to_step,
            event_id=event_id,
        )
        return _ok(event_id=event_id, result=result)

    return _with_workflow_task(args.get("task_id"), "wf_advance", call)


def _handle_propose(args: dict, **_kw: Any) -> str:
    if not isinstance(args, dict):
        return _error("arguments must be a JSON object")
    action, error = _required_text(args, "action")
    if error:
        return error
    payload, error = _required_object(args, "payload", non_empty=True)
    if error:
        return error

    def call(task_id: str, conn: Any) -> str:
        result = _engine_function("propose")(
            conn,
            task_id,
            action=action,
            payload=payload,
        )
        return _ok(result=result)

    return _with_workflow_task(args.get("task_id"), "wf_propose", call)


def _handle_review(args: dict, **_kw: Any) -> str:
    if not isinstance(args, dict):
        return _error("arguments must be a JSON object")
    reason, error = _required_text(args, "reason")
    if error:
        return error
    options = args.get("options")
    if not isinstance(options, list):
        return _error("options must be a JSON array")

    def call(task_id: str, conn: Any) -> str:
        result = _engine_function("review")(
            conn,
            task_id,
            reason=reason,
            options=options,
        )
        return _ok(result=result)

    return _with_workflow_task(args.get("task_id"), "wf_review", call)


def _handle_exception(args: dict, **_kw: Any) -> str:
    if not isinstance(args, dict):
        return _error("arguments must be a JSON object")
    reason, error = _required_text(args, "reason")
    if error:
        return error

    def call(task_id: str, conn: Any) -> str:
        result = _engine_function("exception")(
            conn,
            task_id,
            reason=reason,
        )
        return _ok(result=result)

    return _with_workflow_task(args.get("task_id"), "wf_exception", call)


def _signal_metadata(payload: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return external id, event type, and a validation error."""
    metadata = None
    for key in ("_metadata", "metadata", "_meta"):
        if key in payload:
            metadata = payload[key]
            break
    if metadata is not None and not isinstance(metadata, dict):
        return None, None, _error("payload metadata must be a JSON object")
    metadata = metadata or {}

    def value(name: str) -> Any:
        if name in metadata:
            return metadata[name]
        return payload.get(name)

    external_id = value("external_id")
    event_type = value("event_type")
    if external_id is not None:
        if not isinstance(external_id, str) or not external_id.strip():
            return None, None, _error("external_id must be a non-empty string")
        external_id = external_id.strip()
    else:
        external_id = f"worker-wf-signal-{uuid.uuid4().hex}"
    if event_type is not None:
        if not isinstance(event_type, str) or not event_type.strip():
            return None, None, _error("event_type must be a non-empty string")
        event_type = event_type.strip()
    return external_id, event_type, None


def _handle_signal(args: dict, **_kw: Any) -> str:
    if not isinstance(args, dict):
        return _error("arguments must be a JSON object")
    source, error = _required_text(args, "source")
    if error:
        return error
    payload, error = _required_object(args, "payload")
    if error:
        return error
    corr, error = _required_object(args, "corr")
    if error:
        return error
    external_id, event_type, error = _signal_metadata(payload)
    if error:
        return error

    def call(_task_id: str, conn: Any) -> str:
        event_id = _engine_function("ingest_event")(
            conn,
            source=source,
            external_id=external_id,
            payload=payload,
            corr=corr,
            event_type=event_type,
        )
        return _ok(event_id=event_id, duplicate=event_id is None)

    return _with_workflow_task(None, "wf_signal", call)


WF_CONTEXT_SCHEMA = {
    "name": "wf_context",
    "description": "Read the current workflow context for the assigned task.",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Workflow task id."},
        },
        "additionalProperties": False,
    },
}

WF_ADVANCE_SCHEMA = {
    "name": "wf_advance",
    "description": "Advance the assigned workflow task with structured evidence.",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Workflow task id."},
            "to_step": {"type": "string", "description": "Next workflow step."},
            "evidence": {"type": "object", "description": "Structured advance evidence."},
        },
        "required": ["to_step", "evidence"],
        "additionalProperties": False,
    },
}

WF_PROPOSE_SCHEMA = {
    "name": "wf_propose",
    "description": "Propose a structured workflow action for review.",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Workflow task id."},
            "action": {"type": "string", "description": "Allowlisted action name."},
            "payload": {"type": "object", "description": "Complete action payload."},
        },
        "required": ["action", "payload"],
        "additionalProperties": False,
    },
}

WF_REVIEW_SCHEMA = {
    "name": "wf_review",
    "description": "Send the assigned workflow task to review with options.",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Workflow task id."},
            "reason": {"type": "string", "description": "Why review is required."},
            "options": {"type": "array", "description": "Structured review options."},
        },
        "required": ["reason", "options"],
        "additionalProperties": False,
    },
}

WF_EXCEPTION_SCHEMA = {
    "name": "wf_exception",
    "description": "Record a resumable exception for the assigned workflow task.",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Workflow task id."},
            "reason": {"type": "string", "description": "Why the workflow cannot continue."},
        },
        "required": ["reason"],
        "additionalProperties": False,
    },
}

WF_SIGNAL_SCHEMA = {
    "name": "wf_signal",
    "description": "Ledger a structured workflow signal without exposing raw bodies.",
    "parameters": {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Signal source."},
            "payload": {"type": "object", "description": "Structured signal payload or body reference."},
            "corr": {"type": "object", "description": "Structured correlation keys."},
        },
        "required": ["source", "payload", "corr"],
        "additionalProperties": False,
    },
}


registry.register(
    name="wf_context", toolset="kanban", schema=WF_CONTEXT_SCHEMA,
    handler=_handle_context, check_fn=_check_workflow_task, emoji="🧭",
)
registry.register(
    name="wf_advance", toolset="kanban", schema=WF_ADVANCE_SCHEMA,
    handler=_handle_advance, check_fn=_check_workflow_task, emoji="➡",
)
registry.register(
    name="wf_propose", toolset="kanban", schema=WF_PROPOSE_SCHEMA,
    handler=_handle_propose, check_fn=_check_workflow_task, emoji="📝",
)
registry.register(
    name="wf_review", toolset="kanban", schema=WF_REVIEW_SCHEMA,
    handler=_handle_review, check_fn=_check_workflow_task, emoji="🔎",
)
registry.register(
    name="wf_exception", toolset="kanban", schema=WF_EXCEPTION_SCHEMA,
    handler=_handle_exception, check_fn=_check_workflow_task, emoji="⚠",
)
registry.register(
    name="wf_signal", toolset="kanban", schema=WF_SIGNAL_SCHEMA,
    handler=_handle_signal, check_fn=_check_workflow_task, emoji="📡",
)


__all__ = [
    "_check_workflow_task",
    "_handle_context",
    "_handle_advance",
    "_handle_propose",
    "_handle_review",
    "_handle_exception",
    "_handle_signal",
]
