"""Shared helper: re-bind the committed MTU baseline replay to the CURRENT corpus.

The committed baseline (`evidence/mtu-eval-replay-2026-08-02.json`) is a frozen
replay record and is never rewritten to follow later corpus amendments. Test
fixtures that use it as a stand-in for "a report against today's corpus" must
therefore reconcile it, or `validate_report_shape` reports assertion/case
coverage defects that belong to the fixture, not to the code under test.

Three amendment classes are reconciled here, all by construction:

* an expectation's kind/text changed  -> the recorded assertion is re-labelled;
* a case was retired or merged away   -> its recorded row is dropped;
* a case gained a declared expectation (e.g. a merge folding the retired case's
  expectation into the surviving one) -> the assertion is materialised. Semantic
  kinds land as `pending_judge` (the baseline never scored them); exact kinds are
  scored deterministically against the recorded response with the runner's own
  normalisation, so no verdict is invented.

Corpus cases with no recorded row at all (newly authored cases) are NOT
fabricated — the reconciled report simply does not cover them, which is the
truth about a replay that predates them.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from mtu_eval_policy import _expectations_by_turn  # noqa: F401
from gateway.pa_eval import normalize_for_exact_match

_SEMANTIC = {"must", "must_not"}
_EXACT = {"exact_present", "exact_absent"}


def rebind_report_to_corpus(report: dict[str, Any], corpus: dict[str, Any]) -> dict[str, Any]:
    """Return ``report`` with case + assertion coverage aligned to ``corpus``."""
    corpus_cases = {str(case.get("case_id")): case for case in corpus.get("cases") or []}
    rebound_cases: list[dict[str, Any]] = []
    for case in report.get("cases") or []:
        declared_case = corpus_cases.get(str(case.get("case_id")))
        if declared_case is None:
            continue  # retired or merged-away id: drop the stale row
        expected_turns = _expectations_by_turn(declared_case)
        turns = list(case.get("turns") or [])
        if len(turns) != len(expected_turns):
            rebound_cases.append(case)
            continue
        exact_total = exact_passed = exact_failed = 0
        for index, turn in enumerate(turns):
            recorded = {
                str(item.get("label")): item for item in turn.get("assertions") or []
            }
            response = str(turn.get("response") or "")
            rebuilt: list[dict[str, Any]] = []
            for label, kind, text in expected_turns[index]:
                previous = recorded.get(label) or {}
                assertion = copy.deepcopy(previous)
                assertion["label"] = label
                assertion["kind"] = kind
                if kind in _EXACT:
                    unchanged = (
                        str(previous.get("kind")) == kind
                        and str(previous.get("text") or "") == text
                        and isinstance(previous.get("passed"), bool)
                    )
                    assertion["text"] = text
                    if not unchanged:
                        # No recorded verdict for this identity: score it the way the
                        # runner does, against the response the baseline recorded.
                        present = (
                            normalize_for_exact_match(text)
                            in normalize_for_exact_match(response)
                        )
                        passed = present if kind == "exact_present" else not present
                        assertion.update(
                            status="passed" if passed else "failed", passed=passed
                        )
                    assertion.pop("judge_why", None)
                    assertion.pop("review_needed", None)
                    exact_total += 1
                    exact_passed += int(assertion.get("passed") is True)
                    exact_failed += int(assertion.get("passed") is False)
                else:
                    assertion["text"] = None
                    if not isinstance(assertion.get("passed"), bool):
                        assertion.update(status="pending_judge", passed=None)
                rebuilt.append(assertion)
            turn["assertions"] = rebuilt
        case["deterministic"] = {
            "status": "not_applicable" if not exact_total else (
                "passed" if not exact_failed else "failed"
            ),
            "assertion_count": exact_total,
            "passed": exact_passed,
            "failed": exact_failed,
        }
        rebound_cases.append(case)
    report["cases"] = rebound_cases
    return report


def synthesize_missing_selected_cases(
    report: dict[str, Any], corpus: dict[str, Any]
) -> dict[str, Any]:
    """Add synthetic PASSING rows for corpus cases the baseline replay predates.

    Only for gate fixtures that must present a complete report for their declared
    tags: a real gate run replays every selected case, but the committed baseline
    cannot contain cases authored after it. The synthetic response is built from
    the case's own ``exact_present`` texts so deterministic assertions score
    honestly against it; semantic assertions land as ``pending_judge`` and are
    resolved by the caller's usual fixture pass.
    """
    tags = set(str(tag) for tag in (report.get("corpus") or {}).get("tags") or [])
    present = {str(case.get("case_id")) for case in report.get("cases") or []}
    for case in corpus.get("cases") or []:
        case_id = str(case.get("case_id"))
        if case_id in present:
            continue
        if tags and not tags.intersection(case.get("tags") or []):
            continue
        expected_turns = _expectations_by_turn(case)
        turns = []
        for index, expectations in enumerate(expected_turns):
            response = "\n".join(
                text for _label, kind, text in expectations if kind == "exact_present"
            )
            assertions = []
            for label, kind, text in expectations:
                assertion: dict[str, Any] = {"label": label, "kind": kind, "text": None}
                if kind in _EXACT:
                    assertion["text"] = text
                    found = (
                        normalize_for_exact_match(text)
                        in normalize_for_exact_match(response)
                    )
                    passed = found if kind == "exact_present" else not found
                    assertion.update(status="passed" if passed else "failed", passed=passed)
                else:
                    assertion.update(status="pending_judge", passed=None)
                assertions.append(assertion)
            turns.append(
                {
                    "turn_index": index,
                    "attempt_id": f"fixture-{case_id}-d1-turn-{index}",
                    "processed": True,
                    "response": response,
                    "outbound_count": 1,
                    "assertions": assertions,
                }
            )
        exact = [
            item for turn in turns for item in turn["assertions"] if item["kind"] in _EXACT
        ]
        failed = sum(1 for item in exact if item["passed"] is False)
        report["cases"].append(
            {
                "case_id": case_id,
                "tags": list(case.get("tags") or []),
                "draw": 1,
                "canary": bool(case.get("canary")),
                "run_id": f"fixture-{case_id}-d1",
                "replay_namespace": f"agent:replay:fixture-{case_id}-d1",
                "setup": {
                    "command": (case.get("input") or {}).get("setup") or "/new",
                    "attempt_id": f"fixture-{case_id}-d1-setup",
                    "blocked_commands": [],
                },
                "turn_count": len(turns),
                "turns": turns,
                "deterministic": {
                    "status": "not_applicable" if not exact else (
                        "passed" if not failed else "failed"
                    ),
                    "assertion_count": len(exact),
                    "passed": len(exact) - failed,
                    "failed": failed,
                },
            }
        )
    return report


def load_rebound_baseline(baseline_path: Path, corpus_path: Path) -> tuple[dict, dict]:
    report = json.loads(Path(baseline_path).read_text())
    corpus = json.loads(Path(corpus_path).read_text())
    return rebind_report_to_corpus(report, corpus), corpus
