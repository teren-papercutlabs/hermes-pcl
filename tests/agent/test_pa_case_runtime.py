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


# ── an unclassified case must not read as the default category ──────────
#
# The MTU defect these cover: product_category had no derivation of its own, so
# it was mirrored from the case-type classification; an unclassified case fell
# through to the deployment default (protection_life), the selection config's
# default_category agreed, and the draft was scoped UNDERWRITTEN — inserting the
# pre-existing-medical-conditions sentence on a GIO product with nothing behind
# it.  Both halves are tested: the default never becomes a recorded finding, and
# a client-declared derivation for the category field beats the mirror.


SELECTION_NO_DEFAULT = """
version: 1
exclusive_scope_tags: [SCOPE_A, SCOPE_B]
selection_field: f_category
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


FIELD_SETS_CATEGORY_DERIVED = FIELD_SETS + """
  f_category:
    from: f_other
    matches:
      - pattern: '(?i)beta-ish'
        value: "beta"
    default: null
"""


@pytest.mark.asyncio
async def test_unclassified_case_never_stores_the_default_case_type(
    db, knowledge_root, monkeypatch
):
    _stub_extraction(
        monkeypatch,
        {"boundary": "new", "fields": {"f_source": "swap it", "f_other": "other value"}},
    )
    state = await _turn(db, knowledge_root, message="who knows", message_id="m1")
    # The field set still had to be chosen, so the turn works against `alpha`...
    assert state.field_set.field_set_id == "alpha"
    # ...but nothing pretends the case WAS classified.
    assert state.record.case_type is None
    assert not state.record.is_filled("f_category")


@pytest.mark.asyncio
async def test_unclassified_case_fails_a_completed_draft_closed(
    db, knowledge_root, monkeypatch
):
    from agent.pa_output_assembly import PAOutputAssemblyError

    (knowledge_root / "selection.yaml").write_text(
        SELECTION_NO_DEFAULT, encoding="utf-8"
    )
    _stub_extraction(
        monkeypatch,
        {"boundary": "new", "fields": {"f_source": "swap it", "f_other": "other value"}},
    )
    await _turn(db, knowledge_root, message="who knows", message_id="m1")
    selection = pcrt.resolve_disclaimer_selection(
        session_db=db,
        pa_context=_context(),
        agent_id="agent-x",
        chat_id="chat-x",
        knowledge_root=knowledge_root,
    )
    assert selection.category is None
    assert selection.scope_tag is None
    with pytest.raises(PAOutputAssemblyError, match="exactly one"):
        assemble_pa_response(
            "[[PA_SCOPE:DRAFT]] head [[PA_BLOCK:ANCHOR]] tail",
            _context(),
            knowledge_root=knowledge_root,
            block_selection=selection,
        )
    # The same unresolved case may still ASK — failing closed gates the draft,
    # not the conversation.
    assembled, _ = assemble_pa_response(
        "[[PA_SCOPE:NO_DRAFT]] one question please",
        _context(),
        knowledge_root=knowledge_root,
        block_selection=selection,
    )
    assert "one question please" in assembled


@pytest.mark.asyncio
async def test_declared_category_derivation_beats_the_case_type_mirror(
    db, knowledge_root, monkeypatch
):
    (knowledge_root / "fields.yaml").write_text(
        FIELD_SETS_CATEGORY_DERIVED, encoding="utf-8"
    )
    # The model classifies this as `alpha`; the recorded ANSWER says beta.
    _stub_extraction(
        monkeypatch,
        {
            "boundary": "new",
            "case_type": "alpha",
            "fields": {"f_source": "swap it", "f_other": "beta-ish thing"},
        },
    )
    state = await _turn(db, knowledge_root, message="a case", message_id="m1")
    assert state.record.value_of("f_category") == "beta"

# ── Value contracts, end to end through the runtime ─────────────────────
#
# The unit-level contract behaviour lives in tests/test_pa_case_record.py.
# What these prove is the WIRING: that the runtime hands the deployment's
# contracts to the store so every turn's writes are checked, that an
# unmappable answer becomes a CLARIFICATION question rather than the original
# question repeated, and that the deployment's own config actually parses.


CONTRACT_FIELD_SETS = """
version: 1
field_sets:
  alpha:
    case_type: alpha
    fields:
      - id: f_source
        required: true
        ask_hint: "source?"
      - id: f_grade
        required: true
        ask_hint: "which grade?"
        value_contract:
          kind: enum
          values: [Low, High]
          match:
            - {pattern: '(?i)\\blow\\b', value: Low}
            - {pattern: '(?i)\\bhigh\\b', value: High}
      - id: f_flag
        required: false
        ask_hint: "flagged?"
        value_contract:
          kind: boolean
      - id: f_listed
        required: false
        ask_hint: "which item?"
        value_contract:
          kind: table
          table: items
value_tables:
  items:
    entries:
      - value: "Item One"
        aliases: ["One"]
"""


@pytest.fixture
def contract_knowledge_root(tmp_path: Path) -> Path:
    (tmp_path / "fields.yaml").write_text(CONTRACT_FIELD_SETS, encoding="utf-8")
    (tmp_path / "selection.yaml").write_text(SELECTION, encoding="utf-8")
    (tmp_path / "blocks.yaml").write_text(ARTIFACT, encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_turn_writes_are_canonicalised_by_the_deployments_contracts(
    db, contract_knowledge_root, monkeypatch
):
    _stub_extraction(
        monkeypatch,
        {
            "boundary": "new",
            "case_type": "alpha",
            "fields": {
                "f_grade": "client is high risk",
                "f_flag": "yes",
                "f_listed": "One",
            },
        },
    )
    state = await _turn(
        db, contract_knowledge_root, message="high risk, yes, One", message_id="m1"
    )
    assert state.record.value_of("f_grade") == "High"
    assert state.record.value_of("f_flag") is True
    assert state.record.value_of("f_listed") == "Item One"
    # The advisor's own words survive for the audit trail.
    assert state.record.get("f_grade").raw_value == "client is high risk"


@pytest.mark.asyncio
async def test_restating_a_variant_does_not_churn_the_record_version(
    db, contract_knowledge_root, monkeypatch
):
    """"aggressive" over a stored "Aggressive" changed nothing about the case."""
    _stub_extraction(
        monkeypatch,
        {"boundary": "new", "case_type": "alpha", "fields": {"f_grade": "High"}},
        {"boundary": "continue", "fields": {"f_grade": "high"}},
    )
    first = await _turn(db, contract_knowledge_root, message="High", message_id="m1")
    second = await _turn(db, contract_knowledge_root, message="high", message_id="m2")
    assert second.record.record_version == first.record.record_version
    assert second.recorded_field_ids == ()


@pytest.mark.asyncio
async def test_unmappable_answer_stays_missing_and_asks_a_clarification(
    db, contract_knowledge_root, monkeypatch
):
    """The whole point of canonicalising at the write.

    The advisor DID answer. The answer is not one the case can be built on, so
    the field stays empty, stays in the intake surface, and the rendered block
    asks about THAT answer instead of repeating the question.
    """
    _stub_extraction(
        monkeypatch,
        {
            "boundary": "new",
            "case_type": "alpha",
            "fields": {"f_grade": "somewhere in the middle"},
        },
    )
    state = await _turn(
        db, contract_knowledge_root, message="somewhere in the middle", message_id="m1"
    )

    assert state.record.value_of("f_grade") is None
    assert "f_grade" in [spec.field_id for spec in state.empty_fields]
    assert state.unmappable_field_ids == ("f_grade",)
    assert state.evidence()["unmappable_fields"] == ["f_grade"]

    rendered = pcrt.render_case_record_prompt(state)
    # It is in the ASK set, not the facts set.
    assert "STILL MISSING" in rendered
    assert "somewhere in the middle" in rendered
    assert "must be one of: Low, High" in rendered
    assert "never pick one for them" in rendered


@pytest.mark.asyncio
async def test_an_unusable_answer_never_reaches_the_facts_block(
    db, contract_knowledge_root, monkeypatch
):
    _stub_extraction(
        monkeypatch,
        {"boundary": "new", "case_type": "alpha", "fields": {"f_grade": "no idea"}},
    )
    state = await _turn(db, contract_knowledge_root, message="no idea", message_id="m1")
    rendered = pcrt.render_case_record_prompt(state)
    facts, _, missing = rendered.partition("STILL MISSING")
    assert "no idea" not in facts


# ── the deployment's own config ─────────────────────────────────────────


MTU_CONFIG = (
    Path(__file__).resolve().parents[2] / "deploy" / "finexis" / "mtu"
)


def _mtu_config():
    context = SimpleNamespace(
        job_brief=SimpleNamespace(
            response_policy={
                "case_record": {
                    "enabled": True,
                    "field_sets": "case-field-sets.yaml",
                    "default_case_type": "protection_life",
                    "extraction": {"enabled": False},
                }
            }
        )
    )
    return pcrt.load_case_runtime_config(context, knowledge_root=MTU_CONFIG)


@pytest.mark.skipif(
    not (MTU_CONFIG / "case-field-sets.yaml").is_file(),
    reason="deployment config not present in this checkout",
)
def test_the_deployments_contracts_parse_and_cover_the_named_fields():
    config = _mtu_config()
    contracts = config.value_contracts
    expected = {
        "risk_profile": "enum",
        "cka_status": "enum",
        "objective_alignment": "enum",
        "product_category": "enum",
        "is_replacement": "boolean",
        "sustainability_answer": "boolean",
        "proposed_plan": "table",
        "fund_selection": "table",
        "min_investment_period": "table",
    }
    for field_id, kind in expected.items():
        assert field_id in contracts, f"{field_id} declares no value contract"
        assert contracts[field_id].kind == kind


@pytest.mark.skipif(
    not (MTU_CONFIG / "reference" / "062-approved-products.yaml").is_file(),
    reason="deployment config not present in this checkout",
)
def test_the_deployments_value_tables_load_from_their_reference_files():
    config = _mtu_config()
    assert "Wealth Voyage" in config.value_tables["approved_products"].values
    assert "Franklin Technology Fund" in config.value_tables["funds"].values
    # Keyed table: only the confirmed product carries a constraint.
    mip = config.value_tables["product_min_investment_periods"]
    assert mip.find("Wealth Abundance").keyed_values == (
        "10-year minimum investment period",
    )


@pytest.mark.skipif(
    not (MTU_CONFIG / "case-field-sets.yaml").is_file(),
    reason="deployment config not present in this checkout",
)
@pytest.mark.parametrize(
    "field_id,written,expected",
    [
        # teren's original probe: "is aggressive a valid input".
        ("risk_profile", "aggressive", "Aggressive"),
        ("risk_profile", "Aggressive", "Aggressive"),
        ("risk_profile", "client is fairly conservative", "Conservative"),
        # The narrow pattern must win: "did not pass" is not a pass.
        ("cka_status", "did not pass", "did not pass"),
        ("cka_status", "client failed the CKA", "did not pass"),
        ("cka_status", "passed", "passed"),
        ("cka_status", "CKA cleared", "passed"),
        ("objective_alignment", "the fund does not match", "mismatched"),
        ("objective_alignment", "aligned with the objective", "aligned"),
        ("product_category", "ILP", "investment_linked"),
        ("fund_selection", "Franklin Tech Fund", "Franklin Technology Fund"),
    ],
)
def test_deployment_enum_and_table_fields_resolve_real_advisor_wording(
    db, field_id, written, expected
):
    config = _mtu_config()
    store = config.open_store(db)
    case_id = store.mint_case(case_type="investment_linked")
    store.record_field(case_id, field_id, written)
    assert store.get_case(case_id).value_of(field_id) == expected


@pytest.mark.skipif(
    not (MTU_CONFIG / "case-field-sets.yaml").is_file(),
    reason="deployment config not present in this checkout",
)
def test_deployment_boolean_fields_store_real_booleans(db):
    config = _mtu_config()
    store = config.open_store(db)
    case_id = store.mint_case(case_type="investment_linked")
    store.record_field(case_id, "is_replacement", "yes")
    store.record_field(
        case_id, "sustainability_answer", "premiums do not exceed 50% of surplus"
    )
    record = store.get_case(case_id)
    assert record.value_of("is_replacement") is True
    assert record.value_of("sustainability_answer") is False


@pytest.mark.skipif(
    not (MTU_CONFIG / "case-field-sets.yaml").is_file(),
    reason="deployment config not present in this checkout",
)
def test_a_fund_outside_the_thin_reference_table_is_still_recorded(db):
    """The funds table holds three rows. Refusing everything else would turn
    "our list is incomplete" into "the advisor gave a bad answer" — and block a
    draft on a perfectly specific fund name."""
    config = _mtu_config()
    store = config.open_store(db)
    case_id = store.mint_case(case_type="investment_linked")
    store.record_field(case_id, "fund_selection", "HSBC Global Balanced Fund")
    record = store.get_case(case_id)
    assert record.value_of("fund_selection") == "HSBC Global Balanced Fund"
    field_set = config.field_sets["investment_linked"]
    from agent.pa_case_record import empty_required_field_ids

    assert "fund_selection" not in empty_required_field_ids(record, field_set)


@pytest.mark.skipif(
    not (MTU_CONFIG / "case-field-sets.yaml").is_file(),
    reason="deployment config not present in this checkout",
)
def test_an_unlisted_product_still_records_in_the_advisors_own_words(db):
    """062's escalation_cue: an unlisted product is NOT a reason to withhold."""
    config = _mtu_config()
    store = config.open_store(db)
    case_id = store.mint_case(case_type="protection_life")
    store.record_field(case_id, "proposed_plan", "Some Brand New Plan, $4k annual")
    assert (
        store.get_case(case_id).value_of("proposed_plan")
        == "Some Brand New Plan, $4k annual"
    )


@pytest.mark.skipif(
    not (MTU_CONFIG / "case-field-sets.yaml").is_file(),
    reason="deployment config not present in this checkout",
)
def test_a_listed_product_is_validated_but_never_rewritten(db):
    """The descriptive value carries premium and term — rewriting destroys them."""
    config = _mtu_config()
    store = config.open_store(db)
    case_id = store.mint_case(case_type="investment_linked")
    store.record_field(
        case_id, "proposed_plan", "Wealth Voyage 15, $12k annual, no riders"
    )
    entry = store.get_case(case_id).get("proposed_plan")
    assert entry.value == "Wealth Voyage 15, $12k annual, no riders"


@pytest.mark.skipif(
    not (MTU_CONFIG / "case-field-sets.yaml").is_file(),
    reason="deployment config not present in this checkout",
)
def test_a_minimum_investment_period_the_product_does_not_offer_is_refused(db):
    """R19 (Melody, 2026-08-03): Abundance is MIP10 ONLY.

    A 15-year Abundance reached a real advisor once. The period is now checked
    against the product that has to offer it, at the write.
    """
    config = _mtu_config()
    store = config.open_store(db)
    case_id = store.mint_case(case_type="investment_linked")
    store.record_field(case_id, "proposed_plan", "HSBC Life Wealth Abundance")
    store.record_field(
        case_id, "min_investment_period", "15-year minimum investment period"
    )
    record = store.get_case(case_id)
    assert record.get("min_investment_period").is_unmappable
    # And the confirmed one is accepted.
    store.record_field(
        case_id, "min_investment_period", "10-year minimum investment period"
    )
    assert (
        store.get_case(case_id).value_of("min_investment_period")
        == "10-year minimum investment period"
    )


@pytest.mark.skipif(
    not (MTU_CONFIG / "case-field-sets.yaml").is_file(),
    reason="deployment config not present in this checkout",
)
def test_a_product_with_no_confirmed_periods_constrains_nothing(db):
    """Inventing a restriction is the mirror defect of writing a wrong one."""
    config = _mtu_config()
    store = config.open_store(db)
    case_id = store.mint_case(case_type="investment_linked")
    store.record_field(case_id, "proposed_plan", "HSBC Life Wealth Voyage")
    store.record_field(
        case_id, "min_investment_period", "15-year minimum investment period"
    )
    assert (
        store.get_case(case_id).value_of("min_investment_period")
        == "15-year minimum investment period"
    )


@pytest.mark.skipif(
    not (MTU_CONFIG / "case-field-sets.yaml").is_file(),
    reason="deployment config not present in this checkout",
)
@pytest.mark.asyncio
async def test_a_keyed_contract_holds_even_when_the_key_arrives_second(db, monkeypatch):
    """Field order inside a turn belongs to the extractor, not to us.

    The keyed field can be written BEFORE the field it is keyed on. The write
    re-reads the case for keyed contracts, and the config derivation runs after
    every write, so a 15-year Abundance is refused either way — the R19 defect
    cannot slip through on ordering.
    """
    context = SimpleNamespace(
        job_brief=SimpleNamespace(
            response_policy={
                "case_record": {
                    "enabled": True,
                    "field_sets": "case-field-sets.yaml",
                    "default_case_type": "investment_linked",
                    "category_field": "product_category",
                    "category_source_field": "proposed_plan",
                    "extraction": {"enabled": True},
                }
            }
        )
    )

    async def _fake(*, config, prompt):
        return {
            "boundary": "new",
            "case_type": "investment_linked",
            "fields": {
                # The keyed field FIRST — the adverse order.
                "min_investment_period": "15-year minimum investment period",
                "proposed_plan": "HSBC Life Wealth Abundance",
            },
        }

    monkeypatch.setattr(pcrt, "_run_extraction", _fake)
    state = await pcrt.update_case_for_turn(
        session_db=db,
        pa_context=context,
        agent_id="a",
        chat_id="c",
        session_id="s",
        message="Abundance 15",
        message_id="m1",
        knowledge_root=MTU_CONFIG,
    )
    assert state.record.value_of("proposed_plan") == "HSBC Life Wealth Abundance"
    assert state.record.value_of("min_investment_period") is None
    assert "min_investment_period" in state.unmappable_field_ids
    assert "min_investment_period" in [s.field_id for s in state.empty_fields]


@pytest.mark.skipif(
    not (MTU_CONFIG / "case-field-sets.yaml").is_file(),
    reason="deployment config not present in this checkout",
)
@pytest.mark.asyncio
async def test_a_keyed_clarification_lists_the_keys_values_not_the_tables(
    db, monkeypatch
):
    """A wrong list is worse than no list.

    min_investment_period is keyed on the product, so its permitted set is the
    PRODUCT's row — never the table's product names, which would send the
    advisor after values that were never periods at all.
    """
    context = SimpleNamespace(
        job_brief=SimpleNamespace(
            response_policy={
                "case_record": {
                    "enabled": True,
                    "field_sets": "case-field-sets.yaml",
                    "default_case_type": "investment_linked",
                    "extraction": {"enabled": True},
                }
            }
        )
    )

    async def _fake(*, config, prompt):
        return {
            "boundary": "new",
            "case_type": "investment_linked",
            "fields": {
                "proposed_plan": "HSBC Life Wealth Abundance",
                "min_investment_period": "15-year minimum investment period",
            },
        }

    monkeypatch.setattr(pcrt, "_run_extraction", _fake)
    state = await pcrt.update_case_for_turn(
        session_db=db,
        pa_context=context,
        agent_id="a",
        chat_id="c",
        session_id="s",
        message="Abundance 15",
        message_id="m1",
        knowledge_root=MTU_CONFIG,
    )
    rendered = pcrt.render_case_record_prompt(state)
    assert "must be one of: 10-year minimum investment period" in rendered
    # The table's PRODUCT names must not be offered as period answers.
    assert "must be one of: Wealth Abundance" not in rendered


# ── MTU-011: identity is not completeness, end to end ──────────────────────


@pytest.mark.skipif(
    not (MTU_CONFIG / "case-field-sets.yaml").is_file(),
    reason="deployment config not present in this checkout",
)
@pytest.mark.asyncio
async def test_two_bare_product_names_do_not_make_a_case_draftable(db, monkeypatch):
    """The MTU-011 defect, at its actual cause.

    "Client replacing from GE term plan to Singlife term plan" filled both
    plan fields, emptied the required set, and the rendered block then told
    the model — in as many words — that nothing was missing and it should
    draft now. The model did. R01's material-missing gate never got a chance,
    because the record had already reported the case complete.
    """
    context = SimpleNamespace(
        job_brief=SimpleNamespace(
            response_policy={
                "case_record": {
                    "enabled": True,
                    "field_sets": "case-field-sets.yaml",
                    "default_case_type": "protection_life",
                    "category_field": "product_category",
                    "category_source_field": "proposed_plan",
                    "extraction": {"enabled": True},
                }
            }
        )
    )

    async def _fake(*, config, prompt):
        # Exactly what PR #51's `holds:` text instructs: a bare product name
        # IS the value. That instruction is correct and stays.
        return {
            "boundary": "new",
            "case_type": "protection_life",
            "fields": {
                "existing_plans": "GE term plan",
                "proposed_plan": "Singlife term plan",
            },
        }

    monkeypatch.setattr(pcrt, "_run_extraction", _fake)
    state = await pcrt.update_case_for_turn(
        session_db=db,
        pa_context=context,
        agent_id="a",
        chat_id="c",
        session_id="s",
        message="Client replacing from GE term plan to Singlife term plan",
        message_id="m1",
        knowledge_root=MTU_CONFIG,
    )

    # The names are KEPT — they are real facts the advisor gave.
    assert state.record.value_of("existing_plans") == "GE term plan"
    assert state.record.value_of("proposed_plan") == "Singlife term plan"
    # ...and both fields are still to be answered.
    missing = [spec.field_id for spec in state.empty_fields]
    assert "existing_plans" in missing
    assert "proposed_plan" in missing

    rendered = pcrt.render_case_record_prompt(state)
    # The draft-now instruction must NOT appear.
    assert "Do not ask any intake question" not in rendered
    assert "STILL MISSING — ask for EXACTLY these" in rendered
    # And the ask is for the material facts, not for the name again.
    assert "is on record, but a draft still needs" in rendered
    assert "Ask for that, not for the name again." in rendered


@pytest.mark.skipif(
    not (MTU_CONFIG / "case-field-sets.yaml").is_file(),
    reason="deployment config not present in this checkout",
)
@pytest.mark.asyncio
async def test_a_first_purchase_none_answer_is_complete_as_it_stands(db, monkeypatch):
    """Sufficiency must not trap the answer that has no amounts to give."""
    context = SimpleNamespace(
        job_brief=SimpleNamespace(
            response_policy={
                "case_record": {
                    "enabled": True,
                    "field_sets": "case-field-sets.yaml",
                    "default_case_type": "protection_life",
                    "extraction": {"enabled": True},
                }
            }
        )
    )

    async def _fake(*, config, prompt):
        return {
            "boundary": "new",
            "case_type": "protection_life",
            "fields": {"existing_plans": "None"},
        }

    monkeypatch.setattr(pcrt, "_run_extraction", _fake)
    state = await pcrt.update_case_for_turn(
        session_db=db,
        pa_context=context,
        agent_id="a",
        chat_id="c",
        session_id="s",
        message="first purchase, no existing plans",
        message_id="m1",
        knowledge_root=MTU_CONFIG,
    )
    assert "existing_plans" not in [spec.field_id for spec in state.empty_fields]


@pytest.mark.skipif(
    not (MTU_CONFIG / "case-field-sets.yaml").is_file(),
    reason="deployment config not present in this checkout",
)
@pytest.mark.asyncio
async def test_a_product_shorthand_carrying_its_own_period_is_sufficient(
    db, monkeypatch
):
    """"Voyage 15" states a material fact inside the name — MTU-007's opener."""
    context = SimpleNamespace(
        job_brief=SimpleNamespace(
            response_policy={
                "case_record": {
                    "enabled": True,
                    "field_sets": "case-field-sets.yaml",
                    "default_case_type": "investment_linked",
                    "extraction": {"enabled": True},
                }
            }
        )
    )

    async def _fake(*, config, prompt):
        return {
            "boundary": "new",
            "case_type": "investment_linked",
            "fields": {"proposed_plan": "Voyage 15"},
        }

    monkeypatch.setattr(pcrt, "_run_extraction", _fake)
    state = await pcrt.update_case_for_turn(
        session_db=db,
        pa_context=context,
        agent_id="a",
        chat_id="c",
        session_id="s",
        message="BOR for Voyage 15.",
        message_id="m1",
        knowledge_root=MTU_CONFIG,
    )
    missing = [spec.field_id for spec in state.empty_fields]
    assert "proposed_plan" not in missing
    # The case still holds — the ILP facts and the existing plan are absent.
    assert "existing_plans" in missing
    assert "cka_status" in missing


@pytest.mark.skipif(
    not (MTU_CONFIG / "case-field-sets.yaml").is_file(),
    reason="deployment config not present in this checkout",
)
@pytest.mark.asyncio
async def test_an_unresolved_unaskable_field_withholds_the_draft_now_instruction(
    db, monkeypatch
):
    """The same defect class as MTU-011, on the other axis.

    A required field marked ``askable: false`` is absent from the ask set BY
    DESIGN — the config says asking it as an intake item is a defect. But when
    the derivation layer cannot resolve it either, it vanishes from the record
    entirely and the case reads as complete. Here: both plans fully stated,
    nothing saying whether it is a replacement, and the record would have said
    "draft now" with ROP status unknown.
    """
    context = SimpleNamespace(
        job_brief=SimpleNamespace(
            response_policy={
                "case_record": {
                    "enabled": True,
                    "field_sets": "case-field-sets.yaml",
                    "default_case_type": "protection_life",
                    "extraction": {"enabled": True},
                }
            }
        )
    )

    async def _fake(*, config, prompt):
        return {
            "boundary": "new",
            "case_type": "protection_life",
            "fields": {
                "existing_plans": (
                    "GE Term, $300,000 death, premium S$1,200 yearly, to age 65"
                ),
                "proposed_plan": (
                    "Singlife Elite Term, $500,000 death, premium S$1,500 yearly, "
                    "to age 70"
                ),
            },
        }

    monkeypatch.setattr(pcrt, "_run_extraction", _fake)
    state = await pcrt.update_case_for_turn(
        session_db=db,
        pa_context=context,
        agent_id="a",
        chat_id="c",
        session_id="s",
        message="switching plans",
        message_id="m1",
        knowledge_root=MTU_CONFIG,
    )
    # Nothing to ASK — the plan facts are all there.
    assert [spec.field_id for spec in state.empty_fields] == []
    # ...but ROP status is genuinely unknown.
    assert "is_replacement" in [
        spec.field_id for spec in state.unresolved_unaskable_fields
    ]
    assert state.evidence()["unresolved_unaskable_fields"] == ["is_replacement"]

    rendered = pcrt.render_case_record_prompt(state)
    assert "draft now from the facts above" not in rendered
    assert "NOT YET RESOLVED" in rendered
    assert "is_replacement" in rendered


@pytest.mark.skipif(
    not (MTU_CONFIG / "case-field-sets.yaml").is_file(),
    reason="deployment config not present in this checkout",
)
@pytest.mark.asyncio
async def test_a_fully_resolved_case_still_gets_the_draft_now_instruction(
    db, monkeypatch
):
    """Control: the hold must not become permanent."""
    context = SimpleNamespace(
        job_brief=SimpleNamespace(
            response_policy={
                "case_record": {
                    "enabled": True,
                    "field_sets": "case-field-sets.yaml",
                    "default_case_type": "protection_life",
                    "extraction": {"enabled": True},
                }
            }
        )
    )

    async def _fake(*, config, prompt):
        return {
            "boundary": "new",
            "case_type": "protection_life",
            "fields": {
                "existing_plans": (
                    "GE Term, $300,000 death, premium S$1,200 yearly, to age 65, "
                    "being replaced"
                ),
                "proposed_plan": (
                    "Singlife Elite Term, $500,000 death, premium S$1,500 yearly, "
                    "to age 70"
                ),
            },
        }

    monkeypatch.setattr(pcrt, "_run_extraction", _fake)
    state = await pcrt.update_case_for_turn(
        session_db=db,
        pa_context=context,
        agent_id="a",
        chat_id="c",
        session_id="s",
        message="replacing plans",
        message_id="m1",
        knowledge_root=MTU_CONFIG,
    )
    assert state.record.value_of("is_replacement") is True
    assert state.unresolved_unaskable_fields == ()
    assert "draft now from the facts above" in pcrt.render_case_record_prompt(state)
