import asyncio
import json

from gateway.pa_eval import (
    PAEvalCorpus,
    PAEvalExpectation,
    adapt_case_to_replay,
    normalize_whitespace,
    run_exact_assertion,
    run_replay_bundle,
)
from gateway.replay import ReplayResult


def _corpus(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({
        "meta": {"version": 1},
        "cases": [{
            "case_id": "CASE-1",
            "tags": ["ask-once", "formatting"],
            "input": {
                "setup": "/new",
                "turns": [
                    {
                        "text": "First turn",
                        "expected_before_next": ["asks only once"],
                    },
                    {"text": "Second turn"},
                ],
            },
            "expected": [
                {"label": "asks only once", "kind": "must"},
                {
                    "label": "required sentence",
                    "kind": "exact_present",
                    "text": "Required sentence.",
                    "critical": True,
                },
                {
                    "label": "no markdown",
                    "kind": "exact_absent",
                    "text": "**",
                },
            ],
        }],
    }), encoding="utf-8")
    return PAEvalCorpus.from_path(path)


def test_adapter_builds_true_multi_turn_shared_namespace(tmp_path):
    case = _corpus(tmp_path).cases[0]
    bundle = adapt_case_to_replay(case, draw=1)

    assert bundle.setup_plan is not None
    assert bundle.setup_plan.replay_safe_commands == ("new",)
    assert len(bundle.turns) == 2
    assert bundle.turns[0].plan.replay_namespace == bundle.turns[1].plan.replay_namespace
    assert bundle.turns[0].plan.attempt_id != bundle.turns[1].plan.attempt_id
    assert [plan.plan.messages[0]["body"] for plan in bundle.turns] == [
        "First turn",
        "Second turn",
    ]
    assert "First turn\nSecond turn" not in json.dumps(bundle.turns[0].plan.to_dict())
    assert [item.label for item in bundle.turns[0].expectations] == ["asks only once"]
    assert {item.kind for item in bundle.turns[1].expectations} == {
        "must",
        "exact_present",
        "exact_absent",
    }


def test_tag_selection_is_union_and_empty_means_all(tmp_path):
    corpus = _corpus(tmp_path)
    assert corpus.select() == corpus.cases
    assert corpus.select(["ask-once"]) == corpus.cases
    assert corpus.select(["missing"]) == ()


def test_exact_assertions_are_whitespace_normalized_and_case_sensitive():
    response = "Required\n\t sentence. Nothing else."
    present = PAEvalExpectation("present", "exact_present", "Required sentence.")
    absent = PAEvalExpectation("absent", "exact_absent", "**")

    assert normalize_whitespace(response) == "Required sentence. Nothing else."
    assert run_exact_assertion(response, present)["passed"] is True
    assert run_exact_assertion(response, absent)["passed"] is True
    assert run_exact_assertion(response.lower(), present)["passed"] is False


def test_bundle_runner_preserves_per_turn_outputs_and_assertions(tmp_path):
    bundle = adapt_case_to_replay(_corpus(tmp_path).cases[0])

    class Runner:
        def __init__(self):
            self.calls = []

        async def replay(self, plan):
            self.calls.append(plan)
            if plan.attempt_id.endswith("setup"):
                text = "New conversation"
            elif plan.attempt_id.endswith("turn-1"):
                text = "What is the missing fact?"
            else:
                text = "Required\n sentence."
            outbound = [{"kwargs": {"content": text}, "args": [], "kind": "send"}]
            if plan.attempt_id.endswith("turn-2"):
                outbound = [
                    {"kwargs": {"content": "**progress**"}, "args": [], "kind": "send"},
                    {"kwargs": {"content": text}, "args": [], "kind": "send"},
                    {"kwargs": {}, "args": ["chat-id"], "kind": "delete_message"},
                ]
            return ReplayResult(
                run_id=plan.run_id,
                attempt_id=plan.attempt_id,
                platform=plan.platform,
                processed=1,
                outbound=outbound,
                blocked_commands=[],
                delivery_mode="capture",
            )

    runner = Runner()
    report = asyncio.run(run_replay_bundle(runner, bundle))

    assert len(runner.calls) == 3
    assert report["turn_count"] == 2
    assert report["turns"][0]["response"] == "What is the missing fact?"
    assert report["turns"][0]["assertions"][0]["status"] == "pending_judge"
    assert report["turns"][1]["assertions"][1]["status"] == "passed"
    assert report["turns"][1]["assertions"][2]["status"] == "passed"
    assert report["turns"][1]["response"] == "Required\n sentence."
    assert report["turns"][1]["response_source"]["outbound_index"] == 1
    assert report["deterministic"] == {
        "status": "passed",
        "assertion_count": 2,
        "passed": 2,
        "failed": 0,
    }
