#!/usr/bin/env python3
"""Score semantic MTU expectations with a pinned, schema-constrained judge."""
from __future__ import annotations

import argparse
import copy
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from mtu_eval_policy import DEFAULT_POLICY, load_policy, read_json

JUDGE_PROMPT = """You are judging one synthetic MTU BOR-assistant eval turn.
Score only the supplied must/must_not labels against the assistant response and
the conversation so far. A `must` passes only when the behavior is present. A
`must_not` passes only when the forbidden behavior is absent. Do not reward
plausibility, infer missing acts, or grade exact-string assertions; those belong
to the deterministic runner. Copy every label and kind exactly. Set
review_needed when the evidence is genuinely ambiguous. Return schema-valid
JSON only.
"""


def _pending_turns(report: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [
        (case, turn)
        for case in report.get("cases") or []
        for turn in case.get("turns") or []
        if any(a.get("kind") in {"must", "must_not"} for a in turn.get("assertions") or [])
    ]


def _judge_turn(
    case: dict[str, Any], turn: dict[str, Any], *, model: str, effort: str,
    schema: Path, timeout: int, input_turns: list[dict[str, Any]],
) -> dict[str, Any]:
    expectations = [
        {"label": a["label"], "kind": a["kind"], "critical": bool(a.get("critical"))}
        for a in turn.get("assertions") or []
        if a.get("kind") in {"must", "must_not"}
    ]
    turn_index = int(turn.get("turn_index", 0))
    conversation: list[dict[str, str]] = []
    for index in range(turn_index + 1):
        if index < len(input_turns):
            conversation.append({"role": "user", "text": str(input_turns[index].get("text") or "")})
        if index < turn_index and index < len(case.get("turns") or []):
            conversation.append({"role": "assistant", "text": str(case["turns"][index].get("response") or "")})
    packet = {
        "case_id": case.get("case_id"),
        "draw": case.get("draw", 1),
        "turn_index": turn.get("turn_index", 0),
        "conversation": conversation,
        "assistant_response": turn.get("response", ""),
        "expectations": expectations,
    }
    with tempfile.TemporaryDirectory(prefix="mtu-judge-") as work:
        final = Path(work) / "final.json"
        cmd = [
            "codex", "exec", "--ignore-user-config", "--ignore-rules",
            "--skip-git-repo-check", "--cd", work, "--sandbox", "read-only",
            "--json", "-o", str(final), "--output-schema", str(schema),
            "--model", model, "-c", f'model_reasoning_effort="{effort}"', "-",
        ]
        proc = subprocess.run(
            cmd, input=JUDGE_PROMPT + "\nPACKET:\n" + json.dumps(packet, ensure_ascii=False),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, timeout=timeout,
        )
        if proc.returncode:
            raise RuntimeError(f"judge failed: {proc.stderr[:1000]}")
        result = read_json(final)
    if (result.get("case_id"), result.get("draw"), result.get("turn_index")) != (
        packet["case_id"], packet["draw"], packet["turn_index"]
    ):
        raise ValueError("judge identity mismatch")
    expected_identity = [(x["label"], x["kind"]) for x in expectations]
    actual_identity = [(x.get("label"), x.get("kind")) for x in result.get("results") or []]
    if actual_identity != expected_identity:
        raise ValueError("judge changed, omitted, or reordered expectation labels")
    return result


def score_report(
    report: dict[str, Any], *, policy_path: Path = DEFAULT_POLICY,
    limit_turns: int | None = None, timeout: int = 300,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    policy = load_policy(policy_path)
    judge = policy["judge"]
    model, effort = str(judge["model"]), str(judge["reasoning_effort"])
    schema = (policy_path.parent / judge["schema"]).resolve()
    corpus = read_json((policy_path.parent / policy["corpus"]["path"]).resolve())
    inputs_by_case = {
        str(case["case_id"]): list((case.get("input") or {}).get("turns") or [])
        for case in corpus.get("cases") or []
    }
    scored = copy.deepcopy(report)
    selected = _pending_turns(scored)
    if limit_turns is not None:
        selected = selected[:limit_turns]
    calibration: list[dict[str, Any]] = []
    failures = reviews = assertions = 0
    for case, turn in selected:
        input_turns = inputs_by_case.get(str(case["case_id"]), [])
        verdict = _judge_turn(
            case, turn, model=model, effort=effort, schema=schema,
            timeout=timeout, input_turns=input_turns,
        )
        by_identity = {(x["label"], x["kind"]): x for x in verdict["results"]}
        for assertion in turn.get("assertions") or []:
            if assertion.get("kind") not in {"must", "must_not"}:
                continue
            item = by_identity[(assertion["label"], assertion["kind"])]
            assertion.update({
                "status": "passed" if item["passed"] else "failed",
                "passed": item["passed"], "judge_why": item["why"],
                "review_needed": item["review_needed"],
            })
            assertions += 1
            failures += int(not item["passed"])
            reviews += int(item["review_needed"])
        calibration.append({
            "case_id": case["case_id"], "draw": case.get("draw", 1),
            "turn_index": turn.get("turn_index", 0),
            "input_turns": input_turns[: int(turn.get("turn_index", 0)) + 1],
            "response": turn.get("response", ""),
            "machine_results": verdict["results"],
            "amelia_grade": {"options": ["pass", "fail", "discuss"], "verdicts": []},
        })
    total_pending = len(_pending_turns(report))
    complete = len(selected) == total_pending
    scored["judge_summary"] = {
        "model": model, "reasoning_effort": effort,
        "schema": str(judge["schema"]), "turns_scored": len(selected),
        "turns_total": total_pending, "assertions_scored": assertions,
        "failed": failures, "review_needed": reviews,
        "status": "passed" if complete and failures == 0 and reviews == 0 else (
            "failed" if complete else "calibration_pending"
        ),
    }
    return scored, calibration


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--limit-turns", type=int)
    parser.add_argument("--calibration-output", type=Path)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    scored, calibration = score_report(
        read_json(args.report), policy_path=args.policy,
        limit_turns=args.limit_turns, timeout=args.timeout,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(scored, indent=2, ensure_ascii=False) + "\n")
    if args.calibration_output:
        packet = {
            "schema_version": 1, "reviewer": "amelia", "route_via": "edna-mtu",
            "judge_pin": scored["judge_summary"], "items": calibration,
        }
        args.calibration_output.parent.mkdir(parents=True, exist_ok=True)
        args.calibration_output.write_text(json.dumps(packet, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"ok": True, "judge_summary": scored["judge_summary"]}, sort_keys=True))


if __name__ == "__main__":
    main()
