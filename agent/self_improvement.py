"""Configuration helpers for Hermes self-improvement review output."""

from __future__ import annotations

from typing import Any, Mapping


DEFAULT_SELF_IMPROVEMENT_NOTIFY_POLICY = "channel"
VALID_SELF_IMPROVEMENT_NOTIFY_POLICIES = {"channel", "operator_log", "off"}


def normalize_self_improvement_notify_policy(raw: Any, *, default: str = DEFAULT_SELF_IMPROVEMENT_NOTIFY_POLICY) -> str:
    """Return the canonical self-improvement review notification policy.

    ``channel`` sends the compact review summary back through the active chat.
    ``operator_log`` keeps the review visible only in operator-side logs/TUI.
    ``off`` suppresses the review summary entirely while preserving learning.
    """
    fallback = default if default in VALID_SELF_IMPROVEMENT_NOTIFY_POLICIES else DEFAULT_SELF_IMPROVEMENT_NOTIFY_POLICY
    if raw is None:
        return fallback
    if isinstance(raw, bool):
        return "channel" if raw else "off"

    value = str(raw).strip().lower().replace("-", "_")
    if not value:
        return fallback
    if value in {"channel", "chat", "conversation", "user", "users", "true", "yes", "on", "1"}:
        return "channel"
    if value in {"operator_log", "operator", "log", "logs", "journal", "stdout"}:
        return "operator_log"
    if value in {"off", "none", "silent", "false", "no", "0"}:
        return "off"
    return fallback


def resolve_self_improvement_notify_policy(config: Mapping[str, Any] | None, *, default: str = DEFAULT_SELF_IMPROVEMENT_NOTIFY_POLICY) -> str:
    """Resolve ``agent.self_improvement.notify`` from a parsed config mapping."""
    if not isinstance(config, Mapping):
        return normalize_self_improvement_notify_policy(None, default=default)

    agent_config = config.get("agent") or {}
    if not isinstance(agent_config, Mapping):
        return normalize_self_improvement_notify_policy(None, default=default)

    nested = agent_config.get("self_improvement") or {}
    if isinstance(nested, Mapping) and "notify" in nested:
        return normalize_self_improvement_notify_policy(nested.get("notify"), default=default)

    # Backward-compatible flat key for deployments that can't easily write a
    # nested config object.
    return normalize_self_improvement_notify_policy(
        agent_config.get("self_improvement_notify"),
        default=default,
    )
