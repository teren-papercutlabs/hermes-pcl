"""Acceptance matrix for the frozen workflow correlator hard cases.

These tests deliberately use one opaque, tenant-neutral template and the real
workflow engine against a fresh SQLite board for every scenario.  ``match`` is
kept separate from ``apply`` throughout: matching must never advance a task.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db
from hermes_cli import wf_engine


FIXED_NOW = 1_785_000_000

# The six rows are the six top-level rows in the frozen plan's §2 hard-case
# table.  Rows 7 and 8 in the worker brief are cross-cutting acceptance
# requirements, covered below but intentionally not added to this six-row set.
HARD_CASE_ROWS = (
    "shared_booking_ref",
    "event_matches_nothing",
    "event_matches_two",
    "out_of_order_events",
    "duplicate_events",
    "event_for_done_instance",
)


def _spec() -> dict:
    return {
        "id": "opaque-workflow",
        "entity": "strong_key",
        "correlation_keys": ["strong_key", "weak_key"],
        "disambiguators": ["region"],
        "create_on": ["created_event"],
        "steps": [
            {"key": "start", "turn": {"brief": "start"}, "advance_to": "awaiting"},
            {
                "key": "awaiting",
                "waits": [
                    {
                        "kind": "event",
                        "types": ["current_event"],
                        "schema": "typed-v1",
                        "advance_to": "processing",
                    },
                ],
            },
            {"key": "processing", "turn": {"brief": "process"}, "advance_to": "future"},
            {
                "key": "future",
                "waits": [
                    {
                        "kind": "event",
                        "types": ["future_event"],
                        "schema": "typed-v1",
                        "advance_to": "finishing",
                    },
                ],
            },
            {"key": "finishing", "turn": {"brief": "finish"}, "advance_to": "done"},
            {"key": "done"},
        ],
    }


@pytest.fixture
def board(tmp_path, monkeypatch):
    """Fresh bare board with a deterministic engine clock."""

    monkeypatch.setattr(wf_engine, "_now", lambda: FIXED_NOW)
    conn = kanban_db.connect(tmp_path / "board.sqlite")
    template_id, _version = wf_engine.register_template(conn, _spec())
    try:
        yield conn, template_id
    finally:
        conn.close()


def _event(
    conn,
    external_id: str,
    *,
    corr: dict | None = None,
    payload: dict | list | None = None,
    event_type: str | None = "current_event",
) -> int:
    event_id = wf_engine.ingest_event(
        conn,
        source="synthetic",
        external_id=external_id,
        payload={} if payload is None else payload,
        corr={} if corr is None else corr,
        event_type=event_type,
    )
    assert event_id is not None
    return event_id


def _instance(
    conn,
    template_id: str,
    entity_key: str,
    *,
    corr: dict | None = None,
    vars: dict | None = None,
) -> str:
    return wf_engine.create_instance(
        conn,
        template_id=template_id,
        entity_key=entity_key,
        corr=corr or {"strong_key": entity_key, "weak_key": entity_key},
        vars=vars or {},
        source_event_id=None,
    )


def _at_awaiting(conn, task_id: str) -> None:
    setup = _event(
        conn,
        f"setup-{task_id}",
        corr={},
        payload={},
        event_type="worker_setup",
    )
    wf_engine.advance(conn, task_id, to_step="awaiting", event_id=setup)


def _status(conn, event_id: int) -> str:
    return conn.execute("SELECT status FROM wf_event WHERE id = ?", (event_id,)).fetchone()[0]


def _event_row(conn, event_id: int):
    return conn.execute("SELECT * FROM wf_event WHERE id = ?", (event_id,)).fetchone()


def _transition_count(conn, task_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM wf_transition WHERE task_id = ?", (task_id,)
    ).fetchone()[0]


def test_shared_booking_ref_compatibility_then_declared_disambiguator(board):
    """A shared weak key resolves by compatibility, then by ``region``."""

    conn, template_id = board
    compatible = _instance(
        conn,
        template_id,
        "entity-compatible",
        corr={"strong_key": "strong-a", "weak_key": "shared"},
        vars={"region": "north"},
    )
    incompatible = _instance(
        conn,
        template_id,
        "entity-incompatible",
        corr={"strong_key": "strong-b", "weak_key": "shared"},
        vars={"region": "south"},
    )
    _at_awaiting(conn, compatible)
    _at_awaiting(conn, incompatible)
    conn.execute(
        "UPDATE wf_wait SET event_types = ? WHERE task_id = ?",
        (json.dumps(["other_event"]), incompatible),
    )

    event_id = _event(conn, "compatibility", corr={"weak_key": "shared"})
    result = wf_engine.match_event(conn, event_id)

    assert result.kind == "matched"
    assert result.task_id == compatible
    assert result.match_method == "deterministic"
    assert _status(conn, event_id) == "matched"
    assert _transition_count(conn, compatible) == 1  # setup only; match did not apply
    assert conn.execute(
        "SELECT current_step_key FROM tasks WHERE id = ?", (compatible,)
    ).fetchone()[0] == "awaiting"

    # Rebuild the shared-key case with both waits compatible.  Region selects
    # one deterministic candidate; no region keeps both candidates visible.
    conn.execute(
        "UPDATE wf_wait SET event_types = ? WHERE task_id = ?",
        (json.dumps(["current_event"]), incompatible),
    )
    selected = _event(
        conn,
        "disambiguated",
        corr={"weak_key": "shared"},
        payload={"region": "south"},
    )
    selected_result = wf_engine.match_event(conn, selected)
    assert selected_result.kind == "matched"
    assert selected_result.task_id == incompatible
    assert selected_result.candidate_task_ids == ()
    assert _status(conn, selected) == "matched"


def test_shared_booking_ref_remains_ambiguous_without_recency_fallback(board):
    conn, template_id = board
    first = _instance(
        conn,
        template_id,
        "entity-first",
        corr={"strong_key": "strong-a", "weak_key": "shared"},
        vars={"region": "north"},
    )
    second = _instance(
        conn,
        template_id,
        "entity-second",
        corr={"strong_key": "strong-b", "weak_key": "shared"},
        vars={"region": "south"},
    )
    _at_awaiting(conn, first)
    _at_awaiting(conn, second)
    event_id = _event(conn, "ambiguous-shared", corr={"weak_key": "shared"})
    conn.execute("UPDATE wf_event SET created_at = ? WHERE id = ?", (FIXED_NOW + 1, event_id))

    result = wf_engine.match_event(conn, event_id)

    assert result.kind == "ambiguous"
    assert result.match_method == "deterministic"
    assert set(result.candidate_task_ids) == {first, second}
    assert _status(conn, event_id) == "ambiguous"
    assert _transition_count(conn, first) == 1
    assert _transition_count(conn, second) == 1
    assert conn.execute(
        "SELECT matched_task_id FROM wf_event WHERE id = ?", (event_id,)
    ).fetchone()[0] is None


def test_event_matches_two_exposes_human_resolution_data(board):
    """Two compatible candidates produce a deterministic human-pick surface."""

    conn, template_id = board
    task_ids = [
        _instance(
            conn,
            template_id,
            entity,
            corr={"strong_key": strong, "weak_key": "shared"},
            vars={"region": region},
        )
        for entity, strong, region in (
            ("entity-north", "strong-north", "north"),
            ("entity-south", "strong-south", "south"),
        )
    ]
    for task_id in task_ids:
        _at_awaiting(conn, task_id)
    event_id = _event(
        conn,
        "two-match",
        corr={"weak_key": "shared"},
        payload={"summary": "choose one"},
    )

    result = wf_engine.match_event(conn, event_id)
    row = _event_row(conn, event_id)

    assert result.kind == "ambiguous"
    assert set(result.candidate_task_ids) == set(task_ids)
    assert result.match_method == "deterministic"
    assert row["status"] == "ambiguous"
    assert row["matched_task_id"] is None
    assert json.loads(row["corr"]) == {"weak_key": "shared"}
    assert json.loads(row["payload"]) == {"summary": "choose one"}


def test_event_matches_nothing_create_unmatched_and_foreign_are_visible(board):
    conn, template_id = board

    create_id = _event(
        conn,
        "create-event",
        corr={"strong_key": "new-entity", "weak_key": "new-weak"},
        event_type="created_event",
    )
    created = wf_engine.match_event(conn, create_id)
    assert created.kind == "create"
    assert created.match_method == "deterministic:create"
    assert created.task_id
    assert _status(conn, create_id) == "matched"

    unmatched_id = _event(
        conn,
        "held-event",
        corr={"strong_key": "future-entity", "weak_key": "future-weak"},
        event_type="current_event",
    )
    unmatched = wf_engine.match_event(conn, unmatched_id)
    assert unmatched.kind == "unmatched"
    assert unmatched.match_method == "deterministic"
    assert _status(conn, unmatched_id) == "unmatched"
    assert wf_engine.match_event(conn, unmatched_id).kind == "unmatched"

    task_id = _instance(
        conn,
        template_id,
        "future-entity",
        corr={"strong_key": "future-entity", "weak_key": "future-weak"},
    )
    assert conn.execute(
        "SELECT matched_task_id, status FROM wf_event WHERE id = ?", (unmatched_id,)
    ).fetchone()[:] == (task_id, "buffered")
    _at_awaiting(conn, task_id)
    assert conn.execute(
        "SELECT matched_task_id, status FROM wf_event WHERE id = ?", (unmatched_id,)
    ).fetchone()[:] == (task_id, "matched")
    swept = wf_engine.sweep(conn, FIXED_NOW)
    assert unmatched_id not in swept.event_ids
    assert _transition_count(conn, task_id) == 1

    foreign_id = _event(conn, "foreign-event", corr={}, event_type="foreign_event")
    foreign = wf_engine.match_event(conn, foreign_id)
    assert foreign.kind == "unmatched"
    assert foreign.match_method == "deterministic"
    assert _status(conn, foreign_id) == "routed_out"
    assert _event_row(conn, foreign_id)["matched_task_id"] is None


def test_event_matches_nothing_aged_declared_event_enters_needs_review_path(board):
    """An aged declared unmatched event must not disappear from the ledger."""

    conn, _template_id = board
    event_id = _event(
        conn,
        "aged-unmatched",
        corr={"strong_key": "never-seen"},
        event_type="current_event",
    )
    wf_engine.match_event(conn, event_id)
    conn.execute(
        "UPDATE wf_event SET created_at = ? WHERE id = ?",
        (FIXED_NOW - wf_engine.DONE_RETENTION_SECONDS - 1, event_id),
    )

    result = wf_engine.sweep(conn, FIXED_NOW)

    assert event_id in result.needs_review_ids
    assert _status(conn, event_id) == "needs_review"


def test_out_of_order_future_buffers_then_sweep_rematches_after_advance(board):
    conn, template_id = board
    task_id = _instance(
        conn,
        template_id,
        "ordered-entity",
        corr={"strong_key": "ordered", "weak_key": "ordered-weak"},
        vars={"tracked_value": "initial"},
    )
    _at_awaiting(conn, task_id)

    future_id = _event(
        conn,
        "future-event",
        corr={"strong_key": "ordered"},
        event_type="future_event",
    )
    future_result = wf_engine.match_event(conn, future_id)
    assert future_result.kind == "buffered"
    assert future_result.task_id == task_id
    assert _status(conn, future_id) == "buffered"

    current_id = _event(
        conn,
        "current-event",
        corr={"strong_key": "ordered"},
        payload={"tracked_value": "initial"},
        event_type="current_event",
    )
    assert wf_engine.match_event(conn, current_id).kind == "matched"
    applied = wf_engine.apply_event(conn, current_id, task_id, expected_step="awaiting")
    assert applied.kind == "applied"
    assert conn.execute(
        "SELECT current_step_key FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()[0] == "processing"

    move_id = _event(conn, "move-to-future", corr={}, event_type="worker_setup")
    wf_engine.advance(conn, task_id, to_step="future", event_id=move_id)
    assert _status(conn, future_id) == "matched"

    swept = wf_engine.sweep(conn, FIXED_NOW)
    assert future_id not in swept.event_ids
    assert _status(conn, future_id) == "matched"
    assert _transition_count(conn, task_id) == 3  # setup + current + stage turn


def test_out_of_order_past_is_superseded_and_contradiction_needs_review(board):
    conn, template_id = board
    task_id = _instance(
        conn,
        template_id,
        "past-entity",
        corr={"strong_key": "past"},
        vars={"tracked_value": "initial"},
    )
    _at_awaiting(conn, task_id)
    current_id = _event(
        conn,
        "past-current",
        corr={"strong_key": "past"},
        payload={"tracked_value": "initial"},
        event_type="current_event",
    )
    assert wf_engine.match_event(conn, current_id).kind == "matched"
    assert wf_engine.apply_event(conn, current_id, task_id, expected_step="awaiting").applied
    move_id = _event(conn, "past-move", corr={}, event_type="worker_setup")
    wf_engine.advance(conn, task_id, to_step="future", event_id=move_id)

    past_id = _event(
        conn,
        "past-repeat",
        corr={"strong_key": "past"},
        payload={"tracked_value": "initial"},
        event_type="current_event",
    )
    contradiction_id = _event(
        conn,
        "past-contradiction",
        corr={"strong_key": "past"},
        payload={"tracked_value": "changed"},
        event_type="current_event",
    )

    past = wf_engine.match_event(conn, past_id)
    contradiction = wf_engine.match_event(conn, contradiction_id)
    assert past.kind == "superseded"
    assert past.task_id == task_id
    assert _status(conn, past_id) == "superseded"
    assert contradiction.kind == "needs_review"
    assert contradiction.task_id == task_id
    assert contradiction.candidate_task_ids == ()
    assert _status(conn, contradiction_id) == "needs_review"


def test_duplicate_events_are_deduped_and_satisfied_wait_is_superseded(board):
    conn, template_id = board
    task_id = _instance(conn, template_id, "duplicate-entity", corr={"strong_key": "duplicate"})
    _at_awaiting(conn, task_id)

    first_id = _event(conn, "same-external", corr={"strong_key": "duplicate"})
    duplicate_id = wf_engine.ingest_event(
        conn,
        source="synthetic",
        external_id="same-external",
        payload={},
        corr={"strong_key": "duplicate"},
        event_type="current_event",
    )
    assert duplicate_id is None
    assert conn.execute(
        "SELECT COUNT(*) FROM wf_event WHERE source = 'synthetic' AND external_id = 'same-external'"
    ).fetchone()[0] == 1

    assert wf_engine.match_event(conn, first_id).kind == "matched"
    assert wf_engine.apply_event(conn, first_id, task_id, expected_step="awaiting").applied

    forwarded_id = _event(conn, "different-external", corr={"strong_key": "duplicate"})
    forwarded = wf_engine.match_event(conn, forwarded_id)
    assert forwarded.kind == "superseded"
    assert forwarded.task_id == task_id
    assert _status(conn, forwarded_id) == "superseded"


def test_duplicate_transition_and_stale_apply_never_double_transition(board):
    conn, template_id = board
    task_id = _instance(conn, template_id, "stale-entity", corr={"strong_key": "stale"})
    _at_awaiting(conn, task_id)
    event_id = _event(conn, "stale-event", corr={"strong_key": "stale"})
    assert wf_engine.match_event(conn, event_id).kind == "matched"
    assert wf_engine.apply_event(conn, event_id, task_id, expected_step="awaiting").applied
    transitions = _transition_count(conn, task_id)

    replay = wf_engine.apply_event(conn, event_id, task_id, expected_step="awaiting")
    stale = wf_engine.apply_event(conn, event_id, task_id, expected_step="start")

    assert replay.kind == "re_correlate"
    assert stale.kind == "re_correlate"
    assert _transition_count(conn, task_id) == transitions
    assert conn.execute(
        "SELECT COUNT(*) FROM wf_transition WHERE task_id = ? AND event_id = ?",
        (task_id, event_id),
    ).fetchone()[0] == 1


def _finish(conn, task_id: str) -> None:
    future_id = _event(conn, f"finish-{task_id}", corr={}, event_type="worker_setup")
    wf_engine.advance(conn, task_id, to_step="future", event_id=future_id)
    terminal_id = _event(conn, f"terminal-{task_id}", corr={}, event_type="worker_setup")
    wf_engine.advance(conn, task_id, to_step="done", event_id=terminal_id)


def test_done_instance_retention_is_soft_and_create_signal_starts_new_instance(board):
    conn, template_id = board
    task_id = _instance(
        conn,
        template_id,
        "done-entity",
        corr={"strong_key": "done-key"},
        vars={"tracked_value": "initial"},
    )
    _at_awaiting(conn, task_id)
    current_id = _event(conn, "done-current", corr={"strong_key": "done-key"}, payload={"tracked_value": "initial"})
    assert wf_engine.match_event(conn, current_id).kind == "matched"
    assert wf_engine.apply_event(conn, current_id, task_id, expected_step="awaiting").applied
    move_id = _event(conn, "review-move", corr={}, event_type="worker_setup")
    wf_engine.advance(conn, task_id, to_step="future", event_id=move_id)
    future_id = _event(conn, "done-future", corr={"strong_key": "done-key"}, event_type="future_event")
    assert wf_engine.match_event(conn, future_id).kind == "matched"
    assert wf_engine.apply_event(conn, future_id, task_id, expected_step="future").applied
    finish_id = _event(conn, "done-finish", corr={}, event_type="worker_setup")
    wf_engine.advance(conn, task_id, to_step="done", event_id=finish_id)
    assert conn.execute(
        "SELECT state FROM wf_instance WHERE task_id = ?", (task_id,)
    ).fetchone()[0] == "done"

    late_id = _event(conn, "late-normal", corr={"strong_key": "done-key"})
    late = wf_engine.match_event(conn, late_id)
    assert late.kind == "superseded"
    assert late.task_id == task_id
    assert _status(conn, late_id) == "superseded"

    create_id = _event(
        conn,
        "repeat-create",
        corr={"strong_key": "done-key"},
        event_type="created_event",
    )
    created = wf_engine.match_event(conn, create_id)
    assert created.kind == "create"
    assert created.task_id != task_id
    assert _status(conn, create_id) == "matched"
    assert conn.execute("SELECT COUNT(*) FROM wf_instance").fetchone()[0] == 2


def test_done_instance_mutation_and_contradiction_need_review(board):
    conn, template_id = board
    task_id = _instance(
        conn,
        template_id,
        "done-review-entity",
        corr={"strong_key": "done-review"},
        vars={"tracked_value": "initial"},
    )
    _at_awaiting(conn, task_id)
    current_id = _event(
        conn,
        "review-current",
        corr={"strong_key": "done-review"},
        payload={"tracked_value": "initial"},
    )
    assert wf_engine.match_event(conn, current_id).kind == "matched"
    assert wf_engine.apply_event(conn, current_id, task_id, expected_step="awaiting").applied
    move_id = _event(conn, "review-move", corr={}, event_type="worker_setup")
    wf_engine.advance(conn, task_id, to_step="future", event_id=move_id)
    future_id = _event(conn, "review-future", corr={"strong_key": "done-review"}, event_type="future_event")
    assert wf_engine.match_event(conn, future_id).kind == "matched"
    assert wf_engine.apply_event(conn, future_id, task_id, expected_step="future").applied
    finish_id = _event(conn, "review-finish", corr={}, event_type="worker_setup")
    wf_engine.advance(conn, task_id, to_step="done", event_id=finish_id)

    mutation_id = _event(
        conn,
        "done-mutation",
        corr={"strong_key": "done-review"},
        payload={"mutation": True},
    )
    contradiction_id = _event(
        conn,
        "done-contradiction",
        corr={"strong_key": "done-review"},
        payload={"tracked_value": "changed"},
    )
    mutation = wf_engine.match_event(conn, mutation_id)
    contradiction = wf_engine.match_event(conn, contradiction_id)
    assert mutation.kind == "needs_review"
    assert mutation.task_id == task_id
    assert _status(conn, mutation_id) == "needs_review"
    assert contradiction.kind == "needs_review"
    assert contradiction.task_id == task_id
    assert _status(conn, contradiction_id) == "needs_review"


def test_schema_invalid_payload_is_needs_review_not_a_guessed_match(board):
    """Invalid typed ledger payloads must stop at the human-review path."""

    conn, template_id = board
    task_id = _instance(conn, template_id, "schema-entity", corr={"strong_key": "schema"})
    _at_awaiting(conn, task_id)
    event_id = _event(conn, "invalid-typed", corr={"strong_key": "schema"}, payload=["not", "an", "object"])
    result = wf_engine.match_event(conn, event_id)

    assert result.kind == "needs_review"
    assert result.task_id == task_id
    assert result.match_method == "deterministic"
    assert _status(conn, event_id) == "needs_review"
    assert _transition_count(conn, task_id) == 1


def test_match_status_methods_and_candidate_payload_are_exact(board):
    conn, template_id = board
    task_id = _instance(conn, template_id, "exact-entity", corr={"strong_key": "exact"})
    _at_awaiting(conn, task_id)

    event_id = _event(conn, "exact-match", corr={"strong_key": "exact"})
    result = wf_engine.match_event(conn, event_id)
    row = _event_row(conn, event_id)
    assert result.kind == "matched"
    assert result.match_method == "deterministic"
    assert _status(conn, event_id) == "matched"
    assert row["matched_task_id"] == task_id
    assert _transition_count(conn, task_id) == 1

    # Matching alone never applies: the task remains parked and the ledger
    # event is not marked applied until the explicit apply API is called.
    assert conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()[0] == "blocked"
    assert _status(conn, event_id) != "applied"


HARD_CASE_SCENARIOS = {
    "shared_booking_ref": test_shared_booking_ref_compatibility_then_declared_disambiguator,
    "event_matches_nothing": test_event_matches_nothing_create_unmatched_and_foreign_are_visible,
    "event_matches_two": test_event_matches_two_exposes_human_resolution_data,
    "out_of_order_events": test_out_of_order_future_buffers_then_sweep_rematches_after_advance,
    "duplicate_events": test_duplicate_events_are_deduped_and_satisfied_wait_is_superseded,
    "event_for_done_instance": test_done_instance_retention_is_soft_and_create_signal_starts_new_instance,
}


@pytest.mark.parametrize("row_id", HARD_CASE_ROWS)
def test_hard_case_rows_have_named_executable_scenarios(row_id):
    """Coverage guard: every frozen top-level row has a named test below."""

    assert len(HARD_CASE_ROWS) == 6
    assert len(set(HARD_CASE_ROWS)) == 6
    assert set(HARD_CASE_SCENARIOS) == set(HARD_CASE_ROWS)
    assert callable(HARD_CASE_SCENARIOS[row_id])
