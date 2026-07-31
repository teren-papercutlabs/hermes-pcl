#!/usr/bin/env python3
"""Assemble and verify principal-supplied per-client provider keys."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml


CAPABILITY_MODE = "per-client-principal-supplied"
PROVENANCE_VERSION = 1
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class ProviderKeyContractError(RuntimeError):
    """A provider-key declaration or assembled runtime failed closed."""


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ProviderKeyContractError(f"{path} must contain a YAML mapping")
    return value


def _client_suffix(client: str) -> str:
    suffix = re.sub(r"[^A-Z0-9]+", "_", client.upper()).strip("_")
    if not suffix:
        raise ProviderKeyContractError("deployment client has no usable slot suffix")
    return suffix


def load_contract(spec_path: Path) -> dict[str, Any]:
    document = _load_yaml(spec_path)
    client = str(document.get("metadata", {}).get("client") or "").strip()
    raw = (
        document.get("spec", {})
        .get("capabilities", {})
        .get("providerKeys")
    )
    if not isinstance(raw, dict):
        raise ProviderKeyContractError(
            "ClientAgentDeployment must declare "
            "spec.capabilities.providerKeys"
        )
    if raw.get("mode") != CAPABILITY_MODE:
        raise ProviderKeyContractError(
            f"provider-key capability mode must be {CAPABILITY_MODE}"
        )
    if raw.get("suppliedBy") != "principal":
        raise ProviderKeyContractError(
            "provider keys must be declared principal-supplied"
        )
    provenance_path = str(raw.get("provenancePath") or "").strip()
    if not provenance_path.startswith("/"):
        raise ProviderKeyContractError(
            "provider-key provenancePath must be an absolute runtime path"
        )
    providers = raw.get("providers")
    if not isinstance(providers, list) or not providers:
        raise ProviderKeyContractError(
            "provider-key capability must declare at least one provider"
        )

    suffix = _client_suffix(client)
    normalized: list[dict[str, str]] = []
    runtime_names: set[str] = set()
    source_slots: set[str] = set()
    for row in providers:
        if not isinstance(row, dict):
            raise ProviderKeyContractError(
                "provider-key provider entries must be mappings"
            )
        provider = str(row.get("provider") or "").strip().lower()
        runtime_env = str(row.get("runtimeEnv") or "").strip()
        source_slot = str(row.get("sourceSlot") or "").strip()
        if not provider:
            raise ProviderKeyContractError("provider-key entry missing provider")
        if not _ENV_NAME.fullmatch(runtime_env):
            raise ProviderKeyContractError(
                f"invalid runtime provider env name: {runtime_env!r}"
            )
        if not _ENV_NAME.fullmatch(source_slot):
            raise ProviderKeyContractError(
                f"invalid canonical provider slot name: {source_slot!r}"
            )
        expected_slot = f"{runtime_env}_{suffix}"
        if source_slot != expected_slot:
            raise ProviderKeyContractError(
                f"{provider} must source client slot {expected_slot}; "
                f"generic/fleet slot {source_slot or '<missing>'} is forbidden"
            )
        if runtime_env in runtime_names:
            raise ProviderKeyContractError(
                f"duplicate runtime provider env: {runtime_env}"
            )
        if source_slot in source_slots:
            raise ProviderKeyContractError(
                f"duplicate canonical provider slot: {source_slot}"
            )
        runtime_names.add(runtime_env)
        source_slots.add(source_slot)
        normalized.append(
            {
                "provider": provider,
                "runtimeEnv": runtime_env,
                "sourceSlot": source_slot,
            }
        )

    return {
        "client": client,
        "mode": CAPABILITY_MODE,
        "suppliedBy": "principal",
        "provenancePath": provenance_path,
        "providers": normalized,
    }


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ProviderKeyContractError(f"secrets/env file does not exist: {path}")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _ENV_NAME.fullmatch(key):
            continue
        raw_value = raw_value.strip()
        if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] == '"':
            try:
                value = json.loads(raw_value)
            except json.JSONDecodeError as exc:
                raise ProviderKeyContractError(
                    f"invalid quoted value for {key} in {path}"
                ) from exc
        elif len(raw_value) >= 2 and raw_value[0] == raw_value[-1] == "'":
            value = raw_value[1:-1]
        else:
            value = raw_value
        values[key] = value
    return values


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def assemble(
    *,
    spec_path: Path,
    secrets_path: Path,
    output_env: Path,
    output_provenance: Path,
) -> dict[str, Any]:
    contract = load_contract(spec_path)
    canonical = _read_dotenv(secrets_path)
    runtime: dict[str, str] = {}
    provenance_rows: list[dict[str, str]] = []
    for row in contract["providers"]:
        slot = row["sourceSlot"]
        value = canonical.get(slot, "")
        if not value:
            raise ProviderKeyContractError(
                f"missing required per-client provider slot {slot}. "
                "Obtain the client key from Teren or Amelia and register it in "
                f"{secrets_path} before the first deploy; fleet keys are forbidden."
            )
        runtime[row["runtimeEnv"]] = value
        provenance_rows.append(
            {
                **row,
                "sha256": _digest(value),
            }
        )

    env_body = "".join(
        f"{name}={json.dumps(value)}\n" for name, value in runtime.items()
    )
    provenance = {
        "version": PROVENANCE_VERSION,
        "client": contract["client"],
        "mode": contract["mode"],
        "suppliedBy": contract["suppliedBy"],
        "providers": provenance_rows,
    }
    _atomic_write(output_env, env_body)
    _atomic_write(
        output_provenance,
        json.dumps(provenance, indent=2, sort_keys=True) + "\n",
    )
    return {
        "client": contract["client"],
        "mode": contract["mode"],
        "sourceSlots": [row["sourceSlot"] for row in contract["providers"]],
        "runtimeEnv": [row["runtimeEnv"] for row in contract["providers"]],
    }


def _read_process_environ(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ProviderKeyContractError(
            f"live process environment is unavailable: {path}"
        )
    values: dict[str, str] = {}
    for item in path.read_bytes().split(b"\0"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        values[key.decode(errors="replace")] = value.decode(errors="replace")
    return values


def verify(
    *,
    spec_path: Path,
    env_path: Path,
    provenance_path: Path,
    process_environ_path: Path | None = None,
) -> dict[str, Any]:
    contract = load_contract(spec_path)
    runtime = _read_dotenv(env_path)
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderKeyContractError(
            f"provider-key provenance is unreadable: {provenance_path}"
        ) from exc
    if not isinstance(provenance, dict):
        raise ProviderKeyContractError("provider-key provenance must be a mapping")
    expected_header = {
        "version": PROVENANCE_VERSION,
        "client": contract["client"],
        "mode": contract["mode"],
        "suppliedBy": contract["suppliedBy"],
    }
    for key, expected in expected_header.items():
        if provenance.get(key) != expected:
            raise ProviderKeyContractError(
                f"provider-key provenance {key} mismatch"
            )

    provenance_rows = provenance.get("providers")
    if not isinstance(provenance_rows, list):
        raise ProviderKeyContractError(
            "provider-key provenance providers must be a list"
        )
    by_runtime = {
        str(row.get("runtimeEnv")): row
        for row in provenance_rows
        if isinstance(row, dict)
    }
    process = (
        _read_process_environ(process_environ_path)
        if process_environ_path is not None
        else None
    )
    for declared in contract["providers"]:
        runtime_env = declared["runtimeEnv"]
        recorded = by_runtime.get(runtime_env)
        if recorded is None:
            raise ProviderKeyContractError(
                f"provider-key provenance missing {runtime_env}"
            )
        for key in ("provider", "runtimeEnv", "sourceSlot"):
            if recorded.get(key) != declared[key]:
                raise ProviderKeyContractError(
                    f"provider-key provenance drift for {runtime_env}:{key}"
                )
        value = runtime.get(runtime_env, "")
        if not value:
            raise ProviderKeyContractError(
                f"runtime env missing provider key {runtime_env}"
            )
        if recorded.get("sha256") != _digest(value):
            raise ProviderKeyContractError(
                f"runtime provider key {runtime_env} does not match "
                f"assembled client slot {declared['sourceSlot']}"
            )
        if process is not None and process.get(runtime_env) != value:
            raise ProviderKeyContractError(
                f"live process provider key {runtime_env} does not match "
                f"assembled client slot {declared['sourceSlot']}"
            )
    if set(by_runtime) != {
        row["runtimeEnv"] for row in contract["providers"]
    }:
        raise ProviderKeyContractError(
            "provider-key provenance contains undeclared runtime keys"
        )
    return {
        "client": contract["client"],
        "mode": contract["mode"],
        "sourceSlots": [row["sourceSlot"] for row in contract["providers"]],
        "liveProcessChecked": process is not None,
        "status": "pass",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Assemble and verify principal-supplied per-client provider keys "
            "for PA client runtimes."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    assemble_parser = subparsers.add_parser("assemble")
    assemble_parser.add_argument("--spec", type=Path, required=True)
    assemble_parser.add_argument("--secrets-file", type=Path, required=True)
    assemble_parser.add_argument("--output-env", type=Path, required=True)
    assemble_parser.add_argument("--output-provenance", type=Path, required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--spec", type=Path, required=True)
    verify_parser.add_argument("--env-file", type=Path, required=True)
    verify_parser.add_argument("--provenance", type=Path, required=True)
    verify_parser.add_argument("--process-environ", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "assemble":
            result = assemble(
                spec_path=args.spec,
                secrets_path=args.secrets_file,
                output_env=args.output_env,
                output_provenance=args.output_provenance,
            )
        else:
            result = verify(
                spec_path=args.spec,
                env_path=args.env_file,
                provenance_path=args.provenance,
                process_environ_path=args.process_environ,
            )
    except ProviderKeyContractError as exc:
        print(f"provider-key contract refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
