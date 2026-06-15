"""``record_event`` — the agent-facing tool for PA universal observability.

The agent calls this mid- or end-turn to emit a SEMANTIC event: the agent's own
account of what it did and why, with pointers to the messages that are evidence.
These are the ONLY source of events on a turn — there is no mechanical fallback,
so recording is the agent's responsibility (see the constitution directive). A
turn the agent records nothing for has no events; its tool-calls are still
captured separately.

The event does not know the turn_id (minted at the turn-boundary), so this tool
STAGES the event into a per-session buffer; the gateway turn-boundary hook
drains the buffer with the correct turn_id and writes it to ``pa_events``
(source='agent_recorded').

STAGING KEY RESOLUTION (must match the drain key)
-------------------------------------------------
The drain side keys on the gateway ``session_entry.session_id``.  The agent
loop passes that same id into every tool dispatch as the ``session_id`` kwarg
(run_agent ``_invoke_tool`` -> model_tools.handle_function_call ->
registry.dispatch), so the EXPLICIT kwarg is the authoritative staging key.

The ContextVar / env fallbacks exist only for callers outside the agent loop.
``os.environ["HERMES_SESSION_ID"]`` is process-global and is OVERWRITTEN by
ANY ``AIAgent.__init__`` in the process (background review forks, hygiene
compression agents, delegate children...).  Keying staging on it lost events:
a bg-review fork constructed mid-run re-pointed the env var at its own fresh
session id, so every later record_event staged under a key the turn-boundary
never drained (sk-day26-v6: turns 11-14 staged, never landed in pa_events).

This tool is observability-only — it never mutates client state and never opens
the client schema.  ``evidence_message_refs`` are opaque string pointers.
"""

from __future__ import annotations

import os
from typing import Any, List, Mapping, Optional

from tools.registry import registry, tool_error, tool_result


def _current_session_id() -> Optional[str]:
    """FALLBACK-ONLY session-id resolution (no explicit tool-context id).

    Prefer the ``gateway.session_context._SESSION_ID`` ContextVar
    (task-local), then the HERMES_SESSION_ID env var.  NOTE: the previous
    import (``from run_agent import _SESSION_ID``) always raised ImportError —
    run_agent only imports the ContextVar function-locally, so it never exists
    as a module attribute — which silently degraded resolution to the
    process-global env var for every call.
    """
    try:
        from gateway.session_context import _SESSION_ID, _UNSET

        sid = _SESSION_ID.get(None)
        if sid and sid is not _UNSET:
            return str(sid)
    except Exception:
        pass
    sid = os.environ.get("HERMES_SESSION_ID")
    return str(sid) if sid else None


def _coerce_refs(value: Any) -> Optional[List[Any]]:
    if value is None:
        return None
    if isinstance(value, list):
        return value
    # Tolerate a single string / scalar.
    return [value]


def _handle_record_event(args: Mapping[str, Any], **kwargs: Any) -> str:
    event_type = str(args.get("event_type") or "").strip()
    if not event_type:
        return tool_error("event_type is required")
    reason = args.get("reason")
    if reason is not None:
        reason = str(reason)
    evidence = _coerce_refs(args.get("evidence_message_refs"))

    # Authoritative key: the explicit session_id the agent loop threads
    # through tool dispatch (same id the turn-boundary drains on).  Only
    # fall back to ambient resolution when no explicit id was provided
    # (non-agent-loop callers).
    session_id = kwargs.get("session_id")
    session_id = str(session_id) if session_id else _current_session_id()
    if not session_id:
        # Without a session id we cannot correlate the event to a turn.  Fail
        # soft: tell the agent it was not recorded rather than raising.
        return tool_error(
            "record_event: no active session context; event not recorded"
        )

    try:
        from gateway.pa_observability import stage_agent_event

        stage_agent_event(
            session_id,
            event_type=event_type,
            reason=reason,
            evidence_message_refs=evidence,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return tool_error(f"record_event failed: {exc}")

    return tool_result(
        {"recorded": True, "event_type": event_type, "source": "agent_recorded"}
    )


RECORD_EVENT_SCHEMA = {
    "name": "record_event",
    "description": (
        "Record a semantic EVENT — your own account of what you did this turn "
        "and why, for the observability portal. Call this at the END of a turn "
        "(or mid-turn for a notable action) to annotate what happened: e.g. "
        "after confirming a case update, escalating, answering a policy "
        "question, or declining out of scope. Provide a short event_type, a "
        "one-line reason, and evidence_message_refs pointing at the messages "
        "that justify it. This is observability only — it does not change any "
        "client data and is invisible to the user."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "event_type": {
                "type": "string",
                "description": (
                    "Short snake_case label for what happened, e.g. "
                    "'case_update_confirmed', 'escalated_to_human', "
                    "'policy_question_answered', 'out_of_scope_declined'."
                ),
            },
            "reason": {
                "type": "string",
                "description": "One-line explanation of why this event occurred.",
            },
            "evidence_message_refs": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Opaque string pointers to the messages that are evidence "
                    "for this event (message ids / refs). Optional."
                ),
            },
        },
        "required": ["event_type"],
    },
}


registry.register(
    name="record_event",
    toolset="pa-observability",
    schema=RECORD_EVENT_SCHEMA,
    handler=_handle_record_event,
)
