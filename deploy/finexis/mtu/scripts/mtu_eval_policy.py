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

#: Assertion kinds the RUNNER scores, so the gates count them as evidence
#: rather than waiting on a judge.  Kept as a literal set rather than imported
#: from ``gateway.pa_eval``: these gate scripts run standalone against a
#: report file, with no gateway import path and often no runtime at all.
#: ``gateway/pa_eval.py`` is the definition; ``test_deterministic_kinds_match``
#: keeps the two from drifting.
DETERMINISTIC_KINDS = frozenset(
    {"exact_present", "exact_absent", "no_approved_block"}
)


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
        if assertion.get("kind") in DETERMINISTIC_KINDS
        and assertion.get("passed") is False
    }


def _expectations_by_turn(case: dict[str, Any]) -> list[list[tuple[str, str, str]]]:
    expected = list(case.get("expected") or [])
    by_label = {str(item.get("label")): item for item in expected}
    turns = list((case.get("input") or {}).get("turns") or [])
    result: list[list[tuple[str, str, str]]] = []
    for index, turn in enumerate(turns):
        captured = [
            by_label.get(str(label)) or {"label": str(label), "kind": "must"}
            for label in turn.get("expected_before_next") or []
        ]
        if index == len(turns) - 1:
            seen = {
                (str(item.get("label")), str(item.get("kind")), str(item.get("text") or ""))
                for item in captured
            }
            captured.extend(
                item for item in expected
                if (str(item.get("label")), str(item.get("kind")), str(item.get("text") or ""))
                not in seen
            )
        result.append([
            (str(item.get("label")), str(item.get("kind")), str(item.get("text") or ""))
            for item in captured
        ])
    return result


def validate_report_shape(
    report: dict[str, Any], corpus: dict[str, Any], *, require_judge: bool
) -> list[str]:
    """Return structural defects that could otherwise erase failed assertions."""
    defects: list[str] = []
    corpus_cases = {str(case.get("case_id")): case for case in corpus.get("cases") or []}
    seen_runs: set[tuple[str, int]] = set()
    semantic_turns = semantic_assertions = semantic_failures = semantic_reviews = 0
    for actual_case in report.get("cases") or []:
        case_id = str(actual_case.get("case_id"))
        draw = actual_case.get("draw")
        if case_id not in corpus_cases or not isinstance(draw, int) or draw < 1:
            defects.append(f"invalid case/draw identity: {case_id}|{draw}")
            continue
        run_key = (case_id, draw)
        if run_key in seen_runs:
            defects.append(f"duplicate case/draw identity: {case_id}|{draw}")
            continue
        seen_runs.add(run_key)
        expected_turns = _expectations_by_turn(corpus_cases[case_id])
        actual_turns = list(actual_case.get("turns") or [])
        if len(actual_turns) != len(expected_turns):
            defects.append(f"{case_id}|{draw} has incomplete turn coverage")
            continue
        for index, expected in enumerate(expected_turns):
            turn = actual_turns[index]
            if turn.get("turn_index") != index:
                defects.append(f"{case_id}|{draw} has invalid turn order")
                continue
            actual_assertions = list(turn.get("assertions") or [])
            identities = [
                (str(item.get("label")), str(item.get("kind")), str(item.get("text") or ""))
                for item in actual_assertions
            ]
            if identities != expected:
                defects.append(f"{case_id}|{draw}|{index} has incomplete assertion coverage")
                continue
            turn_has_semantic = False
            for assertion in actual_assertions:
                kind = assertion.get("kind")
                if kind in DETERMINISTIC_KINDS:
                    if assertion.get("status") == "not_applicable":
                        # A draft detector with no needles neither passed nor
                        # failed; it is reported, not counted.
                        continue
                    if not isinstance(assertion.get("passed"), bool):
                        defects.append(f"{case_id}|{draw}|{index} has unscored exact assertion")
                    elif assertion.get("status") != (
                        "passed" if assertion["passed"] else "failed"
                    ):
                        defects.append(f"{case_id}|{draw}|{index} has inconsistent exact verdict")
                elif kind in {"must", "must_not"}:
                    turn_has_semantic = True
                    semantic_assertions += 1
                    semantic_failures += int(assertion.get("passed") is False)
                    semantic_reviews += int(assertion.get("review_needed") is True)
                    if require_judge and (
                        assertion.get("passed") is not True
                        or assertion.get("status") != "passed"
                        or assertion.get("review_needed") is not False
                        or not str(assertion.get("judge_why") or "").strip()
                    ):
                        defects.append(f"{case_id}|{draw}|{index} has incomplete judge evidence")
            semantic_turns += int(turn_has_semantic)
    if require_judge:
        summary = report.get("judge_summary") or {}
        expected_summary = {
            "turns_scored": semantic_turns,
            "turns_total": semantic_turns,
            "assertions_scored": semantic_assertions,
            "failed": semantic_failures,
            "review_needed": semantic_reviews,
        }
        if any(summary.get(key) != value for key, value in expected_summary.items()):
            defects.append("judge summary does not reconcile to per-assertion evidence")
    return defects


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
