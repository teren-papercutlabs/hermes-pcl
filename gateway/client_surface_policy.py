"""Fail-closed output policy for client-facing Hermes runtimes.

Papercut Agents runtimes declare ``agent.profile: pa``.  That profile is a
client surface, so operational sidebands and raw exception details must never
be routed back to the bound chat.  This module deliberately does not expose an
opt-out: a PA deployment may customize its final agent response, but it cannot
turn engine diagnostics into client-visible messages.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


CLIENT_SAFE_FAILURE = "I couldn't complete that request. Please try again."


def is_client_facing_config(config: Mapping[str, Any] | None) -> bool:
    """Return whether *config* declares a client-facing PA runtime."""
    if not isinstance(config, Mapping):
        return False
    agent = config.get("agent")
    if not isinstance(agent, Mapping):
        return False
    return str(agent.get("profile") or "").strip().lower() == "pa"


def client_safe_failure() -> str:
    """Return the fixed client-safe failure response.

    The string is intentionally free of exception details, provider/runtime
    names, tool names, iteration counters, config keys, and slash commands.
    Full diagnostics remain in gateway logs.
    """
    return CLIENT_SAFE_FAILURE


def is_client_facing_home(hermes_home: Path | None = None) -> bool:
    """Read the raw gateway config and apply the client-facing policy.

    Platform adapters run below :class:`GatewayRunner` and therefore do not
    receive the merged user config.  Reading the profile here keeps their
    last-resort exception path fail-closed as well.
    """
    try:
        import yaml

        if hermes_home is None:
            from hermes_constants import get_hermes_home

            hermes_home = get_hermes_home()
        data = yaml.safe_load((Path(hermes_home) / "config.yaml").read_text()) or {}
    except Exception:
        return False
    return is_client_facing_config(data)
