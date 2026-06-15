#!/usr/bin/env python3
"""Prepare Christopher's live Hermes config/constitution for the validated gpt-5.4-mini replay rig.

Reads the current live config and constitution, changes only the engine/runtime
shape needed by the replay harness, and preserves the live PA gate
(`pa.enabled=false`). It does not restart anything.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

MAIN_PROVIDER = "openai-direct-primary"
MAIN_MODEL = "gpt-5.4-mini"
VISION_PROVIDER = "gemini"
VISION_MODEL = "gemini-3.1-flash-lite"
CONSTITUTION_PATH = "/home/pclaw/.hermes-christopher-tgg/christopher_tgg_constitution.yaml"


def _set_nested(data: dict[str, Any], path: list[str], value: Any) -> None:
    cur = data
    for key in path[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[path[-1]] = value


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"{path} did not load as a mapping")
    return data


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def transform_config(config: dict[str, Any]) -> dict[str, Any]:
    config = json.loads(json.dumps(config))
    config["providers"] = {
        MAIN_PROVIDER: {
            "name": "OpenAI Direct Primary",
            "api": "https://api.openai.com/v1",
            "key_env": "OPENAI_API_KEY",
            "default_model": MAIN_MODEL,
            "transport": "codex_responses",
        }
    }
    _set_nested(config, ["model", "provider"], MAIN_PROVIDER)
    _set_nested(config, ["model", "default"], MAIN_MODEL)
    # Keep these explicit so old provider-resolution paths cannot accidentally
    # route the GPT-5.x model through Gemini's endpoint.
    _set_nested(config, ["model", "base_url"], "https://api.openai.com/v1")
    _set_nested(config, ["model", "api_key_source"], {"type": "env", "secrets_env_key": "OPENAI_API_KEY"})

    _set_nested(config, ["agent", "profile"], "pa")
    _set_nested(config, ["agent", "max_turns"], 12)

    # HARD GATE: staging the engine must not enable live PA processing.
    _set_nested(config, ["pa", "enabled"], False)
    _set_nested(config, ["pa", "constitution_path"], CONSTITUTION_PATH)

    auxiliary = config.setdefault("auxiliary", {})
    if isinstance(auxiliary, dict):
        for value in auxiliary.values():
            if isinstance(value, dict):
                value["provider"] = "main"
                value["model"] = MAIN_MODEL
        vision = auxiliary.setdefault("vision", {})
        if isinstance(vision, dict):
            vision["provider"] = VISION_PROVIDER
            vision["model"] = VISION_MODEL
            vision["max_concurrency"] = max(1, int(vision.get("max_concurrency") or 8))

    return config


def transform_constitution(constitution: dict[str, Any]) -> dict[str, Any]:
    constitution = json.loads(json.dumps(constitution))
    _set_nested(constitution, ["runtime", "provider"], MAIN_PROVIDER)
    _set_nested(constitution, ["runtime", "model"], MAIN_MODEL)
    for brief in (constitution.get("job_briefs") or {}).values():
        if isinstance(brief, dict):
            runtime = brief.setdefault("runtime", {})
            runtime["provider"] = MAIN_PROVIDER
            runtime["model"] = MAIN_MODEL
    return constitution


def validate(config: dict[str, Any], constitution: dict[str, Any]) -> None:
    failures = []
    if config.get("pa", {}).get("enabled") is not False:
        failures.append("pa.enabled is not false")
    if config.get("model", {}).get("provider") != MAIN_PROVIDER:
        failures.append("config model.provider not openai-direct-primary")
    if config.get("model", {}).get("default") != MAIN_MODEL:
        failures.append("config model.default not gpt-5.4-mini")
    provider = config.get("providers", {}).get(MAIN_PROVIDER, {})
    if provider.get("transport") != "codex_responses":
        failures.append("provider transport not codex_responses")
    if provider.get("key_env") != "OPENAI_API_KEY":
        failures.append("provider key_env not OPENAI_API_KEY")
    if constitution.get("runtime", {}).get("provider") != MAIN_PROVIDER:
        failures.append("constitution runtime.provider not openai-direct-primary")
    if constitution.get("runtime", {}).get("model") != MAIN_MODEL:
        failures.append("constitution runtime.model not gpt-5.4-mini")
    for name, brief in (constitution.get("job_briefs") or {}).items():
        runtime = brief.get("runtime") if isinstance(brief, dict) else None
        if isinstance(runtime, dict) and runtime.get("model") != MAIN_MODEL:
            failures.append(f"job_brief {name} runtime.model not gpt-5.4-mini")
    if failures:
        raise SystemExit("validation failed: " + "; ".join(failures))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config-in", required=True)
    ap.add_argument("--constitution-in", required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    out = Path(args.out_dir)
    config = transform_config(_load_yaml(Path(args.config_in)))
    constitution = transform_constitution(_load_yaml(Path(args.constitution_in)))
    validate(config, constitution)

    config_out = out / "config.gpt54-staged.yaml"
    constitution_out = out / "christopher_tgg_constitution.gpt54-staged.yaml"
    _write_yaml(config_out, config)
    _write_yaml(constitution_out, constitution)

    manifest = {
        "engine": {"provider": MAIN_PROVIDER, "model": MAIN_MODEL, "transport": "codex_responses"},
        "pa_enabled": config["pa"]["enabled"],
        "vision": config.get("auxiliary", {}).get("vision"),
        "files": {
            str(config_out): _sha(config_out),
            str(constitution_out): _sha(constitution_out),
        },
    }
    (out / "engine-stage-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
