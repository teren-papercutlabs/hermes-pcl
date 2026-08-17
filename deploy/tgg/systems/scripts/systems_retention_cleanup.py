#!/usr/bin/env python3
"""Deterministic, allowlisted Systems retention for tgg-app-1.

The policy only names direct-child disposable artifacts.  It never discovers
live databases, capture, corpus, WhatsApp media, the active runtime, or the
current rollback release.  A producer must identify a terminal artifact before
it can be removed.  ``--apply`` deliberately frees the selected space; without
it this produces the same durable receipt as a non-mutating preview.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

TERMINAL_VALUES = {"complete", "completed", "committed", "success", "succeeded", "terminal"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(
        (Path(base) / name).stat().st_size
        for base, dirs, names in os.walk(path, followlinks=False)
        for name in names
        if not (Path(base) / name).is_symlink()
    )


def _file_count(path: Path) -> int:
    if path.is_file():
        return 1
    return sum(1 for base, _, names in os.walk(path, followlinks=False)
               for name in names if not (Path(base) / name).is_symlink())


def _terminal_receipt(path: Path, names: Iterable[str]) -> bool:
    """Accept producer terminal states, including the direct deploy ``committed`` state."""
    if not path.is_dir():
        return False
    for name in names:
        candidate = path / name
        if not candidate.is_file() or candidate.is_symlink():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("ok") is True:
            return True
        if any(str(data.get(key, "")).lower() in TERMINAL_VALUES
               for key in ("status", "state", "outcome", "result", "stage")):
            return True
    return False


@dataclass(frozen=True)
class Candidate:
    rule: str
    path: Path
    size_bytes: int
    files: int
    mtime: float


class PartialDeletionError(RuntimeError):
    def __init__(self, deleted: list[dict[str, Any]], error: Exception):
        super().__init__(str(error))
        self.deleted = deleted


def _load_policy(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 2:
        raise RuntimeError("retention policy must be a version-2 YAML mapping")
    if not isinstance(data.get("rules"), list) or not data["rules"]:
        raise RuntimeError("retention policy must have non-empty rules")
    return data


def _resolve_absolute(value: Any, label: str) -> Path:
    path = Path(str(value or ""))
    if not path.is_absolute():
        raise RuntimeError(f"{label} must be an absolute path")
    return path.resolve()


def _collect(policy: dict[str, Any], *, now: float | None = None) -> tuple[list[Candidate], list[dict[str, Any]]]:
    protected = [_resolve_absolute(value, "protected_roots entry") for value in policy.get("protected_roots", [])]
    current = time.time() if now is None else now
    selected: list[Candidate] = []
    skipped: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for raw_rule in policy["rules"]:
        if not isinstance(raw_rule, dict):
            raise RuntimeError("retention rule must be a mapping")
        rule_id = str(raw_rule.get("id") or "").strip()
        root = _resolve_absolute(raw_rule.get("root"), f"rule {rule_id or '<unnamed>'} root")
        if not rule_id or not root.is_dir():
            raise RuntimeError(f"retention rule {rule_id or '<unnamed>'} has no existing directory root")
        if any(_inside(root, item) or _inside(item, root) for item in protected):
            raise RuntimeError(f"retention rule {rule_id} overlaps protected data")
        prefixes = raw_rule.get("direct_child_prefixes") or []
        if not isinstance(prefixes, list) or not all(isinstance(value, str) and value for value in prefixes):
            raise RuntimeError(f"retention rule {rule_id} must name direct_child_prefixes")
        suffixes = raw_rule.get("direct_child_suffixes") or []
        if not isinstance(suffixes, list) or not all(isinstance(value, str) and value for value in suffixes):
            raise RuntimeError(f"retention rule {rule_id} has invalid direct_child_suffixes")
        age_seconds = float(raw_rule.get("min_age_days", 0)) * 86400
        keep_newest = int(raw_rule.get("keep_newest", 0))
        if age_seconds < 0 or keep_newest < 0:
            raise RuntimeError(f"retention rule {rule_id} has invalid age/keep settings")
        receipt_names = raw_rule.get("terminal_receipt_names") or []
        require_terminal = raw_rule.get("require_terminal_receipt") is True
        candidate_mode = raw_rule.get("candidate_mode", "entry")
        if candidate_mode not in {"entry", "payload"}:
            raise RuntimeError(f"retention rule {rule_id} has invalid candidate_mode")
        payload_globs = raw_rule.get("payload_file_globs") or []
        if candidate_mode == "payload" and (not payload_globs or not all(isinstance(value, str) and value for value in payload_globs)):
            raise RuntimeError(f"retention rule {rule_id} must name payload_file_globs")
        children = [child for child in root.iterdir()
                    if not child.is_symlink() and any(child.name.startswith(prefix) for prefix in prefixes)
                    and (not suffixes or any(child.name.endswith(suffix) for suffix in suffixes))]
        children.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        retained = set(children[:keep_newest])
        for child in children:
            resolved = child.resolve()
            if not _inside(resolved, root):
                skipped.append({"rule": rule_id, "path": str(child), "reason": "escapes-root"})
            elif child in retained:
                skipped.append({"rule": rule_id, "path": str(child), "reason": "keep-newest"})
            elif current - child.stat().st_mtime < age_seconds:
                skipped.append({"rule": rule_id, "path": str(child), "reason": "too-recent"})
            elif require_terminal and not _terminal_receipt(child, receipt_names):
                skipped.append({"rule": rule_id, "path": str(child), "reason": "no-terminal-receipt"})
            elif resolved in seen:
                raise RuntimeError(f"retention policy selected duplicate path {child}")
            else:
                if candidate_mode == "entry":
                    seen.add(resolved)
                    selected.append(Candidate(rule_id, child, _bytes(child), _file_count(child), child.stat().st_mtime))
                    continue
                # Payload mode retains the terminal run directory and its manifest forever.
                # Only direct, explicitly named heavyweight payload files may be removed.
                if not child.is_dir():
                    skipped.append({"rule": rule_id, "path": str(child), "reason": "payload-parent-not-directory"})
                    continue
                payloads = [item for item in child.iterdir()
                            if item.is_file() and not item.is_symlink()
                            and any(fnmatch.fnmatch(item.name, pattern) for pattern in payload_globs)]
                if not payloads:
                    skipped.append({"rule": rule_id, "path": str(child), "reason": "no-matching-payload"})
                    continue
                for payload in payloads:
                    payload_resolved = payload.resolve()
                    if payload_resolved in seen:
                        raise RuntimeError(f"retention policy selected duplicate payload {payload}")
                    seen.add(payload_resolved)
                    selected.append(Candidate(rule_id, payload, _bytes(payload), 1, payload.stat().st_mtime))
    return selected, skipped


def _delete(candidates: list[Candidate]) -> list[dict[str, Any]]:
    """Delete only candidates selected from the closed policy; no quarantine retains disk use."""
    deleted: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            if candidate.path.is_dir():
                shutil.rmtree(candidate.path)
            else:
                candidate.path.unlink()
            deleted.append({"rule": candidate.rule, "path": str(candidate.path),
                            "size_bytes": candidate.size_bytes, "files": candidate.files})
        except Exception as exc:
            raise PartialDeletionError(deleted, exc) from exc
    return deleted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--receipt-dir", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="delete selected, policy-verified artifacts")
    args = parser.parse_args()
    policy = _load_policy(args.policy.resolve())
    selected, skipped = _collect(policy)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    receipt_dir = args.receipt_dir.resolve()
    selection = [{"rule": item.rule, "path": str(item.path), "size_bytes": item.size_bytes,
                  "files": item.files, "mtime": datetime.fromtimestamp(item.mtime, timezone.utc).isoformat()}
                 for item in selected]
    receipt = {
        "schema": "systems-retention/v1", "run_id": run_id, "status": "completed", "created_at": _utc_now(),
        "mode": "apply" if args.apply else "dry-run", "policy": str(args.policy.resolve()),
        "selected": selection,
        "selected_bytes": sum(item.size_bytes for item in selected), "skipped": skipped,
        "deleted": [], "deleted_bytes": 0,
    }
    receipt_path = receipt_dir / f"systems-retention-{run_id}.json"
    if args.apply:
        # This fsynced intent is written before the first irreversible operation.
        _atomic_json(receipt_dir / f"systems-retention-{run_id}.intent.json", {
            **receipt, "status": "intent-recorded", "intent_recorded_at": _utc_now(),
        })
        try:
            receipt["deleted"] = _delete(selected)
        except PartialDeletionError as exc:
            receipt.update({"status": "partial-failure", "failure": str(exc), "deleted": exc.deleted})
            receipt["deleted_bytes"] = sum(item["size_bytes"] for item in exc.deleted)
            _atomic_json(receipt_path, receipt)
            print(json.dumps({"status": receipt["status"], "deleted": len(exc.deleted), "receipt": str(receipt_path)}))
            return 2
    receipt["deleted_bytes"] = sum(item["size_bytes"] for item in receipt["deleted"])
    _atomic_json(receipt_path, receipt)
    print(json.dumps({"status": "completed", "mode": receipt["mode"], "selected": len(selected),
                      "deleted": len(receipt["deleted"]), "deleted_bytes": receipt["deleted_bytes"],
                      "receipt": str(receipt_path)}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"systems retention refused: {exc}", file=sys.stderr)
        raise SystemExit(2)
