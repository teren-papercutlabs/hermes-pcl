#!/usr/bin/env python3
"""Receipt-producing, allowlisted retention quarantine for tgg-app-1.

This is intentionally not a general-purpose disk cleaner.  A policy names
literal roots, protects the live TGG data roots, and selects only direct child
artifacts that have aged out.  The default action is a dry run.  ``--apply``
atomically moves eligible paths to a same-volume quarantine; it never purges
them.  A later, separately approved purge can consume the receipts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml


TERMINAL_VALUES = {"complete", "completed", "success", "succeeded", "terminal"}


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


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for base, dirs, names in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(base) / name).is_symlink()]
        for name in names:
            candidate = Path(base) / name
            if candidate.is_file() and not candidate.is_symlink():
                total += candidate.stat().st_size
    return total


def _file_count(path: Path) -> int:
    if path.is_file():
        return 1
    return sum(
        1
        for base, _, names in os.walk(path, followlinks=False)
        for name in names
        if (Path(base) / name).is_file() and not (Path(base) / name).is_symlink()
    )


def _terminal_receipt(path: Path, names: Iterable[str]) -> bool:
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
        values = [data.get(key) for key in ("status", "state", "outcome")]
        if any(str(value).lower() in TERMINAL_VALUES for value in values):
            return True
    return False


@dataclass(frozen=True)
class Candidate:
    rule: str
    path: Path
    size_bytes: int
    mtime: float


def _load_policy(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise RuntimeError("retention policy must be a version-1 YAML mapping")
    if not isinstance(data.get("rules"), list) or not data["rules"]:
        raise RuntimeError("retention policy must have non-empty rules")
    return data


def _resolve_absolute(value: Any, label: str) -> Path:
    path = Path(str(value or ""))
    if not path.is_absolute():
        raise RuntimeError(f"{label} must be an absolute path")
    return path.resolve()


def _collect(policy: dict[str, Any]) -> tuple[list[Candidate], list[dict[str, Any]]]:
    protected = [_resolve_absolute(value, "protected_roots entry")
                 for value in policy.get("protected_roots", [])]
    now = datetime.now(timezone.utc).timestamp()
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
        age_seconds = float(raw_rule.get("min_age_days", 0)) * 86400
        keep_newest = int(raw_rule.get("keep_newest", 0))
        if age_seconds < 0 or keep_newest < 0:
            raise RuntimeError(f"retention rule {rule_id} has invalid age/keep settings")
        receipt_required = raw_rule.get("require_terminal_receipt") is True
        receipt_names = raw_rule.get("terminal_receipt_names") or ["receipt.json", "deployment-receipt.json"]
        if not isinstance(receipt_names, list) or not all(isinstance(value, str) for value in receipt_names):
            raise RuntimeError(f"retention rule {rule_id} has invalid terminal_receipt_names")

        children: list[Path] = []
        for child in root.iterdir():
            if child.is_symlink() or not any(child.name.startswith(prefix) for prefix in prefixes):
                continue
            resolved = child.resolve()
            if not _inside(resolved, root):
                skipped.append({"rule": rule_id, "path": str(child), "reason": "escapes-root"})
                continue
            children.append(child)
        children.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        retained = set(children[:keep_newest])
        for child in children:
            if child in retained:
                skipped.append({"rule": rule_id, "path": str(child), "reason": "keep-newest"})
                continue
            age = now - child.stat().st_mtime
            if age < age_seconds:
                skipped.append({"rule": rule_id, "path": str(child), "reason": "too-recent"})
                continue
            if receipt_required and not _terminal_receipt(child, receipt_names):
                skipped.append({"rule": rule_id, "path": str(child), "reason": "no-terminal-receipt"})
                continue
            if child.resolve() in seen:
                raise RuntimeError(f"retention policy selected duplicate path {child}")
            seen.add(child.resolve())
            selected.append(Candidate(rule_id, child, _bytes(child), child.stat().st_mtime))
    return selected, skipped


def _quarantine(candidates: list[Candidate], root: Path, receipt_id: str) -> list[dict[str, Any]]:
    root.mkdir(parents=True, exist_ok=True)
    moves: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate.path.stat().st_dev != root.stat().st_dev:
            raise RuntimeError(f"refusing cross-volume move for {candidate.path}")
        destination = root / receipt_id / candidate.rule / candidate.path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise RuntimeError(f"quarantine destination already exists: {destination}")
        os.replace(candidate.path, destination)
        moves.append({"path": str(candidate.path), "quarantined_to": str(destination),
                      "size_bytes": candidate.size_bytes, "rule": candidate.rule})
    return moves


def _purge(
    *, policy: dict[str, Any], quarantine_receipt: Path, expected_hash: str
) -> dict[str, Any]:
    """Irreversibly delete exactly the payloads named by one quarantine receipt.

    This is deliberately a second, explicit operation.  It cannot discover
    targets from a directory listing; the operator supplies the exact prior
    receipt and its SHA-256, and every payload is revalidated against the
    policy's dedicated quarantine root immediately before removal.
    """
    if len(expected_hash) != 64 or any(ch not in "0123456789abcdef" for ch in expected_hash.lower()):
        raise RuntimeError("purge receipt SHA-256 must be 64 hexadecimal characters")
    raw = quarantine_receipt.read_bytes()
    actual_hash = hashlib.sha256(raw).hexdigest()
    if actual_hash != expected_hash.lower():
        raise RuntimeError("purge receipt SHA-256 does not match the supplied receipt")
    try:
        prior = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError("purge receipt is not valid JSON") from exc
    if not isinstance(prior, dict) or prior.get("mode") != "quarantine":
        raise RuntimeError("purge requires a prior quarantine receipt")
    moves = prior.get("moves")
    if not isinstance(moves, list) or not moves:
        raise RuntimeError("prior quarantine receipt names no payloads")
    quarantine_root = _resolve_absolute(policy.get("quarantine_root"), "quarantine_root")
    protected = [_resolve_absolute(value, "protected_roots entry") for value in policy.get("protected_roots", [])]
    if any(_inside(quarantine_root, item) or _inside(item, quarantine_root) for item in protected):
        raise RuntimeError("quarantine_root overlaps protected data")
    purged: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for move in moves:
        if not isinstance(move, dict):
            raise RuntimeError("prior quarantine receipt has malformed move")
        candidate = Path(str(move.get("quarantined_to") or ""))
        if not candidate.is_absolute() or candidate.is_symlink() or not candidate.exists():
            raise RuntimeError("quarantine payload is missing, relative, or a symlink")
        resolved = candidate.resolve()
        if resolved == quarantine_root or not _inside(resolved, quarantine_root):
            raise RuntimeError("purge payload escapes dedicated quarantine root")
        if any(_inside(resolved, item) or _inside(item, resolved) for item in protected):
            raise RuntimeError("purge payload overlaps protected data")
        if resolved in seen:
            raise RuntimeError("prior quarantine receipt names a duplicate payload")
        seen.add(resolved)
        size_bytes, files = _bytes(resolved), _file_count(resolved)
        if resolved.is_dir():
            shutil.rmtree(resolved)
        else:
            resolved.unlink()
        purged.append({"quarantined_to": str(resolved), "size_bytes": size_bytes, "files": files})
    return {
        "source_quarantine_receipt": str(quarantine_receipt.resolve()),
        "source_quarantine_receipt_sha256": actual_hash,
        "purged": purged,
        "purged_bytes": sum(item["size_bytes"] for item in purged),
        "purged_files": sum(item["files"] for item in purged),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True, type=Path)
    receipt_group = parser.add_mutually_exclusive_group(required=True)
    receipt_group.add_argument("--receipt", type=Path)
    receipt_group.add_argument("--receipt-dir", type=Path)
    parser.add_argument("--apply", action="store_true", help="move selected paths to quarantine")
    parser.add_argument("--purge-receipt", type=Path, help="prior quarantine receipt to purge")
    parser.add_argument("--purge-receipt-sha256", help="exact SHA-256 of --purge-receipt")
    args = parser.parse_args()
    policy = _load_policy(args.policy.resolve())
    if args.purge_receipt is not None:
        if args.apply or not args.purge_receipt_sha256:
            raise RuntimeError("purge requires --purge-receipt-sha256 and cannot combine with --apply")
        purge = _purge(
            policy=policy,
            quarantine_receipt=args.purge_receipt.resolve(),
            expected_hash=args.purge_receipt_sha256,
        )
        receipt_path = (
            args.receipt.resolve()
            if args.receipt is not None
            else (args.receipt_dir.resolve() / f"retention-purge-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
        )
        receipt = {
            "version": 1,
            "created_at": _utc_now(),
            "mode": "purge",
            "policy": str(args.policy.resolve()),
            "irreversible_deletion": True,
            **purge,
        }
        _atomic_json(receipt_path, receipt)
        print(json.dumps({"mode": "purge", "purged": len(purge["purged"]),
                          "purged_bytes": purge["purged_bytes"], "receipt": str(receipt_path)}))
        return 0
    candidates, skipped = _collect(policy)
    receipt_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    moved: list[dict[str, Any]] = []
    if args.apply:
        quarantine_root = _resolve_absolute(policy.get("quarantine_root"), "quarantine_root")
        protected = [_resolve_absolute(value, "protected_roots entry") for value in policy.get("protected_roots", [])]
        if any(_inside(quarantine_root, item) or _inside(item, quarantine_root) for item in protected):
            raise RuntimeError("quarantine_root overlaps protected data")
        moved = _quarantine(candidates, quarantine_root, receipt_id)
    receipt_path = (
        args.receipt.resolve()
        if args.receipt is not None
        else (args.receipt_dir.resolve() / f"retention-{receipt_id}.json")
    )
    receipt = {
        "version": 1,
        "created_at": _utc_now(),
        "mode": "quarantine" if args.apply else "dry-run",
        "policy": str(args.policy.resolve()),
        "selected": [{"rule": item.rule, "path": str(item.path), "size_bytes": item.size_bytes,
                      "mtime": datetime.fromtimestamp(item.mtime, timezone.utc).isoformat()}
                     for item in candidates],
        "selected_bytes": sum(item.size_bytes for item in candidates),
        "skipped": skipped,
        "moves": moved,
        "irreversible_deletion": False,
    }
    _atomic_json(receipt_path, receipt)
    print(json.dumps({"mode": receipt["mode"], "selected": len(candidates),
                      "selected_bytes": receipt["selected_bytes"], "receipt": str(receipt_path)}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"retention cleanup refused: {exc}", file=sys.stderr)
        raise SystemExit(2)
