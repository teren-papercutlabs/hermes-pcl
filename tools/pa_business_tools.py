"""Opt-in PA business-fact bridge tools.

The bridge deliberately keeps business facts outside Hermes-owned state. It
only calls configured HTTP endpoints or local commands and returns their JSON
results to the caller.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from tools.registry import registry, tool_error, tool_result


DEFAULT_TIMEOUT_SECONDS = 30.0

ILINKED_READ_OPERATIONS = frozenset(
    {"ilinked_lookup", "ilinked_status", "ilinked_wc_lookup"}
)
ILINKED_EXPLICIT_CUES = ("ilinked", "hdb", "source-system", "source system")
WC_EXPLICIT_CUES = ("wc", "work costing", "costing")
ILINKED_ALLOW_PAYLOAD_KEYS = (
    "explicit_source_system",
    "source_system_requested",
    "ilinked_requested",
)


@dataclass(frozen=True)
class PABusinessOperation:
    name: str
    kind: str
    method: str = "POST"
    url: str | None = None
    command: tuple[str, ...] | None = None
    headers: Mapping[str, str] | None = None
    tenant: str | None = None
    auth: Mapping[str, Any] | None = None
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    path_params: tuple[str, ...] = ()


@dataclass(frozen=True)
class PABusinessBridgeConfig:
    operations: Mapping[str, PABusinessOperation]
    tenant: str | None = None
    auth: Mapping[str, Any] | None = None


class TenantScopeMismatch(ValueError):
    """Raised when a PA business operation crosses the resolved client tenant."""

    code = "TENANT_SCOPE_MISMATCH"

    def __init__(self, *, current_tenant: str | None, target_tenant: str | None, operation: str):
        self.current_tenant = current_tenant
        self.target_tenant = target_tenant
        self.operation = operation
        super().__init__(
            "TENANT_SCOPE_MISMATCH: "
            f"operation {operation!r} targets tenant {target_tenant!r} "
            f"from resolved tenant {current_tenant!r}"
        )


def _bridge_section(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    pa_section = config.get("pa")
    if isinstance(pa_section, Mapping):
        nested = pa_section.get("business")
        if isinstance(nested, Mapping):
            return nested
        for key in ("pa_business", "pa-business", "pa_business_bridge"):
            value = pa_section.get(key)
            if isinstance(value, Mapping):
                return value
    for key in ("pa_business", "pa-business", "pa_business_bridge"):
        value = config.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def load_business_bridge_config(
    config: Mapping[str, Any] | None,
    *,
    pa_context: Any | None = None,
) -> PABusinessBridgeConfig:
    """Parse bridge config from a Hermes-style config mapping.

    Expected shape:

        pa_business:
          operations:
            lookup_case:
              type: http
              url: http://127.0.0.1:8080/cases/lookup
              method: POST
              headers: {X-Bridge: pa}
            local_check:
              type: command
              command: [python, -c, "..."]

    Unknown or absent configuration produces an empty, inactive bridge.
    """
    section = _bridge_section(config)
    client_bridge = _client_business_bridge(pa_context)
    tenant = _client_tenant(pa_context)
    auth = _client_auth(pa_context)
    if isinstance(client_bridge.get("auth"), Mapping):
        auth = client_bridge["auth"]
    if client_bridge.get("tenant"):
        tenant = str(client_bridge.get("tenant"))
    raw_operations = section.get("operations", {})
    client_operations = client_bridge.get("operations", {})
    if client_operations:
        if not isinstance(client_operations, Mapping):
            raise ValueError("constitution.client.business_bridge.operations must be a mapping")
        raw_operations = {**dict(raw_operations), **dict(client_operations)}
    if not isinstance(raw_operations, Mapping):
        raise ValueError("pa_business.operations must be a mapping")

    operations: dict[str, PABusinessOperation] = {}
    for name, raw in raw_operations.items():
        if not isinstance(raw, Mapping):
            raise ValueError(f"operation {name!r} must be a mapping")

        op_name = str(name)
        kind = str(raw.get("type") or raw.get("kind") or "").strip().lower()
        if kind not in {"http", "command"}:
            raise ValueError(f"operation {op_name!r} type must be 'http' or 'command'")

        timeout = float(raw.get("timeout", DEFAULT_TIMEOUT_SECONDS))
        if timeout <= 0:
            raise ValueError(f"operation {op_name!r} timeout must be positive")

        if kind == "http":
            url = str(raw.get("url") or "").strip()
            if not url:
                raise ValueError(f"operation {op_name!r} requires url")
            method = str(raw.get("method") or "POST").strip().upper()
            headers = raw.get("headers") or {}
            if not isinstance(headers, Mapping):
                raise ValueError(f"operation {op_name!r} headers must be a mapping")
            op_auth = raw.get("auth")
            if op_auth is not None and not isinstance(op_auth, Mapping):
                raise ValueError(f"operation {op_name!r} auth must be a mapping")
            raw_path_params = raw.get("path_params") or ()
            if isinstance(raw_path_params, (str, bytes)) or not isinstance(
                raw_path_params, (list, tuple)
            ):
                raise ValueError(
                    f"operation {op_name!r} path_params must be a list of strings"
                )
            path_params = tuple(str(p) for p in raw_path_params)
            operations[op_name] = PABusinessOperation(
                name=op_name,
                kind=kind,
                method=method,
                url=url,
                headers={str(k): str(v) for k, v in headers.items()},
                tenant=str(raw.get("tenant")) if raw.get("tenant") is not None else None,
                auth=dict(op_auth) if isinstance(op_auth, Mapping) else None,
                timeout=timeout,
                path_params=path_params,
            )
            continue

        command = raw.get("command")
        if isinstance(command, str):
            command_tuple = (command,)
        elif isinstance(command, list) and all(isinstance(part, str) for part in command):
            command_tuple = tuple(command)
        else:
            raise ValueError(
                f"operation {op_name!r} command must be a string or list of strings"
            )
        if not command_tuple:
            raise ValueError(f"operation {op_name!r} requires command")
        operations[op_name] = PABusinessOperation(
            name=op_name,
            kind=kind,
            command=command_tuple,
            tenant=str(raw.get("tenant")) if raw.get("tenant") is not None else None,
            timeout=timeout,
        )

    return PABusinessBridgeConfig(
        operations=operations,
        tenant=tenant,
        auth=dict(auth) if isinstance(auth, Mapping) else None,
    )


def _client_business_bridge(pa_context: Any | None) -> Mapping[str, Any]:
    constitution = getattr(pa_context, "constitution", None)
    client = getattr(constitution, "client", None)
    if not isinstance(client, Mapping):
        return {}
    for key in ("business_bridge", "business", "pa_business_bridge"):
        value = client.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _client_tenant(pa_context: Any | None) -> str | None:
    constitution = getattr(pa_context, "constitution", None)
    client = getattr(constitution, "client", None)
    if not isinstance(client, Mapping):
        return None
    for key in ("tenant", "tenant_id", "id", "slug"):
        value = client.get(key)
        if value:
            return str(value)
    name = client.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip().lower().replace(" ", "-")
    return None


def _client_auth(pa_context: Any | None) -> Mapping[str, Any]:
    constitution = getattr(pa_context, "constitution", None)
    client = getattr(constitution, "client", None)
    if not isinstance(client, Mapping):
        return {}
    auth = client.get("auth")
    return auth if isinstance(auth, Mapping) else {}


def _runtime_pa_context(config: Mapping[str, Any] | None) -> Any | None:
    if not isinstance(config, Mapping):
        return None
    pa_config = config.get("pa")
    if not isinstance(pa_config, Mapping) or not pa_config.get("enabled"):
        return None
    try:
        from agent.pa_constitution import resolve_context
        from gateway.session_context import get_session_env

        metadata = {
            "source": {
                "platform": get_session_env("HERMES_SESSION_PLATFORM", ""),
                "chat_id": get_session_env("HERMES_SESSION_CHAT_ID", ""),
                "chat_name": get_session_env("HERMES_SESSION_CHAT_NAME", ""),
                "thread_id": get_session_env("HERMES_SESSION_THREAD_ID", ""),
                "user_id": get_session_env("HERMES_SESSION_USER_ID", ""),
                "user_name": get_session_env("HERMES_SESSION_USER_NAME", ""),
            },
            "session_key": get_session_env("HERMES_SESSION_KEY", ""),
        }
        return resolve_context(pa_config, metadata)
    except Exception:
        return None


def _load_runtime_bridge_config() -> PABusinessBridgeConfig:
    try:
        from hermes_cli.config import read_raw_config
    except Exception:
        return PABusinessBridgeConfig(operations={})
    config = read_raw_config()
    return load_business_bridge_config(config, pa_context=_runtime_pa_context(config))


def _bridge_available() -> bool:
    try:
        return bool(_load_runtime_bridge_config().operations)
    except Exception:
        return False


def _json_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be a mapping")
    return dict(payload)


def _parse_jsonish(text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    parsed = json.loads(text)
    if isinstance(parsed, dict):
        return parsed
    return {"result": parsed}


def _execute_http_operation(
    op: PABusinessOperation,
    payload: Mapping[str, Any] | None,
    bridge_config: PABusinessBridgeConfig,
) -> dict[str, Any]:
    request_payload = _json_payload(payload)
    url = op.url or ""
    data: bytes | None = None
    headers = {"Accept": "application/json", **dict(op.headers or {})}
    headers.update(_auth_headers(op.auth or bridge_config.auth or {}))

    # Path-param substitution: extract listed keys from payload, URL-encode,
    # and replace {name} placeholders in op.url. Remaining payload becomes
    # the query string (GET) or JSON body (non-GET).
    for param_name in op.path_params:
        placeholder = "{" + param_name + "}"
        if placeholder not in url:
            raise ValueError(
                f"operation {op.name!r} declares path_param {param_name!r} "
                f"but URL has no {placeholder} placeholder"
            )
        if param_name not in request_payload:
            raise ValueError(
                f"operation {op.name!r} requires path_param {param_name!r} in payload"
            )
        value = request_payload.pop(param_name)
        encoded = urllib.parse.quote(str(value), safe="")
        url = url.replace(placeholder, encoded)

    if op.method == "GET":
        if request_payload:
            query = urllib.parse.urlencode(request_payload, doseq=True)
            separator = "&" if urllib.parse.urlparse(url).query else "?"
            url = f"{url}{separator}{query}"
    else:
        data = json.dumps(request_payload).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")

    request = urllib.request.Request(url, data=data, headers=headers, method=op.method)
    try:
        with urllib.request.urlopen(request, timeout=op.timeout) as response:
            body = response.read().decode("utf-8")
            result = _parse_jsonish(body)
            result.setdefault("status_code", response.status)
            return result
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = _parse_jsonish(body)
        except json.JSONDecodeError:
            parsed = {"body": body}
        parsed["status_code"] = exc.code
        parsed.setdefault("error", f"HTTP {exc.code}")
        return parsed


def _execute_command_operation(
    op: PABusinessOperation,
    payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not op.command:
        raise ValueError(f"operation {op.name!r} has no command configured")
    completed = subprocess.run(
        op.command,
        input=json.dumps(_json_payload(payload)),
        text=True,
        capture_output=True,
        timeout=op.timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"operation {op.name!r} exited {completed.returncode}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return _parse_jsonish(completed.stdout)


def execute_business_operation(
    config: Mapping[str, Any] | PABusinessBridgeConfig | None,
    operation: str,
    payload: Mapping[str, Any] | None = None,
    *,
    pa_context: Any | None = None,
) -> dict[str, Any]:
    """Execute a configured business operation and return its JSON-ish result."""
    bridge_config = (
        config
        if isinstance(config, PABusinessBridgeConfig)
        else load_business_bridge_config(config, pa_context=pa_context)
    )
    op = bridge_config.operations.get(operation)
    if op is None:
        known = ", ".join(sorted(bridge_config.operations)) or "none configured"
        raise ValueError(f"unknown PA business operation {operation!r}; known: {known}")
    if op.tenant and bridge_config.tenant and op.tenant != bridge_config.tenant:
        raise TenantScopeMismatch(
            current_tenant=bridge_config.tenant,
            target_tenant=op.tenant,
            operation=operation,
        )

    if op.kind == "http":
        return _execute_http_operation(op, payload, bridge_config)
    if op.kind == "command":
        return _execute_command_operation(op, payload)
    raise ValueError(f"unsupported PA business operation type {op.kind!r}")


def _auth_headers(auth: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(auth, Mapping) or not auth:
        return {}
    token = auth.get("token")
    token_env = auth.get("token_env")
    if not token and token_env:
        token = os.getenv(str(token_env), "")
    if not token:
        return {}
    auth_type = str(auth.get("type") or auth.get("kind") or "header").strip().lower()
    if auth_type in {"oauth", "bearer"}:
        return {"Authorization": f"Bearer {token}"}
    header = str(auth.get("header") or "Authorization")
    scheme = auth.get("scheme")
    value = f"{scheme} {token}" if scheme else str(token)
    return {header: value}


# ── Live agent behavior config ─────────────────────────────────────────────
#
# The PS spine exposes an operator-tuned agent_config the deployed agent reads
# on every decision turn. The bridge reaches it through a configured GET
# operation (default name ``agent_config_read``); the gateway injects the
# rendered behavior block into the decision-turn prompt. Every accessor below
# fails soft — an inactive bridge, an unconfigured operation (a client
# without this operation configured), or a failed call yields an empty result
# so the agent simply runs on its constitution defaults.

AGENT_CONFIG_OPERATION = "agent_config_read"
AGENT_ACTION_OPERATIONS = (
    "agent_action_record",
    "agent_actions_write",
    "record_agent_action",
)
AGENT_ACTION_TYPES = {
    "observation",
    "photo-pair-classified",
    "dry-run-reply",
    "executed-reply",
    "config-mutation",
}
AGENT_ACTION_STATUSES = {
    "pending",
    "dry-run",
    "executed",
    "suppressed",
}


def _resolve_bridge(
    config: Mapping[str, Any] | PABusinessBridgeConfig | None,
) -> PABusinessBridgeConfig | None:
    """Resolve a bridge config from an explicit arg, or the runtime config."""
    try:
        if isinstance(config, PABusinessBridgeConfig):
            return config
        if config is not None:
            return load_business_bridge_config(config)
        return _load_runtime_bridge_config()
    except Exception:
        return None


def record_agent_action(
    *,
    agent_id: str,
    engagement_id: str,
    action_type: str,
    payload: dict,
    source: str | None = None,
    cost_usd: float = 0.0,
    tokens_input: int = 0,
    tokens_output: int = 0,
    status: str = "pending",
    turn_id: str | None = None,
    config: Mapping[str, Any] | PABusinessBridgeConfig | None = None,
) -> bool:
    """Record an agent action to dev.agent_actions. Fails soft."""
    try:
        if not agent_id or not engagement_id:
            return False
        if action_type not in AGENT_ACTION_TYPES or status not in AGENT_ACTION_STATUSES:
            return False
        if not isinstance(payload, dict):
            return False

        bridge = _resolve_bridge(config)
        if bridge is None:
            return False
        operation = next(
            (name for name in AGENT_ACTION_OPERATIONS if name in bridge.operations),
            None,
        )
        if operation is None:
            return False

        result = execute_business_operation(
            bridge,
            operation=operation,
            payload={
                "agent_id": agent_id,
                "engagement_id": engagement_id,
                "action_type": action_type,
                "payload": payload,
                "source": source,
                "cost_usd": float(cost_usd or 0.0),
                "tokens_input": int(tokens_input or 0),
                "tokens_output": int(tokens_output or 0),
                "status": status,
                "turn_id": turn_id,
            },
        )
        status_code = int(result.get("status_code") or 200)
        if status_code >= 400:
            return False
        return result.get("ok") is not False
    except Exception:
        return False


def fetch_agent_config_view(
    config: Mapping[str, Any] | PABusinessBridgeConfig | None = None,
    *,
    operation: str = AGENT_CONFIG_OPERATION,
) -> dict[str, Any]:
    """Fetch the live agent-config view ``{config, directives, keys}``.

    Returns ``{}`` when the bridge is inactive, the operation is not configured
    (e.g. a client without this operation configured), or the call fails.
    """
    bridge = _resolve_bridge(config)
    if bridge is None or operation not in bridge.operations:
        return {}
    try:
        result = execute_business_operation(bridge, operation=operation)
    except Exception:
        return {}
    data = result.get("data")
    if isinstance(data, Mapping):
        return dict(data)
    # Tolerate a flat response shape that omits the {ok, data} envelope.
    if isinstance(result.get("config"), Mapping):
        return {k: v for k, v in result.items() if k != "status_code"}
    return {}


def fetch_agent_config_map(
    config: Mapping[str, Any] | PABusinessBridgeConfig | None = None,
    *,
    operation: str = AGENT_CONFIG_OPERATION,
) -> dict[str, Any]:
    """The resolved agent-config key→value map. Empty when unavailable."""
    view = fetch_agent_config_view(config, operation=operation)
    cfg = view.get("config")
    return dict(cfg) if isinstance(cfg, Mapping) else {}


def read_agent_config(
    key: str,
    config: Mapping[str, Any] | PABusinessBridgeConfig | None = None,
    *,
    operation: str = AGENT_CONFIG_OPERATION,
) -> Any:
    """Read one live agent-config value from the PS spine.

    Returns ``None`` when the key is absent or the config cannot be reached.
    """
    return fetch_agent_config_map(config, operation=operation).get(key)


def render_agent_config_prompt(
    config: Mapping[str, Any] | PABusinessBridgeConfig | None = None,
    *,
    operation: str = AGENT_CONFIG_OPERATION,
) -> str:
    """Render the live behavior-config block for decision-turn prompt injection.

    Empty string when no config is available — the agent then runs on its
    constitution defaults with no extra block.
    """
    view = fetch_agent_config_view(config, operation=operation)
    directives = view.get("directives")
    if not isinstance(directives, list) or not directives:
        return ""
    lines = [
        "## Live Behavior Configuration",
        "Operations-tuned settings for this client. They may change between "
        "messages — apply them to your reply right now:",
    ]
    lines.extend(f"- {directive}" for directive in directives)
    return "\n".join(lines)


def _truthy_marker(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _payload_allows_ilinked(payload: Mapping[str, Any] | None) -> bool:
    if not isinstance(payload, Mapping):
        return False
    return any(_truthy_marker(payload.get(key)) for key in ILINKED_ALLOW_PAYLOAD_KEYS)


def _strip_control_payload_keys(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    clean = _json_payload(payload)
    for key in ILINKED_ALLOW_PAYLOAD_KEYS:
        clean.pop(key, None)
    return clean


def _user_task_allows_ilinked(
    operation: str,
    user_task: Any,
    payload: Mapping[str, Any] | None = None,
) -> bool:
    """Fail-closed guard for TGG source-system reads.

    Christopher's operator DB may contain facts imported from iLinked, but
    live iLinked read operations are opt-in. The model sometimes infers
    iLinked from vague words like "latest" or "chase"; this guard keeps the
    source-system bridge closed unless the current user message explicitly
    asks for it.
    """
    if operation not in ILINKED_READ_OPERATIONS:
        return True
    text = str(user_task or "").lower()
    if text:
        if any(cue in text for cue in ILINKED_EXPLICIT_CUES):
            return True
        if operation == "ilinked_wc_lookup" and any(cue in text for cue in WC_EXPLICIT_CUES):
            return True
        return False
    return _payload_allows_ilinked(payload)


def _handle_business_read(args: Mapping[str, Any], **kwargs: Any) -> str:
    return _handle_business_call(args, user_task=kwargs.get("user_task"))


def _handle_business_write(args: Mapping[str, Any], **kwargs: Any) -> str:
    return _handle_business_call(args, user_task=kwargs.get("user_task"))


def _handle_business_call(args: Mapping[str, Any], *, user_task: Any = None) -> str:
    operation = str(args.get("operation") or "").strip()
    if not operation:
        return tool_error("operation is required")
    payload = args.get("payload") or {}
    if not _user_task_allows_ilinked(operation, user_task, payload):
        return tool_error(
            "iLinked/source-system reads are opt-in. Use operator DB operations "
            "for normal case questions. Call iLinked operations only when the "
            "current user message explicitly says iLinked, HDB, source-system, "
            "or asks for WC/work-costing details. If the current user did make "
            "that explicit request, include source_system_requested=true in the "
            "payload."
        )
    payload = _strip_control_payload_keys(payload)
    try:
        result = execute_business_operation(
            _load_runtime_bridge_config(),
            operation=operation,
            payload=payload,
        )
    except Exception as exc:
        return tool_error(exc)
    return tool_result(result)


_PA_BUSINESS_PAYLOAD_SCHEMA = {
    "type": "object",
    "description": "JSON payload to pass through to the configured PA business operation.",
    "additionalProperties": True,
}


PA_BUSINESS_READ_SCHEMA = {
    "name": "pa_business_read",
    "description": (
        "Run an opt-in configured PA business read operation. The tool calls "
        "an external HTTP endpoint or local command and returns JSON; it does "
        "not persist business facts in Hermes state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "description": "Configured PA business operation name.",
            },
            "payload": _PA_BUSINESS_PAYLOAD_SCHEMA,
        },
        "required": ["operation"],
    },
}


PA_BUSINESS_WRITE_SCHEMA = {
    "name": "pa_business_write",
    "description": (
        "Run an opt-in configured PA business write operation. The tool calls "
        "an external HTTP endpoint or local command and returns JSON; it does "
        "not persist business facts in Hermes state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "description": "Configured PA business operation name.",
            },
            "payload": _PA_BUSINESS_PAYLOAD_SCHEMA,
        },
        "required": ["operation"],
    },
}


registry.register(
    name="pa_business_read",
    toolset="pa-business",
    schema=PA_BUSINESS_READ_SCHEMA,
    handler=_handle_business_read,
    check_fn=_bridge_available,
)

registry.register(
    name="pa_business_write",
    toolset="pa-business",
    schema=PA_BUSINESS_WRITE_SCHEMA,
    handler=_handle_business_write,
    check_fn=_bridge_available,
)
