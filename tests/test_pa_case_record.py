"""Unit tests for the PA structured case record (program step S7, phase 1).

Covers the substrate contract:
  (a) the three tables + indexes are created by the declarative schema;
  (b) minting on the new-case boundary, with provenance of the message that
      opened it, and supersede-on-mint (the ONLY status transition — there
      is no finalize/lock);
  (c) field writes carry value + source message id + timestamp + origin;
  (d) a correction OVERWRITES, bumps record_version, and preserves the
      replaced value in field history;
  (e) empty_required_fields — the surface phase-2 intake generation consumes
      — never returns a filled field, a derived field, or an unaskable one;
  (f) the derivation hook records origin=derived and inherits the source
      message id of the answer it derived from;
  (g) the field-set loader is client-agnostic and parses the deployment's
      own config file.

Pure unit tests — no gateway, no event loop, no network.
"""

import sqlite3

import pytest

from hermes_state import SessionDB
from agent import pa_case_record as pcr


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield session_db
    finally:
        session_db.close()


@pytest.fixture
def store(db):
    return pcr.CaseRecordStore(db)


@pytest.fixture
def field_set():
    """A deliberately client-agnostic field set — opaque ids only."""
    sets = pcr.parse_field_sets(
        {
            "version": 1,
            "field_sets": {
                "alpha": {
                    "case_type": "alpha",
                    "fields": [
                        {"id": "f_one", "required": True, "ask_hint": "one?"},
                        {"id": "f_two", "required": True, "ask_hint": "two?"},
                        {
                            "id": "f_derived",
                            "required": True,
                            "askable": False,
                            "derived_from": ["f_one"],
                        },
                        {"id": "f_optional", "required": False},
                    ],
                }
            },
        }
    )
    return sets["alpha"]


# ── (a) schema ──────────────────────────────────────────────────────────


def test_case_tables_and_indexes_created(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        conn = sqlite3.connect(tmp_path / "state.db")
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
                ).fetchall()
            }
        finally:
            conn.close()
    finally:
        db.close()

    assert "pa_case_records" in names
    assert "pa_case_fields" in names
    assert "pa_case_field_history" in names
    assert "idx_pa_case_records_scope" in names
    assert "idx_pa_case_fields_case" in names
    assert "idx_pa_case_field_history_case" in names


# ── (b) mint ────────────────────────────────────────────────────────────


def test_mint_creates_open_record_at_version_zero(store):
    case_id = store.mint_case(
        case_type="alpha",
        agent_id="agent-1",
        chat_id="chat-1",
        session_id="sess-1",
        field_set_id="alpha",
        minted_from_message_id="msg-100",
    )
    record = store.get_case(case_id)

    assert record is not None
    assert record.case_id == case_id
    assert record.status == pcr.STATUS_OPEN
    assert record.is_open
    assert record.case_type == "alpha"
    assert record.field_set_id == "alpha"
    assert record.minted_from_message_id == "msg-100"
    assert record.record_version == 0
    assert record.fields == {}
    assert record.created_at is not None
    assert record.version_stamp() == f"{case_id}@v0"


def test_open_case_is_readable_by_scope(store):
    case_id = store.mint_case(agent_id="a", chat_id="c", case_type="alpha")
    found = store.get_open_case(agent_id="a", chat_id="c")
    assert found is not None and found.case_id == case_id
    # A different chat is a different scope.
    assert store.get_open_case(agent_id="a", chat_id="other") is None


def test_new_mint_supersedes_prior_open_case(store):
    first = store.mint_case(agent_id="a", chat_id="c", minted_from_message_id="m1")
    store.record_field(first, "f_one", "kept-on-the-old-case", source_message_id="m1")

    second = store.mint_case(agent_id="a", chat_id="c", minted_from_message_id="m9")

    old = store.get_case(first)
    new = store.get_case(second)

    assert old.status == pcr.STATUS_SUPERSEDED
    assert old.superseded_by_case_id == second
    assert old.superseded_at is not None
    # Superseded is NOT a lock: the old record keeps everything it held.
    assert old.value_of("f_one") == "kept-on-the-old-case"

    assert new.is_open
    assert new.record_version == 0
    assert new.fields == {}  # facts are never carried across the boundary

    # Exactly one open case in the scope.
    assert store.get_open_case(agent_id="a", chat_id="c").case_id == second


def test_mint_does_not_supersede_another_scope(store):
    a = store.mint_case(agent_id="a", chat_id="chat-a")
    store.mint_case(agent_id="a", chat_id="chat-b")
    assert store.get_case(a).status == pcr.STATUS_OPEN


def test_no_lock_state_exists(store):
    """There is no finalize/lock: status only ever takes the two values."""
    case_id = store.mint_case(agent_id="a", chat_id="c")
    assert pcr.STATUS_OPEN == "open"
    assert pcr.STATUS_SUPERSEDED == "superseded"
    # Nothing on the store can move a record to any other status.
    assert not [
        name
        for name in dir(store)
        if any(k in name for k in ("finalize", "lock", "close_case"))
    ]
    assert store.get_case(case_id).status == pcr.STATUS_OPEN


# ── (c) field writes ────────────────────────────────────────────────────


def test_record_field_carries_full_provenance(store):
    case_id = store.mint_case(case_type="alpha", agent_id="a", chat_id="c")
    version = store.record_field(
        case_id, "f_one", {"nested": ["structured", "value"]}, source_message_id="m7"
    )

    assert version == 1
    record = store.get_case(case_id)
    assert record.record_version == 1
    assert record.version_stamp() == f"{case_id}@v1"

    entry = record.get("f_one")
    assert entry.value == {"nested": ["structured", "value"]}
    assert entry.source_message_id == "m7"
    assert entry.origin == pcr.ORIGIN_USER_STATED
    assert entry.recorded_at is not None
    assert entry.record_version == 1
    assert entry.is_filled


def test_each_field_write_bumps_version(store):
    case_id = store.mint_case(agent_id="a", chat_id="c")
    assert store.record_field(case_id, "f_one", 1, source_message_id="m1") == 1
    assert store.record_field(case_id, "f_two", 2, source_message_id="m2") == 2
    assert store.get_case(case_id).record_version == 2


def test_record_field_rejects_unknown_case(store):
    with pytest.raises(KeyError):
        store.record_field("no-such-case", "f_one", "v", source_message_id="m1")


def test_record_field_rejects_bad_origin_and_unsourced_derivation(store):
    case_id = store.mint_case(agent_id="a", chat_id="c")
    with pytest.raises(ValueError):
        store.record_field(case_id, "f_one", "v", origin="guessed")
    with pytest.raises(ValueError):
        store.record_field(case_id, "f_one", "v", origin=pcr.ORIGIN_DERIVED)


# ── (d) corrections ─────────────────────────────────────────────────────


def test_correction_overwrites_bumps_version_and_keeps_history(store):
    case_id = store.mint_case(agent_id="a", chat_id="c")
    store.record_field(case_id, "f_one", "first", source_message_id="m1")
    new_version = store.record_field(
        case_id, "f_one", "corrected", source_message_id="m5"
    )

    assert new_version == 2
    record = store.get_case(case_id)
    assert record.record_version == 2
    # ONE current value — the correction overwrote, it did not append.
    assert len(record.fields) == 1
    entry = record.get("f_one")
    assert entry.value == "corrected"
    assert entry.source_message_id == "m5"
    assert entry.record_version == 2

    history = store.field_history(case_id, "f_one")
    assert len(history) == 1
    assert history[0].value == "first"
    assert history[0].source_message_id == "m1"
    assert history[0].record_version == 1


def test_history_accumulates_in_order(store):
    case_id = store.mint_case(agent_id="a", chat_id="c")
    for i, msg in enumerate(("m1", "m2", "m3"), start=1):
        store.record_field(case_id, "f_one", f"v{i}", source_message_id=msg)

    history = store.field_history(case_id, "f_one")
    assert [h.value for h in history] == ["v1", "v2"]
    assert store.get_case(case_id).value_of("f_one") == "v3"
    assert store.get_case(case_id).record_version == 3


def test_history_is_scoped_per_field(store):
    case_id = store.mint_case(agent_id="a", chat_id="c")
    store.record_field(case_id, "f_one", "a", source_message_id="m1")
    store.record_field(case_id, "f_one", "b", source_message_id="m2")
    store.record_field(case_id, "f_two", "x", source_message_id="m3")
    store.record_field(case_id, "f_two", "y", source_message_id="m4")

    assert [h.value for h in store.field_history(case_id, "f_one")] == ["a"]
    assert [h.value for h in store.field_history(case_id, "f_two")] == ["x"]
    assert len(store.field_history(case_id)) == 2


# ── (e) empty_required_fields — the phase-2 intake surface ──────────────


def test_empty_required_fields_on_fresh_record(store, field_set):
    case_id = store.mint_case(case_type="alpha", agent_id="a", chat_id="c")
    record = store.get_case(case_id)

    specs = pcr.empty_required_fields(record, field_set)
    # f_derived is unaskable, f_optional is not required.
    assert [s.field_id for s in specs] == ["f_one", "f_two"]
    assert specs[0].ask_hint == "one?"


def test_filled_field_is_never_re_asked(store, field_set):
    case_id = store.mint_case(case_type="alpha", agent_id="a", chat_id="c")
    store.record_field(case_id, "f_one", "answered", source_message_id="m1")

    assert pcr.empty_required_field_ids(store.get_case(case_id), field_set) == [
        "f_two"
    ]


def test_explicit_negative_answer_counts_as_filled(store, field_set):
    """"none" is an ANSWER — the field is filled and must not be re-asked."""
    case_id = store.mint_case(case_type="alpha", agent_id="a", chat_id="c")
    store.record_field(case_id, "f_one", "none", source_message_id="m1")
    store.record_field(case_id, "f_two", [], source_message_id="m1")

    assert pcr.empty_required_field_ids(store.get_case(case_id), field_set) == []


def test_derived_field_is_never_asked(store, field_set):
    case_id = store.mint_case(case_type="alpha", agent_id="a", chat_id="c")
    record = store.get_case(case_id)

    assert "f_derived" not in pcr.empty_required_field_ids(record, field_set)
    # ...but the completeness view still shows it as missing.
    assert "f_derived" in pcr.empty_required_field_ids(
        record, field_set, include_unaskable=True
    )
    assert not pcr.is_record_complete(record, field_set)


def test_is_record_complete_when_all_required_present(store, field_set):
    case_id = store.mint_case(case_type="alpha", agent_id="a", chat_id="c")
    store.record_field(case_id, "f_one", "a", source_message_id="m1")
    store.record_field(case_id, "f_two", "b", source_message_id="m1")
    store.record_derived_field(
        case_id, "f_derived", True, derived_from_field_id="f_one"
    )

    record = store.get_case(case_id)
    assert pcr.empty_required_fields(record, field_set) == []
    assert pcr.is_record_complete(record, field_set)


def test_store_empty_required_fields_passthrough(store, field_set):
    case_id = store.mint_case(case_type="alpha", agent_id="a", chat_id="c")
    assert [s.field_id for s in store.empty_required_fields(case_id, field_set)] == [
        "f_one",
        "f_two",
    ]
    with pytest.raises(KeyError):
        store.empty_required_fields("no-such-case", field_set)


# ── (f) derivation hook ─────────────────────────────────────────────────


def test_derived_field_inherits_source_message_of_the_answer(store):
    case_id = store.mint_case(agent_id="a", chat_id="c")
    store.record_field(case_id, "f_one", "none", source_message_id="m42")

    version = store.record_derived_field(
        case_id, "f_derived", False, derived_from_field_id="f_one"
    )

    entry = store.get_case(case_id).get("f_derived")
    assert version == 2
    assert entry.value is False
    assert entry.origin == pcr.ORIGIN_DERIVED
    assert entry.derived_from_field_id == "f_one"
    # Provenance points at the message the human actually wrote.
    assert entry.source_message_id == "m42"


def test_cannot_derive_from_an_unrecorded_answer(store):
    case_id = store.mint_case(agent_id="a", chat_id="c")
    with pytest.raises(KeyError):
        store.record_derived_field(
            case_id, "f_derived", True, derived_from_field_id="f_one"
        )


def test_apply_derivations_records_and_skips(store):
    case_id = store.mint_case(agent_id="a", chat_id="c")
    store.record_field(case_id, "f_one", "none", source_message_id="m3")

    def derive_from_one(record):
        source = record.get("f_one")
        if source is None:
            return None
        return (source.value != "none", "f_one")

    def never_resolves(record):
        return None

    recorded = store.apply_derivations(
        case_id, {"f_derived": derive_from_one, "f_other": never_resolves}
    )
    assert recorded == ["f_derived"]

    entry = store.get_case(case_id).get("f_derived")
    assert entry.value is False
    assert entry.source_message_id == "m3"
    assert store.get_case(case_id).get("f_other") is None

    # Re-running is a no-op: a derived answer never stomps what is there.
    assert store.apply_derivations(case_id, {"f_derived": derive_from_one}) == []


def test_apply_derivations_does_not_stomp_a_stated_value(store):
    case_id = store.mint_case(agent_id="a", chat_id="c")
    store.record_field(case_id, "f_one", "plan-x", source_message_id="m1")
    store.record_field(case_id, "f_derived", "stated", source_message_id="m2")

    assert store.apply_derivations(
        case_id, {"f_derived": lambda r: ("derived", "f_one")}
    ) == []
    assert store.get_case(case_id).value_of("f_derived") == "stated"


# ── (g) field-set config ────────────────────────────────────────────────


def test_parse_field_sets_defaults_and_duplicate_guard():
    sets = pcr.parse_field_sets(
        {"field_sets": {"s": {"fields": [{"id": "x"}]}}}
    )
    spec = sets["s"].spec("x")
    assert spec.required is True and spec.askable is True
    assert sets["s"].case_type == "s"
    assert sets["s"].required_field_ids == ["x"]

    with pytest.raises(ValueError):
        pcr.parse_field_sets(
            {"field_sets": {"s": {"fields": [{"id": "x"}, {"id": "x"}]}}}
        )
    with pytest.raises(ValueError):
        pcr.parse_field_sets({"field_sets": {"s": {"fields": [{"required": True}]}}})


def test_select_field_set_by_id_and_case_type():
    sets = pcr.parse_field_sets(
        {"field_sets": {"s1": {"case_type": "ct", "fields": [{"id": "x"}]}}}
    )
    assert pcr.select_field_set(sets, field_set_id="s1").field_set_id == "s1"
    assert pcr.select_field_set(sets, case_type="ct").field_set_id == "s1"
    assert pcr.select_field_set(sets, case_type="nope") is None


def test_load_field_sets_from_yaml(tmp_path):
    path = tmp_path / "sets.yaml"
    path.write_text(
        "version: 1\n"
        "field_sets:\n"
        "  s:\n"
        "    fields:\n"
        "      - id: a\n"
        "        ask_hint: 'a?'\n"
        "      - id: b\n"
        "        askable: false\n",
        encoding="utf-8",
    )
    sets = pcr.load_field_sets(path)
    assert sets["s"].spec("a").ask_hint == "a?"
    assert sets["s"].spec("b").askable is False


def test_load_field_sets_from_json(tmp_path):
    path = tmp_path / "sets.json"
    path.write_text(
        '{"field_sets": {"s": {"fields": [{"id": "a"}]}}}', encoding="utf-8"
    )
    assert pcr.load_field_sets(path)["s"].required_field_ids == ["a"]


def test_deployment_field_set_file_parses(store):
    """The client-owned config in the deploy tree is loadable as written."""
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "finexis"
        / "mtu"
        / "case-field-sets.yaml"
    )
    if not path.exists():  # deploy tree not present in this checkout
        pytest.skip("deployment field-set config not present")

    sets = pcr.load_field_sets(path)
    assert sets  # at least one case type

    for field_set in sets.values():
        assert field_set.fields
        case_id = store.mint_case(
            case_type=field_set.case_type, field_set_id=field_set.field_set_id
        )
        record = store.get_case(case_id)
        specs = pcr.empty_required_fields(record, field_set)
        # Every askable required field is initially empty and askable.
        assert all(s.required and s.askable for s in specs)
        # Nothing marked unaskable leaks into the ask set.
        unaskable = {f.field_id for f in field_set.fields if not f.askable}
        assert unaskable.isdisjoint({s.field_id for s in specs})


# ── safety wrapper ──────────────────────────────────────────────────────


def test_safe_record_field_swallows(store):
    assert pcr.safe_record_field(store, "no-such-case", "f", "v") is None
    case_id = store.mint_case(agent_id="a", chat_id="c")
    assert pcr.safe_record_field(store, case_id, "f_one", "v") == 1


# ── The value contract: enforced at the record write ────────────────────
#
# teren, 2026-08-04, from the probe "is aggressive a valid input": a field
# whose values come from a fixed set is converted AT THE WRITE, not by
# whoever reads it later. These tests are about WHERE the conversion happens
# as much as whether it happens — a value arriving canonical at a downstream
# reader proves nothing if each reader canonicalises for itself.


@pytest.fixture
def contracted_store(db):
    """A store carrying one contract of each kind, plus a keyed table."""
    sets = pcr.parse_field_sets(
        {
            "version": 1,
            "field_sets": {
                "alpha": {
                    "case_type": "alpha",
                    "fields": [
                        {
                            "id": "f_enum",
                            "required": True,
                            "ask_hint": "which one?",
                            "value_contract": {
                                "kind": "enum",
                                "values": ["Low", "Mid", "High"],
                                "match": [
                                    {"pattern": r"(?i)\blow\b", "value": "Low"},
                                    {"pattern": r"(?i)\bmid\b", "value": "Mid"},
                                    {"pattern": r"(?i)\bhigh\b", "value": "High"},
                                ],
                            },
                        },
                        {
                            "id": "f_bool",
                            "required": True,
                            "value_contract": {"kind": "boolean"},
                        },
                        {
                            "id": "f_table",
                            "required": True,
                            "value_contract": {
                                "kind": "table",
                                "table": "widgets",
                            },
                        },
                        {
                            "id": "f_table_loose",
                            "required": False,
                            "value_contract": {
                                "kind": "table",
                                "table": "widgets",
                                "on_unmatched": "accept",
                                "stored_form": "as_written",
                            },
                        },
                        {
                            "id": "f_keyed",
                            "required": False,
                            "value_contract": {
                                "kind": "table",
                                "table": "widgets",
                                "key_field": "f_table_loose",
                            },
                        },
                    ],
                }
            },
        }
    )
    tables = pcr.parse_value_tables(
        {
            "value_tables": {
                "widgets": {
                    "entries": [
                        {
                            "value": "Widget Alpha",
                            "aliases": ["Alpha"],
                            "values": ["size-10"],
                        },
                        {"value": "Widget Beta"},
                    ]
                }
            }
        }
    )
    return pcr.CaseRecordStore(
        db,
        value_contracts=pcr.value_contracts_from_field_sets(sets),
        value_tables=tables,
    ), sets["alpha"]


# ── kind: enum ──


def test_enum_variant_is_canonicalised_at_the_write(contracted_store):
    store, _ = contracted_store
    case_id = store.mint_case(case_type="alpha")
    store.record_field(case_id, "f_enum", "high", source_message_id="m1")
    entry = store.get_case(case_id).get("f_enum")
    assert entry.value == "High"
    assert entry.canonicalization == pcr.CANON_MAPPED
    # PROVENANCE: what the writer actually said survives beside the value.
    assert entry.raw_value == "high"
    assert entry.source_message_id == "m1"


def test_enum_value_already_canonical_is_stored_untouched(contracted_store):
    store, _ = contracted_store
    case_id = store.mint_case(case_type="alpha")
    store.record_field(case_id, "f_enum", "High")
    entry = store.get_case(case_id).get("f_enum")
    assert entry.value == "High"
    assert entry.canonicalization == pcr.CANON_EXACT
    assert entry.raw_value is None


def test_enum_free_text_reaches_a_value_through_the_mapping(contracted_store):
    store, _ = contracted_store
    case_id = store.mint_case(case_type="alpha")
    store.record_field(case_id, "f_enum", "client is on the low side")
    assert store.get_case(case_id).value_of("f_enum") == "Low"


# ── unmappable -> empty -> intake asks ──


def test_unmappable_answer_leaves_the_field_empty(contracted_store):
    """NEVER a guess. The advisor answered; the answer is not one we can use."""
    store, field_set = contracted_store
    case_id = store.mint_case(case_type="alpha")
    store.record_field(case_id, "f_enum", "somewhere in between", source_message_id="m2")
    record = store.get_case(case_id)
    entry = record.get("f_enum")
    assert entry.value is None
    assert not entry.is_filled
    assert entry.is_unmappable
    # The raw answer is kept so the clarification can quote it.
    assert entry.raw_value == "somewhere in between"
    # AND THIS IS THE POINT: it stays in the intake surface.
    assert "f_enum" in pcr.empty_required_field_ids(record, field_set)


def test_a_clarified_answer_fills_the_field_and_clears_the_ask(contracted_store):
    store, field_set = contracted_store
    case_id = store.mint_case(case_type="alpha")
    store.record_field(case_id, "f_enum", "not sure really")
    assert "f_enum" in pcr.empty_required_field_ids(
        store.get_case(case_id), field_set
    )
    store.record_field(case_id, "f_enum", "mid")
    record = store.get_case(case_id)
    assert record.value_of("f_enum") == "Mid"
    assert "f_enum" not in pcr.empty_required_field_ids(record, field_set)
    # The unusable answer is preserved in history, not erased.
    history = store.field_history(case_id, "f_enum")
    assert history[-1].raw_value == "not sure really"
    assert history[-1].canonicalization == pcr.CANON_UNMAPPABLE


# ── kind: boolean ──


@pytest.mark.parametrize(
    "written,expected",
    [
        ("yes", True),
        ("Yes", True),
        ("true", True),
        ("1", True),
        ("no", False),
        ("No", False),
        ("false", False),
        ("0", False),
        (True, True),
        (False, False),
    ],
)
def test_boolean_contract_stores_a_real_boolean(contracted_store, written, expected):
    store, _ = contracted_store
    case_id = store.mint_case(case_type="alpha")
    store.record_field(case_id, "f_bool", written)
    value = store.get_case(case_id).value_of("f_bool")
    assert value is expected


def test_boolean_contract_refuses_an_answer_it_cannot_read(contracted_store):
    store, field_set = contracted_store
    case_id = store.mint_case(case_type="alpha")
    store.record_field(case_id, "f_bool", "probably")
    record = store.get_case(case_id)
    assert record.get("f_bool").is_unmappable
    assert "f_bool" in pcr.empty_required_field_ids(record, field_set)


# ── kind: table ──


def test_table_contract_stores_the_tables_canonical_spelling(contracted_store):
    store, _ = contracted_store
    case_id = store.mint_case(case_type="alpha")
    store.record_field(case_id, "f_table", "Alpha")
    assert store.get_case(case_id).value_of("f_table") == "Widget Alpha"


def test_table_contract_unpopulates_an_unknown_value(contracted_store):
    store, field_set = contracted_store
    case_id = store.mint_case(case_type="alpha")
    store.record_field(case_id, "f_table", "Widget Omega")
    record = store.get_case(case_id)
    assert record.get("f_table").is_unmappable
    assert "f_table" in pcr.empty_required_field_ids(record, field_set)


def test_table_contract_can_validate_without_rewriting(contracted_store):
    """stored_form: as_written — the answer carries more than the identifier."""
    store, _ = contracted_store
    case_id = store.mint_case(case_type="alpha")
    store.record_field(
        case_id, "f_table_loose", "Widget Alpha, 15-year term, $12k annual"
    )
    entry = store.get_case(case_id).get("f_table_loose")
    # NOT rewritten to "Widget Alpha": the term and premium are case facts.
    assert entry.value == "Widget Alpha, 15-year term, $12k annual"
    assert entry.canonicalization == pcr.CANON_VALIDATED


def test_table_contract_can_accept_an_unlisted_value(contracted_store):
    """on_unmatched: accept — a name missing from the table is a gap in the
    TABLE, not a bad answer. (The approved-product list rules exactly this.)"""
    store, _ = contracted_store
    case_id = store.mint_case(case_type="alpha")
    store.record_field(case_id, "f_table_loose", "Some Unlisted Product")
    entry = store.get_case(case_id).get("f_table_loose")
    assert entry.value == "Some Unlisted Product"
    assert entry.is_filled


# ── kind: table, KEYED on another field ──


def test_keyed_table_validates_against_the_keying_answer(contracted_store):
    store, _ = contracted_store
    case_id = store.mint_case(case_type="alpha")
    store.record_field(case_id, "f_table_loose", "Widget Alpha")
    store.record_field(case_id, "f_keyed", "size-10")
    assert store.get_case(case_id).value_of("f_keyed") == "size-10"


def test_keyed_table_refuses_a_value_the_key_does_not_permit(contracted_store):
    """The R19 shape: a period the product does not actually offer."""
    store, _ = contracted_store
    case_id = store.mint_case(case_type="alpha")
    store.record_field(case_id, "f_table_loose", "Widget Alpha")
    store.record_field(case_id, "f_keyed", "size-20")
    assert store.get_case(case_id).get("f_keyed").is_unmappable


def test_keyed_table_places_no_constraint_when_the_key_declares_none(
    contracted_store,
):
    """An UNDECLARED product constrains nothing — asserting otherwise would
    invent a restriction nobody ruled."""
    store, _ = contracted_store
    case_id = store.mint_case(case_type="alpha")
    store.record_field(case_id, "f_table_loose", "Widget Beta")
    store.record_field(case_id, "f_keyed", "size-99")
    assert store.get_case(case_id).value_of("f_keyed") == "size-99"


# ── every write path funnels through the contract ──


def test_a_derived_write_is_contracted_too(contracted_store):
    """Derivation is a write, so it canonicalises like any other."""
    store, _ = contracted_store
    case_id = store.mint_case(case_type="alpha")
    store.record_field(case_id, "f_table_loose", "Widget Alpha", source_message_id="m5")
    store.record_derived_field(
        case_id, "f_enum", "high", derived_from_field_id="f_table_loose"
    )
    entry = store.get_case(case_id).get("f_enum")
    assert entry.value == "High"
    assert entry.origin == pcr.ORIGIN_DERIVED
    # Provenance still leads back to the message the writer actually wrote.
    assert entry.source_message_id == "m5"


def test_an_uncontracted_field_is_stored_verbatim(contracted_store):
    store, _ = contracted_store
    case_id = store.mint_case(case_type="alpha")
    store.record_field(case_id, "f_free", "whatever the advisor said")
    entry = store.get_case(case_id).get("f_free")
    assert entry.value == "whatever the advisor said"
    assert entry.canonicalization == pcr.CANON_UNCONTRACTED


# ── config parsing refuses incoherent contracts ──


def test_a_match_producing_an_undeclared_value_refuses_to_parse():
    with pytest.raises(ValueError, match="not one of the declared values"):
        pcr.parse_field_sets(
            {
                "field_sets": {
                    "a": {
                        "fields": [
                            {
                                "id": "f",
                                "value_contract": {
                                    "kind": "enum",
                                    "values": ["X"],
                                    "match": [{"pattern": "y", "value": "Y"}],
                                },
                            }
                        ]
                    }
                }
            }
        )


def test_an_unknown_contract_kind_refuses_to_parse():
    with pytest.raises(ValueError, match="kind must be one of"):
        pcr.parse_field_sets(
            {
                "field_sets": {
                    "a": {
                        "fields": [
                            {"id": "f", "value_contract": {"kind": "freeform"}}
                        ]
                    }
                }
            }
        )


def test_an_enum_without_values_refuses_to_parse():
    with pytest.raises(ValueError, match="must declare values"):
        pcr.parse_field_sets(
            {"field_sets": {"a": {"fields": [{"id": "f", "value_contract": {"kind": "enum"}}]}}}
        )


# ── Sufficiency: identity is not completeness ───────────────────────────
#
# MTU-011: "Client replacing from GE term plan to Singlife term plan" — one
# line, two bare product names — produced a COMPLETE BOR. The record was the
# cause, not the model: both plan fields were filled, so the empty-required
# set was empty, so the rendered block told the model in as many words that
# nothing was missing and it should draft now.


@pytest.fixture
def sufficiency_set():
    return pcr.parse_field_sets(
        {
            "version": 1,
            "field_sets": {
                "alpha": {
                    "case_type": "alpha",
                    "fields": [
                        {
                            "id": "f_plan",
                            "required": True,
                            "ask_hint": "which plan?",
                            "sufficiency": {
                                "description": "the premium or the term",
                                "any_of": [
                                    {"name": "an amount", "pattern": r"\$\s?[\d,]+"},
                                    {
                                        "name": "a duration",
                                        "pattern": r"(?i)\b\d{1,2}\s*-?\s*years?\b",
                                    },
                                ],
                            },
                        },
                        {
                            "id": "f_both",
                            "required": True,
                            "sufficiency": {
                                "all_of": [
                                    {"name": "an insurer", "pattern": r"(?i)insurer:"},
                                    {"name": "an amount", "pattern": r"\$\s?[\d,]+"},
                                ]
                            },
                        },
                    ],
                }
            },
        }
    )["alpha"]


def test_a_bare_name_fills_the_field_but_does_not_answer_it(store, sufficiency_set):
    """The MTU-011 shape, at the record."""
    case_id = store.mint_case(case_type="alpha")
    store.record_field(case_id, "f_plan", "Singlife term plan")
    record = store.get_case(case_id)
    # The name IS a fact and is KEPT — dropping it would lose what was said.
    assert record.value_of("f_plan") == "Singlife term plan"
    assert record.is_filled("f_plan")
    # ...and the case is NOT ready to draft from.
    assert "f_plan" in pcr.empty_required_field_ids(record, sufficiency_set)
    assert not pcr.is_record_complete(record, sufficiency_set)


def test_a_value_carrying_the_material_fact_answers_the_field(store, sufficiency_set):
    case_id = store.mint_case(case_type="alpha")
    store.record_field(case_id, "f_plan", "Singlife term plan, premium $1,500 yearly")
    record = store.get_case(case_id)
    assert "f_plan" not in pcr.empty_required_field_ids(record, sufficiency_set)


def test_any_of_is_satisfied_by_either_probe(store, sufficiency_set):
    case_id = store.mint_case(case_type="alpha")
    store.record_field(case_id, "f_plan", "Singlife term plan, 20 years")
    assert "f_plan" not in pcr.empty_required_field_ids(
        store.get_case(case_id), sufficiency_set
    )


def test_all_of_needs_every_probe(store, sufficiency_set):
    case_id = store.mint_case(case_type="alpha")
    store.record_field(case_id, "f_both", "insurer: Singlife")
    record = store.get_case(case_id)
    assert "f_both" in pcr.empty_required_field_ids(record, sufficiency_set)
    store.record_field(case_id, "f_both", "insurer: Singlife, $500,000")
    assert "f_both" not in pcr.empty_required_field_ids(
        store.get_case(case_id), sufficiency_set
    )


def test_the_gap_names_the_missing_parts_not_the_whole_field(sufficiency_set):
    """So intake can ask for what is absent instead of re-asking the name."""
    spec = sufficiency_set.spec("f_plan")
    assert spec.sufficiency_gap("Singlife term plan") == ["an amount or a duration"]
    assert spec.sufficiency_gap("Singlife term plan, $1,200") == []
    both = sufficiency_set.spec("f_both")
    assert both.sufficiency_gap("insurer: Singlife") == ["an amount"]


def test_a_field_with_no_sufficiency_is_answered_by_any_value(store, field_set):
    case_id = store.mint_case(case_type=field_set.case_type)
    store.record_field(case_id, "f_one", "x")
    assert "f_one" not in pcr.empty_required_field_ids(
        store.get_case(case_id), field_set
    )


def test_an_empty_field_reports_no_sufficiency_gap(sufficiency_set):
    """Empty is a DIFFERENT problem, already handled by the filled check."""
    assert sufficiency_set.spec("f_plan").sufficiency_gap(None) == []


def test_a_sufficiency_with_no_probes_refuses_to_parse():
    with pytest.raises(ValueError, match="can never fail"):
        pcr.parse_field_sets(
            {
                "field_sets": {
                    "a": {"fields": [{"id": "f", "sufficiency": {"description": "x"}}]}
                }
            }
        )
