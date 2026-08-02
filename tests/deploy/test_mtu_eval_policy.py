import copy
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "deploy/finexis/mtu/scripts"
BASELINE = ROOT / "deploy/finexis/mtu/evidence/mtu-eval-replay-2026-08-02.json"
POLICY = ROOT / "deploy/finexis/mtu/eval-policy.yaml"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mtu_eval_policy import compare_baseline, load_policy


def _module(name):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _baseline():
    return json.loads(BASELINE.read_text())


def _nightly(status="green"):
    return {"status": status, "recorded_at": datetime.now(timezone.utc).isoformat()}


def _passing_rule_report():
    report = _baseline()
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    report["corpus"]["tags"] = [
        "intake", "rop", "never-ask", "fabrication", "compliance", "client-surface"
    ]
    report["judge_summary"] = {
        "status": "passed", "model": "gpt-5.6-sol",
        "reasoning_effort": "medium", "schema": "evals/mtu-judge.schema.json",
    }
    runtime = report["execution"]["runtime"]
    runtime["source_config_sha256"] = hashlib.sha256(
        (ROOT / "deploy/finexis/mtu/config.yaml").read_bytes()
    ).hexdigest()
    runtime["source_constitution_sha256"] = hashlib.sha256(
        (ROOT / "deploy/finexis/mtu/mtu_constitution.yaml").read_bytes()
    ).hexdigest()
    return report


def test_baseline_comparison_names_population_and_new_failures():
    baseline = _baseline()
    assert compare_baseline(baseline, baseline) == {
        "status": "green",
        "accepted_failure_count": 10,
        "current_failure_count": 10,
        "new_failure_count": 0,
        "new_failure_keys": [],
        "expected_case_count": 43,
        "selected_case_count": 43,
        "missing_case_count": 0,
    }
    broken = copy.deepcopy(baseline)
    assertion = next(
        item
        for case in broken["cases"]
        for turn in case["turns"]
        for item in turn["assertions"]
        if item.get("kind") in {"exact_present", "exact_absent"} and item.get("passed") is True
    )
    assertion.update(status="failed", passed=False)
    verdict = compare_baseline(broken, baseline)
    assert verdict["status"] == "red"
    assert verdict["new_failure_count"] == 1


def test_subset_comparison_does_not_claim_missing_unselected_cases():
    subset = _baseline()
    subset["corpus"]["selected_case_count"] = 3
    subset["cases"] = subset["cases"][:3]
    verdict = compare_baseline(subset, _baseline(), require_full_corpus=False)
    assert verdict["status"] == "green"
    assert verdict["selected_case_count"] == 3
    assert verdict["missing_case_count"] == 0


def test_every_typed_source_has_an_affected_tag_mapping():
    policy = load_policy(POLICY)
    mtu_root = ROOT / "deploy/finexis/mtu"
    typed = {
        str(path.relative_to(mtu_root))
        for directory in ("rules", "compliance", "templates", "job-briefs", "reference")
        for path in (mtu_root / directory).glob("*.yaml")
    }
    assert typed == set(policy["source_tags"])


def test_rule_gate_accepts_mapped_tags_smoke_judge_and_green_nightly():
    deploy = _module("deploy_guarded")
    verdict = deploy.evaluate_gate(
        change_class="rule",
        changed_files=["deploy/finexis/mtu/rules/040-intake.yaml"],
        report=_passing_rule_report(), nightly=_nightly(),
        policy=load_policy(POLICY), baseline=_baseline(),
    )
    assert verdict["ok"] is True


def test_rule_gate_refuses_deliberately_broken_canary():
    deploy = _module("deploy_guarded")
    report = _passing_rule_report()
    assertion = next(
        item
        for case in report["cases"]
        for turn in case["turns"]
        for item in turn["assertions"]
        if item.get("kind") in {"exact_present", "exact_absent"} and item.get("passed") is True
    )
    assertion.update(status="failed", passed=False)
    with pytest.raises(deploy.DeployRefused, match="new deterministic failures"):
        deploy.evaluate_gate(
            change_class="rule",
            changed_files=["deploy/finexis/mtu/rules/040-intake.yaml"],
            report=report, nightly=_nightly(), policy=load_policy(POLICY),
            baseline=_baseline(),
        )


def test_rule_gate_refuses_declared_tag_population_with_a_missing_case():
    deploy = _module("deploy_guarded")
    report = _passing_rule_report()
    report["cases"] = [
        case for case in report["cases"] if case["case_id"] != "MTU-001_safe_opening_intake"
    ]
    with pytest.raises(deploy.DeployRefused, match="omits 1 cases"):
        deploy.evaluate_gate(
            change_class="rule",
            changed_files=["deploy/finexis/mtu/rules/040-intake.yaml"],
            report=report, nightly=_nightly(), policy=load_policy(POLICY),
            baseline=_baseline(),
        )


def test_rule_gate_refuses_unpinned_judge_claim():
    deploy = _module("deploy_guarded")
    report = _passing_rule_report()
    report["judge_summary"]["model"] = "some-other-model"
    with pytest.raises(deploy.DeployRefused, match="pinned model/schema"):
        deploy.evaluate_gate(
            change_class="rule",
            changed_files=["deploy/finexis/mtu/rules/040-intake.yaml"],
            report=report, nightly=_nightly(), policy=load_policy(POLICY),
            baseline=_baseline(),
        )


def test_red_nightly_blocks_until_recorded_word_waiver():
    deploy = _module("deploy_guarded")
    kwargs = dict(
        change_class="rule",
        changed_files=["deploy/finexis/mtu/rules/040-intake.yaml"],
        report=_passing_rule_report(), nightly=_nightly("red"),
        policy=load_policy(POLICY), baseline=_baseline(),
    )
    with pytest.raises(deploy.DeployRefused, match="nightly is red"):
        deploy.evaluate_gate(**kwargs)
    assert deploy.evaluate_gate(
        **kwargs,
        waiver={"principal": "amelia", "quote": "ship this case", "waives": ["nightly_red"]},
    )["ok"] is True


def test_judge_layer_only_scores_semantic_assertions(monkeypatch):
    judge = _module("judge_eval_report")
    report = {
        "cases": [{"case_id": "x", "draw": 1, "turns": [{
            "turn_index": 0, "response": "hello", "assertions": [
                {"label": "semantic", "kind": "must", "status": "pending_judge"},
                {"label": "literal", "kind": "exact_present", "text": "hello", "status": "passed", "passed": True},
            ],
        }]}],
    }
    monkeypatch.setattr(judge, "_judge_turn", lambda *a, **k: {
        "case_id": "x", "draw": 1, "turn_index": 0,
        "results": [{"label": "semantic", "kind": "must", "passed": True, "why": "present", "review_needed": False}],
    })
    scored, _ = judge.score_report(report, policy_path=POLICY)
    assert scored["judge_summary"]["model"] == "gpt-5.6-sol"
    assert scored["judge_summary"]["status"] == "passed"
    assert scored["cases"][0]["turns"][0]["assertions"][1]["status"] == "passed"
