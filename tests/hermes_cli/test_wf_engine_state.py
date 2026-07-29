"""State-machine tests for the atomic workflow engine wave."""

from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db
from hermes_cli import wf_engine


def _conn(tmp_path):
    return kanban_db.connect(tmp_path / "board.sqlite")


def _spec():
    return {
        "id": "state-flow",
        "steps": [
            {"key": "start", "turn": {"brief": "start"}, "advance_to": "waiting"},
            {
                "key": "waiting",
                "waits": [
                    {
                        "kind": "event",
                        "types": ["released"],
                        "schema": "release-v1",
                        "advance_to": "turn",
                    },
                    {
                        "kind": "timer",
                        "after": 3600,
                        "action": "chase",
                        "advance_to": "turn",
                    },
                ],
            },
            {"key": "turn", "turn": {"brief": "turn"}, "advance_to": "done"},
            {"key": "done"},
        ],
    }


def _registered(tmp_path):
    conn = _conn(tmp_path)
    template_id, _ = wf_engine.register_template(conn, _spec())
    return conn, template_id


def _instance(tmp_path, entity="entity-1"):
    conn, template_id = _registered(tmp_path)
    task_id = wf_engine.create_instance(
        conn,
        template_id=template_id,
        entity_key=entity,
        corr={"ref": entity},
        vars={"existing": 1},
        source_event_id=None,
    )
    return conn, template_id, task_id


def test_nested_write_txn_commit_and_inner_rollback_isolation(tmp_path):
    conn = _conn(tmp_path)
    try:
        with kanban_db.write_txn(conn):
            conn.execute(
                "INSERT INTO wf_event (source, external_id, status, created_at) "
                "VALUES ('test', 'outer', 'received', 1)"
            )
            with pytest.raises(ValueError):
                with kanban_db.write_txn(conn):
                    conn.execute(
                        "INSERT INTO wf_event (source, external_id, status, created_at) "
                        "VALUES ('test', 'inner', 'received', 1)"
                    )
                    raise ValueError("rollback savepoint")
            assert conn.execute(
                "SELECT COUNT(*) FROM wf_event WHERE external_id = 'inner'"
            ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM wf_event WHERE external_id = 'outer'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_create_retry_dedupes_by_entity_and_preserves_initial_invariant(tmp_path):
    conn, template_id = _registered(tmp_path)
    try:
        first = wf_engine.create_instance(
            conn, template_id=template_id, entity_key="same",
            corr={"r": "same"}, vars=None, source_event_id=None,
        )
        second = wf_engine.create_instance(
            conn, template_id=template_id, entity_key="same",
            corr={"r": "same"}, vars={"new": True}, source_event_id=None,
        )
        assert second == first
        assert conn.execute("SELECT COUNT(*) FROM wf_instance").fetchone()[0] == 1
        row = conn.execute(
            "SELECT status, workflow_template_id, current_step_key FROM tasks WHERE id = ?",
            (first,),
        ).fetchone()
        assert tuple(row) == ("ready", template_id, "start")
        wf_engine._assert_invariant(conn, first)
    finally:
        conn.close()


def test_context_pins_structure_but_reads_live_mode(tmp_path):
    conn = _conn(tmp_path)
    try:
        version_one = {
            "id": "posture-flow",
            "steps": [
                {
                    "key": "start",
                    "mode": "propose",
                    "turn": {"brief": "version-one"},
                    "actions": ["version-one-action"],
                    "waits": [
                        {
                            "kind": "event",
                            "types": ["version-one-event"],
                            "schema": "version-one-schema",
                            "advance_to": "finish-one",
                        }
                    ],
                    "advance_to": "finish-one",
                },
                {"key": "finish-one"},
            ],
        }
        pinned_template, _ = wf_engine.register_template(conn, version_one)
        task_id = wf_engine.create_instance(
            conn,
            template_id=pinned_template,
            entity_key="posture-entity",
            corr={},
            vars={},
            source_event_id=None,
        )

        version_two = {
            "id": "posture-flow",
            "steps": [
                {
                    "key": "start",
                    "mode": "auto",
                    "turn": {"brief": "version-two"},
                    "actions": ["version-two-action"],
                    "waits": [
                        {
                            "kind": "event",
                            "types": ["version-two-event"],
                            "schema": "version-two-schema",
                            "advance_to": "finish-two",
                        }
                    ],
                    "advance_to": "finish-two",
                },
                {"key": "finish-two"},
            ],
        }
        latest_template, _ = wf_engine.register_template(conn, version_two)

        context = wf_engine.context(conn, task_id)

        assert latest_template == "posture-flow@2"
        assert context["template_id"] == pinned_template == "posture-flow@1"
        assert context["step"] == {
            "key": "start",
            "mode": "auto",
            "turn": {"brief": "version-one"},
            "actions": ["version-one-action"],
            "waits": [
                {
                    "kind": "event",
                    "types": ["version-one-event"],
                    "schema": "version-one-schema",
                    "advance_to": "finish-one",
                }
            ],
            "advance_to": "finish-one",
        }
    finally:
        conn.close()


def test_assert_invariant_covers_every_frozen_pair(tmp_path):
    conn, _template_id, task_id = _instance(tmp_path)
    try:
        pairs = [
            ("ready", "advancing"),
            ("running", "advancing"),
            ("blocked", "parked"),
            ("blocked", "pending_approval"),
            ("blocked", "needs_review"),
            ("blocked", "exception"),
            ("done", "done"),
        ]
        for status, state in pairs:
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
            conn.execute("UPDATE wf_instance SET state = ? WHERE task_id = ?", (state, task_id))
            wf_engine._assert_invariant(conn, task_id)

        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        conn.execute("UPDATE wf_instance SET state = 'parked' WHERE task_id = ?", (task_id,))
        with pytest.raises(wf_engine.WorkflowInvariantError):
            wf_engine._assert_invariant(conn, task_id)
    finally:
        conn.close()


def test_park_arms_waits_blocks_and_rotates_tokens(tmp_path):
    conn, _template_id, task_id = _instance(tmp_path)
    try:
        wf_engine.park(
            conn,
            task_id,
            "start",
            [
                {"kind": "event", "types": ["released"], "schema": "release-v1", "advance_to": "waiting"},
                {"kind": "timer", "after": 60, "action": "chase", "advance_to": "waiting"},
            ],
        )
        task = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        instance = conn.execute("SELECT state FROM wf_instance WHERE task_id = ?", (task_id,)).fetchone()
        waits = conn.execute(
            "SELECT kind, status, resume_token FROM wf_wait WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        assert task["status"] == "blocked"
        assert instance["state"] == "parked"
        assert [row["kind"] for row in waits] == ["event", "timer"]
        assert all(row["status"] == "armed" and row["resume_token"] for row in waits)
        assert len({row["resume_token"] for row in waits}) == 2
        wf_engine._assert_invariant(conn, task_id)
    finally:
        conn.close()


def test_apply_atomic_transition_unblocks_merges_and_invalidates_tokens(tmp_path):
    conn, _template_id, task_id = _instance(tmp_path)
    try:
        setup_event = wf_engine.ingest_event(conn, source="worker", external_id="setup-1", payload={}, corr={}, event_type="worker")
        wf_engine.advance(conn, task_id, to_step="waiting", event_id=setup_event)
        event_id = wf_engine.ingest_event(
            conn,
            source="synthetic",
            external_id="message-1",
            payload={"new": 2},
            corr={"ref": "entity-1"},
            event_type="released",
        )
        result = wf_engine.apply_event(conn, event_id, task_id, expected_step="waiting")
        assert result.kind == "applied"
        assert result.applied is True
        assert result.to_step == "turn"
        assert conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0] == "ready"
        assert conn.execute("SELECT state FROM wf_instance WHERE task_id = ?", (task_id,)).fetchone()[0] == "advancing"
        event = conn.execute("SELECT status, matched_task_id FROM wf_event WHERE id = ?", (event_id,)).fetchone()
        assert tuple(event) == ("applied", task_id)
        waits = conn.execute(
            "SELECT status, resume_token FROM wf_wait WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        assert [row["status"] for row in waits] == ["satisfied", "superseded"]
        assert all(row["resume_token"] is None for row in waits)
        vars_value = json.loads(conn.execute("SELECT vars FROM wf_instance WHERE task_id = ?", (task_id,)).fetchone()[0])
        assert vars_value == {"existing": 1, "new": 2}
        wf_engine._assert_invariant(conn, task_id)
    finally:
        conn.close()


def test_exact_event_replay_and_stage_cas_miss_are_noops(tmp_path):
    conn, _template_id, task_id = _instance(tmp_path)
    try:
        setup_event = wf_engine.ingest_event(conn, source="worker", external_id="setup-2", payload={}, corr={}, event_type="worker")
        wf_engine.advance(conn, task_id, to_step="waiting", event_id=setup_event)
        kwargs = dict(source="synthetic", external_id="same-message", payload={}, corr={}, event_type="released")
        event_id = wf_engine.ingest_event(conn, **kwargs)
        assert wf_engine.ingest_event(conn, **kwargs) is None
        assert conn.execute("SELECT COUNT(*) FROM wf_event").fetchone()[0] == 2
        stale = wf_engine.apply_event(conn, event_id, task_id, expected_step="turn")
        assert stale.kind == "re_correlate"
        assert conn.execute("SELECT status FROM wf_event WHERE id = ?", (event_id,)).fetchone()[0] == "received"
    finally:
        conn.close()


def test_duplicate_transition_is_noop(tmp_path):
    conn, _template_id, task_id = _instance(tmp_path)
    try:
        setup_event = wf_engine.ingest_event(conn, source="worker", external_id="setup-3", payload={}, corr={}, event_type="worker")
        wf_engine.advance(conn, task_id, to_step="waiting", event_id=setup_event)
        event_id = wf_engine.ingest_event(conn, source="synthetic", external_id="duplicate", payload={}, corr={}, event_type="released")
        conn.execute(
            "INSERT INTO wf_transition (task_id, step_key, to_step, event_id, applied_at) VALUES (?, 'waiting', 'turn', ?, 1)",
            (task_id, event_id),
        )
        result = wf_engine.apply_event(conn, event_id, task_id, expected_step="waiting")
        assert result.kind == "duplicate"
        assert conn.execute("SELECT status FROM wf_event WHERE id = ?", (event_id,)).fetchone()[0] == "received"
        assert conn.execute("SELECT state FROM wf_instance WHERE task_id = ?", (task_id,)).fetchone()[0] == "parked"
    finally:
        conn.close()


def test_apply_exception_rolls_back_every_table(tmp_path, monkeypatch):
    conn, _template_id, task_id = _instance(tmp_path)
    try:
        setup_event = wf_engine.ingest_event(conn, source="worker", external_id="setup-4", payload={}, corr={}, event_type="worker")
        wf_engine.advance(conn, task_id, to_step="waiting", event_id=setup_event)
        event_id = wf_engine.ingest_event(conn, source="synthetic", external_id="rollback", payload={"lost": True}, corr={}, event_type="released")

        def fail_unblock(*_args, **_kwargs):
            raise RuntimeError("injected apply failure")

        monkeypatch.setattr(kanban_db, "unblock_task", fail_unblock)
        with pytest.raises(RuntimeError, match="injected apply failure"):
            wf_engine.apply_event(conn, event_id, task_id, expected_step="waiting")
        assert conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0] == "blocked"
        assert conn.execute("SELECT state FROM wf_instance WHERE task_id = ?", (task_id,)).fetchone()[0] == "parked"
        assert conn.execute("SELECT status FROM wf_event WHERE id = ?", (event_id,)).fetchone()[0] == "received"
        assert conn.execute("SELECT COUNT(*) FROM wf_transition WHERE event_id = ?", (event_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM wf_wait WHERE task_id = ? AND status = 'armed'", (task_id,)).fetchone()[0] == 2
        assert json.loads(conn.execute("SELECT vars FROM wf_instance WHERE task_id = ?", (task_id,)).fetchone()[0]) == {"existing": 1}
    finally:
        conn.close()


def test_advance_wait_turn_and_terminal_paths(tmp_path):
    conn, template_id, task_id = _instance(tmp_path)
    try:
        first_event = wf_engine.ingest_event(conn, source="worker", external_id="advance-1", payload={}, corr={}, event_type="worker")
        wf_engine.advance(conn, task_id, to_step="waiting", event_id=first_event)
        task = conn.execute("SELECT status, current_step_key FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert tuple(task) == ("blocked", "waiting")
        assert conn.execute("SELECT state FROM wf_instance WHERE task_id = ?", (task_id,)).fetchone()[0] == "parked"

        event_id = wf_engine.ingest_event(conn, source="synthetic", external_id="advance-2", payload={"x": 1}, corr={}, event_type="released")
        result = wf_engine.apply_event(conn, event_id, task_id, expected_step="waiting")
        assert result.kind == "applied"
        terminal_event = wf_engine.ingest_event(conn, source="worker", external_id="advance-3", payload={}, corr={}, event_type="worker")
        wf_engine.advance(conn, task_id, to_step="done", event_id=terminal_event)
        task = conn.execute("SELECT status, current_step_key FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert tuple(task) == ("done", "done")
        assert conn.execute("SELECT state FROM wf_instance WHERE task_id = ?", (task_id,)).fetchone()[0] == "done"
        assert conn.execute("SELECT COUNT(*) FROM wf_wait WHERE task_id = ? AND status = 'armed'", (task_id,)).fetchone()[0] == 0
        wf_engine._assert_invariant(conn, task_id)
    finally:
        conn.close()


def test_propose_review_and_exception_are_atomic_state_writers(tmp_path):
    conn, _template_id, propose_task = _instance(tmp_path, "propose")
    _conn2, _template2, review_task = _instance(tmp_path / "review", "review")
    _conn3, _template3, exception_task = _instance(tmp_path / "exception", "exception")
    try:
        approval_id = wf_engine.propose(conn, propose_task, "send", {"body": "complete"})
        approval = conn.execute("SELECT action, payload, status, resume_token FROM wf_approval WHERE id = ?", (approval_id,)).fetchone()
        assert tuple(approval[:3]) == ("send", '{"body":"complete"}', "pending")
        assert approval[3]
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (propose_task,)).fetchone()
        assert row[0] == "blocked"
        assert conn.execute("SELECT state FROM wf_instance WHERE task_id = ?", (propose_task,)).fetchone()[0] == "pending_approval"
        wf_engine._assert_invariant(conn, propose_task)

        # The other instances use their own connections because each helper
        # owns its outer transaction and the fixture keeps setup isolated.
        review_conn = _conn2
        wf_engine.review(review_conn, review_task, "ambiguous", options=["a", "b"])
        assert review_conn.execute("SELECT state FROM wf_instance WHERE task_id = ?", (review_task,)).fetchone()[0] == "needs_review"
        wf_engine._assert_invariant(review_conn, review_task)
        exception_conn = _conn3
        wf_engine.exception(exception_conn, exception_task, "deadline")
        assert exception_conn.execute("SELECT state FROM wf_instance WHERE task_id = ?", (exception_task,)).fetchone()[0] == "exception"
        wf_engine._assert_invariant(exception_conn, exception_task)
    finally:
        conn.close()
        _conn2.close()
        _conn3.close()
