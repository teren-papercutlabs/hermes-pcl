from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.pa_output_assembly import (
    PAOutputAssemblyError,
    PAOutputAssemblyRetry,
    assemble_pa_response,
    load_compliance_blocks,
    render_output_assembly_skill,
)


def _context(*artifacts: str):
    return SimpleNamespace(
        job_brief=SimpleNamespace(
            response_policy={
                "output_assembly": {
                    "enabled": True,
                    "max_attempts": 2,
                    "artifacts": list(artifacts),
                }
            }
        )
    )


def _artifact(path: Path, *, status: str = "approved") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "# pa-source:",
                "# approved_by: [amelia]",
                "# approved_date: ['2026-08-01']",
                "# ruling_ref: [R13]",
                f"# status: {status}",
                "# sequence: 1",
                "# compose: false",
                "# ---",
                "schema_version: 1",
                "blocks:",
                "  - id: exact_block",
                "    marker: '[[PA_BLOCK:EXACT]]'",
                "    required_tags: [DRAFT]",
                "    text: Exact approved sentence.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_unverified_artifact_refuses_deterministic_assembly(tmp_path):
    _artifact(tmp_path / "unverified.yaml", status="unverified")
    with pytest.raises(PAOutputAssemblyError, match="status=approved"):
        load_compliance_blocks(_context("unverified.yaml"), knowledge_root=tmp_path)


def test_task_adjacent_skill_contains_markers_not_protected_text(tmp_path):
    _artifact(tmp_path / "approved.yaml")
    rendered = render_output_assembly_skill(
        _context("approved.yaml"), knowledge_root=tmp_path
    )
    assert rendered.startswith("# Output Compliance Skill")
    assert "[[PA_BLOCK:EXACT]]" in rendered
    assert "Exact approved sentence." not in rendered


@pytest.mark.parametrize(
    "model_draw",
    [
        "[[PA_SCOPE:DRAFT]]\nBefore. [[PA_BLOCK:EXACT]] After.",
        "Before.\n[[PA_SCOPE:DRAFT]]\n[[PA_BLOCK:EXACT]]\nAfter.",
        "[[PA_BLOCK:EXACT]]\nBefore. [[PA_SCOPE:DRAFT]] After.",
        "Before [[PA_SCOPE:DRAFT]] [[PA_BLOCK:EXACT]] After",
    ],
)
def test_four_model_draws_cannot_alter_approved_block(tmp_path, model_draw):
    _artifact(tmp_path / "approved.yaml")
    assembled, evidence = assemble_pa_response(
        model_draw, _context("approved.yaml"), knowledge_root=tmp_path
    )
    assert assembled.count("Exact approved sentence.") == 1
    assert "[[PA_" not in assembled
    assert evidence["inserted"][0]["artifact"] == "approved.yaml"
    assert evidence["inserted"][0]["approved_by"] == ["amelia"]
    assert evidence["inserted"][0]["ruling_ref"] == ["R13"]
    assert evidence["inserted"][0]["status"] == "approved"


def test_missing_or_mutated_marker_fails_closed(tmp_path):
    _artifact(tmp_path / "approved.yaml")
    with pytest.raises(PAOutputAssemblyRetry, match="missing or duplicated"):
        assemble_pa_response(
            "[[PA_SCOPE:DRAFT]] [[PA_BLOCK:ALTERED]]",
            _context("approved.yaml"),
            knowledge_root=tmp_path,
        )


def test_no_draft_scope_removes_untriggered_marker(tmp_path):
    _artifact(tmp_path / "approved.yaml")
    assembled, evidence = assemble_pa_response(
        "[[PA_SCOPE:NO_DRAFT]] Need one missing fact. [[PA_BLOCK:EXACT]]",
        _context("approved.yaml"),
        knowledge_root=tmp_path,
    )
    assert assembled == "Need one missing fact."
    assert evidence["inserted"] == []


def test_mtu_sustainability_inversion_is_removed(tmp_path):
    source = (
        Path(__file__).resolve().parents[2]
        / "deploy/finexis/mtu/compliance/010-sustainability-enforcement.yaml"
    )
    target = tmp_path / "compliance/010-sustainability-enforcement.yaml"
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())
    context = _context("compliance/010-sustainability-enforcement.yaml")
    inverted = (
        "[[PA_SCOPE:DRAFT,SUSTAINABILITY_NO]]\n"
        "[[PA_BLOCK:SUSTAINABILITY_NO]]\n"
        "Client has catered enough savings and emergency funds for unforeseen "
        "circumstances, and is comfortable to proceed even though the premium "
        "amount is more than 50% of their surplus."
    )
    assembled, _ = assemble_pa_response(inverted, context, knowledge_root=tmp_path)
    assert assembled == (
        "Total annual premiums do not exceed 50% of the client's surplus."
    )


def test_conflicting_inversion_tags_fail_closed(tmp_path):
    source = (
        Path(__file__).resolve().parents[2]
        / "deploy/finexis/mtu/compliance/010-sustainability-enforcement.yaml"
    )
    target = tmp_path / "compliance/010-sustainability-enforcement.yaml"
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())
    context = _context("compliance/010-sustainability-enforcement.yaml")
    with pytest.raises(PAOutputAssemblyRetry, match="mutually-exclusive"):
        assemble_pa_response(
            "[[PA_SCOPE:DRAFT,SUSTAINABILITY_NO,SUSTAINABILITY_YES]] "
            "[[PA_BLOCK:SUSTAINABILITY_NO]] [[PA_BLOCK:SUSTAINABILITY_YES]]",
            context,
            knowledge_root=tmp_path,
        )


def test_explicit_source_fact_overrides_inverted_model_scope(tmp_path):
    source = (
        Path(__file__).resolve().parents[2]
        / "deploy/finexis/mtu/compliance/010-sustainability-enforcement.yaml"
    )
    target = tmp_path / "compliance/010-sustainability-enforcement.yaml"
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())
    context = _context("compliance/010-sustainability-enforcement.yaml")
    with pytest.raises(PAOutputAssemblyRetry, match="missing or duplicated"):
        assemble_pa_response(
            "[[PA_SCOPE:DRAFT,SUSTAINABILITY_YES]] "
            "[[PA_BLOCK:SUSTAINABILITY_YES]]",
            context,
            knowledge_root=tmp_path,
            source_text="Total annual premiums do not exceed 50% of the client's surplus.",
        )

    assembled, evidence = assemble_pa_response(
        "[[PA_SCOPE:DRAFT,SUSTAINABILITY_YES]] "
        "[[PA_BLOCK:SUSTAINABILITY_NO]]",
        context,
        knowledge_root=tmp_path,
        source_text="Total annual premiums do not exceed 50% of the client's surplus.",
    )
    assert assembled == "Total annual premiums do not exceed 50% of the client's surplus."
    assert evidence["deterministic_scope"] == "SUSTAINABILITY_NO"


def test_latest_explicit_source_fact_wins(tmp_path):
    source = (
        Path(__file__).resolve().parents[2]
        / "deploy/finexis/mtu/compliance/010-sustainability-enforcement.yaml"
    )
    target = tmp_path / "compliance/010-sustainability-enforcement.yaml"
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())
    assembled, evidence = assemble_pa_response(
        "[[PA_SCOPE:DRAFT,SUSTAINABILITY_NO]] [[PA_BLOCK:SUSTAINABILITY_YES]]",
        _context("compliance/010-sustainability-enforcement.yaml"),
        knowledge_root=tmp_path,
        source_text=(
            "Earlier: total annual premiums do not exceed 50% of surplus.\n"
            "Correction: total annual premiums exceed 50% of surplus."
        ),
    )
    assert assembled.startswith("Client has catered enough savings")
    assert evidence["deterministic_scope"] == "SUSTAINABILITY_YES"


# ── Repeatable blocks: the same approved text, once per component ──────────


def _repeatable_artifact(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "# pa-source:",
                "# approved_by: [amelia]",
                "# approved_date: ['2026-07-23']",
                "# ruling_ref: [R13]",
                "# status: approved",
                "# sequence: 190",
                "# compose: false",
                "# ---",
                "schema_version: 1",
                "blocks:",
                "  - id: per_component",
                "    marker: '[[PA_BLOCK:ROP]]'",
                "    required_tags: [DRAFT, ROP]",
                "    repeatable: true",
                "    text: Approved replacement disadvantages.",
                "  - id: once_only",
                "    marker: '[[PA_BLOCK:ONCE]]'",
                "    required_tags: [DRAFT]",
                "    text: Approved general disclosure.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_repeatable_block_lands_in_every_component(tmp_path):
    """MTU-021: two supported replacement components, one approved paragraph.

    Before this, a second marker read as 'duplicated' and burned the draft.
    """
    _repeatable_artifact(tmp_path / "rop.yaml")
    assembled, evidence = assemble_pa_response(
        "[[PA_SCOPE:DRAFT,ROP]]\n"
        "Term component. [[PA_BLOCK:ROP]]\n"
        "ILP component. [[PA_BLOCK:ROP]]\n"
        "[[PA_BLOCK:ONCE]]",
        _context("rop.yaml"),
        knowledge_root=tmp_path,
    )
    assert assembled.count("Approved replacement disadvantages.") == 2
    assert assembled.count("Approved general disclosure.") == 1
    assert "[[PA_" not in assembled
    rop = [item for item in evidence["inserted"] if item["id"] == "per_component"][0]
    assert rop["occurrences"] == 2


def test_repeatable_block_still_fails_closed_when_absent(tmp_path):
    _repeatable_artifact(tmp_path / "rop.yaml")
    with pytest.raises(PAOutputAssemblyRetry, match="missing or duplicated"):
        assemble_pa_response(
            "[[PA_SCOPE:DRAFT,ROP]] no rop marker [[PA_BLOCK:ONCE]]",
            _context("rop.yaml"),
            knowledge_root=tmp_path,
        )


def test_non_repeatable_block_still_refuses_a_duplicate(tmp_path):
    """MTU-022: a duplicated general disclosure remains a defect."""
    _repeatable_artifact(tmp_path / "rop.yaml")
    with pytest.raises(PAOutputAssemblyRetry, match="missing or duplicated"):
        assemble_pa_response(
            "[[PA_SCOPE:DRAFT,ROP]] [[PA_BLOCK:ROP]] [[PA_BLOCK:ONCE]] [[PA_BLOCK:ONCE]]",
            _context("rop.yaml"),
            knowledge_root=tmp_path,
        )


def test_skill_tells_the_model_to_repeat_the_marker_per_component(tmp_path):
    _repeatable_artifact(tmp_path / "rop.yaml")
    rendered = render_output_assembly_skill(_context("rop.yaml"), knowledge_root=tmp_path)
    assert "EACH component" in rendered
    # The tag vocabulary is derived from the artifacts, never hard-coded.
    assert "Applicable tags: ROP." in rendered
    assert "Approved replacement disadvantages." not in rendered


# ── Slotted blocks: approved wording, case-record values ───────────────────


def _slotted_artifact(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "# pa-source:",
                "# approved_by: [amelia]",
                "# approved_date: ['2026-07-31']",
                "# ruling_ref: [R07]",
                "# status: approved",
                "# sequence: 197",
                "# compose: false",
                "# ---",
                "schema_version: 1",
                "blocks:",
                "  - id: cka_declaration",
                "    marker: '[[PA_BLOCK:CKA]]'",
                "    required_tags: [DRAFT, ILP]",
                "    slots:",
                "      - name: cka_result",
                "        field: cka_status",
                "        matches:",
                "          - {pattern: '(?i)(did not pass|not pass|fail)', value: 'did not pass'}",
                "          - {pattern: '(?i)pass', value: 'passed'}",
                "      - name: risk_profile",
                "        field: risk_profile",
                "        matches:",
                "          - {pattern: '(?i)aggressive', value: 'Aggressive'}",
                "    text: Client {cka_result} CKA and has a risk profile of {risk_profile}.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_slots_are_filled_from_the_case_record_not_the_model(tmp_path):
    _slotted_artifact(tmp_path / "cka.yaml")
    assembled, evidence = assemble_pa_response(
        "[[PA_SCOPE:DRAFT,ILP]] Draft. [[PA_BLOCK:CKA]]",
        _context("cka.yaml"),
        knowledge_root=tmp_path,
        field_values={"cka_status": "CKA passed", "risk_profile": "Aggressive"},
    )
    assert "Client passed CKA and has a risk profile of Aggressive." in assembled
    assert evidence["inserted"][0]["id"] == "cka_declaration"


def test_slot_answers_map_to_canonical_tokens(tmp_path):
    _slotted_artifact(tmp_path / "cka.yaml")
    assembled, _ = assemble_pa_response(
        "[[PA_SCOPE:DRAFT,ILP]] Draft. [[PA_BLOCK:CKA]]",
        _context("cka.yaml"),
        knowledge_root=tmp_path,
        field_values={"cka_status": "did not pass", "risk_profile": "aggressive"},
    )
    assert "Client did not pass CKA and has a risk profile of Aggressive." in assembled


def test_unresolvable_slot_drops_the_block_instead_of_shipping_a_hole(tmp_path):
    _slotted_artifact(tmp_path / "cka.yaml")
    assembled, evidence = assemble_pa_response(
        "[[PA_SCOPE:DRAFT,ILP]] Draft. [[PA_BLOCK:CKA]]",
        _context("cka.yaml"),
        knowledge_root=tmp_path,
        field_values={"cka_status": "passed"},
    )
    assert "{risk_profile}" not in assembled
    assert "CKA" not in assembled
    assert evidence["unresolved_slots"][0]["id"] == "cka_declaration"


def test_slot_placeholder_without_a_declaration_refuses_to_load(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        "\n".join(
            [
                "# pa-source:",
                "# approved_by: [amelia]",
                "# approved_date: ['2026-07-31']",
                "# ruling_ref: [R07]",
                "# status: approved",
                "# sequence: 1",
                "# compose: false",
                "# ---",
                "schema_version: 1",
                "blocks:",
                "  - id: broken",
                "    marker: '[[PA_BLOCK:BROKEN]]'",
                "    required_tags: [DRAFT]",
                "    text: Client {cka_result} CKA.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(PAOutputAssemblyError, match="declares no slots"):
        load_compliance_blocks(_context("bad.yaml"), knowledge_root=tmp_path)


def test_additive_runtime_scope_tag_does_not_wait_for_the_model_to_declare_it(tmp_path):
    """R07: the record already knows the case is an ILP."""
    _slotted_artifact(tmp_path / "cka.yaml")
    selection = SimpleNamespace(
        additional_scope_tags=("ILP",),
        to_dict=lambda: {"additional_scope_tags": ["ILP"]},
    )
    with pytest.raises(PAOutputAssemblyRetry, match="missing or duplicated"):
        assemble_pa_response(
            "[[PA_SCOPE:DRAFT]] Draft with no CKA marker.",
            _context("cka.yaml"),
            knowledge_root=tmp_path,
            block_selection=selection,
            field_values={"cka_status": "passed", "risk_profile": "Aggressive"},
        )
    assembled, _ = assemble_pa_response(
        "[[PA_SCOPE:DRAFT]] Draft. [[PA_BLOCK:CKA]]",
        _context("cka.yaml"),
        knowledge_root=tmp_path,
        block_selection=selection,
        field_values={"cka_status": "passed", "risk_profile": "Aggressive"},
    )
    assert "Client passed CKA and has a risk profile of Aggressive." in assembled
