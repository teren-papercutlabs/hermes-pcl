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


def test_approved_template_owns_sentence_while_model_supplies_slots(tmp_path):
    artifact = tmp_path / "template.yaml"
    artifact.write_text(
        "\n".join(
            [
                "# pa-source:",
                "# approved_by: [amelia]",
                "# approved_date: [null]",
                "# ruling_ref: [R13]",
                "# status: approved",
                "# sequence: 1",
                "# compose: false",
                "# ---",
                "schema_version: 1",
                "blocks:",
                "  - id: template_block",
                "    marker: '[[PA_BLOCK:TEMPLATE]]'",
                "    required_tags: [DRAFT, PROTECTION_ALTERNATIVES]",
                "    slots: [PRODUCT, REASON]",
                "    text_template: 'Approved frame: {{PRODUCT}} because {{REASON}}.'",
                "",
            ]
        ),
        encoding="utf-8",
    )
    model_draw = (
        "[[PA_SCOPE:DRAFT,PROTECTION_ALTERNATIVES]]\n"
        "Model paraphrase that must not survive.\n"
        "[[PA_BLOCK:TEMPLATE]]"
        "[[PA_SLOT:PRODUCT]]Harbor EliteTerm[[/PA_SLOT:PRODUCT]]"
        "[[PA_SLOT:REASON]]the client wants higher protection[[/PA_SLOT:REASON]]"
    )
    assembled, evidence = assemble_pa_response(
        model_draw, _context("template.yaml"), knowledge_root=tmp_path
    )
    assert assembled.endswith(
        "Approved frame: Harbor EliteTerm because the client wants higher protection."
    )
    assert evidence["inserted"][0]["id"] == "template_block"


def test_approved_template_missing_slot_fails_closed(tmp_path):
    source = (
        Path(__file__).resolve().parents[2]
        / "deploy/finexis/mtu/compliance/196-protection-alternatives.yaml"
    )
    target = tmp_path / "compliance/196-protection-alternatives.yaml"
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())
    with pytest.raises(PAOutputAssemblyRetry, match="slot is missing"):
        assemble_pa_response(
            "[[PA_SCOPE:DRAFT,PROTECTION_ALTERNATIVES]] "
            "[[PA_BLOCK:PROTECTION_STANDARD_ALTERNATIVES]]",
            _context("compliance/196-protection-alternatives.yaml"),
            knowledge_root=tmp_path,
        )
