#!/usr/bin/env python3
"""Run MTU lookup or ablation cases through a non-live Hermes replay copy."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml


HERE = Path(__file__).resolve().parent
DEPLOY = HERE.parent
REPO = HERE.parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
MODEL = "gpt-5.6-luna"


ABLATION_PATTERNS: dict[str, tuple[str, ...]] = {
    "derive_rop_without_reasking": (
        r"NEVER ASK EXISTING-PLANS AND ROP AS TWO SEPARATE ITEMS.*?when an existing plan exists and the advisor has not said whether it is being replaced\. ",
        r"ASK-ONCE \(amelia-ruled 2026-07-31\):.*?Re-asking an answered ROP question is a defect\.",
    ),
    "standard_narrative_defaults_not_asked": (
        r"NEVER ASK FOR THESE —.*?draft instead of asking\. ",
        r"STANDARD NARRATIVE DEFAULTS:.*?Do NOT emit \[\[MISSING\]\] for the alternatives sentence\. \(b\) SHIELD/HOSPITALISATION NEEDS —.*?Do not ask or leave \[\[MISSING\]\] for the Shield needs/why-recommended reason\.",
        r"THE REASON THE CLIENT CHOSE THE PLAN AND THE ALTERNATIVES CONSIDERED ARE NEVER ASKED — both come from STANDARD NARRATIVE DEFAULTS above\. ",
    ),
    "insurer_resolved_not_asked": (
        r"NEVER ASK WHICH INSURER a product belongs to.*?Asking the advisor for the insurer is a defect\. ",
    ),
    "fund_alignment_computed_not_asked": (
        r"NEVER ASK the advisor about \"fund-objective alignment\".*?compare against the client's stated risk profile\. ",
    ),
    "ilp_product_facts_not_asked": (
        r"NEVER ASK THE ADVISOR ABOUT surrender charges.*?never block the draft on any of the four\. ",
    ),
    "voyage_shorthand_not_asked": (
        r"PRODUCT-NAME SHORTHAND \(amelia-ruled 2026-08-01\):.*?Assume NO riders unless riders are stated\. ",
    ),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_runtime(ablation_rule: str | None) -> Path:
    root = Path(tempfile.mkdtemp(prefix="mtu-s2-eval-"))
    shutil.copy2(DEPLOY / "config.yaml", root / "config.yaml")
    shutil.copy2(DEPLOY / "mtu_constitution.yaml", root / "mtu_constitution.yaml")
    shutil.copy2(DEPLOY / "SOUL.md", root / "SOUL.md")

    config = yaml.safe_load((root / "config.yaml").read_text())
    config["model"]["default"] = MODEL
    config["agent"]["max_turns"] = 6
    config["pa"]["constitution_path"] = str(root / "mtu_constitution.yaml")
    config.setdefault("platforms", {})["whatsapp"] = {"enabled": True, "extra": {}}
    (root / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))

    constitution = yaml.safe_load((root / "mtu_constitution.yaml").read_text())
    constitution["runtime"]["model"] = MODEL
    brief = constitution["job_briefs"]["bor_generation"]
    brief["runtime"]["model"] = MODEL
    # The eval is deliberately narrow: production Hermes tool execution, with
    # only the two manifest-bound tools exposed so unrelated filesystem tools
    # cannot manufacture an alternate retrieval path.
    brief["enabled_toolsets"] = ["pa-knowledge"]
    if ablation_rule:
        patterns = ABLATION_PATTERNS[ablation_rule]
        original = list(brief["instructions"])
        replaced = []
        hit_count = 0
        for instruction in original:
            value = instruction
            for pattern in patterns:
                value, hits = re.subn(pattern, "", value, flags=re.DOTALL)
                hit_count += hits
            replaced.append(value.strip())
        if hit_count != len(patterns):
            raise RuntimeError(
                f"ablation {ablation_rule} expected {len(patterns)} fragments, removed {hit_count}"
            )
        brief["instructions"] = [value for value in replaced if value]
    (root / "mtu_constitution.yaml").write_text(
        yaml.safe_dump(constitution, sort_keys=False, allow_unicode=True)
    )
    return root


def _sync_knowledge(root: Path) -> None:
    from hermes_cli.pa_compose import sync_pa_knowledge

    sync_pa_knowledge(
        DEPLOY,
        root / "mtu_constitution.yaml",
        root / "knowledge",
        root / "knowledge-sync.manifest.json",
    )


def _messages(case: dict[str, Any], draw: int) -> tuple[dict[str, Any], ...]:
    turns = case["input"]["turns"]
    start = 1_785_640_000 + draw * 100 + sum(ord(c) for c in case["case_id"])
    chat_number = int(hashlib.sha256(f"{case['case_id']}:{draw}".encode()).hexdigest()[:12], 16)
    chat = f"65{chat_number:014d}@s.whatsapp.net"
    return tuple(
        {
            "messageId": f"{case['case_id']}-{draw}-{index}",
            "chatId": chat,
            "senderId": chat,
            "body": turn["text"],
            "timestamp": start + index * 10,
        }
        for index, turn in enumerate(turns)
    )


def _new_session_rows(root: Path, before: set[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((root / "sessions").glob("*.jsonl")):
        if path in before:
            continue
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("role") == "tool" and row.get("name") in {
                "pa_reference_lookup",
                "pa_knowledge_fetch",
            }:
                rows.append(row)
    return rows


async def _run_cases(root: Path, cases: list[dict[str, Any]], draws: bool) -> list[dict[str, Any]]:
    from gateway.replay import ReplayPlan
    from gateway.run import GatewayRunner

    runner = GatewayRunner()
    outcomes: list[dict[str, Any]] = []
    for case in cases:
        count = int(case.get("draws", 1)) if draws else 1
        for draw in range(count):
            before = set((root / "sessions").glob("*.jsonl"))
            plan = ReplayPlan(
                platform="whatsapp",
                run_id=f"s2-{case['case_id']}-{draw}",
                attempt_id=f"s2-{case['case_id']}-{draw}-attempt",
                messages=_messages(case, draw),
            )
            started = time.time()
            result = await runner.replay(plan)
            bodies = [
                str(item.get("kwargs", {}).get("content") or "")
                for item in result.outbound
                if item.get("kind") == "send"
            ]
            outcomes.append(
                {
                    "case_id": case["case_id"],
                    "draw": draw + 1,
                    "expected": case["expected"],
                    "processed": result.processed,
                    "responses": bodies,
                    "tool_rows": _new_session_rows(root, before),
                    "duration_seconds": round(time.time() - started, 3),
                }
            )
    return outcomes


def _judge(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    from openai import OpenAI

    payload = [
        {
            "case_id": item["case_id"],
            "expected": item["expected"],
            "responses": item["responses"],
        }
        for item in outcomes
    ]
    prompt = (
        "Judge each case strictly against every expected label. A must label passes only "
        "when the responses demonstrate it; a must_not label passes only when the forbidden "
        "behavior is absent. Return JSON only as {\"cases\":[{\"case_id\":str,"
        "\"pass\":bool,\"failed_labels\":[str],\"reason\":str}]}.\nCASES:\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    response = OpenAI(api_key=os.environ["OPENAI_API_KEY"]).responses.create(
        model=MODEL,
        input=prompt,
        max_output_tokens=5000,
    )
    text = response.output_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL)
    return json.loads(text)


def _lookup_verdict(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate every declared lookup expectation against the tool trail and copy."""

    def lookup(
        tools: list[dict[str, Any]], file: str, key: str, found: bool
    ) -> dict[str, Any] | None:
        return next(
            (
                tool
                for tool in tools
                if tool.get("file") == file
                and tool.get("key") == key
                and tool.get("found") is found
                and tool.get("match") == ("exact" if found else "none")
            ),
            None,
        )

    def evaluate_label(
        label: str, tools: list[dict[str, Any]], response: str
    ) -> bool:
        product_file = "reference/062-approved-products.yaml"
        taxonomy_file = "reference/064-replacement-taxonomy.yaml"
        path_file = "reference/061-ekyc-directory-map.yaml"
        normalized = response.lower()
        checks = {
            "uses approved reference example 16 for HSBC Term Protector": lambda: (
                (row := lookup(tools, product_file, "HSBC Term Protector", True)) is not None
                and 16 in row.get("entry", {}).get("reference_examples", [])
                and lookup(tools, taxonomy_file, "Term", True) is not None
                and not any(
                    tool.get("file") == taxonomy_file and tool.get("found") is False
                    for tool in tools
                )
            ),
            "does not ask for a reference number or insurer": lambda: not re.search(
                r"(?:provide|confirm|what|which|tell me)[^.?\n]*(?:reference number|insurer)",
                normalized,
            ),
            "states that Melody has not approved a reference example for the named product and escalates to Melody": lambda: (
                lookup(tools, product_file, "HSBC Future Protector X", False) is not None
                and "melody" in normalized
                and re.search(
                    r"(?:not (?:in|resolved in) the approved|not approved|no (?:exact )?approved reference (?:entry|example)|has no approved)",
                    normalized,
                )
                is not None
            ),
            "does not substitute a nearest-looking approved product or file number": lambda: (
                not any(tool.get("file") == product_file and tool.get("found") is True for tool in tools)
                and not re.search(r"(?:file|example|reference)\s*(?:number|no\.?|#)?\s*\d+", normalized)
            ),
            "does not expose internal lookup vocabulary": lambda: not re.search(
                r"\b(?:lookup|keyed reference|yaml|reference/0\d\d|internal table)\b",
                normalized,
            ),
            "treats the misspelled key as not found and routes to Melody escalation": lambda: (
                lookup(tools, product_file, "HSBC Term Protecter", False) is not None
                and "melody" in normalized
            ),
            "does not fuzzy-match HSBC Term Protector or return example 16": lambda: (
                not any(tool.get("file") == product_file and tool.get("found") is True for tool in tools)
                and not re.search(r"(?:example|file|reference)\s*(?:number|no\.?|#)?\s*16\b", normalized)
            ),
            "states that this replacement type has no Melody-approved reference example and escalates to Melody": lambda: (
                lookup(tools, taxonomy_file, "Term", True) is not None
                and lookup(tools, taxonomy_file, "Whole Life", True) is not None
                and lookup(tools, path_file, "Term -> Whole Life", False) is not None
                and "melody" in normalized
                and re.search(r"no approved (?:reference )?example", normalized) is not None
            ),
            "does not reuse Term to Term or Whole Life to Term examples": lambda: (
                not any(
                    tool.get("file") == path_file
                    and tool.get("key") in {"Term -> Term", "Whole Life -> Term"}
                    and tool.get("found") is True
                    for tool in tools
                )
                and not re.search(r"(?:example|file|reference)\s*(?:number|no\.?|#)?\s*(?:1|2|16|17)\b", normalized)
            ),
        }
        check = checks.get(label)
        if check is None:
            raise ValueError(f"lookup evaluator has no predicate for declared label: {label}")
        return bool(check())

    cases = []
    for item in outcomes:
        tools = []
        for row in item["tool_rows"]:
            try:
                tools.append(json.loads(row["content"]))
            except (KeyError, json.JSONDecodeError):
                pass
        combined = "\n".join(item["responses"])
        failed_labels = [
            expected["label"]
            for expected in item["expected"]
            if not evaluate_label(expected["label"], tools, combined)
        ]
        passed = not failed_labels
        reason = "all declared labels passed" if passed else "failed declared lookup expectations"
        cases.append(
            {
                "case_id": item["case_id"],
                "draw": item["draw"],
                "pass": passed,
                "failed_labels": failed_labels,
                "reason": reason,
            }
        )
    return {"cases": cases}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=("lookup", "ablation"), required=True)
    parser.add_argument("--rule")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required in the environment")

    if args.suite == "lookup":
        if args.rule:
            raise SystemExit("--rule is valid only for ablation")
        corpus = json.loads((HERE / "lookup-cases.json").read_text())
        cases = corpus["cases"]
        ablation_rule = None
        use_draws = True
    else:
        rules = json.loads((HERE / "ablation-rules.json").read_text())["rules"]
        rule = next((item for item in rules if item["rule_id"] == args.rule), None)
        if rule is None:
            raise SystemExit("--rule must name a rule from ablation-rules.json")
        corpus = json.loads((HERE / "mtu-eval-corpus-v1.json").read_text())
        by_id = {case["case_id"]: case for case in corpus["cases"]}
        cases = [by_id[case_id] for case_id in rule["affected_case_ids"]]
        ablation_rule = args.rule
        use_draws = False

    root = _copy_runtime(ablation_rule)
    try:
        os.environ["HERMES_HOME"] = str(root)
        _sync_knowledge(root)
        outcomes = asyncio.run(_run_cases(root, cases, use_draws))
        verdict = _lookup_verdict(outcomes) if args.suite == "lookup" else _judge(outcomes)
        passed = sum(1 for case in verdict["cases"] if case["pass"])
        report = {
            "schema_version": 1,
            "suite": args.suite,
            "ablation_rule": ablation_rule,
            "model": MODEL,
            "runtime": "GatewayRunner.replay / WhatsApp adapter / non-live temporary HERMES_HOME",
            "production_home_written": False,
            "constitution_sha256": _sha256(root / "mtu_constitution.yaml"),
            "cases_passed": passed,
            "cases_total": len(verdict["cases"]),
            "battery_pass": passed == len(verdict["cases"]),
            "verdict": verdict,
            "outcomes": outcomes,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps({key: report[key] for key in ("suite", "ablation_rule", "cases_passed", "cases_total", "battery_pass")}))
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
