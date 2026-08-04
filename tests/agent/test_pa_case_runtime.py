"""Unit tests for the PA case-record RUNTIME layer (program step S7, phase 2).

Phase 1 proved the substrate (shape, storage, empty-required query).  These
tests prove the runtime behaviours the deployment actually depends on:

  (a) a turn with no open case MINTS one, and the mint records the message it
      fired on — no slash command anywhere in the path;
  (b) facts the turn supplied are recorded against THAT message's id, and a
      value that did not change does not churn the record version;
  (c) a config-declared derivation fills its target from the recorded answer,
      inheriting that answer's provenance — so the derived field is filled and
      therefore never re-asked;
  (d) the rendered turn block names the filled facts as never-ask and the empty
      required set as ask-exactly-these, and carries the version stamp;
  (e) a boundary classified NEW mints a fresh case and the earlier case's facts
      do not appear in the new turn's state;
  (f) a reset session (the runtime's own /new) is a boundary too;
  (g) disclaimer selection is deterministic from the recorded product category
      and drives which approved block the anchor resolves to at assembly.

Client vocabulary appears only in per-test config, never in the module under
test — the ids below are deliberately opaque.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import pa_case_runtime as pcrt
from agent.pa_case_record import CaseRecordStore
from agent.pa_output_assembly import assemble_pa_response
from hermes_state import SessionDB


FIELD_SETS = """
version: 1
field_sets:
  alpha:
    case_type: alpha
    fields:
      - id: f_source
        required: true
        ask_hint: "source?"
      - id: f_other
        required: true
        ask_hint: "other?"
      - id: f_derived
        required: true
        askable: false
        derived_from: [f_source]
      - id: f_category
        required: false
        askable: false
        derived_from: [f_other]
  beta:
    case_type: beta
    fields:
      - id: f_source
        required: true
        ask_hint: "source?"
      - id: f_other
        required: true
        ask_hint: "other?"
      - id: f_beta_only
        required: false
        ask_hint: "beta only?"
        holds: "what beta cases carry"
      - id: f_category
        required: false
        askable: false
        derived_from: [f_other]
derivations:
  f_derived:
    from: f_source
    matches:
      - pattern: '(?i)^none'
        value: "no"
      - pattern: '(?i)swap'
        value: "yes"
    default: null
"""

SELECTION = """
version: 1
exclusive_scope_tags: [SCOPE_A, SCOPE_B]
selection_field: f_category
default_category: alpha
categories:
  alpha:
    scope_tag: SCOPE_A
    substitutions: {}
    forbid: [variant_block]
  beta:
    scope_tag: SCOPE_B
    substitutions:
      anchor_block: variant_block
    forbid: []
"""

SELECTION_UNMAPPED = """
version: 1
exclusive_scope_tags: [SCOPE_A, SCOPE_B]
selection_field: f_category
categories:
  gamma:
    scope_tag: SCOPE_A
"""

ARTIFACT = """# pa-source:
# approved_by: [amelia]
# approved_date: ['2026-08-01']
# ruling_ref: [R13]
# status: approved
# sequence: 1
# compose: false
# ---
schema_version: 1
blocks:
  - id: anchor_block
    marker: '[[PA_BLOCK:ANCHOR]]'
    required_tags: [DRAFT]
    text: Anchor approved text.
  - id: variant_block
    marker: '[[PA_BLOCK:VARIANT]]'
    required_tags: [DRAFT, VARIANT]
    selection_only: true
    text: Variant approved text.
"""


@pytest.fixture
def knowledge_root(tmp_path: Path) -> Path:
    (tmp_path / "fields.yaml").write_text(FIELD_SETS, encoding="utf-8")
    (tmp_path / "selection.yaml").write_text(SELECTION, encoding="utf-8")
    (tmp_path / "blocks.yaml").write_text(ARTIFACT, encoding="utf-8")
    return tmp_path


@pytest.fixture
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield session_db
    finally:
        session_db.close()


def _context():
    return SimpleNamespace(
        job_brief=SimpleNamespace(
            response_policy={
                "case_record": {
                    "enabled": True,
                    "field_sets": "fields.yaml",
                    "default_case_type": "alpha",
                    "category_field": "f_category",
                    "category_source_field": "f_other",
                    "disclaimer_selection": "selection.yaml",
                    "extraction": {"enabled": True},
                },
                "output_assembly": {
                    "enabled": True,
                    "max_attempts": 2,
                    "artifacts": ["blocks.yaml"],
                },
            }
        )
    )


def _stub_extraction(monkeypatch, *payloads):
    """Replace the extraction LLM call with a scripted queue of results."""
    queue = list(payloads)

    async def _fake(*, config, prompt):
        return queue.pop(0) if queue else {}

    monkeypatch.setattr(pcrt, "_run_extraction", _fake)


async def _turn(db, knowledge_root, *, message, message_id, session_id="s1"):
    return await pcrt.update_case_for_turn(
        session_db=db,
        pa_context=_context(),
        agent_id="agent-x",
        chat_id="chat-x",
        session_id=session_id,
        message=message,
        message_id=message_id,
        knowledge_root=knowledge_root,
    )


# ── mint + record + derive ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_turn_mints_and_records_with_source_message(
    db, knowledge_root, monkeypatch
):
    _stub_extraction(
        monkeypatch,
        {"boundary": "new", "case_type": "alpha", "fields": {"f_source": "swap it"}},
    )
    state = await _turn(db, knowledge_root, message="swap it", message_id="m1")

    assert state.minted is True
    assert state.record.minted_from_message_id == "m1"
    recorded = state.record.get("f_source")
    assert recorded.value == "swap it"
    assert recorded.source_message_id == "m1"
    # The derivation fired off the recorded answer and inherited its message.
    derived = state.record.get("f_derived")
    assert derived.value == "yes"
    assert derived.origin == "derived"
    assert derived.derived_from_field_id == "f_source"
    assert derived.source_message_id == "m1"
    # A derived field is FILLED, so intake never asks for it again.
    assert [spec.field_id for spec in state.empty_fields] == ["f_other"]


@pytest.mark.asyncio
async def test_unchanged_value_does_not_bump_the_record_version(
    db, knowledge_root, monkeypatch
):
    _stub_extraction(
        monkeypatch,
        {"boundary": "new", "case_type": "alpha", "fields": {"f_source": "swap it"}},
        {"boundary": "continue", "case_type": "alpha", "fields": {"f_source": "swap it"}},
    )
    first = await _turn(db, knowledge_root, message="swap it", message_id="m1")
    second = await _turn(db, knowledge_root, message="swap it again", message_id="m2")

    assert second.record.case_id == first.record.case_id
    assert second.record.record_version == first.record.record_version
    assert "f_source" not in second.recorded_field_ids


@pytest.mark.asyncio
async def test_correction_overwrites_and_keeps_history(db, knowledge_root, monkeypatch):
    _stub_extraction(
        monkeypatch,
        {"boundary": "new", "case_type": "alpha", "fields": {"f_source": "none yet"}},
        {"boundary": "continue", "case_type": "alpha", "fields": {"f_source": "swap it"}},
    )
    await _turn(db, knowledge_root, message="none yet", message_id="m1")
    state = await _turn(db, knowledge_root, message="actually swap it", message_id="m2")

    assert state.record.value_of("f_source") == "swap it"
    history = CaseRecordStore(db).field_history(state.record.case_id, "f_source")
    assert [item.value for item in history] == ["none yet"]


# ── boundaries ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_new_boundary_mints_and_carries_no_earlier_facts(
    db, knowledge_root, monkeypatch
):
    _stub_extraction(
        monkeypatch,
        {"boundary": "new", "case_type": "alpha", "fields": {"f_source": "swap it"}},
        {"boundary": "new", "case_type": "alpha", "fields": {}},
    )
    first = await _turn(db, knowledge_root, message="swap it", message_id="m1")
    second = await _turn(db, knowledge_root, message="new one", message_id="m2")

    assert second.record.case_id != first.record.case_id
    assert second.minted is True
    assert second.record.value_of("f_source") is None
    assert CaseRecordStore(db).get_case(first.record.case_id).status == "superseded"


@pytest.mark.asyncio
async def test_session_reset_is_a_boundary(db, knowledge_root, monkeypatch):
    _stub_extraction(
        monkeypatch,
        {"boundary": "new", "case_type": "alpha", "fields": {"f_source": "swap it"}},
        {"boundary": "continue", "case_type": "alpha", "fields": {}},
    )
    first = await _turn(db, knowledge_root, message="swap it", message_id="m1")
    second = await _turn(
        db, knowledge_root, message="hello", message_id="m2", session_id="s2"
    )
    assert second.record.case_id != first.record.case_id


@pytest.mark.asyncio
async def test_extraction_failure_never_breaks_the_turn(db, knowledge_root, monkeypatch):
    async def _boom(*, config, prompt):
        raise RuntimeError("extractor down")

    monkeypatch.setattr(pcrt, "_run_extraction", _boom)
    state = await _turn(db, knowledge_root, message="anything", message_id="m1")
    assert state is not None
    assert state.extraction_ok is False
    assert state.record.record_version == 0


# ── the rendered turn surface ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_rendered_block_names_known_facts_and_exact_missing_set(
    db, knowledge_root, monkeypatch
):
    _stub_extraction(
        monkeypatch,
        {"boundary": "new", "case_type": "alpha", "fields": {"f_source": "swap it"}},
    )
    state = await _turn(db, knowledge_root, message="swap it", message_id="m1")
    rendered = pcrt.render_case_record_prompt(state)

    assert state.record.version_stamp() in rendered
    assert "f_source: swap it" in rendered
    assert "NEVER ask" in rendered
    # The missing list speaks the ask hint, not the internal field id, so a
    # reply cannot pick up record vocabulary by copying this block.
    assert "- other?" in rendered
    assert "- f_other" not in rendered
    assert "THIS BLOCK IS INTERNAL" in rendered
    assert "f_derived" in rendered.split("STILL MISSING")[0]  # filled, not asked


@pytest.mark.asyncio
async def test_complete_record_asks_nothing(db, knowledge_root, monkeypatch):
    _stub_extraction(
        monkeypatch,
        {
            "boundary": "new",
            "case_type": "alpha",
            "fields": {"f_source": "swap it", "f_other": "other value"},
        },
    )
    state = await _turn(db, knowledge_root, message="everything", message_id="m1")
    rendered = pcrt.render_case_record_prompt(state)
    assert state.empty_fields == ()
    assert "STILL MISSING: nothing" in rendered


# ── deterministic disclaimer selection ──────────────────────────────────


@pytest.mark.asyncio
async def test_category_is_derived_from_its_source_answer(
    db, knowledge_root, monkeypatch
):
    _stub_extraction(
        monkeypatch,
        {
            "boundary": "new",
            "case_type": "beta",
            "fields": {"f_source": "swap it", "f_other": "other value"},
        },
    )
    state = await _turn(db, knowledge_root, message="a beta case", message_id="m1")
    category = state.record.get("f_category")
    assert category.value == "beta"
    assert category.origin == "derived"
    assert category.derived_from_field_id == "f_other"


@pytest.mark.asyncio
async def test_selection_substitutes_the_variant_at_the_anchor(
    db, knowledge_root, monkeypatch
):
    _stub_extraction(
        monkeypatch,
        {
            "boundary": "new",
            "case_type": "beta",
            "fields": {"f_source": "swap it", "f_other": "other value"},
        },
    )
    await _turn(db, knowledge_root, message="a beta case", message_id="m1")
    selection = pcrt.resolve_disclaimer_selection(
        session_db=db,
        pa_context=_context(),
        agent_id="agent-x",
        chat_id="chat-x",
        knowledge_root=knowledge_root,
    )
    assert selection.category == "beta"
    assert selection.substitutions == {"anchor_block": "variant_block"}

    assembled, evidence = assemble_pa_response(
        "[[PA_SCOPE:DRAFT]] head [[PA_BLOCK:ANCHOR]] tail",
        _context(),
        knowledge_root=knowledge_root,
        block_selection=selection,
        record_version_stamp="case-1@v4",
    )
    assert "Variant approved text." in assembled
    assert "Anchor approved text." not in assembled
    assert evidence["record_version_stamp"] == "case-1@v4"
    assert evidence["inserted"][0]["substituted_for"] == "anchor_block"


@pytest.mark.asyncio
async def test_selection_default_category_keeps_the_anchor(
    db, knowledge_root, monkeypatch
):
    _stub_extraction(
        monkeypatch,
        {
            "boundary": "new",
            "case_type": "alpha",
            "fields": {"f_source": "swap it", "f_other": "other value"},
        },
    )
    await _turn(db, knowledge_root, message="an alpha case", message_id="m1")
    selection = pcrt.resolve_disclaimer_selection(
        session_db=db,
        pa_context=_context(),
        agent_id="agent-x",
        chat_id="chat-x",
        knowledge_root=knowledge_root,
    )
    assembled, _ = assemble_pa_response(
        "[[PA_SCOPE:DRAFT]] head [[PA_BLOCK:ANCHOR]] tail",
        _context(),
        knowledge_root=knowledge_root,
        block_selection=selection,
    )
    assert "Anchor approved text." in assembled
    assert "Variant approved text." not in assembled


def test_selection_only_block_is_never_shown_to_the_model(knowledge_root):
    from agent.pa_output_assembly import render_output_assembly_skill

    rendered = render_output_assembly_skill(_context(), knowledge_root=knowledge_root)
    assert "[[PA_BLOCK:ANCHOR]]" in rendered
    assert "[[PA_BLOCK:VARIANT]]" not in rendered


# ── extraction parsing ──────────────────────────────────────────────────


def test_extraction_parser_tolerates_fences_and_prose():
    parsed = pcrt.parse_extraction_response(
        'here you go:\n```json\n{"boundary": "new", "case_type": "alpha", '
        '"fields": {"f_source": "x", "f_other": null}}\n```'
    )
    assert parsed["boundary"] == "new"
    assert parsed["fields"] == {"f_source": "x"}


def test_extraction_parser_falls_back_to_continue_on_garbage():
    assert pcrt.parse_extraction_response("not json at all") == {}


# ── exactly-one underwriting scope, or fail closed ──────────────────────


@pytest.mark.asyncio
async def test_draft_declares_exactly_one_scope_from_the_category(
    db, knowledge_root, monkeypatch
):
    _stub_extraction(
        monkeypatch,
        {
            "boundary": "new",
            "case_type": "beta",
            "fields": {"f_source": "swap it", "f_other": "other value"},
        },
    )
    await _turn(db, knowledge_root, message="a beta case", message_id="m1")
    selection = pcrt.resolve_disclaimer_selection(
        session_db=db,
        pa_context=_context(),
        agent_id="agent-x",
        chat_id="chat-x",
        knowledge_root=knowledge_root,
    )
    # The model declared the WRONG exclusive scope; the category overrides it.
    _, evidence = assemble_pa_response(
        "[[PA_SCOPE:DRAFT,SCOPE_A]] head [[PA_BLOCK:ANCHOR]] tail",
        _context(),
        knowledge_root=knowledge_root,
        block_selection=selection,
    )
    assert "SCOPE_B" in evidence["scope_tags"]
    assert "SCOPE_A" not in evidence["scope_tags"]


def test_unresolvable_category_fails_assembly_closed(knowledge_root, tmp_path):
    from agent.pa_output_assembly import PAOutputAssemblyError

    (knowledge_root / "selection.yaml").write_text(SELECTION_UNMAPPED, encoding="utf-8")
    selection = pcrt.DisclaimerSelection(
        category="alpha",
        exclusive_scope_tags=("SCOPE_A", "SCOPE_B"),
        matched=False,
    )
    with pytest.raises(PAOutputAssemblyError, match="exactly one"):
        assemble_pa_response(
            "[[PA_SCOPE:DRAFT]] head [[PA_BLOCK:ANCHOR]] tail",
            _context(),
            knowledge_root=knowledge_root,
            block_selection=selection,
        )


def test_no_draft_scope_never_requires_an_underwriting_tag(knowledge_root):
    selection = pcrt.DisclaimerSelection(
        category="alpha",
        exclusive_scope_tags=("SCOPE_A", "SCOPE_B"),
        matched=False,
    )
    assembled, _ = assemble_pa_response(
        "[[PA_SCOPE:NO_DRAFT]] one question please",
        _context(),
        knowledge_root=knowledge_root,
        block_selection=selection,
    )
    assert "one question please" in assembled


def test_multi_source_derivation_takes_the_first_recorded_answer():
    rules = pcrt._parse_derivations(
        {
            "target": [
                {"from": "a", "matches": [{"pattern": "\\S", "value": "from-a"}]},
                {"from": "b", "matches": [{"pattern": "\\S", "value": "from-b"}]},
            ]
        }
    )
    assert [r.source_field_id for r in rules] == ["a", "b"]
    assert rules[0].resolve("anything") == "from-a"


# ── The contract the first extraction pass could not offer ─────────────────


@pytest.mark.asyncio
async def test_opening_message_records_facts_its_default_contract_lacks(
    db, knowledge_root, monkeypatch
):
    """A case-opening message states facts the DEFAULT contract has no id for."""
    seen: list[str] = []

    async def _fake(*, config, prompt):
        seen.append(prompt)
        assert "f_beta_only" in prompt, "extraction must see every contract's ids"
        return {
            "boundary": "new",
            "case_type": "beta",
            "fields": {"f_beta_only": "beta fact", "f_source": "swap"},
        }

    monkeypatch.setattr(pcrt, "_run_extraction", _fake)
    state = await _turn(db, knowledge_root, message="a beta case", message_id="m1")

    assert len(seen) == 1, "one read of the message, not one per contract"
    assert state.record.value_of("f_beta_only") == "beta fact"
    assert "f_beta_only" in state.recorded_field_ids


@pytest.mark.asyncio
async def test_extraction_never_writes_a_field_the_chosen_contract_lacks(
    db, knowledge_root, monkeypatch
):
    """The union is a READING vocabulary; the contract still owns the record."""
    _stub_extraction(
        monkeypatch,
        {
            "boundary": "new",
            "case_type": "alpha",
            "fields": {"f_source": "swap", "f_beta_only": "not alpha's field"},
        },
    )
    state = await _turn(db, knowledge_root, message="an alpha case", message_id="m1")

    assert state.record.value_of("f_source") == "swap"
    assert state.record.value_of("f_beta_only") is None
    assert "f_beta_only" not in state.recorded_field_ids


def test_extraction_prompt_describes_what_a_field_holds_not_how_to_ask(
    knowledge_root,
):
    from agent.pa_case_record import load_field_sets, select_field_set

    field_sets = load_field_sets(knowledge_root / "fields.yaml")
    prompt = pcrt.build_extraction_prompt(
        message="anything",
        message_id="m1",
        field_set=select_field_set(field_sets, case_type="beta"),
        record=None,
        case_types=["alpha", "beta"],
        has_open_case=False,
    )
    assert "f_beta_only — what beta cases carry" in prompt
    assert "beta only?" not in prompt
    # A field with no description still falls back to its question.
    assert "f_source — source?" in prompt
