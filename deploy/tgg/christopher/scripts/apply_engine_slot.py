#!/usr/bin/env python3
"""Atomically materialize a pre-staged Christopher engine slot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import grp
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


# slot id -> model. Suffixed slots pin an explicit reasoning effort.
SLOT_MODELS = {
    "gpt-5.4-mini": "gpt-5.4-mini",
    "gpt-5.6-luna": "gpt-5.6-luna",
    "gpt-5.6-luna-low": "gpt-5.6-luna",
    "gpt-5.6-luna-xhigh": "gpt-5.6-luna",
    "gpt-5.6-terra-medium": "gpt-5.6-terra",
    "gpt-5.6-terra-high": "gpt-5.6-terra",
}
SLOT_REASONING_EFFORT = {
    "gpt-5.6-luna-low": "low",
    "gpt-5.6-luna-xhigh": "xhigh",
    "gpt-5.6-terra-medium": "medium",
    "gpt-5.6-terra-high": "high",
}
ALLOWED_SLOTS = tuple(SLOT_MODELS)
DEFAULT_SLOT = ALLOWED_SLOTS[0]
CAPABILITY_ID = "christopher-tgg"
CAPABILITY_BASE_FILES = frozenset({
    "christopher-slot-config.yaml",
    "christopher_tgg_constitution.yaml",
    "plugins/tgg-whatsapp-evidence/__init__.py",
    "plugins/tgg-whatsapp-evidence/plugin.yaml",
})
CAPABILITY_SPREADSHEET_SKILL_FILES = frozenset({
    "skills/spreadsheet-work/SKILL.md",
    "skills/spreadsheet-work/agents/openai.yaml",
})
CAPABILITY_WHATSAPP_SKILL_FILES = frozenset({
    "skills/whatsapp-investigation/SKILL.md",
    "skills/whatsapp-investigation/agents/openai.yaml",
})
CAPABILITY_OPTIONAL_FILE_SETS = (
    CAPABILITY_SPREADSHEET_SKILL_FILES,
    CAPABILITY_WHATSAPP_SKILL_FILES,
)
CAPABILITY_ALLOWED_FILE_SETS = {
    CAPABILITY_BASE_FILES
    | frozenset().union(
        *(files for index, files in enumerate(CAPABILITY_OPTIONAL_FILE_SETS) if mask & (1 << index))
    )
    for mask in range(1 << len(CAPABILITY_OPTIONAL_FILE_SETS))
}
PROVIDER_PROFILES = frozenset({"openai-direct-primary", "openai-codex"})
DEFAULT_PROVIDER_PROFILE = "openai-direct-primary"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _expected_hashes(slots_root: Path) -> dict[str, str]:
    expected: dict[str, str] = {}
    for raw in (slots_root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, relative = raw.split(None, 1)
        expected[relative.strip()] = digest
    return expected


def _atomic_copy(source: Path, target: Path, *, mode: int, uid: int, gid: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=target.parent, prefix=f".{target.name}.", delete=False
    ) as handle:
        tmp = Path(handle.name)
        handle.write(source.read_bytes())
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(tmp, mode)
        os.chown(tmp, uid, gid)
        os.replace(tmp, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if tmp.exists():
            tmp.unlink()


def _select_slot(slot_file: Path, requested: str | None) -> str:
    if requested:
        selected = requested.strip()
    elif slot_file.exists():
        selected = slot_file.read_text(encoding="utf-8").strip()
    else:
        selected = DEFAULT_SLOT
    if selected not in ALLOWED_SLOTS:
        raise RuntimeError(f"invalid engine slot {selected!r}; allowed={ALLOWED_SLOTS}")
    return selected


def _atomic_write_json(path: Path, payload: dict, *, mode: int, uid: int, gid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write((json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(temporary, mode)
        os.chown(temporary, uid, gid)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_yaml(path: Path, payload: dict, *, mode: int, uid: int, gid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(yaml.safe_dump(payload, sort_keys=False).encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(temporary, mode)
        os.chown(temporary, uid, gid)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_provider_profile(profile_file: Path) -> tuple[str, str | None]:
    if not profile_file.exists():
        return DEFAULT_PROVIDER_PROFILE, None
    try:
        payload = json.loads(profile_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"invalid provider profile state at {profile_file}: {exc}") from exc
    provider = str(payload.get("provider") or "").strip() if isinstance(payload, dict) else ""
    label = payload.get("credential_label") if isinstance(payload, dict) else None
    if provider not in PROVIDER_PROFILES:
        raise RuntimeError(f"invalid provider profile {provider!r}")
    if label is not None and not isinstance(label, str):
        raise RuntimeError("provider credential_label must be a string or null")
    label = label.strip() if isinstance(label, str) else None
    if provider == "openai-codex" and not label:
        raise RuntimeError("openai-codex requires an explicit credential_label")
    if provider != "openai-codex" and label:
        raise RuntimeError(f"{provider} does not accept credential_label")
    return provider, label


def _apply_provider_profile(
    config_path: Path, constitution_path: Path, *, provider: str,
    credential_label: str | None, model: str, uid: int, gid: int,
) -> None:
    """Overlay accountable provider/account selection onto copied source files."""
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    constitution = yaml.safe_load(constitution_path.read_text(encoding="utf-8"))
    config["model"]["provider"] = provider
    if credential_label:
        config["model"]["credential_label"] = credential_label
    else:
        config["model"].pop("credential_label", None)
    constitution["runtime"] = {"provider": provider, "model": model}
    for brief in constitution.get("job_briefs", {}).values():
        if isinstance(brief.get("runtime"), dict):
            brief["runtime"] = {"provider": provider, "model": model}
    _atomic_write_yaml(config_path, config, mode=0o640, uid=uid, gid=gid)
    _atomic_write_yaml(constitution_path, constitution, mode=0o644, uid=uid, gid=gid)


def _validate_runtime_pair(config_path: Path, constitution_path: Path, slot: str) -> None:
    model = SLOT_MODELS[slot]
    effort = SLOT_REASONING_EFFORT.get(slot)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    constitution = yaml.safe_load(constitution_path.read_text(encoding="utf-8"))
    assert config["pa"]["enabled"] is False
    assert config["group_sessions_per_user"] is False
    assert config["timezone"] == "Asia/Singapore"
    assert config["session_reset"] == {"mode": "none"}
    assert config["platforms"]["whatsapp"]["enabled"] is False
    assert config["model"]["provider"] == "openai-direct-primary"
    assert config["model"]["default"] == model
    if effort is None:
        assert "reasoning_effort" not in config["agent"]
    else:
        assert config["agent"]["reasoning_effort"] == effort
    assert constitution["runtime"] == {
        "provider": "openai-direct-primary",
        "model": model,
    }


def _validate_slot(slot_root: Path, slot: str) -> None:
    _validate_runtime_pair(
        slot_root / "config.yaml",
        slot_root / "christopher_tgg_constitution.yaml",
        slot,
    )


def _parse_sums(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        digest, relative = raw.split(None, 1)
        relative = relative.strip()
        if relative in values:
            raise RuntimeError(f"duplicate capability checksum entry: {relative}")
        values[relative] = digest
    return values


def _external_capability(runtime_root: Path, hermes_home: Path, slot: str) -> dict[str, Any] | None:
    capability_root = runtime_root / "capabilities" / CAPABILITY_ID
    current = capability_root / "current"
    if not current.exists() and not current.is_symlink():
        return None
    if not current.is_symlink():
        raise RuntimeError("external capability current pointer must be a symlink")
    releases_root = (capability_root / "releases").resolve(strict=True)
    release_root = current.resolve(strict=True)
    if not release_root.is_relative_to(releases_root) or release_root.parent != releases_root:
        raise RuntimeError("external capability target escapes the releases directory")
    manifest_path = release_root / "manifest.json"
    sums_path = release_root / "SHA256SUMS"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "christopher-tgg-capability-release/v1":
        raise RuntimeError("external capability manifest schema mismatch")
    if manifest.get("release_id") != release_root.name:
        raise RuntimeError("external capability release id/path mismatch")
    if manifest.get("audience") not in {"shadow", "production"}:
        raise RuntimeError("external capability audience is invalid")
    files = manifest.get("files")
    if not isinstance(files, dict) or frozenset(files) not in CAPABILITY_ALLOWED_FILE_SETS:
        raise RuntimeError("external capability file set mismatch")
    sums = _parse_sums(sums_path)
    if sums.get("manifest.json") != _sha256(manifest_path):
        raise RuntimeError("external capability manifest checksum mismatch")
    for relative, expected in files.items():
        path = release_root / relative
        if not path.is_file() or _sha256(path) != expected or sums.get(relative) != expected:
            raise RuntimeError(f"external capability checksum mismatch: {relative}")
    config_path = release_root / "christopher-slot-config.yaml"
    constitution_path = release_root / "christopher_tgg_constitution.yaml"
    _validate_runtime_pair(config_path, constitution_path, slot)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    constitution = yaml.safe_load(constitution_path.read_text(encoding="utf-8"))
    includes_external_skills = any(
        skill_files.issubset(files) for skill_files in CAPABILITY_OPTIONAL_FILE_SETS
    )
    expected_external_dirs = (
        [str(current / "skills")] if includes_external_skills else []
    )
    configured_external_dirs = config.get("skills", {}).get("external_dirs", [])
    if configured_external_dirs != expected_external_dirs:
        raise RuntimeError("external capability skills path mismatch")
    canary = manifest.get("canary")
    if manifest["audience"] == "production" and canary is not None:
        raise RuntimeError("production external capability cannot restrict management selectors")
    if canary is not None:
        management_chat_ids = canary.get("management_chat_ids") if isinstance(canary, dict) else None
        if (
            not isinstance(management_chat_ids, list)
            or len(management_chat_ids) != 1
            or not isinstance(management_chat_ids[0], str)
        ):
            raise RuntimeError("external capability canary manifest is invalid")
        management_selectors = [
            selector
            for selector in constitution.get("selectors", [])
            if selector.get("job_type") == "tgg_management"
        ]
        expected_selector = {
            "job_type": "tgg_management",
            "match": {
                "source.platform": "whatsapp",
                "source.chat_id": management_chat_ids[0],
            },
        }
        if management_selectors != [expected_selector]:
            raise RuntimeError("external capability canary selector set mismatch")
    configured_constitution = Path(config["pa"]["constitution_path"])
    expected_constitution = current / "christopher_tgg_constitution.yaml"
    if configured_constitution != expected_constitution:
        raise RuntimeError("external capability constitution path mismatch")
    if config.get("plugins", {}).get("enabled", []).count("tgg-whatsapp-evidence") != 1:
        raise RuntimeError("external capability plugin enablement mismatch")
    plugin_source = release_root / "plugins" / "tgg-whatsapp-evidence"
    return {
        "release_root": release_root,
        "release_id": manifest["release_id"],
        "manifest_sha256": _sha256(manifest_path),
        "config_path": config_path,
        "constitution_path": constitution_path,
        "plugin_source": plugin_source,
        "plugin_link": hermes_home / "plugins" / "tgg-whatsapp-evidence",
    }


def _atomic_symlink(target: Path, link: Path, *, uid: int, gid: int) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.parent / f".{link.name}.{os.getpid()}.tmp"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.lchown(temporary, uid, gid)
    try:
        os.replace(temporary, link)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", required=True)
    parser.add_argument("--hermes-home", required=True)
    parser.add_argument("--slot", choices=ALLOWED_SLOTS)
    parser.add_argument("--provider-profile", choices=sorted(PROVIDER_PROFILES))
    parser.add_argument("--credential-label")
    args = parser.parse_args()

    app_root = Path(args.app_root).resolve()
    hermes_home = Path(args.hermes_home).resolve()
    slots_root = app_root / "deploy" / "tgg" / "christopher" / "runtime-slots"
    runtime_root = hermes_home / "runtime"
    slot_file = runtime_root / "engine-slot"
    profile_file = runtime_root / "provider-profile.json"
    selected = _select_slot(slot_file, args.slot)
    slot_root = slots_root / selected
    _validate_slot(slot_root, selected)

    expected = _expected_hashes(slots_root)
    for name in ("config.yaml", "christopher_tgg_constitution.yaml"):
        relative = f"{selected}/{name}"
        actual = _sha256(slot_root / name)
        if expected.get(relative) != actual:
            raise RuntimeError(
                f"slot hash mismatch for {relative}: expected={expected.get(relative)} actual={actual}"
            )

    capability = _external_capability(runtime_root, hermes_home, selected)
    config_source = capability["config_path"] if capability else slot_root / "config.yaml"
    constitution_source = (
        capability["constitution_path"]
        if capability
        else slot_root / "christopher_tgg_constitution.yaml"
    )

    user = pwd.getpwnam("pclaw")
    group = grp.getgrnam("pclaw")
    if args.provider_profile:
        if args.provider_profile == "openai-codex" and not (args.credential_label or "").strip():
            raise RuntimeError("--credential-label is required for openai-codex")
        if args.provider_profile != "openai-codex" and args.credential_label:
            raise RuntimeError("--credential-label is only valid for openai-codex")
        _atomic_write_json(
            profile_file,
            {"version": 1, "provider": args.provider_profile,
             "credential_label": (args.credential_label or "").strip() or None},
            mode=0o640, uid=0, gid=group.gr_gid,
        )
    provider, credential_label = _read_provider_profile(profile_file)
    # The LIVE processing key is owned by the activation transaction
    # (processing_activation_transaction.py flips config pa.enabled + the gate
    # file together, both-or-neither), NOT by the slot: every slot file pins
    # pa.enabled false as its authored disabled-state default. This script
    # runs as ExecStartPre on EVERY service start — re-imposing the slot copy
    # verbatim silently reverted an in-flight activation's pa.enabled=true one
    # second after the transaction wrote it (2026-07-21 rounds 6 and 7:
    # config false + gate true -> consumer standby -> 20s confirmation
    # timeout -> fail-closed rollback, engine-slot receipt stamped between the
    # transaction's write and the consumer's first read). Preserve the live
    # value across the copy; fail-closed default when unreadable. The gate
    # file (which this script never touches) remains the second key, so a
    # preserved-true config alone still processes nothing.
    live_processing_enabled = False
    live_config_path = hermes_home / "config.yaml"
    if live_config_path.is_file():
        pa_match = re.search(
            r"^pa:\s*(?:#.*)?\n((?:[ \t].*\n|\n)*)",
            live_config_path.read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        )
        if pa_match and re.search(
            r"^  enabled:\s*true\s*(?:#.*)?$", pa_match.group(1), flags=re.MULTILINE
        ):
            live_processing_enabled = True
    _atomic_copy(
        config_source,
        hermes_home / "config.yaml",
        mode=0o640,
        uid=0,
        gid=group.gr_gid,
    )
    if live_processing_enabled:
        config_text = (hermes_home / "config.yaml").read_text(encoding="utf-8")
        patched, count = re.subn(
            r"(^pa:\s*(?:#.*)?\n  enabled:)\s*false(\s*(?:#.*)?$)",
            r"\1 true\2",
            config_text,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise RuntimeError(
                f"expected exactly one pa.enabled to re-apply the live processing key, found {count}"
            )
        patch_tmp = hermes_home / f".config.{os.getpid()}.tmp"
        patch_tmp.write_text(patched, encoding="utf-8")
        os.chmod(patch_tmp, 0o640)
        os.chown(patch_tmp, 0, group.gr_gid)
        os.replace(patch_tmp, hermes_home / "config.yaml")
    _atomic_copy(
        constitution_source,
        hermes_home / "christopher_tgg_constitution.yaml",
        mode=0o644,
        uid=0,
        gid=group.gr_gid,
    )
    _apply_provider_profile(
        hermes_home / "config.yaml", hermes_home / "christopher_tgg_constitution.yaml",
        provider=provider, credential_label=credential_label, model=SLOT_MODELS[selected],
        uid=0, gid=group.gr_gid,
    )
    if capability:
        plugin_link = capability["plugin_link"]
        if plugin_link.exists() and not plugin_link.is_symlink():
            raise RuntimeError(
                "external capability plugin destination exists and is not a symlink; "
                "the installer must preserve it before activation"
            )
        _atomic_symlink(
            capability["plugin_source"],
            plugin_link,
            uid=user.pw_uid,
            gid=group.gr_gid,
        )
    runtime_root.mkdir(parents=True, exist_ok=True)
    slot_tmp = runtime_root / f".engine-slot.{os.getpid()}.tmp"
    slot_tmp.write_text(f"{selected}\n", encoding="utf-8")
    os.chmod(slot_tmp, 0o640)
    os.chown(slot_tmp, 0, group.gr_gid)
    os.replace(slot_tmp, slot_file)
    os.chown(hermes_home, user.pw_uid, group.gr_gid)

    receipt = {
        "version": 2,
        "selected_at": datetime.now(timezone.utc).isoformat(),
        "slot": selected,
        "provider": provider,
        "credential_label": credential_label,
        "model": SLOT_MODELS[selected],
        "reasoning_effort": SLOT_REASONING_EFFORT.get(selected),
        "config_sha256": _sha256(hermes_home / "config.yaml"),
        "constitution_sha256": _sha256(
            hermes_home / "christopher_tgg_constitution.yaml"
        ),
        "configuration_source": "external-capability" if capability else "repo-engine-slot",
        "capability_release_id": capability["release_id"] if capability else None,
        "capability_manifest_sha256": capability["manifest_sha256"] if capability else None,
    }
    receipt_path = runtime_root / "engine-slot-receipt.json"
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    os.chmod(receipt_path, 0o640)
    os.chown(receipt_path, 0, group.gr_gid)
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
