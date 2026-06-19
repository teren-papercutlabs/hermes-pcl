"""Reusable personal-assistant constitution primitives.

A constitution is durable identity plus job-specific operating briefs.  This
module keeps loading, selection, prompt rendering, and behavior hashing pure so
gateway/runtime slices can compose it without inheriting IO side effects.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml


@dataclass(frozen=True)
class PAJobBrief:
    job_type: str
    title: str
    purpose: str
    instructions: tuple[str, ...]
    runtime: Mapping[str, Any] = field(default_factory=dict)
    enabled_toolsets: tuple[str, ...] = field(default_factory=tuple)
    disabled_toolsets: tuple[str, ...] = field(default_factory=tuple)
    fact_operations: Mapping[str, Any] = field(default_factory=dict)
    response_policy: Mapping[str, Any] = field(default_factory=dict)
    # Optional per-chat gate / debounce overrides. ``None`` means "not set by
    # this brief" — the caller falls back to global config / universal default
    # so today's behaviour is preserved when a brief sets nothing.
    require_mention: bool | None = None
    debounce_passive_ms: int | None = None
    debounce_addressed_ms: int | None = None
    hash: str = ""


@dataclass(frozen=True)
class PAConstitution:
    id: str
    agent_name: str
    identity: Mapping[str, Any]
    client: Mapping[str, Any]
    sops: tuple[str, ...]
    job_briefs: Mapping[str, PAJobBrief]
    selectors: tuple[Mapping[str, Any], ...]
    source: str
    hash: str


@dataclass(frozen=True)
class PAResolvedContext:
    constitution: PAConstitution
    job_brief: PAJobBrief
    job_type: str
    metadata: Mapping[str, Any]
    identity_hash: str
    job_hash: str
    behavior_hash: str


def load_constitution(path: str | Path) -> PAConstitution:
    """Load a PA constitution from a YAML file."""
    constitution_path = Path(path)
    try:
        loaded = yaml.safe_load(constitution_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - exact YAML errors are library-owned
        raise ValueError(f"Failed to load PA constitution {constitution_path}: {exc}") from exc
    return load_constitution_data(loaded, source=str(constitution_path))


def load_constitution_data(
    data: Mapping[str, Any],
    *,
    source: str = "<memory>",
    client_overlay: Mapping[str, Any] | None = None,
) -> PAConstitution:
    """Validate raw constitution data and return the normalized dataclass form."""
    if not isinstance(data, Mapping):
        raise ValueError(f"{source}: constitution must be a mapping")

    constitution_id = _required_str(data, "id", source)
    agent_name = _required_str(data, "agent_name", source)
    identity = _required_mapping(data, "identity", source)
    client = _required_mapping(data, "client", source)
    if client_overlay is None:
        inline_overlay = data.get("client_overlay")
        if inline_overlay is not None:
            if not isinstance(inline_overlay, Mapping):
                raise ValueError(f"{source}.client_overlay: must be a mapping")
            client_overlay = inline_overlay
    if client_overlay is not None:
        from agent.pa_client_overlay import merge_client_overlay

        agent_name, identity, client = merge_client_overlay(
            agent_name=agent_name,
            identity=identity,
            client=client,
            overlay=client_overlay,
            source=f"{source}.client_overlay",
        )
    sops = _string_tuple(data.get("sops", ()), f"{source}.sops")

    raw_job_briefs = _required_mapping(data, "job_briefs", source)
    job_briefs: dict[str, PAJobBrief] = {}
    for job_type, raw_brief in raw_job_briefs.items():
        if not isinstance(job_type, str) or not job_type:
            raise ValueError(f"{source}.job_briefs: job_type keys must be non-empty strings")
        if not isinstance(raw_brief, Mapping):
            raise ValueError(f"{source}.job_briefs.{job_type}: brief must be a mapping")
        job_briefs[job_type] = _load_job_brief(job_type, raw_brief, source)
    if not job_briefs:
        raise ValueError(f"{source}.job_briefs: at least one job brief is required")

    raw_selectors = data.get("selectors", ())
    if not isinstance(raw_selectors, list | tuple):
        raise ValueError(f"{source}.selectors: selectors must be a list")
    selectors = tuple(_load_selector(selector, job_briefs, source) for selector in raw_selectors)

    identity_payload = {
        "id": constitution_id,
        "agent_name": agent_name,
        "identity": identity,
        "client": client,
        "sops": sops,
    }
    return PAConstitution(
        id=constitution_id,
        agent_name=agent_name,
        identity=dict(identity),
        client=dict(client),
        sops=sops,
        job_briefs=job_briefs,
        selectors=selectors,
        source=source,
        hash=behavior_hash(identity_payload),
    )


def resolve_context(
    config: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
) -> PAResolvedContext | None:
    """Resolve a constitution and job brief for runtime metadata.

    Config may provide a constitution as ``constitution`` / ``pa_constitution``
    (dataclass, mapping, or path) or as ``constitution_path`` /
    ``pa_constitution_path``.  Job selection prefers selectors, then explicit
    ``job_type`` in metadata or config.
    """
    if not isinstance(config, Mapping):
        return None
    if isinstance(config.get("pa"), Mapping) and not any(
        key in config
        for key in (
            "constitution",
            "pa_constitution",
            "constitution_path",
            "pa_constitution_path",
        )
    ):
        config = config["pa"]
    enabled = config.get("enabled")
    if enabled is False or (
        isinstance(enabled, str)
        and enabled.strip().lower() in {"false", "0", "no", "off"}
    ):
        return None

    constitution = _constitution_from_config(config)
    if constitution is None:
        return None

    metadata_map: Mapping[str, Any] = metadata if isinstance(metadata, Mapping) else {}
    job_type = _as_optional_str(metadata_map.get("job_type")) or _as_optional_str(config.get("job_type"))
    if job_type is None:
        job_type = _select_job_type(constitution, metadata_map)
    if job_type is None:
        return None

    job_brief = constitution.job_briefs.get(job_type)
    if job_brief is None:
        raise ValueError(f"{constitution.source}: unknown PA job_type {job_type!r}")

    return PAResolvedContext(
        constitution=constitution,
        job_brief=job_brief,
        job_type=job_type,
        metadata=dict(metadata_map),
        identity_hash=constitution.hash,
        job_hash=job_brief.hash,
        behavior_hash=behavior_hash(constitution.hash, job_brief.hash),
    )


def render_identity_prompt(constitution: PAConstitution) -> str:
    """Render the durable identity block for a PA."""
    lines = [
        "# Personal Assistant Identity",
        f"Constitution ID: {constitution.id}",
        f"Agent name: {constitution.agent_name}",
        "",
        "## Identity",
        *_render_mapping(constitution.identity),
        "",
        "## Client",
        *_render_mapping(constitution.client),
    ]
    if constitution.sops:
        lines.extend(["", "## Standing Operating Procedures"])
        lines.extend(f"- {sop}" for sop in constitution.sops)
    lines.extend(["", f"Identity behavior hash: {constitution.hash}"])
    return "\n".join(lines).strip()


def render_job_prompt(resolved: PAResolvedContext) -> str:
    """Render the selected job brief block."""
    brief = resolved.job_brief
    lines = [
        "# Personal Assistant Job Brief",
        f"Job type: {resolved.job_type}",
        f"Title: {brief.title}",
        f"Purpose: {brief.purpose}",
    ]
    if brief.instructions:
        lines.extend(["", "## Instructions"])
        lines.extend(f"- {instruction}" for instruction in brief.instructions)
    if brief.enabled_toolsets:
        lines.extend(["", "## Enabled Toolsets"])
        lines.extend(f"- {toolset}" for toolset in brief.enabled_toolsets)
    if brief.disabled_toolsets:
        lines.extend(["", "## Disabled Toolsets"])
        lines.extend(f"- {toolset}" for toolset in brief.disabled_toolsets)
    if brief.fact_operations:
        lines.extend(["", "## Fact Operations"])
        lines.extend(_render_mapping(brief.fact_operations))
    if brief.response_policy:
        lines.extend(["", "## Response Policy"])
        lines.extend(_render_mapping(brief.response_policy))
    lines.extend(
        [
            "",
            f"Identity hash: {resolved.identity_hash}",
            f"Job hash: {resolved.job_hash}",
            f"Behavior hash: {resolved.behavior_hash}",
        ]
    )
    return "\n".join(lines).strip()


def behavior_hash(*parts: Any) -> str:
    """Return a stable SHA-256 hash for arbitrary JSON-like behavior parts."""
    canonical = json.dumps(_normalize(parts), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_job_brief(job_type: str, raw_brief: Mapping[str, Any], source: str) -> PAJobBrief:
    title = _required_str(raw_brief, "title", f"{source}.job_briefs.{job_type}")
    purpose = _required_str(raw_brief, "purpose", f"{source}.job_briefs.{job_type}")
    instructions = _string_tuple(raw_brief.get("instructions", ()), f"{source}.job_briefs.{job_type}.instructions")
    runtime = _optional_mapping(raw_brief.get("runtime", {}), f"{source}.job_briefs.{job_type}.runtime")
    fact_operations = _optional_mapping(raw_brief.get("fact_operations", {}), f"{source}.job_briefs.{job_type}.fact_operations")
    response_policy = _optional_mapping(raw_brief.get("response_policy", {}), f"{source}.job_briefs.{job_type}.response_policy")
    enabled_toolsets = _string_tuple(
        raw_brief.get("enabled_toolsets", ()),
        f"{source}.job_briefs.{job_type}.enabled_toolsets",
    )
    disabled_toolsets = _string_tuple(
        raw_brief.get("disabled_toolsets", ()),
        f"{source}.job_briefs.{job_type}.disabled_toolsets",
    )
    require_mention = _optional_bool(
        raw_brief.get("require_mention"),
        f"{source}.job_briefs.{job_type}.require_mention",
    )
    debounce_passive_ms = _optional_int(
        raw_brief.get("debounce_passive_ms"),
        f"{source}.job_briefs.{job_type}.debounce_passive_ms",
    )
    debounce_addressed_ms = _optional_int(
        raw_brief.get("debounce_addressed_ms"),
        f"{source}.job_briefs.{job_type}.debounce_addressed_ms",
    )
    payload = {
        "job_type": job_type,
        "title": title,
        "purpose": purpose,
        "instructions": instructions,
        "runtime": runtime,
        "enabled_toolsets": enabled_toolsets,
        "disabled_toolsets": disabled_toolsets,
        "fact_operations": fact_operations,
        "response_policy": response_policy,
        "require_mention": require_mention,
        "debounce_passive_ms": debounce_passive_ms,
        "debounce_addressed_ms": debounce_addressed_ms,
    }
    return PAJobBrief(
        job_type=job_type,
        title=title,
        purpose=purpose,
        instructions=instructions,
        runtime=dict(runtime),
        enabled_toolsets=enabled_toolsets,
        disabled_toolsets=disabled_toolsets,
        fact_operations=dict(fact_operations),
        response_policy=dict(response_policy),
        require_mention=require_mention,
        debounce_passive_ms=debounce_passive_ms,
        debounce_addressed_ms=debounce_addressed_ms,
        hash=behavior_hash(payload),
    )


def _load_selector(
    selector: Mapping[str, Any],
    job_briefs: Mapping[str, PAJobBrief],
    source: str,
) -> Mapping[str, Any]:
    if not isinstance(selector, Mapping):
        raise ValueError(f"{source}.selectors: each selector must be a mapping")
    job_type = _required_str(selector, "job_type", f"{source}.selectors[]")
    if job_type not in job_briefs:
        raise ValueError(f"{source}.selectors[]: unknown job_type {job_type!r}")
    match = _required_mapping(selector, "match", f"{source}.selectors[{job_type}]")
    return {"job_type": job_type, "match": dict(match)}


def _constitution_from_config(config: Mapping[str, Any]) -> PAConstitution | None:
    raw = (
        config.get("constitution")
        or config.get("pa_constitution")
        or config.get("constitution_path")
        or config.get("pa_constitution_path")
    )
    if raw is None:
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    overlay = _client_overlay_from_config(config)
    if isinstance(raw, PAConstitution):
        return apply_client_overlay(raw, overlay) if overlay else raw
    if isinstance(raw, Mapping):
        return load_constitution_data(raw, client_overlay=overlay)
    if isinstance(raw, str | Path):
        constitution = load_constitution(raw)
        return apply_client_overlay(constitution, overlay) if overlay else constitution
    raise ValueError("PA constitution config must be a path, mapping, or PAConstitution")


def apply_client_overlay(
    constitution: PAConstitution,
    overlay: Mapping[str, Any] | None,
) -> PAConstitution:
    """Return a constitution with identity-visible fields overridden by a client overlay."""
    if not overlay:
        return constitution
    from agent.pa_client_overlay import merge_client_overlay

    agent_name, identity, client = merge_client_overlay(
        agent_name=constitution.agent_name,
        identity=constitution.identity,
        client=constitution.client,
        overlay=overlay,
        source=f"{constitution.source}.client_overlay",
    )
    identity_payload = {
        "id": constitution.id,
        "agent_name": agent_name,
        "identity": identity,
        "client": client,
        "sops": constitution.sops,
    }
    return PAConstitution(
        id=constitution.id,
        agent_name=agent_name,
        identity=identity,
        client=client,
        sops=constitution.sops,
        job_briefs=constitution.job_briefs,
        selectors=constitution.selectors,
        source=constitution.source,
        hash=behavior_hash(identity_payload),
    )


def _client_overlay_from_config(config: Mapping[str, Any]) -> Mapping[str, Any] | None:
    raw = (
        config.get("client_overlay")
        or config.get("pa_client_overlay")
        or config.get("client_overlay_path")
        or config.get("pa_client_overlay_path")
        or config.get("overlay")
        or config.get("overlay_path")
    )
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str | Path):
        if isinstance(raw, str) and not raw.strip():
            return None
        from agent.pa_client_overlay import load_client_overlay

        return load_client_overlay(raw)
    raise ValueError("PA client overlay config must be a path or mapping")


def _select_job_type(constitution: PAConstitution, metadata: Mapping[str, Any]) -> str | None:
    for selector in constitution.selectors:
        match = selector.get("match", {})
        if isinstance(match, Mapping) and all(_metadata_value(metadata, key) == expected for key, expected in match.items()):
            return _as_optional_str(selector.get("job_type"))
    return None


def _metadata_value(metadata: Mapping[str, Any], dotted_key: str) -> Any:
    current: Any = metadata
    for part in dotted_key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _render_mapping(data: Mapping[str, Any]) -> list[str]:
    return [f"- {key}: {_render_value(value)}" for key, value in sorted(data.items(), key=lambda item: item[0])]


def _render_value(value: Any) -> str:
    if isinstance(value, list | tuple):
        return ", ".join(str(item) for item in value)
    if isinstance(value, Mapping):
        return json.dumps(_normalize(value), sort_keys=True, ensure_ascii=True)
    return str(value)


def _required_str(data: Mapping[str, Any], key: str, source: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source}.{key}: required non-empty string")
    return value


def _as_optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_bool(value: Any, source: str) -> bool | None:
    """Parse an optional brief bool leniently; absent / None -> None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True
        if text in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"{source}: must be a boolean")


def _optional_int(value: Any, source: str) -> int | None:
    """Parse an optional brief int leniently; absent / None -> None."""
    if value is None:
        return None
    if isinstance(value, bool):
        # bool is a subclass of int; reject to avoid True->1 ms surprises.
        raise ValueError(f"{source}: must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError as exc:
            raise ValueError(f"{source}: must be an integer") from exc
    raise ValueError(f"{source}: must be an integer")


def _required_mapping(data: Mapping[str, Any], key: str, source: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{source}.{key}: required mapping")
    return value


def _optional_mapping(value: Any, source: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{source}: must be a mapping")
    return value


def _string_tuple(value: Any, source: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list | tuple):
        raise ValueError(f"{source}: must be a list of strings")
    if not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{source}: must contain only non-empty strings")
    return tuple(value)


def _normalize(value: Any) -> Any:
    if isinstance(value, PAConstitution):
        return {
            "id": value.id,
            "agent_name": value.agent_name,
            "identity": value.identity,
            "client": value.client,
            "sops": value.sops,
            "job_briefs": value.job_briefs,
            "selectors": value.selectors,
            "hash": value.hash,
        }
    if isinstance(value, PAJobBrief):
        return {
            "job_type": value.job_type,
            "title": value.title,
            "purpose": value.purpose,
            "instructions": value.instructions,
            "runtime": value.runtime,
            "enabled_toolsets": value.enabled_toolsets,
            "disabled_toolsets": value.disabled_toolsets,
            "fact_operations": value.fact_operations,
            "response_policy": value.response_policy,
            "require_mention": value.require_mention,
            "debounce_passive_ms": value.debounce_passive_ms,
            "debounce_addressed_ms": value.debounce_addressed_ms,
            "hash": value.hash,
        }
    if isinstance(value, PAResolvedContext):
        return {
            "job_type": value.job_type,
            "metadata": value.metadata,
            "identity_hash": value.identity_hash,
            "job_hash": value.job_hash,
            "behavior_hash": value.behavior_hash,
        }
    if isinstance(value, Mapping):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_normalize(item) for item in value]
    return value
