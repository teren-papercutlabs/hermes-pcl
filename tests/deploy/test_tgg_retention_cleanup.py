"""Focused safety tests for the TGG Systems retention tool."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest
import yaml


@pytest.fixture()
def cleanup_module():
    path = Path(__file__).parents[2] / "deploy/tgg/systems/scripts/systems_retention_cleanup.py"
    spec = importlib.util.spec_from_file_location("tgg_systems_retention_cleanup_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_policy(path: Path, root: Path, protected: list[Path] | None = None):
    path.write_text(yaml.safe_dump({
        "version": 2,
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
    _write_policy(policy_path, root)

    selected, skipped = cleanup_module._collect(cleanup_module._load_policy(policy_path))
    assert [item.path for item in selected] == [old]
    assert {item["reason"] for item in skipped} == {"keep-newest", "no-terminal-receipt"}


def test_apply_deletes_only_selected_terminal_artifacts(tmp_path, cleanup_module):
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
    _write_policy(policy_path, root)
    selected, _ = cleanup_module._collect(cleanup_module._load_policy(policy_path))
    deleted = cleanup_module._delete(selected)
    assert not old.exists()
    assert deleted[0]["path"] == str(old)
    assert newest.exists()


def test_policy_refuses_overlap_with_protected_data(tmp_path, cleanup_module):
    root = tmp_path / "capture"
    root.mkdir()
    policy_path = tmp_path / "policy.yaml"
    _write_policy(policy_path, root, protected=[root])
    with pytest.raises(RuntimeError, match="overlaps protected data"):
        cleanup_module._collect(cleanup_module._load_policy(policy_path))


def test_committed_direct_deploy_receipt_is_terminal(tmp_path, cleanup_module):
    root = tmp_path / "staging"
    root.mkdir()
    policy_path = tmp_path / "policy.yaml"
    _write_policy(policy_path, root)
    candidate = root / "fixture-committed"
    candidate.mkdir()
    (candidate / "receipt.json").write_text(json.dumps({"status": "committed"}))
    assert cleanup_module._terminal_receipt(candidate, ["receipt.json"])


def test_report_payload_prune_keeps_terminal_directory_and_manifest(tmp_path, cleanup_module):
    root = tmp_path / "report-cycle"; root.mkdir()
    old = root / "reconcile-old"; old.mkdir()
    (old / "manifest.json").write_text(json.dumps({"ok": True}))
    payload = old / "preview.db"; payload.write_text("heavy database")
    _age(old, 8)
    newest = root / "reconcile-new"; newest.mkdir()
    (newest / "manifest.json").write_text(json.dumps({"ok": True}))
    (newest / "preview.db").write_text("retain")
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(yaml.safe_dump({
        "version": 2, "protected_roots": [], "rules": [{
            "id": "report-payload", "root": str(root), "direct_child_prefixes": ["reconcile-"],
            "min_age_days": 7, "keep_newest": 1, "require_terminal_receipt": True,
            "terminal_receipt_names": ["manifest.json"], "candidate_mode": "payload",
            "payload_file_globs": ["preview.db"],
        }],
    }))
    selected, _ = cleanup_module._collect(cleanup_module._load_policy(policy_path))
    assert [item.path for item in selected] == [payload]
    cleanup_module._delete(selected)
    assert old.is_dir()
    assert (old / "manifest.json").is_file()
    assert not payload.exists()
    assert (newest / "preview.db").is_file()


def test_apply_writes_intent_before_partial_failure(tmp_path, cleanup_module, monkeypatch):
    root = tmp_path / "staging"; root.mkdir()
    old = root / "fixture-old"; old.mkdir()
    (old / "receipt.json").write_text(json.dumps({"status": "completed"}))
    _age(old, 3)
    newest = root / "fixture-new"; newest.mkdir()
    (newest / "receipt.json").write_text(json.dumps({"status": "completed"}))
    policy_path = tmp_path / "policy.yaml"; _write_policy(policy_path, root)
    receipt_dir = tmp_path / "receipts"
    def fail_after_intent(_):
        raise cleanup_module.PartialDeletionError([], RuntimeError("simulated crash"))
    monkeypatch.setattr(cleanup_module, "_delete", fail_after_intent)
    monkeypatch.setattr(sys, "argv", ["retention", "--policy", str(policy_path), "--receipt-dir", str(receipt_dir), "--apply"])
    assert cleanup_module.main() == 2
    intents = list(receipt_dir.glob("*.intent.json"))
    completions = [path for path in receipt_dir.glob("*.json") if not path.name.endswith(".intent.json")]
    assert len(intents) == len(completions) == 1
    assert json.loads(intents[0].read_text())["status"] == "intent-recorded"
    assert json.loads(completions[0].read_text())["status"] == "partial-failure"
    assert old.exists()


def test_apply_main_completes_and_records_deleted_payload(tmp_path, cleanup_module, monkeypatch, capsys):
    root = tmp_path / "staging"; root.mkdir()
    old = root / "fixture-old"; old.mkdir()
    (old / "receipt.json").write_text(json.dumps({"status": "completed"}))
    (old / "payload.bin").write_bytes(b"payload")
    _age(old, 3)
    newest = root / "fixture-new"; newest.mkdir()
    (newest / "receipt.json").write_text(json.dumps({"status": "completed"}))
    policy_path = tmp_path / "policy.yaml"; _write_policy(policy_path, root)
    receipt_dir = tmp_path / "receipts"
    monkeypatch.setattr(sys, "argv", [
        "retention", "--policy", str(policy_path), "--receipt-dir", str(receipt_dir), "--apply",
    ])

    assert cleanup_module.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "completed"
    assert output["deleted"] == 1
    assert not old.exists()
    assert newest.exists()
    completions = [path for path in receipt_dir.glob("*.json") if not path.name.endswith(".intent.json")]
    assert len(completions) == 1
    receipt = json.loads(completions[0].read_text())
    assert receipt["status"] == "completed"
    assert receipt["deleted"][0]["path"] == str(old)
    assert receipt["deleted_bytes"] == len("payload") + len(json.dumps({"status": "completed"}))
