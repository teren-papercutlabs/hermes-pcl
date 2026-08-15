"""Focused safety tests for the tgg-app-1 retention quarantine tool."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest
import yaml


@pytest.fixture()
def cleanup_module():
    path = Path(__file__).parents[2] / "deploy/tgg/christopher/scripts/retention_cleanup.py"
    spec = importlib.util.spec_from_file_location("tgg_retention_cleanup_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_policy(path: Path, root: Path, quarantine: Path, protected: list[Path] | None = None):
    path.write_text(yaml.safe_dump({
        "version": 1,
        "quarantine_root": str(quarantine),
        "protected_roots": [str(item) for item in (protected or [])],
        "rules": [{
            "id": "old-fixtures",
            "root": str(root),
            "direct_child_prefixes": ["fixture-"],
            "min_age_days": 1,
            "keep_newest": 1,
            "require_terminal_receipt": True,
            "terminal_receipt_names": ["receipt.json"],
        }],
    }), encoding="utf-8")


def _age(path: Path, days: int) -> None:
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))


def test_collect_is_direct_child_receipt_gated_and_keeps_newest(tmp_path, cleanup_module):
    root = tmp_path / "staging"
    root.mkdir()
    old = root / "fixture-old"
    old.mkdir()
    (old / "receipt.json").write_text(json.dumps({"status": "completed"}))
    _age(old, 3)
    missing_receipt = root / "fixture-missing"
    missing_receipt.mkdir()
    _age(missing_receipt, 3)
    newest = root / "fixture-new"
    newest.mkdir()
    (newest / "receipt.json").write_text(json.dumps({"status": "completed"}))
    policy_path = tmp_path / "policy.yaml"
    _write_policy(policy_path, root, tmp_path / "quarantine")

    selected, skipped = cleanup_module._collect(cleanup_module._load_policy(policy_path))
    assert [item.path for item in selected] == [old]
    assert {item["reason"] for item in skipped} == {"keep-newest", "no-terminal-receipt"}


def test_quarantine_is_recoverable_move_not_delete(tmp_path, cleanup_module):
    root = tmp_path / "staging"
    root.mkdir()
    old = root / "fixture-old"
    old.mkdir()
    (old / "receipt.json").write_text(json.dumps({"status": "complete"}))
    _age(old, 3)
    newest = root / "fixture-new"
    newest.mkdir()
    (newest / "receipt.json").write_text(json.dumps({"status": "complete"}))
    policy_path = tmp_path / "policy.yaml"
    quarantine = tmp_path / "quarantine"
    _write_policy(policy_path, root, quarantine)
    selected, _ = cleanup_module._collect(cleanup_module._load_policy(policy_path))
    moves = cleanup_module._quarantine(selected, quarantine, "test-receipt")
    assert not old.exists()
    restored = Path(moves[0]["quarantined_to"])
    assert restored.is_dir()
    assert (restored / "receipt.json").is_file()


def test_policy_refuses_overlap_with_protected_data(tmp_path, cleanup_module):
    root = tmp_path / "capture"
    root.mkdir()
    policy_path = tmp_path / "policy.yaml"
    _write_policy(policy_path, root, tmp_path / "quarantine", protected=[root])
    with pytest.raises(RuntimeError, match="overlaps protected data"):
        cleanup_module._collect(cleanup_module._load_policy(policy_path))


def test_purge_requires_the_exact_quarantine_receipt_and_never_discovers_targets(
    tmp_path, cleanup_module
):
    root = tmp_path / "staging"
    root.mkdir()
    quarantine = tmp_path / "quarantine"
    policy_path = tmp_path / "policy.yaml"
    _write_policy(policy_path, root, quarantine)
    payload = quarantine / "receipt-1" / "old-fixtures" / "fixture-old"
    payload.mkdir(parents=True)
    (payload / "payload.txt").write_text("reclaim me", encoding="utf-8")
    protected = tmp_path / "live"
    protected.mkdir()
    receipt = tmp_path / "quarantine-receipt.json"
    receipt.write_text(json.dumps({
        "version": 1, "mode": "quarantine",
        "moves": [{"quarantined_to": str(payload)}],
    }), encoding="utf-8")
    digest = hashlib.sha256(receipt.read_bytes()).hexdigest()

    result = cleanup_module._purge(
        policy=cleanup_module._load_policy(policy_path),
        quarantine_receipt=receipt,
        expected_hash=digest,
    )
    assert result["purged_bytes"] == len("reclaim me")
    assert result["purged_files"] == 1
    assert not payload.exists()
    assert protected.exists()


def test_purge_refuses_a_tampered_receipt(tmp_path, cleanup_module):
    root = tmp_path / "staging"
    root.mkdir()
    policy_path = tmp_path / "policy.yaml"
    _write_policy(policy_path, root, tmp_path / "quarantine")
    receipt = tmp_path / "quarantine-receipt.json"
    receipt.write_text(json.dumps({"mode": "quarantine", "moves": []}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="SHA-256 does not match"):
        cleanup_module._purge(
            policy=cleanup_module._load_policy(policy_path),
            quarantine_receipt=receipt,
            expected_hash="0" * 64,
        )
