"""Canonical, non-persistent Hermes one-turn session carrier."""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any, Mapping


def run_ephemeral_session(*, prompt: str, system_prompt: str, model: str,
                          max_iterations: int, allowed_tool_names: list[str],
                          session_prefix: str = "ephemeral") -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one canonical-provider turn and erase its session/transcript.

    This is deliberately not a gateway or delivery carrier.  The audit holds
    only non-sensitive identity/hash/lifecycle facts, never the prompt or
    tool arguments.
    """
    from hermes_cli.config import load_config
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    from run_agent import AIAgent
    from hermes_cli.oneshot import _oneshot_clarify_callback

    cfg = load_config()
    model_cfg = cfg.get("model") or {}
    configured = model_cfg if isinstance(model_cfg, str) else (model_cfg.get("default") or model_cfg.get("model") or "")
    effective_model = str(model or configured)
    runtime = resolve_runtime_provider(requested=None, target_model=effective_model or None)
    session_id = f"{session_prefix}-{uuid.uuid4().hex}"
    db = SessionDB(); agent = None; outcome: dict[str, Any] = {}; terminal_reason = "failed"; cleanup = {"ended": False, "deleted": False, "agent_closed": False, "db_closed": False}
    try:
        db.create_session(session_id, "ephemeral", model=effective_model, model_config={"provider": runtime.get("provider")})
        agent = AIAgent(api_key=runtime.get("api_key"), base_url=runtime.get("base_url"), provider=runtime.get("provider"),
                        api_mode=runtime.get("api_mode"), model=effective_model, max_iterations=max_iterations,
                        quiet_mode=True, platform="cli", session_id=session_id, session_db=db,
                        credential_pool=runtime.get("credential_pool"), allowed_tool_names=list(allowed_tool_names),
                        skip_memory=True, skip_context_files=True, clarify_callback=_oneshot_clarify_callback,
                        ephemeral_system_prompt=system_prompt)
        agent.suppress_status_output = True; agent.stream_delta_callback = None; agent.tool_gen_callback = None
        outcome = agent.run_conversation(prompt)
        terminal_reason = "completed"
    finally:
        try: db.end_session(session_id, terminal_reason); cleanup["ended"] = True
        except Exception: pass
        try:
            if agent is not None: agent.close(); cleanup["agent_closed"] = True
        finally:
            try: cleanup["deleted"] = bool(db.delete_session(session_id, sessions_dir=get_hermes_home() / "sessions"))
            except Exception: pass
            try: db.close(); cleanup["db_closed"] = True
            except Exception: pass
    return outcome, {"session_id": session_id, "provider": runtime.get("provider"), "model": effective_model,
                     "loaded_tools": sorted(agent.valid_tool_names) if agent is not None else [],
                     "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "terminal_reason": terminal_reason,
                     "cleanup": cleanup}
