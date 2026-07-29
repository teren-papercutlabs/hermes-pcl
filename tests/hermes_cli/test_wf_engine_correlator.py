"""Deterministic workflow correlator, timer, and sweep coverage."""

from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db
from hermes_cli import wf_engine


def _conn(tmp_path):
    return kanban_db.connect(tmp_path / "board.sqlite")


def _spec():
    return {
        "id": "correlator",
        "entity": "strong",
        "correlation_keys": ["strong", "weak"],
        "disambiguators": ["side"],
        "create_on": ["created"],
        "steps": [
            {"key": "start", "turn": {"brief": "start"}, "advance_to": "wait"},
            {
                "key": "wait",
                "waits": [
                    {"kind": "event", "types": ["current"], "advance_to": "future"},
                    {"kind": "event", "types": ["old"], "advance_to": "future"},
                    {"kind": "timer", "after": 60, "action": "chase", "max_fires": 1, "advance_to": "future"},
                ],
            },
            {
                "key": "future",
                "waits": [
                    {"kind": "event", "types": ["future"], "advance_to": "done"},
                    {"kind": "event", "types": ["old"], "advance_to": "done"},
                ],
            },
            {"key": "done"},
        ],
    }


def _registered(tmp_path):
    conn = _conn(tmp_path)
    template_id, _ = wf_engine.register_template(conn, _spec())
    return conn, template_id


def _instance(conn, template_id, entity, *, corr=None, vars=None):
    return wf_engine.create_instance(
        conn,
        template_id=template_id,
        entity_key=entity,
        corr=corr or {"strong": entity, "weak": entity},
        vars=vars or {},
        source_event_id=None,
    )


def _at_wait(conn, task_id):
    setup = wf_engine.ingest_event(
        conn,
        source="worker",
        external_id=f"setup-{task_id}",
        payload={},
        corr={},
        event_type="worker",
    )
    wf_engine.advance(conn, task_id, to_step="wait", event_id=setup)


def _event(conn, external_id, *, corr, event_type, payload=None, created_at=None):
    event_id = wf_engine.ingest_event(
        conn,
        source="synthetic",
        external_id=external_id,
        payload=payload or {},
        corr=corr,
        event_type=event_type,
    )
    if created_at is not None:
        conn.execute("UPDATE wf_event SET created_at = ? WHERE id = ?", (created_at, event_id))
    return event_id


def test_strength_order_does_not_fall_back_to_weaker_candidate(tmp_path):
    conn, template_id = _registered(tmp_path)
    try:
        strong = _instance(conn, template_id, "strong-entity", corr={"strong": "S", "weak": "A"})
        weak = _instance(conn, template_id, "weak-entity", corr={"strong": "other", "weak": "W"})
        _at_wait(conn, strong)
        _at_wait(conn, weak)
        event_id = _event(
            conn,
            "strength",
            corr={"strong": "S", "weak": "W"},
            event_type="future",
        )
        result = wf_engine.match_event(conn, event_id)
        assert result.kind == "buffered"
        assert result.task_id == strong
        assert conn.execute("SELECT matched_task_id, status FROM wf_event WHERE id = ?", (event_id,)).fetchone()[:] == (strong, "buffered")
    finally:
        conn.close()


def test_compatibility_resolves_candidates_without_recency(tmp_path):
    conn, template_id = _registered(tmp_path)
    try:
        compatible = _instance(conn, template_id, "compatible", corr={"strong": "same", "weak": "a"})
        incompatible = _instance(conn, template_id, "incompatible", corr={"strong": "same", "weak": "b"})
        _at_wait(conn, compatible)
        _at_wait(conn, incompatible)
        conn.execute(
            "UPDATE wf_wait SET event_types = ? WHERE task_id = ? AND kind = 'event'",
            (json.dumps(["other"]), incompatible),
        )
        event_id = _event(conn, "compat", corr={"strong": "same"}, event_type="current")
        result = wf_engine.match_event(conn, event_id)
        assert result.kind == "matched"
        assert result.task_id == compatible
    finally:
        conn.close()


def test_declared_disambiguator_selects_one_and_missing_value_stays_ambiguous(tmp_path):
    conn, template_id = _registered(tmp_path)
    try:
        first = _instance(conn, template_id, "first", corr={"strong": "same", "weak": "a"}, vars={"side": "one"})
        second = _instance(conn, template_id, "second", corr={"strong": "same", "weak": "b"}, vars={"side": "two"})
        _at_wait(conn, first)
        _at_wait(conn, second)
        selected = _event(conn, "selected", corr={"strong": "same"}, payload={"side": "two"}, event_type="current")
        result = wf_engine.match_event(conn, selected)
        assert result.kind == "matched"
        assert result.task_id == second

        unresolved = _event(conn, "unresolved", corr={"strong": "same"}, event_type="current")
        result = wf_engine.match_event(conn, unresolved)
        assert result.kind == "ambiguous"
        assert set(result.candidate_task_ids) == {first, second}
        assert conn.execute("SELECT status, matched_task_id FROM wf_event WHERE id = ?", (unresolved,)).fetchone()[:] == ("ambiguous", None)
    finally:
        conn.close()


def test_create_unmatched_and_routed_out_are_visible(tmp_path):
    conn, template_id = _registered(tmp_path)
    try:
        created = _event(conn, "create", corr={"strong": "new"}, event_type="created")
        result = wf_engine.match_event(conn, created)
        assert result.kind == "create"
        assert result.task_id
        assert conn.execute("SELECT status, match_method FROM wf_event WHERE id = ?", (created,)).fetchone()[:] == ("matched", "deterministic:create")
        assert wf_engine.match_event(conn, created).kind == "create"

        unmatched = _event(conn, "unmatched", corr={"strong": "missing"}, event_type="current")
        assert wf_engine.match_event(conn, unmatched).kind == "unmatched"
        assert conn.execute("SELECT status FROM wf_event WHERE id = ?", (unmatched,)).fetchone()[0] == "unmatched"

        foreign = _event(conn, "foreign", corr={}, event_type="unknown")
        assert wf_engine.match_event(conn, foreign).kind == "unmatched"
        assert conn.execute("SELECT status FROM wf_event WHERE id = ?", (foreign,)).fetchone()[0] == "routed_out"
    finally:
        conn.close()


def test_future_is_buffered_past_is_superseded_and_contradiction_needs_review(tmp_path):
    conn, template_id = _registered(tmp_path)
    try:
        task_id = _instance(conn, template_id, "ordered", corr={"strong": "ordered"}, vars={"known": "old"})
        _at_wait(conn, task_id)
        future = _event(conn, "future", corr={"strong": "ordered"}, event_type="future")
        assert wf_engine.match_event(conn, future).kind == "buffered"

        current = _event(conn, "current", corr={"strong": "ordered"}, payload={"known": "old"}, event_type="current")
        assert wf_engine.match_event(conn, current).kind == "matched"
        assert wf_engine.apply_event(conn, current, task_id, expected_step="wait").kind == "applied"

        past = _event(conn, "past", corr={"strong": "ordered"}, event_type="old")
        assert wf_engine.match_event(conn, past).kind == "superseded"

        contradiction = _event(conn, "contradiction", corr={"strong": "ordered"}, payload={"known": "new"}, event_type="old")
        result = wf_engine.match_event(conn, contradiction)
        assert result.kind == "needs_review"
        assert result.task_id == task_id
    finally:
        conn.close()


def test_done_retention_is_soft_and_expires(tmp_path):
    conn, template_id = _registered(tmp_path)
    try:
        task_id = _instance(conn, template_id, "done-entity", corr={"strong": "done"}, vars={"known": "old"})
        _at_wait(conn, task_id)
        current = _event(conn, "done-current", corr={"strong": "done"}, event_type="current")
        assert wf_engine.match_event(conn, current).kind == "matched"
        wf_engine.apply_event(conn, current, task_id, expected_step="wait")
        finish = _event(conn, "finish", corr={}, event_type="worker")
        wf_engine.advance(conn, task_id, to_step="done", event_id=finish)

        late = _event(conn, "late", corr={"strong": "done"}, event_type="current")
        assert wf_engine.match_event(conn, late).kind == "superseded"
        mutation = _event(conn, "late-mutation", corr={"strong": "done"}, payload={"mutation": True}, event_type="current")
        assert wf_engine.match_event(conn, mutation).kind == "needs_review"

        conn.execute("UPDATE tasks SET completed_at = ? WHERE id = ?", (int(wf_engine._now()) - wf_engine.DONE_RETENTION_SECONDS - 1, task_id))
        expired = _event(conn, "expired", corr={"strong": "done"}, event_type="current")
        assert wf_engine.match_event(conn, expired).kind == "unmatched"
    finally:
        conn.close()


def test_timer_firing_is_deduped_and_cancelled_waits_do_not_fire(tmp_path):
    conn, template_id = _registered(tmp_path)
    try:
        task_id = _instance(conn, template_id, "timed", corr={"strong": "timed"})
        _at_wait(conn, task_id)
        timer = conn.execute("SELECT id, timer_at FROM wf_wait WHERE task_id = ? AND kind = 'timer'", (task_id,)).fetchone()
        first = wf_engine.fire_due_timers(conn, timer["timer_at"])
        assert len(first) == 1
        assert wf_engine.fire_due_timers(conn, timer["timer_at"]) == []
        event = conn.execute("SELECT source, external_id, status FROM wf_event WHERE id = ?", (first[0],)).fetchone()
        assert tuple(event) == ("timer", f"wf:{task_id}:wait:{timer['id']}:1", "received")
        assert conn.execute("SELECT fires_used FROM wf_wait WHERE id = ?", (timer["id"],)).fetchone()[0] == 1

        finish = _event(conn, "timer-finish", corr={}, event_type="worker")
        wf_engine.advance(conn, task_id, to_step="done", event_id=finish)
        assert wf_engine.fire_due_timers(conn, timer["timer_at"] + 1000) == []
    finally:
        conn.close()


def test_bounded_sweep_rematches_without_applying(tmp_path, monkeypatch):
    conn, template_id = _registered(tmp_path)
    try:
        task_id = _instance(conn, template_id, "sweep", corr={"strong": "sweep"})
        _at_wait(conn, task_id)
        now = int(wf_engine._now())
        recent = _event(conn, "recent", corr={"strong": "sweep"}, event_type="current", created_at=now)
        old = _event(conn, "old-sweep", corr={"strong": "sweep"}, event_type="current", created_at=now - wf_engine.DONE_RETENTION_SECONDS - 1)

        def fail_apply(*_args, **_kwargs):
            raise AssertionError("match must not apply")

        monkeypatch.setattr(wf_engine, "apply_event", fail_apply)
        result = wf_engine.sweep(conn, now)
        setup = 1
        assert result.processed == 2
        assert result.event_ids == (setup, recent)
        assert result.matched_ids == (recent,)
        assert conn.execute("SELECT status FROM wf_event WHERE id = ?", (recent,)).fetchone()[0] == "matched"
        assert conn.execute("SELECT status FROM wf_event WHERE id = ?", (old,)).fetchone()[0] == "received"
    finally:
        conn.close()


def test_advance_rematches_held_event_without_recursion(tmp_path):
    conn, template_id = _registered(tmp_path)
    try:
        task_id = _instance(conn, template_id, "rematch", corr={"strong": "rematch"})
        held = _event(conn, "held", corr={"strong": "rematch"}, event_type="future")
        assert wf_engine.match_event(conn, held).kind == "buffered"
        setup = _event(conn, "rematch-setup", corr={}, event_type="worker")
        # Move into the step that accepts the held event. The re-match helper
        # changes only the ledger classification; application remains explicit.
        wf_engine.advance(conn, task_id, to_step="wait", event_id=setup)
        second = _event(conn, "rematch-second", corr={}, event_type="worker")
        wf_engine.advance(conn, task_id, to_step="future", event_id=second)
        assert conn.execute("SELECT status, matched_task_id FROM wf_event WHERE id = ?", (held,)).fetchone()[:] == ("matched", task_id)
    finally:
        conn.close()
