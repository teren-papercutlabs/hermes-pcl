"""Focused acceptance tests for extraction, human review, and approvals."""

from __future__ import annotations

from hermes_cli import kanban_db, wf_engine


def _conn(tmp_path):
    return kanban_db.connect(tmp_path / "board.sqlite")


def _wait_spec():
    return {
        "id": "neutral-flow",
        "correlation_keys": ["key"],
        "steps": [
            {"key": "start", "turn": {"brief": "begin"}, "advance_to": "waiting"},
            {"key": "waiting", "waits": [{"kind": "event", "types": ["arrived"], "schema": "typed", "advance_to": "done"}]},
            {"key": "done"},
        ],
    }


def _approval_spec():
    return {
        "id": "approval-flow",
        "steps": [
            {"key": "start", "turn": {"brief": "begin"}, "advance_to": "done", "reject_to": "rejected"},
            {"key": "done"},
            {"key": "rejected"},
        ],
    }


def _instance(conn, template_id, key):
    return wf_engine.create_instance(
        conn, template_id=template_id, entity_key=key, corr={"key": key}, vars={}, source_event_id=None,
    )


def _at_waiting(conn, task_id):
    event_id = wf_engine.ingest_event(conn, source="worker", external_id=f"open-{task_id}", payload={}, corr={}, event_type="open")
    wf_engine.advance(conn, task_id, to_step="waiting", event_id=event_id)


def _ledgered(conn, external_id, payload={"raw": True}):
    return wf_engine.ingest_event(conn, source="intake", external_id=external_id, payload=payload, corr={}, event_type=None)


def test_extraction_validates_then_deterministically_matches(tmp_path):
    conn = _conn(tmp_path)
    try:
        template, _ = wf_engine.register_template(conn, _wait_spec())
        task_id = _instance(conn, template, "one")
        _at_waiting(conn, task_id)
        event_id = _ledgered(conn, "valid")

        seen = []
        def extractor(brief, event):
            seen.append((brief, set(event)))
            return {"event_type": "arrived", "payload": {"value": 1}, "corr": {"key": "one"}}

        result = wf_engine.extract_event(conn, event_id, {"schema": "typed"}, extractor, {"typed": lambda payload: payload == {"value": 1}})
        row = conn.execute("SELECT event_type, payload, corr, status FROM wf_event WHERE id = ?", (event_id,)).fetchone()
        assert result.kind == "matched" and result.task_id == task_id
        assert tuple(row) == ("arrived", '{"value":1}', '{"key":"one"}', "matched")
        assert seen[0][0] == {"schema": "typed"}
        assert "candidates" not in seen[0][1]
    finally:
        conn.close()


def test_extraction_failure_atomically_routes_to_review(tmp_path):
    conn = _conn(tmp_path)
    try:
        event_id = _ledgered(conn, "invalid")
        result = wf_engine.extract_event(conn, event_id, "typed", lambda *_: {"payload": [], "corr": {}}, {"typed": lambda _payload: True})
        row = conn.execute("SELECT event_type, payload, corr, status FROM wf_event WHERE id = ?", (event_id,)).fetchone()
        assert result.kind == "needs_review"
        assert tuple(row) == (None, '{"raw":true}', '{}', "needs_review")
    finally:
        conn.close()


def test_human_resolution_pick_and_neither(tmp_path):
    conn = _conn(tmp_path)
    try:
        template, _ = wf_engine.register_template(conn, _wait_spec())
        first, second = _instance(conn, template, "same-1"), _instance(conn, template, "same-2")
        _at_waiting(conn, first)
        _at_waiting(conn, second)
        event_id = wf_engine.ingest_event(conn, source="intake", external_id="ambiguous", payload={"x": 1}, corr={"key": "same-1"}, event_type="arrived")
        # Make both candidates share the declared key only after their setup events.
        conn.execute("UPDATE wf_instance SET corr = ? WHERE task_id = ?", ('{"key":"shared"}', first))
        conn.execute("UPDATE wf_instance SET corr = ? WHERE task_id = ?", ('{"key":"shared"}', second))
        conn.execute("UPDATE wf_event SET corr = ? WHERE id = ?", ('{"key":"shared"}', event_id))
        assert wf_engine.match_event(conn, event_id).kind == "ambiguous"
        picked = wf_engine.resolve_event(conn, event_id, second)
        event = conn.execute("SELECT status, matched_task_id, match_method FROM wf_event WHERE id = ?", (event_id,)).fetchone()
        assert picked.task_id == second
        assert tuple(event) == ("applied", second, "human")
        assert conn.execute("SELECT COUNT(*) FROM wf_event WHERE source = 'human_resolution'").fetchone()[0] == 1

        neither_id = wf_engine.ingest_event(conn, source="intake", external_id="neither", payload={}, corr={"key": "shared"}, event_type="arrived")
        # The first is still waiting; a second distinct unresolved candidate is enough to retain ambiguity.
        third = _instance(conn, template, "same-3")
        _at_waiting(conn, third)
        conn.execute("UPDATE wf_instance SET corr = ? WHERE task_id = ?", ('{"key":"shared"}', third))
        assert wf_engine.match_event(conn, neither_id).kind == "ambiguous"
        result = wf_engine.resolve_human_review(conn, neither_id, None)
        assert result.kind == "unmatched"
        assert tuple(conn.execute("SELECT status, matched_task_id FROM wf_event WHERE id = ?", (neither_id,)).fetchone()) == ("routed_out", None)
    finally:
        conn.close()


def test_approval_decisions_are_one_shot_and_exact(tmp_path):
    conn = _conn(tmp_path)
    try:
        template, _ = wf_engine.register_template(conn, _approval_spec())
        task_id = _instance(conn, template, "approve")
        approval_id = wf_engine.propose(conn, task_id, "deliver", {"body": "original"})
        token = conn.execute("SELECT resume_token FROM wf_approval WHERE id = ?", (approval_id,)).fetchone()[0]
        decision_event = wf_engine.decide_approval(conn, approval_id, token, "edited_approved", decided_by="reviewer", payload={"body": "edited", "extra": 1})
        approval = conn.execute("SELECT status, decided_by, decision_diff, resume_token FROM wf_approval WHERE id = ?", (approval_id,)).fetchone()
        outbox = conn.execute("SELECT action, payload, status FROM wf_outbox WHERE task_id = ?", (task_id,)).fetchone()
        assert decision_event is not None
        assert tuple(approval[:3]) == ("edited_approved", "reviewer", '[{"op":"replace","path":"/body","value":"edited"},{"op":"add","path":"/extra","value":1}]')
        assert approval[3] != token
        assert tuple(outbox) == ("deliver", '{"body":"edited","extra":1}', "queued")
        assert wf_engine.decide_approval(conn, approval_id, token, "edited_approved", decided_by="reviewer", payload={"body": "edited", "extra": 1}) is None
        assert conn.execute("SELECT COUNT(*) FROM wf_outbox WHERE task_id = ?", (task_id,)).fetchone()[0] == 1

        approved_task = _instance(conn, template, "approved")
        approved_id = wf_engine.propose(conn, approved_task, "deliver", {"body": "yes"})
        approved_token = conn.execute("SELECT resume_token FROM wf_approval WHERE id = ?", (approved_id,)).fetchone()[0]
        wf_engine.decide_approval(conn, approved_id, approved_token, "approved", decided_by="reviewer")
        assert conn.execute("SELECT status FROM wf_approval WHERE id = ?", (approved_id,)).fetchone()[0] == "approved"
        assert tuple(conn.execute("SELECT action, payload FROM wf_outbox WHERE task_id = ?", (approved_task,)).fetchone()) == ("deliver", '{"body":"yes"}')

        rejected_task = _instance(conn, template, "reject")
        rejected_id = wf_engine.propose(conn, rejected_task, "deliver", {"body": "no"})
        rejected_token = conn.execute("SELECT resume_token FROM wf_approval WHERE id = ?", (rejected_id,)).fetchone()[0]
        wf_engine.resolve_approval(conn, rejected_id, rejected_token, "rejected", decided_by="reviewer")
        assert conn.execute("SELECT status FROM wf_approval WHERE id = ?", (rejected_id,)).fetchone()[0] == "rejected"
        assert conn.execute("SELECT COUNT(*) FROM wf_outbox WHERE task_id = ?", (rejected_task,)).fetchone()[0] == 0
    finally:
        conn.close()


def test_park_exit_invalidates_pending_approval_tokens(tmp_path):
    conn = _conn(tmp_path)
    try:
        template, _ = wf_engine.register_template(conn, _wait_spec())
        task_id = _instance(conn, template, "invalidate")
        _at_waiting(conn, task_id)
        # Deliberately add an outstanding approval to model a stale capability
        # alongside the armed wait; leaving the parked stage must revoke both.
        approval_id = wf_engine.propose(conn, task_id, "deliver", {"x": 1})
        approval_token = conn.execute("SELECT resume_token FROM wf_approval WHERE id = ?", (approval_id,)).fetchone()[0]
        # Re-arm the parked waiting posture for the event path under test.
        conn.execute("UPDATE wf_instance SET state = 'parked' WHERE task_id = ?", (task_id,))
        event_id = wf_engine.ingest_event(conn, source="intake", external_id="leave", payload={}, corr={"key": "invalidate"}, event_type="arrived")
        assert wf_engine.apply_event(conn, event_id, task_id, expected_step="waiting").kind == "applied"
        assert conn.execute("SELECT resume_token FROM wf_approval WHERE id = ?", (approval_id,)).fetchone()[0] != approval_token
    finally:
        conn.close()
