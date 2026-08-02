import importlib.util
import json
from pathlib import Path

import yaml


RUNNER = Path(__file__).parents[2] / "deploy/finexis/mtu/evals/run_eval.py"
SPEC = importlib.util.spec_from_file_location("mtu_s2_eval", RUNNER)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _row(file: str, key: str, found: bool, entry=None) -> dict:
    return {
        "content": json.dumps(
            {
                "file": file,
                "key": key,
                "found": found,
                "entry": entry,
                "match": "exact" if found else "none",
            }
        )
    }


def _known_outcome(category_key: str, category_found: bool) -> dict:
    return {
        "case_id": "MTU-044_known_product_lookup_multiturn",
        "draw": 1,
        "expected": [
            {
                "label": "uses approved reference example 16 for HSBC Term Protector",
                "kind": "must",
            },
            {"label": "does not ask for a reference number or insurer", "kind": "must_not"},
        ],
        "responses": ["Please provide the remaining material plan facts."],
        "tool_rows": [
            _row(
                "reference/062-approved-products.yaml",
                "HSBC Term Protector",
                True,
                {"key": "HSBC Term Protector", "reference_examples": [16]},
            ),
            _row(
                "reference/064-replacement-taxonomy.yaml",
                category_key,
                category_found,
                {"key": "Term", "normalized_category": "Term"} if category_found else None,
            ),
        ],
    }


def test_lookup_verdict_requires_canonical_category_hit() -> None:
    verdict = MODULE._lookup_verdict([_known_outcome("term", False)])
    assert verdict["cases"][0]["pass"] is False
    assert verdict["cases"][0]["failed_labels"] == [
        "uses approved reference example 16 for HSBC Term Protector"
    ]


def test_lookup_verdict_checks_every_declared_label() -> None:
    verdict = MODULE._lookup_verdict([_known_outcome("Term", True)])
    assert verdict["cases"][0]["pass"] is True
    assert verdict["cases"][0]["failed_labels"] == []


def test_lookup_verdict_refuses_unknown_expectation() -> None:
    outcome = _known_outcome("Term", True)
    outcome["expected"].append({"label": "new unimplemented contract", "kind": "must"})
    try:
        MODULE._lookup_verdict([outcome])
    except ValueError as exc:
        assert "new unimplemented contract" in str(exc)
    else:
        raise AssertionError("undeclared evaluator predicate did not fail closed")


def test_approved_product_keys_align_with_insurer_keys() -> None:
    reference = RUNNER.parents[1] / "reference"
    approved = yaml.safe_load((reference / "062-approved-products.yaml").read_text())
    insurers = yaml.safe_load((reference / "063-product-insurers.yaml").read_text())
    approved_keys = {entry["key"] for entry in approved["entries"]}
    insurer_keys = {entry["key"] for entry in insurers["entries"]}
    assert {"Wealth Voyage", "Wealth Abundance"} <= approved_keys & insurer_keys
