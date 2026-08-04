"""Eval-mode switch: what a replay must NOT do to itself.

A replay exists to measure the deployed behavior. Anything that lets a run
mutate the agent it is measuring makes the measurement unrepresentative and the
next run non-comparable — the 2026-08-03 nightly created a ``bor-drafting``
skill mid-corpus, so from that turn on the corpus was scoring an agent the
deploy tree does not contain.

The switch is set by the harness that stages the disposable runtime, read by the
runtime, and defaults OFF, so production behavior is untouched by its existence.
"""

from __future__ import annotations

import os
from typing import Any, Mapping


EVAL_MODE_ENV = "HERMES_EVAL_MODE"

_TRUE = {"1", "true", "yes", "on", "eval"}
_FALSE = {"0", "false", "no", "off", ""}


def _coerce(raw: Any) -> bool | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    value = str(raw).strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return None


def eval_mode_enabled(config: Mapping[str, Any] | None = None) -> bool:
    """True when this process is a measurement run, not a live deployment.

    The environment wins over config: the harness sets it in the same process it
    stages, so it holds even for a runtime whose config was copied from a live
    home that knows nothing about evaluation.
    """
    from_env = _coerce(os.environ.get(EVAL_MODE_ENV))
    if from_env is not None:
        return from_env
    if isinstance(config, Mapping):
        agent_config = config.get("agent")
        if isinstance(agent_config, Mapping):
            from_config = _coerce(agent_config.get("eval_mode"))
            if from_config is not None:
                return from_config
        from_top = _coerce(config.get("eval_mode"))
        if from_top is not None:
            return from_top
    return False


def self_modification_allowed(config: Mapping[str, Any] | None = None) -> bool:
    """False when the agent must not rewrite its own memory or skills."""
    return not eval_mode_enabled(config)


__all__ = ["EVAL_MODE_ENV", "eval_mode_enabled", "self_modification_allowed"]
