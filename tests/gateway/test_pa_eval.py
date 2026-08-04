import asyncio
import json

from gateway.pa_eval import (
    PAEvalCorpus,
    PAEvalExpectation,
    adapt_case_to_replay,
    normalize_for_exact_match,
    run_exact_assertion,
    run_no_draft_assertion,
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

    assert normalize_for_exact_match(response) == "Required sentence. Nothing else."
    assert run_exact_assertion(response, present)["passed"] is True
    assert run_exact_assertion(response, absent)["passed"] is True
    assert run_exact_assertion(response.lower(), present)["passed"] is False


def test_exact_present_folds_curly_apostrophe_in_model_output():
    # Nightly false-RED source: model emits U+2019 in a byte-exact mandated
    # sentence while the corpus authors U+0027.
    response = "I can’t action that without approval."
    present = PAEvalExpectation("present", "exact_present", "I can't action that")

    assert run_exact_assertion(response, present)["passed"] is True


def test_exact_present_folds_typographic_text_in_expectation():
    # Symmetric: authored expectation may carry the typographic glyph.
    response = 'He said "I can\'t" - firmly.'
    present = PAEvalExpectation(
        "present",
        "exact_present",
        "He said “I can’t” — firmly.",
    )

    assert run_exact_assertion(response, present)["passed"] is True


def test_exact_absent_still_detects_forbidden_phrase_across_glyphs():
    # Curly-quoted forbidden phrase vs straight output, and vice versa:
    # folding must not let a banned phrase slip past on glyph choice.
    curly_expectation = PAEvalExpectation(
        "absent", "exact_absent", "we’ll guarantee"
    )
    straight_expectation = PAEvalExpectation(
        "absent", "exact_absent", "we'll guarantee"
    )

    assert run_exact_assertion("Sure, we'll guarantee it.", curly_expectation)[
        "passed"
    ] is False
    assert run_exact_assertion(
        "Sure, we’ll guarantee it.", straight_expectation
    )["passed"] is False
    assert run_exact_assertion("No promises here.", curly_expectation)[
        "passed"
    ] is True


def test_exact_assertions_fold_dashes_and_nbsp():
    assert normalize_for_exact_match("a–b") == "a-b"
    assert normalize_for_exact_match("a—b") == "a-b"
    assert normalize_for_exact_match("a b") == "a b"

    response = "Delivery window – two weeks."
    present = PAEvalExpectation(
        "present", "exact_present", "Delivery window - two weeks."
    )
    assert run_exact_assertion(response, present)["passed"] is True


def test_exact_match_normalization_preserves_case_and_wording():
    assert normalize_for_exact_match("Don’t Shout") == "Don't Shout"
    assert normalize_for_exact_match("don’t shout") == "don't shout"


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


# ── no_approved_block: the deterministic NO-DRAFT check ────────────────────
#
# A material-missing gate fails by PRODUCING a draft, so its guard must not be
# judge-kind: an unrun judge scores nothing while the case still reports a
# clean deterministic summary. Naming individual sentences with exact_absent
# only guards the sentences somebody listed.


def _no_draft_expectation():
    return PAEvalExpectation.from_mapping(
        {
            "label": "returns no completed draft",
            "kind": "no_approved_block",
            "critical": True,
        }
    )


def test_no_approved_block_passes_on_an_intake_question():
    result = run_no_draft_assertion(
        "Before I draft, could you confirm the premium and the coverage term?",
        _no_draft_expectation(),
        ["Total annual premiums do not exceed 50% of the client's surplus."],
    )
    assert result["passed"] is True
    assert result["status"] == "passed"


def test_no_approved_block_catches_a_draft_by_its_approved_text():
    approved = "Total annual premiums do not exceed 50% of the client's surplus."
    result = run_no_draft_assertion(
        f"Here is the BOR.\n\n{approved}\n\nRegards.",
        _no_draft_expectation(),
        [approved, "Some other approved sentence."],
    )
    assert result["passed"] is False
    assert result["approved_blocks_present"] == [approved]


def test_no_approved_block_folds_typography_like_every_exact_check():
    result = run_no_draft_assertion(
        "Premiums do not exceed 50% of the client’s surplus.",
        _no_draft_expectation(),
        ["Premiums do not exceed 50% of the client's surplus."],
    )
    assert result["passed"] is False


def test_no_approved_block_without_needles_is_not_applicable():
    """A detector with nothing to detect must never report a pass."""
    result = run_no_draft_assertion("anything at all", _no_draft_expectation(), [])
    assert result["status"] == "not_applicable"
    assert result["passed"] is None


def test_a_not_applicable_draft_check_is_not_counted_as_evidence():
    """...and the summary must not count it either."""
    from gateway.pa_eval import DETERMINISTIC_KINDS

    assert "no_approved_block" in DETERMINISTIC_KINDS
    assert "exact_present" in DETERMINISTIC_KINDS


def test_no_approved_block_needs_no_text_field():
    """Its needles come from the artifacts, so authoring text would be wrong."""
    expectation = _no_draft_expectation()
    assert expectation.text is None


# ── assembly defects: WHY a turn produced no draft ─────────────────────────
#
# A refusal-exhaustion and a content regression are the same assertion failure
# without this field. The eval layer read three exhausted marker refusals as
# two unrelated content regressions for exactly that reason.


def test_replay_report_carries_assembly_defects_per_turn(tmp_path):
    from agent.pa_output_assembly import (
        PAOutputAssemblyRetry,
        drain_assembly_defects,
        record_assembly_defect,
    )

    drain_assembly_defects()
    bundle = adapt_case_to_replay(_corpus(tmp_path).cases[0])

    class Runner:
        async def replay(self, plan):
            text = "Required sentence."
            if plan.attempt_id.endswith("turn-1"):
                # The turn the guard withheld: recorded by the gateway on
                # every attempt, the exhausting one included.
                for attempt in (1, 2, 3):
                    record_assembly_defect(
                        PAOutputAssemblyRetry(
                            ("[[PA_BLOCK:GENERAL_DISCLOSURES]]",),
                            "required deterministic compliance markers are missing",
                        ).to_defect(attempt=attempt, exhausted=attempt == 3)
                    )
                text = "Sorry, I encountered an error (PAOutputAssemblyRetry)."
            elif plan.attempt_id.endswith("turn-2"):
                record_assembly_defect(
                    {
                        "mode": "duplicate",
                        "marker": "[[PA_BLOCK:GENERAL_DISCLOSURES]]",
                        "id": "general_disclosures",
                        "occurrences": 2,
                        "removed": 1,
                        "outcome": "healed",
                        "attempt": 1,
                    }
                )
            return ReplayResult(
                run_id=plan.run_id,
                attempt_id=plan.attempt_id,
                platform=plan.platform,
                processed=1,
                outbound=[{"kwargs": {"content": text}, "args": [], "kind": "send"}],
                blocked_commands=[],
                delivery_mode="capture",
            )

    report = asyncio.run(run_replay_bundle(Runner(), bundle))

    withheld = report["turns"][0]["assembly_defects"]
    assert len(withheld) == 3
    assert {item["mode"] for item in withheld} == {"missing"}
    assert withheld[0]["markers"] == ["[[PA_BLOCK:GENERAL_DISCLOSURES]]"]
    assert [item["attempt"] for item in withheld] == [1, 2, 3]
    assert withheld[-1]["exhausted"] is True

    healed = report["turns"][1]["assembly_defects"]
    assert len(healed) == 1
    assert healed[0]["mode"] == "duplicate"
    assert healed[0]["outcome"] == "healed"

    # Attribution is per turn, never smeared across the case.
    assert report["assembly_defect_count"] == 4
    assert report["assembly_withheld_count"] == 3
    assert report["assembly_healed_count"] == 1


def test_defects_from_an_earlier_turn_are_not_attributed_to_a_later_one(tmp_path):
    from agent.pa_output_assembly import drain_assembly_defects, record_assembly_defect

    drain_assembly_defects()
    # Residue from a previous run, sitting in the trail before the turn starts.
    record_assembly_defect({"mode": "missing", "outcome": "withheld", "attempt": 1})
    bundle = adapt_case_to_replay(_corpus(tmp_path).cases[0])

    class Runner:
        async def replay(self, plan):
            return ReplayResult(
                run_id=plan.run_id,
                attempt_id=plan.attempt_id,
                platform=plan.platform,
                processed=1,
                outbound=[
                    {"kwargs": {"content": "Required sentence."}, "args": [], "kind": "send"}
                ],
                blocked_commands=[],
                delivery_mode="capture",
            )

    report = asyncio.run(run_replay_bundle(Runner(), bundle))
    assert report["assembly_defect_count"] == 0
