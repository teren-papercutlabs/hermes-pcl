"""Shared replay-evaluation instrumentation for PA agents.

The native replay runner owns execution and isolation.  This module adds the
measurement layer around it: immutable input pins, per-arm trace checks,
receipt indexing, and paired comparison.  Client semantics remain in config
and score manifests; no tenant schema is hard-coded here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from gateway.replay import ReplayCorpus, ReplayPlan, canonical_digest, canonical_json


CONFIG_SCHEMA = "hermes-replay-eval/v1"
SCORE_SCHEMA = "hermes-replay-eval-score/v1"
RECEIPT_SCHEMA = "hermes-replay-eval-receipt/v1"
INDEX_SCHEMA = "hermes-replay-eval-receipt-index/v1"
COMPARISON_SCHEMA = "hermes-replay-eval-comparison/v1"
TRACE_SCHEMA = "hermes-adaptive-trace/v1"
PROBE_SCHEMA = "hermes-replay-eval-probes/v1"


class EvalInstrumentError(RuntimeError):
    """Configuration, integrity, or evaluation failure."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(value)).strip("-")
    return cleaned or "evaluation"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temp.replace(path)


def _read_document(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        import yaml

        return yaml.safe_load(text)
    return json.loads(text)


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        expanded = os.path.expandvars(value)
        if re.search(r"\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*", expanded):
            raise EvalInstrumentError(f"unresolved environment reference: {value}")
        return expanded
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _expand_env(item) for key, item in value.items()}
    return value


def _resolve_path(base_dir: Path, value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise EvalInstrumentError(f"{label} path is required")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EvalInstrumentError(f"{label} must be an object")
    return dict(value)


def _require_string(mapping: Mapping[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvalInstrumentError(f"{label}.{key} is required")
    return value.strip()


def _verify_expected_sha(path: Path, expected: Any, *, label: str) -> str:
    if not path.is_file():
        raise EvalInstrumentError(f"{label} does not exist: {path}")
    actual = _sha256_file(path)
    if expected and str(expected).removeprefix("sha256:") != actual:
        raise EvalInstrumentError(
            f"{label} sha256 mismatch: expected={expected} actual={actual}"
        )
    return actual


@dataclass(frozen=True)
class LoadedEvalConfig:
    path: Path
    data: dict[str, Any]

    @property
    def base_dir(self) -> Path:
        return self.path.parent


def load_eval_config(path: str | Path) -> LoadedEvalConfig:
    config_path = Path(path).expanduser().resolve()
    raw = _read_document(config_path)
    data = _expand_env(_require_mapping(raw, "eval config"))
    if data.get("schema") != CONFIG_SCHEMA:
        raise EvalInstrumentError(
            f"eval config schema must be {CONFIG_SCHEMA!r}; got {data.get('schema')!r}"
        )
    _require_string(data, "instrument_id", "config")
    agent = _require_mapping(data.get("agent"), "config.agent")
    _require_string(agent, "id", "config.agent")
    _require_string(agent, "constitution", "config.agent")
    _require_string(agent, "deployment_manifest", "config.agent")
    corpus = _require_mapping(data.get("corpus"), "config.corpus")
    _require_string(corpus, "manifest", "config.corpus")
    sources = corpus.get("sources")
    if not isinstance(sources, list) or not sources:
        raise EvalInstrumentError("config.corpus.sources must be a non-empty list")
    tenant = _require_mapping(data.get("tenant"), "config.tenant")
    _require_string(tenant, "slug", "config.tenant")
    if tenant.get("isolation") != "process_data_root":
        raise EvalInstrumentError(
            "config.tenant.isolation must be 'process_data_root' (tenant-name-only isolation is unsafe)"
        )
    arms = data.get("arms")
    if not isinstance(arms, list) or len(arms) < 2:
        raise EvalInstrumentError("config.arms must contain at least two model arms")
    arm_ids: set[str] = set()
    for index, raw_arm in enumerate(arms):
        arm = _require_mapping(raw_arm, f"config.arms[{index}]")
        arm_id = _require_string(arm, "id", f"config.arms[{index}]")
        _require_string(arm, "provider", f"config.arms[{index}]")
        _require_string(arm, "model", f"config.arms[{index}]")
        if arm_id in arm_ids:
            raise EvalInstrumentError(f"duplicate arm id: {arm_id}")
        arm_ids.add(arm_id)
    integrity = _require_mapping(data.get("integrity"), "config.integrity")
    seed = _require_mapping(integrity.get("seed_boundary"), "config.integrity.seed_boundary")
    _require_string(seed, "cutoff", "config.integrity.seed_boundary")
    _require_string(seed, "snapshot", "config.integrity.seed_boundary")
    twins = _require_mapping(integrity.get("twin_sequences"), "config.integrity.twin_sequences")
    if twins.get("agent_policy") != "include_and_score":
        raise EvalInstrumentError("twin agent_policy must be include_and_score")
    if twins.get("judge_policy") != "exclude_future_resolution":
        raise EvalInstrumentError("twin judge_policy must be exclude_future_resolution")
    trace = _require_mapping(data.get("trace"), "config.trace")
    probes = trace.get("paired_probes")
    probe_manifest = trace.get("paired_probe_manifest")
    if (not isinstance(probes, list) or not probes) and not probe_manifest:
        raise EvalInstrumentError(
            "config.trace requires paired_probes or paired_probe_manifest"
        )
    return LoadedEvalConfig(path=config_path, data=data)


def _trace_probes(config: LoadedEvalConfig) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    trace = dict(config.data["trace"])
    inline = trace.get("paired_probes")
    if isinstance(inline, list) and inline:
        return [
            _require_mapping(probe, f"config.trace.paired_probes[{index}]")
            for index, probe in enumerate(inline)
        ], None
    raw_manifest = trace.get("paired_probe_manifest")
    if isinstance(raw_manifest, Mapping):
        manifest_spec = dict(raw_manifest)
    else:
        manifest_spec = {"path": raw_manifest}
    manifest_path = _resolve_path(
        config.base_dir,
        manifest_spec.get("path") or manifest_spec.get("file"),
        label="paired probe manifest",
    )
    actual_sha = _verify_expected_sha(
        manifest_path,
        manifest_spec.get("sha256"),
        label="paired probe manifest",
    )
    document = _require_mapping(_read_document(manifest_path), "paired probe manifest")
    if document.get("schema") != PROBE_SCHEMA:
        raise EvalInstrumentError(
            f"paired probe manifest schema must be {PROBE_SCHEMA!r}"
        )
    probes = document.get("probes")
    if not isinstance(probes, list) or not probes:
        raise EvalInstrumentError("paired probe manifest probes must be a non-empty list")
    return [
        _require_mapping(probe, f"paired probe manifest probes[{index}]")
        for index, probe in enumerate(probes)
    ], {
        "sha256": actual_sha,
        "probe_count": len(probes),
    }


def _arm(config: LoadedEvalConfig, arm_id: str) -> dict[str, Any]:
    for raw_arm in config.data["arms"]:
        if str(raw_arm.get("id")) == arm_id:
            return dict(raw_arm)
    raise EvalInstrumentError(f"unknown arm id: {arm_id}")


def pin_eval_config(config: LoadedEvalConfig) -> dict[str, Any]:
    data = config.data
    agent = dict(data["agent"])
    corpus = dict(data["corpus"])
    integrity = dict(data["integrity"])
    seed = dict(integrity["seed_boundary"])
    probes, probe_manifest_pin = _trace_probes(config)

    constitution_path = _resolve_path(config.base_dir, agent["constitution"], label="constitution")
    deployment_path = _resolve_path(
        config.base_dir, agent["deployment_manifest"], label="deployment manifest"
    )
    corpus_manifest_path = _resolve_path(
        config.base_dir, corpus["manifest"], label="corpus manifest"
    )
    seed_path = _resolve_path(config.base_dir, seed["snapshot"], label="seed snapshot")

    source_pins: list[dict[str, Any]] = []
    for index, raw_source in enumerate(corpus["sources"]):
        source = _require_mapping(raw_source, f"config.corpus.sources[{index}]")
        source_path = _resolve_path(
            config.base_dir,
            source.get("path") or source.get("file"),
            label=f"corpus source {index}",
        )
        source_pins.append(
            {
                "id": str(source.get("id") or f"source-{index + 1}"),
                "sha256": _verify_expected_sha(
                    source_path, source.get("sha256"), label=f"corpus source {index}"
                ),
                "bytes": source_path.stat().st_size,
                "record_path": source.get("record_path") or source.get("recordPath"),
            }
        )

    media_pin = None
    if corpus.get("media_manifest"):
        media_manifest_path = _resolve_path(
            config.base_dir,
            corpus["media_manifest"],
            label="corpus media manifest",
        )
        media_pin = {
            "sha256": _verify_expected_sha(
                media_manifest_path,
                corpus.get("media_manifest_sha256"),
                label="corpus media manifest",
            ),
            "bytes": media_manifest_path.stat().st_size,
        }

    judge_pin = None
    judge = config.data.get("judge")
    if judge is not None:
        judge_config = _require_mapping(judge, "config.judge")
        judge_sources = judge_config.get("sources")
        if not isinstance(judge_sources, list) or not judge_sources:
            raise EvalInstrumentError("config.judge.sources must be a non-empty list")
        pinned_sources = []
        for index, raw_source in enumerate(judge_sources):
            source = _require_mapping(raw_source, f"config.judge.sources[{index}]")
            source_path = _resolve_path(
                config.base_dir,
                source.get("path") or source.get("file"),
                label=f"judge source {index}",
            )
            pinned_sources.append(
                {
                    "id": str(source.get("id") or f"judge-source-{index + 1}"),
                    "sha256": _verify_expected_sha(
                        source_path,
                        source.get("sha256"),
                        label=f"judge source {index}",
                    ),
                    "bytes": source_path.stat().st_size,
                }
            )
        judge_pin = {
            "policy": judge_config.get("policy"),
            "scorer": judge_config.get("scorer"),
            "sources": pinned_sources,
        }
        judge_pin["digest"] = canonical_digest(judge_pin)

    pins: dict[str, Any] = {
        "schema": CONFIG_SCHEMA,
        "instrument_id": data["instrument_id"],
        "config_sha256": _sha256_file(config.path),
        "agent": {
            "id": agent["id"],
            "job_type": agent.get("job_type"),
            "constitution_sha256": _verify_expected_sha(
                constitution_path, agent.get("constitution_sha256"), label="constitution"
            ),
            "deployment_manifest_sha256": _verify_expected_sha(
                deployment_path,
                agent.get("deployment_manifest_sha256"),
                label="deployment manifest",
            ),
        },
        "corpus": {
            "manifest_sha256": _verify_expected_sha(
                corpus_manifest_path,
                corpus.get("manifest_sha256"),
                label="corpus manifest",
            ),
            "media_manifest": media_pin,
            "sources": source_pins,
        },
        "tenant": {
            "slug": data["tenant"]["slug"],
            "isolation": data["tenant"]["isolation"],
        },
        "integrity": {
            "seed_boundary": {
                "cutoff": seed["cutoff"],
                "snapshot_sha256": _verify_expected_sha(
                    seed_path, seed.get("snapshot_sha256"), label="seed snapshot"
                ),
                "snapshot_bytes": seed_path.stat().st_size,
            },
            "twin_sequences": {
                "agent_policy": integrity["twin_sequences"]["agent_policy"],
                "judge_policy": integrity["twin_sequences"]["judge_policy"],
                "probe_ids": [
                    str(probe.get("id"))
                    for probe in probes
                    if isinstance(probe, Mapping) and probe.get("id")
                ],
                "probe_manifest": probe_manifest_pin,
            },
        },
        "judge": judge_pin,
        "arms": [
            {
                "id": arm["id"],
                "provider": arm["provider"],
                "model": arm["model"],
                "fallback": bool(arm.get("fallback")),
            }
            for arm in data["arms"]
        ],
    }
    pins["digest"] = canonical_digest(pins)
    return pins


def _corpus_from_config_source(
    config: LoadedEvalConfig, source: Mapping[str, Any]
) -> ReplayCorpus:
    spec = dict(source)
    source_path = _resolve_path(
        config.base_dir,
        spec.get("path") or spec.get("file"),
        label="corpus source",
    )
    spec["path"] = str(source_path)
    media_root = spec.get("media_root") or spec.get("mediaRoot")
    if media_root:
        spec["media_root"] = str(
            _resolve_path(config.base_dir, media_root, label="corpus media_root")
        )
    return ReplayCorpus.from_source(spec)


def _runtime_manifest_pin(
    config: LoadedEvalConfig,
    arm_id: str,
    runtime_manifest: Mapping[str, Any] | str | Path | None,
) -> dict[str, Any] | None:
    if runtime_manifest is None:
        return None
    if isinstance(runtime_manifest, Mapping):
        manifest = dict(runtime_manifest)
    else:
        manifest = _require_mapping(
            _read_document(Path(runtime_manifest).expanduser().resolve()),
            "runtime manifest",
        )
    if manifest.get("schema") != "hermes-replay-eval-runtime/v1":
        raise EvalInstrumentError("runtime manifest schema mismatch")
    if manifest.get("instrument_id") != config.data["instrument_id"]:
        raise EvalInstrumentError("runtime manifest instrument mismatch")
    if (manifest.get("arm") or {}).get("id") != arm_id:
        raise EvalInstrumentError("runtime manifest arm mismatch")
    expected_digest = pin_eval_config(config)["digest"]
    if manifest.get("instrument_digest") != expected_digest:
        raise EvalInstrumentError("runtime manifest instrument digest mismatch")
    config_sha = _require_string(manifest, "config_sha256", "runtime manifest")
    constitution_sha = _require_string(
        manifest, "constitution_sha256", "runtime manifest"
    )
    return {
        "config_sha256": config_sha,
        "constitution_sha256": constitution_sha,
    }


def build_arm_plan(
    config: LoadedEvalConfig,
    arm_id: str,
    *,
    runtime_manifest: Mapping[str, Any] | str | Path | None = None,
) -> ReplayPlan:
    pins = pin_eval_config(config)
    arm = _arm(config, arm_id)
    agent = dict(config.data["agent"])
    tenant = dict(config.data["tenant"])
    corpora = [
        _corpus_from_config_source(config, source)
        for source in config.data["corpus"]["sources"]
    ]
    messages: list[dict[str, Any]] = []
    for corpus in corpora:
        for raw_message in corpus.messages:
            message = dict(raw_message)
            job_type = agent.get("job_type")
            if job_type:
                message.setdefault("_hermes_pa_job_type", job_type)
                message.setdefault(
                    "_hermes_pa_context",
                    {
                        "tenant": tenant["slug"],
                        "agent_id": agent["id"],
                        "job_type": job_type,
                    },
                )
            messages.append(message)
    combined = ReplayCorpus.from_messages(
        messages,
        source_type="eval_instrument",
        source_manifest={
            "instrument_id": config.data["instrument_id"],
            "instrument_digest": pins["digest"],
            "source_pins": pins["corpus"]["sources"],
            "seed_boundary": pins["integrity"]["seed_boundary"],
            "twin_sequences": pins["integrity"]["twin_sequences"],
        },
    )
    overlay = {
        "schema": CONFIG_SCHEMA,
        "instrument_id": config.data["instrument_id"],
        "instrument_digest": pins["digest"],
        "agent": pins["agent"],
        "tenant": pins["tenant"],
        "arm": {
            "id": arm["id"],
            "provider": arm["provider"],
            "model": arm["model"],
        },
    }
    runtime_pin = _runtime_manifest_pin(config, arm_id, runtime_manifest)
    if runtime_pin:
        overlay["runtime"] = runtime_pin
    return ReplayPlan(
        platform=str(config.data.get("platform") or "whatsapp"),
        messages=combined.messages,
        delivery_mode="capture",
        bypass_require_mention=True,
        bypass_auth=True,
        source_path=str(_resolve_path(config.base_dir, config.data["corpus"]["manifest"], label="corpus manifest")),
        replay_policy=combined.replay_policy_manifest(),
        corpus_manifest=combined.manifest(),
        config_overlay_manifest=overlay,
    )


def write_arm_plan(
    config: LoadedEvalConfig,
    arm_id: str,
    output_path: str | Path,
    *,
    runtime_manifest: Mapping[str, Any] | str | Path | None = None,
) -> dict[str, Any]:
    plan = build_arm_plan(config, arm_id, runtime_manifest=runtime_manifest)
    path = Path(output_path).expanduser().resolve()
    _write_json_atomic(path, plan.to_dict())
    return {
        "ok": True,
        "path": str(path),
        "sha256": _sha256_file(path),
        "arm_id": arm_id,
        "corpus": plan.readable_corpus_manifest(),
        "config_overlay_digest": canonical_digest(plan.config_overlay_manifest),
    }


def _set_nested(mapping: dict[str, Any], path: Sequence[str], value: Any) -> None:
    current = mapping
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[path[-1]] = value


def materialize_arm_runtime(
    config: LoadedEvalConfig,
    arm_id: str,
    output_dir: str | Path,
    *,
    business_base_url: str | None = None,
) -> dict[str, Any]:
    """Materialize an isolated HERMES_HOME for one model arm."""

    arm = _arm(config, arm_id)
    agent = dict(config.data["agent"])
    runtime = dict(config.data.get("runtime") or {})
    config_template = _resolve_path(
        config.base_dir,
        runtime.get("config_template") or agent.get("config_template"),
        label="runtime config template",
    )
    constitution_template = _resolve_path(
        config.base_dir, agent["constitution"], label="constitution"
    )
    out = Path(output_dir).expanduser().resolve()
    if out.exists() and any(out.iterdir()):
        raise EvalInstrumentError(f"runtime output must be empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    hermes_config = _require_mapping(_read_document(config_template), "runtime config")
    constitution = _require_mapping(_read_document(constitution_template), "constitution")
    provider = arm["provider"]
    model = arm["model"]
    _set_nested(hermes_config, ["model", "provider"], provider)
    _set_nested(hermes_config, ["model", "default"], model)
    _set_nested(hermes_config, ["pa", "constitution_path"], str(out / "constitution.yaml"))
    _set_nested(hermes_config, ["streaming", "enabled"], False)
    _set_nested(constitution, ["runtime", "provider"], provider)
    _set_nested(constitution, ["runtime", "model"], model)
    for brief in (constitution.get("job_briefs") or {}).values():
        if isinstance(brief, dict):
            brief_runtime = brief.setdefault("runtime", {})
            if isinstance(brief_runtime, dict):
                brief_runtime["provider"] = provider
                brief_runtime["model"] = model
    provider_profile = dict(arm.get("provider_profile") or {})
    if provider_profile:
        provider_profile.setdefault("default_model", model)
        hermes_config["providers"] = {provider: provider_profile}
    operation_overrides = runtime.get("operation_overrides") or {}
    if operation_overrides:
        operations = hermes_config
        for key in ("pa", "overlay", "client", "business_bridge", "operations"):
            child = operations.get(key)
            if not isinstance(child, dict):
                child = {}
                operations[key] = child
            operations = child
        for operation_name, raw_operation in dict(operation_overrides).items():
            operations[str(operation_name)] = _require_mapping(
                raw_operation,
                f"runtime.operation_overrides.{operation_name}",
            )
    if business_base_url:
        old_base = str(runtime.get("business_base_url_from") or "")

        def replace_urls(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in list(value.items()):
                    if key == "url" and isinstance(child, str) and old_base and child.startswith(old_base):
                        value[key] = business_base_url.rstrip("/") + child[len(old_base) :]
                    else:
                        replace_urls(child)
            elif isinstance(value, list):
                for child in value:
                    replace_urls(child)

        replace_urls(hermes_config)
    import yaml

    (out / "config.yaml").write_text(
        yaml.safe_dump(hermes_config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    (out / "constitution.yaml").write_text(
        yaml.safe_dump(constitution, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    soul_value = agent.get("soul")
    if soul_value:
        soul_path = _resolve_path(config.base_dir, soul_value, label="SOUL")
        (out / "SOUL.md").write_bytes(soul_path.read_bytes())
    (out / "sessions").mkdir(exist_ok=True)
    manifest = {
        "schema": "hermes-replay-eval-runtime/v1",
        "instrument_id": config.data["instrument_id"],
        "instrument_digest": pin_eval_config(config)["digest"],
        "arm": {"id": arm_id, "provider": provider, "model": model},
        "config_sha256": _sha256_file(out / "config.yaml"),
        "constitution_sha256": _sha256_file(out / "constitution.yaml"),
        "source_constitution_sha256": _sha256_file(constitution_template),
        "created_at": _utc_now(),
    }
    _write_json_atomic(out / "eval-runtime-manifest.json", manifest)
    return {**manifest, "path": str(out / "eval-runtime-manifest.json")}


def _json_value(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def _open_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_trace_turns(
    session_db: Path,
    *,
    run_id: str,
    attempt_id: str | None,
    agent_id: str,
) -> list[dict[str, Any]]:
    clauses = ["replay_run_id = ?", "agent_id = ?"]
    params: list[Any] = [run_id, agent_id]
    if attempt_id:
        clauses.append("replay_attempt_id = ?")
        params.append(attempt_id)
    with _open_readonly(session_db) as conn:
        turn_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM pa_turns WHERE "
                + " AND ".join(clauses)
                + " ORDER BY started_at, turn_id",
                params,
            )
        ]
        for turn in turn_rows:
            turn_id = turn["turn_id"]
            turn["message_refs"] = _json_value(turn.get("message_refs_json"), [])
            turn["raw_envelope"] = _json_value(turn.get("raw_turn_envelope_json"), {})
            turn["tool_calls"] = [
                {
                    **dict(row),
                    "input": _json_value(row["input_json"], {}),
                    "result": _json_value(row["result_json"], {}),
                }
                for row in conn.execute(
                    "SELECT * FROM pa_tool_calls WHERE turn_id = ? ORDER BY id", (turn_id,)
                )
            ]
            turn["events"] = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM pa_events WHERE turn_id = ? ORDER BY id", (turn_id,)
                )
            ]
    return turn_rows


def _judgment_turn(turn: Mapping[str, Any], trace: Mapping[str, Any]) -> bool:
    layer = dict(trace.get("judgment_layer") or {})
    tools = [str(call.get("tool_name") or "") for call in turn.get("tool_calls") or []]
    excludes = {str(name) for name in layer.get("exclude_tool_names") or []}
    if tools and all(name in excludes for name in tools):
        return False
    prefixes = [str(prefix) for prefix in layer.get("tool_prefixes") or []]
    names = {str(name) for name in layer.get("tool_names") or []}
    if prefixes or names:
        return any(name in names or any(name.startswith(prefix) for prefix in prefixes) for name in tools)
    return True


def _model_output_present(turn: Mapping[str, Any]) -> bool:
    envelope = turn.get("raw_envelope") or {}
    messages = envelope.get("messages") if isinstance(envelope, Mapping) else []
    if not isinstance(messages, list) or not messages:
        return False
    last_user = max(
        (
            index
            for index, message in enumerate(messages)
            if isinstance(message, Mapping) and message.get("role") == "user"
        ),
        default=-1,
    )
    current_turn = messages[last_user + 1 :]
    assistant_call_ids: set[str] = set()
    model_output_seen = False
    for message in current_turn:
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        if role == "assistant":
            has_output = bool(
                message.get("content")
                or message.get("tool_calls")
                or message.get("reasoning")
                or message.get("reasoning_content")
                or message.get("reasoning_details")
                or message.get("codex_reasoning_items")
            )
            model_output_seen = model_output_seen or has_output
            for call in message.get("tool_calls") or []:
                if isinstance(call, Mapping) and call.get("id"):
                    assistant_call_ids.add(str(call["id"]))
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if not model_output_seen or (call_id and call_id not in assistant_call_ids):
                return False
    api_calls = envelope.get("api_calls") if isinstance(envelope, Mapping) else None
    return bool(model_output_seen and api_calls is not None and int(api_calls) > 0)


def _sequence(turns: Sequence[Mapping[str, Any]]) -> list[str]:
    sequence: list[str] = []
    for turn in turns:
        names = [str(call.get("tool_name") or "") for call in turn.get("tool_calls") or []]
        sequence.extend(name for name in names if name)
        sequence.append("<turn>")
    if sequence and sequence[-1] == "<turn>":
        sequence.pop()
    return sequence


def _selector_matches(turn: Mapping[str, Any], selector: Mapping[str, Any]) -> bool:
    refs = {str(ref) for ref in turn.get("message_refs") or []}
    wanted_refs = {str(ref) for ref in selector.get("message_refs_any") or []}
    if wanted_refs and refs.isdisjoint(wanted_refs):
        return False
    wanted_turns = {str(value) for value in selector.get("turn_ids") or []}
    if wanted_turns and str(turn.get("turn_id")) not in wanted_turns:
        return False
    tool_names = {str(call.get("tool_name") or "") for call in turn.get("tool_calls") or []}
    wanted_tools = {str(value) for value in selector.get("tool_names_any") or []}
    if wanted_tools and tool_names.isdisjoint(wanted_tools):
        return False
    event_types = {str(event.get("event_type") or "") for event in turn.get("events") or []}
    wanted_events = {str(value) for value in selector.get("event_types_any") or []}
    if wanted_events and event_types.isdisjoint(wanted_events):
        return False
    pointers = {
        str(call.get("client_entity_pointer") or "")
        for call in turn.get("tool_calls") or []
    }
    wanted_pointers = {
        str(value) for value in selector.get("client_entity_pointers_any") or []
    }
    if wanted_pointers and pointers.isdisjoint(wanted_pointers):
        return False
    return bool(wanted_refs or wanted_turns or wanted_tools or wanted_events or wanted_pointers)


def _runtime_file_manifest(code_manifest: Mapping[str, Any]) -> dict[str, Any]:
    value = code_manifest.get("runtime_files") or code_manifest.get("runtimeFiles") or {}
    return dict(value) if isinstance(value, Mapping) else {}


def evaluate_adaptive_trace(
    config: LoadedEvalConfig,
    arm_id: str,
    *,
    run_manifest_path: str | Path,
    session_db_path: str | Path,
) -> dict[str, Any]:
    pins = pin_eval_config(config)
    arm = _arm(config, arm_id)
    manifest_path = Path(run_manifest_path).expanduser().resolve()
    session_db = Path(session_db_path).expanduser().resolve()
    manifest = _require_mapping(_read_document(manifest_path), "replay run manifest")
    attempts = manifest.get("attempts") or []
    if not attempts:
        raise EvalInstrumentError("replay run manifest has no attempts")
    attempt = dict(attempts[-1])
    run_id = str(manifest.get("run_id") or attempt.get("run_id") or "")
    attempt_id = str(attempt.get("attempt_id") or "") or None
    turns = _load_trace_turns(
        session_db,
        run_id=run_id,
        attempt_id=attempt_id,
        agent_id=pins["agent"]["id"],
    )
    trace_config = dict(config.data["trace"])
    scoped = [turn for turn in turns if _judgment_turn(turn, trace_config)]

    sequence_rows = []
    for turn in scoped:
        sequence = _sequence([turn])
        sequence_rows.append(
            {
                "turn_id": turn["turn_id"],
                "sequence": sequence,
                "digest": canonical_digest(sequence),
            }
        )
    distinct = sorted({row["digest"] for row in sequence_rows})
    minimum = int(trace_config.get("min_distinct_sequences") or 2)
    variance = {
        "name": "sequence-variance",
        "ok": len(distinct) >= minimum,
        "actual": {
            "judgment_turns": len(scoped),
            "distinct_sequences": len(distinct),
            "sequence_digests": distinct,
        },
        "expected": {"min_distinct_sequences": minimum},
    }

    probe_rows: list[dict[str, Any]] = []
    paired_probes, _probe_manifest_pin = _trace_probes(config)
    for raw_probe in paired_probes:
        probe = _require_mapping(raw_probe, "paired probe")
        probe_id = _require_string(probe, "id", "paired probe")
        left_selector = _require_mapping(probe.get("left"), f"probe {probe_id}.left")
        right_selector = _require_mapping(probe.get("right"), f"probe {probe_id}.right")
        left_turns = [turn for turn in scoped if _selector_matches(turn, left_selector)]
        right_turns = [turn for turn in scoped if _selector_matches(turn, right_selector)]
        left_sequence = _sequence(left_turns)
        right_sequence = _sequence(right_turns)
        left_required = {str(name) for name in probe.get("left_requires_any") or []}
        right_required = {str(name) for name in probe.get("right_requires_any") or []}
        required_ok = (
            (not left_required or not left_required.isdisjoint(left_sequence))
            and (not right_required or not right_required.isdisjoint(right_sequence))
        )
        ok = bool(left_turns and right_turns and left_sequence != right_sequence and required_ok)
        probe_rows.append(
            {
                "id": probe_id,
                "ok": ok,
                "left": {
                    "turn_ids": [turn["turn_id"] for turn in left_turns],
                    "sequence": left_sequence,
                    "digest": canonical_digest(left_sequence),
                },
                "right": {
                    "turn_ids": [turn["turn_id"] for turn in right_turns],
                    "sequence": right_sequence,
                    "digest": canonical_digest(right_sequence),
                },
                "required_tools_ok": required_ok,
            }
        )
    paired = {
        "name": "paired-probes",
        "ok": bool(probe_rows) and all(row["ok"] for row in probe_rows),
        "actual": {"probes": probe_rows},
        "expected": "every configured pair resolves on both sides and diverges at the decision path",
    }

    missing_reasoning = [turn["turn_id"] for turn in scoped if not _model_output_present(turn)]
    reasoning = {
        "name": "reasoning-present",
        "ok": bool(scoped) and not missing_reasoning,
        "actual": {
            "judgment_turns": len(scoped),
            "missing_model_output_turn_ids": missing_reasoning,
        },
        "expected": {"missing_model_output_turn_ids": []},
    }

    plan = dict(attempt.get("plan") or {})
    overlay = dict(plan.get("config_overlay_manifest") or {})
    attempt_payload = dict((attempt.get("result") or {}).get("attempt") or {})
    code_manifest = dict(
        attempt_payload.get("code_manifest")
        or plan.get("code_manifest")
        or {}
    )
    runtime_files = _runtime_file_manifest(code_manifest)
    turn_models = sorted({str(turn.get("model")) for turn in scoped if turn.get("model")})
    turn_providers = sorted({str(turn.get("provider")) for turn in scoped if turn.get("provider")})
    target = dict(manifest.get("target") or {})
    descriptor = dict(target.get("descriptor") or plan.get("target_descriptor_manifest") or {})
    runtime_expected = dict(overlay.get("runtime") or {})
    provenance_actual = {
        "instrument_digest": overlay.get("instrument_digest"),
        "constitution_source_sha256": (overlay.get("agent") or {}).get("constitution_sha256") if isinstance(overlay.get("agent"), Mapping) else None,
        "deployment_manifest_sha256": (overlay.get("agent") or {}).get("deployment_manifest_sha256") if isinstance(overlay.get("agent"), Mapping) else None,
        "runtime_config_sha256": runtime_files.get("config_sha256"),
        "runtime_constitution_sha256": runtime_files.get("constitution_sha256"),
        "code_commit": code_manifest.get("git_commit"),
        "code_dirty": code_manifest.get("git_dirty"),
        "turn_models": turn_models,
        "turn_providers": turn_providers,
        "target_mode": descriptor.get("mode"),
        "target_tenant": descriptor.get("tenantSlug") or descriptor.get("tenant_slug"),
    }
    provenance_ok = all(
        [
            overlay.get("instrument_digest") == pins["digest"],
            isinstance(overlay.get("agent"), Mapping),
            (overlay.get("agent") or {}).get("constitution_sha256")
            == pins["agent"]["constitution_sha256"],
            (overlay.get("agent") or {}).get("deployment_manifest_sha256")
            == pins["agent"]["deployment_manifest_sha256"],
            bool(code_manifest.get("git_commit")),
            code_manifest.get("git_dirty") is False,
            turn_models == [arm["model"]],
            turn_providers == [arm["provider"]],
            descriptor.get("mode") == "eval",
            (descriptor.get("tenantSlug") or descriptor.get("tenant_slug"))
            == pins["tenant"]["slug"],
            bool(runtime_files.get("config_sha256")),
            bool(runtime_files.get("constitution_sha256")),
            not runtime_expected
            or (
                runtime_files.get("config_sha256") == runtime_expected.get("config_sha256")
                and runtime_files.get("constitution_sha256")
                == runtime_expected.get("constitution_sha256")
            ),
        ]
    )
    provenance = {
        "name": "provenance",
        "ok": provenance_ok,
        "actual": provenance_actual,
        "expected": {
            "instrument_digest": pins["digest"],
            "constitution_source_sha256": pins["agent"]["constitution_sha256"],
            "deployment_manifest_sha256": pins["agent"]["deployment_manifest_sha256"],
            "model": arm["model"],
            "provider": arm["provider"],
            "target_mode": "eval",
            "target_tenant": pins["tenant"]["slug"],
            "code_dirty": False,
        },
    }
    checks = [variance, paired, reasoning, provenance]
    return {
        "schema": TRACE_SCHEMA,
        "ok": all(check["ok"] for check in checks),
        "instrument_id": config.data["instrument_id"],
        "arm_id": arm_id,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "checks": checks,
        "stats": {
            "recorded_turns": len(turns),
            "judgment_turns": len(scoped),
            "tool_calls": sum(len(turn.get("tool_calls") or []) for turn in scoped),
        },
        "checked_at": _utc_now(),
    }


def _load_score(path: Path, arm_id: str, required_metrics: Sequence[str]) -> dict[str, Any]:
    score = _require_mapping(_read_document(path), "score manifest")
    if score.get("schema") != SCORE_SCHEMA:
        raise EvalInstrumentError(f"score manifest schema must be {SCORE_SCHEMA}")
    if score.get("arm_id") != arm_id:
        raise EvalInstrumentError(
            f"score arm mismatch: expected={arm_id} actual={score.get('arm_id')}"
        )
    metrics = _require_mapping(score.get("metrics"), "score.metrics")
    missing = [key for key in required_metrics if key not in metrics]
    if missing:
        raise EvalInstrumentError(f"score manifest missing metrics: {', '.join(missing)}")
    return score


def _append_receipt_index(index_path: Path, entry: Mapping[str, Any]) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, dict[str, Any]] = {}
    if index_path.exists():
        for line in index_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            existing[str(row.get("receipt_id"))] = row
    receipt_id = str(entry["receipt_id"])
    if receipt_id in existing:
        if canonical_json(existing[receipt_id]) != canonical_json(entry):
            raise EvalInstrumentError(f"receipt index conflict for {receipt_id}")
        return
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(canonical_json(entry) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def record_evaluation_invocation(
    *,
    config_path: str | Path,
    arm_id: str,
    mode: str,
    invocation_id: str,
    run_manifest_path: str | Path,
    session_db_path: str | Path,
    output_dir: str | Path,
    receipt_index_path: str | Path,
    score_manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    if mode not in {"eval", "graduation"}:
        raise EvalInstrumentError("mode must be eval or graduation")
    config = load_eval_config(config_path)
    pins = pin_eval_config(config)
    receipt_id = f"{_safe_name(invocation_id)}:{_safe_name(arm_id)}:{mode}"
    out = Path(output_dir).expanduser().resolve()
    receipt_path = out / f"{_safe_name(invocation_id)}-{_safe_name(arm_id)}-{mode}.json"
    index_path = Path(receipt_index_path).expanduser().resolve()
    score: dict[str, Any] | None = None
    trace: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    run_manifest_sha256: str | None = None
    session_db_sha256: str | None = None
    score_manifest_sha256: str | None = None
    try:
        run_manifest_sha256 = _verify_expected_sha(
            Path(run_manifest_path).expanduser().resolve(),
            None,
            label="run manifest",
        )
        session_db_sha256 = _verify_expected_sha(
            Path(session_db_path).expanduser().resolve(),
            None,
            label="session db",
        )
        trace = evaluate_adaptive_trace(
            config,
            arm_id,
            run_manifest_path=run_manifest_path,
            session_db_path=session_db_path,
        )
        if score_manifest_path:
            metric_defs = config.data.get("metrics") or []
            required = [
                str(item.get("key"))
                for item in metric_defs
                if isinstance(item, Mapping) and item.get("key")
            ]
            score_path = Path(score_manifest_path).expanduser().resolve()
            score_manifest_sha256 = _verify_expected_sha(
                score_path, None, label="score manifest"
            )
            score = _load_score(score_path, arm_id, required)
    except Exception as exc:  # receipt failures too; the caller still fails closed
        error = {"type": type(exc).__name__, "message": str(exc)}
    ok = bool(trace and trace.get("ok") and error is None)
    if score is not None:
        ok = ok and bool(score.get("eligible", True))
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "receipt_id": receipt_id,
        "invocation_id": invocation_id,
        "mode": mode,
        "instrument_id": config.data["instrument_id"],
        "arm_id": arm_id,
        "ok": ok,
        "pins": pins,
        "run_manifest_sha256": run_manifest_sha256,
        "session_db_sha256": session_db_sha256,
        "trace": trace,
        "score": score,
        "error": error,
        "created_at": _utc_now(),
    }
    if score_manifest_path:
        receipt["score_manifest_sha256"] = score_manifest_sha256
    if receipt_path.exists():
        existing = _require_mapping(_read_document(receipt_path), "existing receipt")
        stable_existing = {key: value for key, value in existing.items() if key != "created_at"}
        stable_new = {key: value for key, value in receipt.items() if key != "created_at"}
        if canonical_json(stable_existing) != canonical_json(stable_new):
            raise EvalInstrumentError(f"immutable receipt already exists with different content: {receipt_path}")
        receipt = existing
    else:
        _write_json_atomic(receipt_path, receipt)
    entry = {
        "schema": INDEX_SCHEMA,
        "receipt_id": receipt_id,
        "invocation_id": invocation_id,
        "mode": mode,
        "instrument_id": config.data["instrument_id"],
        "arm_id": arm_id,
        "ok": bool(receipt["ok"]),
        "receipt_path": str(receipt_path),
        "receipt_sha256": _sha256_file(receipt_path),
        "created_at": receipt["created_at"],
    }
    _append_receipt_index(index_path, entry)
    return {**receipt, "receipt_path": str(receipt_path), "receipt_index_path": str(index_path)}


def audit_receipt_index(
    index_path: str | Path,
    *,
    instrument_id: str | None = None,
    mode: str | None = None,
    expected_invocation_ids: Iterable[str] = (),
) -> dict[str, Any]:
    path = Path(index_path).expanduser().resolve()
    rows: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(_require_mapping(json.loads(line), "receipt index row"))
    scoped = [
        row
        for row in rows
        if (instrument_id is None or row.get("instrument_id") == instrument_id)
        and (mode is None or row.get("mode") == mode)
    ]
    expected = {str(value) for value in expected_invocation_ids}
    observed = {str(row.get("invocation_id")) for row in scoped}
    missing = sorted(expected - observed)
    failed = [row["receipt_id"] for row in scoped if not row.get("ok")]
    missing_files = [
        row["receipt_id"]
        for row in scoped
        if not Path(str(row.get("receipt_path") or "")).is_file()
    ]
    receipt_ids = [str(row.get("receipt_id") or "") for row in scoped]
    duplicate_ids = sorted(
        {receipt_id for receipt_id in receipt_ids if receipt_ids.count(receipt_id) > 1}
    )
    invalid_receipts: list[str] = []
    for row in scoped:
        receipt_id = str(row.get("receipt_id") or "")
        receipt_path = Path(str(row.get("receipt_path") or ""))
        if not receipt_path.is_file():
            continue
        try:
            receipt = _require_mapping(_read_document(receipt_path), "receipt")
            valid = all(
                [
                    receipt.get("schema") == RECEIPT_SCHEMA,
                    receipt.get("receipt_id") == receipt_id,
                    bool(receipt.get("ok")) == bool(row.get("ok")),
                    _sha256_file(receipt_path) == row.get("receipt_sha256"),
                ]
            )
        except Exception:
            valid = False
        if not valid:
            invalid_receipts.append(receipt_id)
    ok = bool(scoped) and not any(
        [missing, failed, missing_files, duplicate_ids, invalid_receipts]
    )
    return {
        "ok": ok,
        "index_path": str(path),
        "scope": {"instrument_id": instrument_id, "mode": mode},
        "receipt_count": len(scoped),
        "missing_invocation_ids": missing,
        "failed_receipt_ids": failed,
        "missing_receipt_files": missing_files,
        "duplicate_receipt_ids": duplicate_ids,
        "invalid_receipt_ids": invalid_receipts,
        "checked_at": _utc_now(),
    }


def _metric_definitions(config: LoadedEvalConfig) -> list[dict[str, Any]]:
    definitions = []
    for item in config.data.get("metrics") or []:
        metric = _require_mapping(item, "metric definition")
        _require_string(metric, "key", "metric definition")
        goal = str(metric.get("goal") or "report")
        if goal not in {"minimize", "maximize", "target", "report"}:
            raise EvalInstrumentError(f"unsupported metric goal: {goal}")
        definitions.append(metric)
    return definitions


def compare_receipts(
    config_path: str | Path,
    receipt_paths: Sequence[str | Path],
    *,
    output_dir: str | Path,
) -> dict[str, Any]:
    config = load_eval_config(config_path)
    if len(receipt_paths) < 2:
        raise EvalInstrumentError("comparison requires at least two receipts")
    receipts = [
        _require_mapping(_read_document(Path(path).expanduser().resolve()), "receipt")
        for path in receipt_paths
    ]
    for receipt in receipts:
        if receipt.get("schema") != RECEIPT_SCHEMA:
            raise EvalInstrumentError("comparison input is not an eval receipt")
        if not receipt.get("ok"):
            raise EvalInstrumentError(f"cannot compare failed receipt: {receipt.get('receipt_id')}")
        if receipt.get("score") is None:
            raise EvalInstrumentError(f"receipt has no score manifest: {receipt.get('receipt_id')}")
    arm_ids = [str(receipt["arm_id"]) for receipt in receipts]
    if len(set(arm_ids)) != len(arm_ids):
        raise EvalInstrumentError("comparison receipts must use distinct arm ids")
    invariant_keys = (
        ("pins", "config_sha256"),
        ("pins", "corpus", "manifest_sha256"),
        ("pins", "integrity", "seed_boundary", "snapshot_sha256"),
        ("pins", "agent", "constitution_sha256"),
        ("pins", "agent", "deployment_manifest_sha256"),
        ("pins", "judge", "digest"),
    )

    def nested(value: Mapping[str, Any], path: Sequence[str]) -> Any:
        current: Any = value
        for key in path:
            if not isinstance(current, Mapping):
                return None
            current = current.get(key)
        return current

    invariant_rows = []
    for path in invariant_keys:
        values = [nested(receipt, path) for receipt in receipts]
        invariant_rows.append(
            {"field": ".".join(path), "ok": len(set(map(str, values))) == 1, "values": values}
        )
    if not all(row["ok"] for row in invariant_rows):
        raise EvalInstrumentError("comparison invariant mismatch across arms")

    metric_defs = _metric_definitions(config)
    matrix: dict[str, dict[str, Any]] = {}
    for definition in metric_defs:
        key = definition["key"]
        matrix[key] = {
            "label": definition.get("label") or key,
            "goal": definition.get("goal") or "report",
            "target": definition.get("target"),
            "values": {
                receipt["arm_id"]: receipt["score"]["metrics"].get(key)
                for receipt in receipts
            },
        }
    case_ids: list[str] = []
    case_maps: dict[str, dict[str, Any]] = {}
    twin_matrix: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        arm_id = str(receipt["arm_id"])
        score = receipt["score"]
        for case in score.get("cases") or []:
            if not isinstance(case, Mapping) or not case.get("case_id"):
                continue
            case_id = str(case["case_id"])
            if case_id not in case_ids:
                case_ids.append(case_id)
            case_maps.setdefault(case_id, {})[arm_id] = {
                "outcome": case.get("outcome"),
                "correct": case.get("correct"),
                "notes": case.get("notes"),
            }
        for twin in score.get("twin_discrimination") or []:
            if not isinstance(twin, Mapping) or not twin.get("probe_id"):
                continue
            twin_matrix.setdefault(str(twin["probe_id"]), {})[arm_id] = {
                "passed": twin.get("passed"),
                "outcome": twin.get("outcome"),
            }
    comparison = {
        "schema": COMPARISON_SCHEMA,
        "ok": True,
        "instrument_id": config.data["instrument_id"],
        "arms": arm_ids,
        "invariants": invariant_rows,
        "metrics": matrix,
        "case_matrix": {case_id: case_maps.get(case_id, {}) for case_id in case_ids},
        "twin_discrimination": twin_matrix,
        "decision": {
            "status": "driver_verdict_required",
            "note": "The instrument compares evidence; it never changes the deployed engine slot.",
            "fallback_arms": [arm["id"] for arm in config.data["arms"] if arm.get("fallback")],
        },
        "created_at": _utc_now(),
    }
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "comparison.json"
    markdown_path = output / "comparison.md"
    _write_json_atomic(json_path, comparison)
    lines = [
        f"# Replay eval comparison — {config.data['instrument_id']}",
        "",
        "Same pinned corpus, seed boundary, constitution, and deployment manifest across arms.",
        "Engine selection remains a driver decision; this report does not mutate deployment config.",
        "",
        "## Metrics",
        "",
        "| Metric | Goal | " + " | ".join(arm_ids) + " |",
        "|---|---|" + "---:|" * len(arm_ids),
    ]
    for key, row in matrix.items():
        lines.append(
            f"| {row['label']} | {row['goal']} | "
            + " | ".join(str(row["values"].get(arm)) for arm in arm_ids)
            + " |"
        )
    if twin_matrix:
        lines.extend(["", "## Twin-sequence discrimination", "", "| Probe | " + " | ".join(arm_ids) + " |", "|---|" + "---|" * len(arm_ids)])
        for probe_id, values in twin_matrix.items():
            lines.append(
                f"| {probe_id} | "
                + " | ".join(
                    "PASS" if values.get(arm, {}).get("passed") else "FAIL"
                    for arm in arm_ids
                )
                + " |"
            )
    lines.extend(["", "## Decision", "", "Driver verdict required. Qualified fallback arm(s): " + ", ".join(comparison["decision"]["fallback_arms"]) + "."])
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        **comparison,
        "comparison_json": str(json_path),
        "comparison_markdown": str(markdown_path),
        "comparison_sha256": _sha256_file(json_path),
    }


__all__ = [
    "COMPARISON_SCHEMA",
    "CONFIG_SCHEMA",
    "EvalInstrumentError",
    "LoadedEvalConfig",
    "RECEIPT_SCHEMA",
    "SCORE_SCHEMA",
    "TRACE_SCHEMA",
    "audit_receipt_index",
    "build_arm_plan",
    "compare_receipts",
    "evaluate_adaptive_trace",
    "load_eval_config",
    "materialize_arm_runtime",
    "pin_eval_config",
    "record_evaluation_invocation",
    "write_arm_plan",
]
