"""Policy helpers shared by the MTU replay, nightly, and deploy gates."""
from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

MTU_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = MTU_ROOT / "eval-policy.yaml"


def load_policy(path: Path = DEFAULT_POLICY) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("MTU eval policy must be a version 1 mapping")
    return data


def assertion_key(case: dict[str, Any], turn: dict[str, Any], assertion: dict[str, Any]) -> str:
    return "|".join((
        str(case.get("case_id")), str(case.get("draw", 1)),
        str(turn.get("turn_index", 0)), str(assertion.get("kind")),
        str(assertion.get("label")), str(assertion.get("text") or ""),
    ))


def deterministic_failures(report: dict[str, Any]) -> set[str]:
    return {
        assertion_key(case, turn, assertion)
        for case in report.get("cases") or []
        for turn in case.get("turns") or []
        for assertion in turn.get("assertions") or []
        if assertion.get("kind") in {"exact_present", "exact_absent"}
        and assertion.get("passed") is False
    }


def compare_baseline(
    report: dict[str, Any], baseline: dict[str, Any], *, require_full_corpus: bool = True
) -> dict[str, Any]:
    current = deterministic_failures(report)
    accepted = deterministic_failures(baseline)
    expected_cases = int((baseline.get("corpus") or {}).get("declared_case_count") or 0)
    selected = int((report.get("corpus") or {}).get("selected_case_count") or 0)
    new = sorted(current - accepted)
    missing_cases = max(0, expected_cases - selected) if require_full_corpus else 0
    return {
        "status": "green" if not new and missing_cases == 0 else "red",
        "accepted_failure_count": len(accepted),
        "current_failure_count": len(current),
        "new_failure_count": len(new),
        "new_failure_keys": new,
        "expected_case_count": expected_cases,
        "selected_case_count": selected,
        "missing_case_count": missing_cases,
    }


def infer_affected_tags(policy: dict[str, Any], changed_files: Iterable[str]) -> set[str]:
    mappings = policy.get("source_tags") or {}
    tags: set[str] = set()
    for raw in changed_files:
        path = str(raw).replace("\\", "/")
        marker = "deploy/finexis/mtu/"
        relative = path.split(marker, 1)[1] if marker in path else path.lstrip("./")
        tags.update(str(tag) for tag in mappings.get(relative, ()))
    return tags


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object: {path}")
    return data


def canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
