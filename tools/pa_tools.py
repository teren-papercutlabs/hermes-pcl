"""Manifest-bound reference knowledge tools for PA runtimes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import yaml

from agent.pa_constitution import PAJobBrief, resolve_context
from hermes_cli.config import load_config
from hermes_constants import get_hermes_home
from tools.registry import registry, tool_error, tool_result

DEFAULT_MAX_BYTES = 100_000
MAX_CONFIGURABLE_BYTES = 1_000_000


def _pa_config(config: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    loaded = config if config is not None else load_config()
    if not isinstance(loaded, Mapping):
        return {}
    pa = loaded.get("pa", {})
    return pa if isinstance(pa, Mapping) else {}


def _knowledge_root(
    config: Mapping[str, Any] | None = None,
    *,
    hermes_home: Path | None = None,
) -> Path:
    pa = _pa_config(config)
    configured = str(pa.get("knowledge_path") or "").strip()
    home = (hermes_home or get_hermes_home()).expanduser().resolve()
    if not configured:
        return (home / "knowledge").resolve()
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = home / path
    return path.resolve()


def _active_brief(config: Mapping[str, Any] | None = None) -> PAJobBrief:
    loaded = config if config is not None else load_config()
    resolved = resolve_context(loaded, None)
    if resolved is None:
        raise ValueError("PA knowledge tools require an enabled, resolvable PA job brief")
    return resolved.job_brief


def _manifest_entry(name: str, brief: PAJobBrief) -> str:
    requested = str(name or "").strip().replace("\\", "/")
    if not requested:
        raise ValueError("name is required")
    matches = [entry for entry in brief.knowledge if entry == requested]
    if not matches:
        raise ValueError(f"knowledge entry is not declared by the active job brief: {requested}")
    return matches[0]


def _resolve_manifest_path(root: Path, entry: str) -> Path:
    relative = Path(entry)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("knowledge manifest entries must be relative and cannot traverse")
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("knowledge entry escapes the configured knowledge root") from exc
    if not target.is_file():
        raise ValueError(f"knowledge entry does not exist: {entry}")
    return target


def _max_bytes(config: Mapping[str, Any] | None = None) -> int:
    raw = _pa_config(config).get("knowledge_max_bytes", DEFAULT_MAX_BYTES)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("pa.knowledge_max_bytes must be an integer") from exc
    if value < 1 or value > MAX_CONFIGURABLE_BYTES:
        raise ValueError(
            f"pa.knowledge_max_bytes must be between 1 and {MAX_CONFIGURABLE_BYTES}"
        )
    return value


def fetch_knowledge(
    name: str,
    *,
    config: Mapping[str, Any] | None = None,
    brief: PAJobBrief | None = None,
    hermes_home: Path | None = None,
) -> dict[str, Any]:
    """Return one declared knowledge file, refusing unlisted or oversized data."""
    active = brief or _active_brief(config)
    entry = _manifest_entry(name, active)
    root = _knowledge_root(config, hermes_home=hermes_home)
    target = _resolve_manifest_path(root, entry)
    size = target.stat().st_size
    limit = _max_bytes(config)
    if size > limit:
        raise ValueError(f"knowledge entry exceeds size limit: {size} > {limit} bytes")
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("knowledge entries must be UTF-8 text") from exc
    return {"name": entry, "bytes": size, "content": content}


def lookup_reference(
    name: str,
    key: str,
    *,
    config: Mapping[str, Any] | None = None,
    brief: PAJobBrief | None = None,
    hermes_home: Path | None = None,
) -> dict[str, Any]:
    """Look up an exact key in a declared structured reference file."""
    fetched = fetch_knowledge(
        name,
        config=config,
        brief=brief,
        hermes_home=hermes_home,
    )
    try:
        document = yaml.safe_load(fetched["content"])
    except yaml.YAMLError as exc:
        raise ValueError(f"structured reference is invalid YAML: {exc}") from exc
    if not isinstance(document, Mapping):
        raise ValueError("structured reference must be a mapping")
    if document.get("kind") != "keyed-reference":
        raise ValueError("structured reference kind must be 'keyed-reference'")
    escalation_cue = document.get("escalation_cue")
    if not isinstance(escalation_cue, str) or not escalation_cue.strip():
        raise ValueError("structured reference requires a non-empty escalation_cue")
    entries = document.get("entries")
    if not isinstance(entries, list):
        raise ValueError("structured reference entries must be a list")

    index: dict[str, Mapping[str, Any]] = {}
    for position, row in enumerate(entries):
        if not isinstance(row, Mapping):
            raise ValueError(f"structured reference entry {position} must be a mapping")
        row_key = row.get("key")
        if not isinstance(row_key, str) or not row_key:
            raise ValueError(f"structured reference entry {position} requires string key")
        if row_key in index:
            raise ValueError(f"structured reference contains duplicate key: {row_key}")
        index[row_key] = dict(row)

    requested = str(key or "")
    entry = index.get(requested)
    return {
        "file": fetched["name"],
        "key": requested,
        "found": entry is not None,
        "entry": dict(entry) if entry is not None else None,
        "escalation_cue": escalation_cue,
        "match": "exact" if entry is not None else "none",
    }


def _tools_available() -> bool:
    try:
        pa = _pa_config()
        return bool(pa.get("enabled")) and bool(_active_brief().knowledge)
    except Exception:
        return False


def _handle_fetch(args: Mapping[str, Any], **_: Any) -> str:
    try:
        return tool_result(fetch_knowledge(str(args.get("name") or "")))
    except ValueError as exc:
        return tool_error(str(exc))


def _handle_lookup(args: Mapping[str, Any], **_: Any) -> str:
    try:
        return tool_result(
            lookup_reference(
                str(args.get("name") or ""),
                str(args.get("key") or ""),
            )
        )
    except ValueError as exc:
        return tool_error(str(exc))


registry.register(
    name="pa_knowledge_fetch",
    toolset="pa-knowledge",
    schema={
        "name": "pa_knowledge_fetch",
        "description": "Read one file declared in the active PA job's knowledge manifest.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    handler=_handle_fetch,
    check_fn=_tools_available,
)

registry.register(
    name="pa_reference_lookup",
    toolset="pa-knowledge",
    schema={
        "name": "pa_reference_lookup",
        "description": (
            "Return the row for an exact key from a declared structured PA reference. "
            "A miss returns found=false plus the configured escalation cue; never guess."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "key": {"type": "string", "description": "Exact key; no fuzzy matching."},
            },
            "required": ["name", "key"],
        },
    },
    handler=_handle_lookup,
    check_fn=_tools_available,
)
