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

import yaml


# slot id -> model. Suffixed slots pin an explicit reasoning effort.
SLOT_MODELS = {
    "gpt-5.4-mini": "gpt-5.4-mini",
    "gpt-5.6-luna": "gpt-5.6-luna",
    "gpt-5.6-luna-low": "gpt-5.6-luna",
    "gpt-5.6-luna-xhigh": "gpt-5.6-luna",
    "gpt-5.6-terra-medium": "gpt-5.6-terra",
}
SLOT_REASONING_EFFORT = {
    "gpt-5.6-luna-low": "low",
    "gpt-5.6-luna-xhigh": "xhigh",
    "gpt-5.6-terra-medium": "medium",
}
ALLOWED_SLOTS = tuple(SLOT_MODELS)
DEFAULT_SLOT = ALLOWED_SLOTS[0]


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


def _validate_slot(slot_root: Path, slot: str) -> None:
    model = SLOT_MODELS[slot]
    effort = SLOT_REASONING_EFFORT.get(slot)
    config = yaml.safe_load((slot_root / "config.yaml").read_text(encoding="utf-8"))
    constitution = yaml.safe_load(
        (slot_root / "christopher_tgg_constitution.yaml").read_text(encoding="utf-8")
    )
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", required=True)
    parser.add_argument("--hermes-home", required=True)
    parser.add_argument("--slot", choices=ALLOWED_SLOTS)
    args = parser.parse_args()

    app_root = Path(args.app_root).resolve()
    hermes_home = Path(args.hermes_home).resolve()
    slots_root = app_root / "deploy" / "tgg" / "christopher" / "runtime-slots"
    runtime_root = hermes_home / "runtime"
    slot_file = runtime_root / "engine-slot"
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

    user = pwd.getpwnam("pclaw")
    group = grp.getgrnam("pclaw")
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
        slot_root / "config.yaml",
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
        slot_root / "christopher_tgg_constitution.yaml",
        hermes_home / "christopher_tgg_constitution.yaml",
        mode=0o644,
        uid=0,
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
        "version": 1,
        "selected_at": datetime.now(timezone.utc).isoformat(),
        "slot": selected,
        "provider": "openai-direct-primary",
        "model": SLOT_MODELS[selected],
        "reasoning_effort": SLOT_REASONING_EFFORT.get(selected),
        "config_sha256": _sha256(hermes_home / "config.yaml"),
        "constitution_sha256": _sha256(
            hermes_home / "christopher_tgg_constitution.yaml"
        ),
    }
    receipt_path = runtime_root / "engine-slot-receipt.json"
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    os.chmod(receipt_path, 0o640)
    os.chown(receipt_path, 0, group.gr_gid)
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
