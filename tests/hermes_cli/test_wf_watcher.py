"""Synthetic acceptance coverage for the gateway workflow watcher."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_db, wf_engine, wf_watcher


NOW = 1_785_100_000


def _spec(timer: dict | None = None) -> dict:
    waits: list[dict] = [
        {
            "kind": "event",
            "types": ["observed"],
            "schema": "observation-v1",
            "advance_to": "done",
        }
    ]
    if timer:
        waits.append({"kind": "timer", **timer})
    return {
        "id": "watcher-test",
        "entity": "entity",
        "correlation_keys": ["entity"],
        "disambiguators": [],
        "create_on": [],
        "steps": [
            {"key": "start", "advance_to": "wait"},
            {"key": "wait", "waits": waits},
            {"key": "done"},
        ],
    }


def _board(tmp_path, monkeypatch, timer: dict | None = None):
    monkeypatch.setattr(wf_engine, "_now", lambda: NOW)
    path = tmp_path / "workflow.sqlite"
    conn = kanban_db.connect(path)
    template_id, _ = wf_engine.register_template(conn, _spec(timer))
    task_id = wf_engine.create_instance(
        conn,
        template_id=template_id,
        entity_key="entity-1",
        corr={"entity": "entity-1"},
        vars={},
        source_event_id=None,
    )
    conn.execute("UPDATE tasks SET tenant = 'tenant' WHERE id = ?", (task_id,))
    setup = wf_engine.ingest_event(
        conn,
        source="synthetic",
        external_id="setup",
        payload={},
        corr={},
        event_type="setup",
    )
    assert setup is not None
    wf_engine.advance(conn, task_id, to_step="wait", event_id=setup)
    return path, conn, task_id


def _row(conn: sqlite3.Connection, sql: str, *params):
    return conn.execute(sql, params).fetchone()


def test_due_timer_applies_once_and_event_race_cancels_timer(tmp_path, monkeypatch):
    _path, conn, task_id = _board(
        tmp_path,
        monkeypatch,
        {"after": 30, "action": "advance", "advance_to": "done"},
    )
    try:
        due = _row(
            conn,
            "SELECT timer_at FROM wf_wait WHERE task_id = ? AND kind = 'timer'",
            task_id,
        )[0]
        first = wf_watcher.run_tick(conn, due)
        second = wf_watcher.run_tick(conn, due + 60)

        assert len(first.timers_fired) == 1
        assert first.timer_results == ("applied",)
        assert second.timers_fired == ()
        assert tuple(_row(
            conn,
            "SELECT current_step_key, status FROM tasks WHERE id = ?",
            task_id,
        )) == ("done", "ready")
        assert _row(
            conn,
            "SELECT status FROM wf_wait WHERE task_id = ? AND kind = 'timer'",
            task_id,
        )[0] == "satisfied"
    finally:
        conn.close()

    # The opposite side of the race: an observed state wins first and
    # supersedes the due timer before the watcher scans it.
    race_path = tmp_path / "race"
    race_path.mkdir()
    _path, conn, task_id = _board(
        race_path,
        monkeypatch,
        {"after": 30, "action": "advance", "advance_to": "done"},
    )
    try:
        event_id = wf_engine.ingest_event(
            conn,
            source="state_poll",
            external_id="state-won",
            payload={},
            corr={"entity": "entity-1"},
            event_type="observed",
        )
        assert event_id is not None
        assert wf_engine.match_event(conn, event_id).kind == "matched"
        assert wf_engine.apply_event(
            conn, event_id, task_id, expected_step="wait"
        ).kind == "applied"
        due = NOW + 30
        result = wf_watcher.run_tick(conn, due)
        assert result.timers_fired == ()
        assert _row(
            conn,
            "SELECT status FROM wf_wait WHERE task_id = ? AND kind = 'timer'",
            task_id,
        )[0] == "superseded"
    finally:
        conn.close()


def test_chase_cap_escalates_and_double_ticks_noop(tmp_path, monkeypatch):
    _path, conn, task_id = _board(
        tmp_path,
        monkeypatch,
        {
            "after": 10,
            "action": "chase",
            "max_fires": 3,
            "then": "escalate",
            "advance_to": "wait",
        },
    )
    try:
        due = NOW + 10
        results = [
            wf_watcher.run_tick(conn, due),
            wf_watcher.run_tick(conn, due + 10),
            wf_watcher.run_tick(conn, due + 20),
        ]
        assert [result.timer_results for result in results] == [
            ("chase",),
            ("chase",),
            ("exception",),
        ]
        assert _row(
            conn,
            "SELECT state FROM wf_instance WHERE task_id = ?",
            task_id,
        )[0] == "exception"
        assert _row(
            conn,
            "SELECT COUNT(*) FROM wf_outbox WHERE task_id = ? AND action = 'chase'",
            task_id,
        )[0] == 3
        assert wf_watcher.run_tick(conn, due + 1_000).timers_fired == ()
        assert _row(
            conn,
            "SELECT COUNT(*) FROM wf_event WHERE source = 'timer'",
        )[0] == 3
        assert _row(
            conn,
            """
            SELECT COUNT(*) FROM task_events
             WHERE task_id = ? AND kind = 'blocked'
               AND json_extract(payload, '$.source') = 'workflow_chase_cap'
            """,
            task_id,
        )[0] == 1
    finally:
        conn.close()


def test_stuck_deadline_enters_resumable_exception(tmp_path, monkeypatch):
    _path, conn, task_id = _board(
        tmp_path,
        monkeypatch,
        {"after": 60, "action": "deadline", "advance_to": "done"},
    )
    try:
        result = wf_watcher.run_tick(conn, NOW + 60)
        assert result.timer_results == ("exception",)
        assert _row(
            conn,
            "SELECT state FROM wf_instance WHERE task_id = ?",
            task_id,
        )[0] == "exception"
        assert _row(
            conn,
            "SELECT status FROM tasks WHERE id = ?",
            task_id,
        )[0] == "blocked"
    finally:
        conn.close()


def test_read_only_tenant_probe_ingests_applies_and_dedupes(tmp_path, monkeypatch):
    _path, conn, task_id = _board(tmp_path, monkeypatch)
    seen = []

    def probe(targets):
        seen.append(targets)
        with pytest.raises(TypeError):
            targets[0].corr["entity"] = "mutated"
        return [
            wf_watcher.ProbeObservation(
                external_id="probe:entity-1:observed",
                event_type="observed",
                corr={"entity": "entity-1"},
                payload={"observed": True},
            )
        ]

    with pytest.raises(ValueError, match="read_only=True"):
        wf_watcher.register_state_probe("tenant", probe, read_only=False)
    wf_watcher.register_state_probe("tenant", probe, read_only=True)
    try:
        first = wf_watcher.run_tick(conn, NOW)
        second = wf_watcher.run_tick(conn, NOW + 60)
        assert len(seen) == 2
        assert len(first.poll_events) == 1
        assert first.applied_events == first.poll_events
        assert second.poll_events == ()
        assert second.poll_duplicates == 1
        assert tuple(_row(
            conn,
            "SELECT source, status FROM wf_event WHERE id = ?",
            first.poll_events[0],
        )) == ("state_poll", "applied")
        assert _row(
            conn,
            "SELECT current_step_key FROM tasks WHERE id = ?",
            task_id,
        )[0] == "done"
    finally:
        wf_watcher.unregister_state_probe("tenant")
        conn.close()


def test_raw_email_is_extracted_before_sweep(tmp_path, monkeypatch):
    _path, conn, task_id = _board(tmp_path, monkeypatch)
    event_id = wf_engine.ingest_event(
        conn,
        source="email",
        external_id="raw-email",
        payload={"body_ref": "/private/raw-email.txt"},
        corr={},
        event_type=None,
    )
    assert event_id is not None
    seen = []

    def boundary(event):
        seen.append(event)
        return wf_watcher.ExtractionRequest(
            brief={"schema": "observation-v1"},
            extractor=lambda _brief, _event: {
                "event_type": "observed",
                "payload": {"result": "accepted"},
                "corr": {"entity": "entity-1"},
            },
            schema_validator={
                "observation-v1": lambda payload: payload == {"result": "accepted"}
            },
        )

    real_sweep = wf_engine.sweep

    def assert_extracted_before_sweep(target_conn, now):
        row = _row(
            target_conn,
            "SELECT status, event_type FROM wf_event WHERE id = ?",
            event_id,
        )
        assert tuple(row) == ("matched", "observed")
        return real_sweep(target_conn, now)

    monkeypatch.setattr(wf_engine, "sweep", assert_extracted_before_sweep)
    try:
        wf_watcher.run_tick(conn, NOW, extractor_boundary=boundary)
        assert len(seen) == 1
        assert seen[0]["id"] == event_id
        assert "candidates" not in seen[0]
        assert _row(
            conn, "SELECT status FROM wf_event WHERE id = ?", event_id
        )[0] == "applied"
        assert _row(
            conn, "SELECT current_step_key FROM tasks WHERE id = ?", task_id
        )[0] == "done"
    finally:
        conn.close()


def test_extractor_boundary_failure_is_needs_review_before_sweep(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(wf_engine, "_now", lambda: NOW)
    conn = kanban_db.connect(tmp_path / "workflow.sqlite")
    event_id = wf_engine.ingest_event(
        conn,
        source="email",
        external_id="broken-raw-email",
        payload={"body_ref": "/private/broken-email.txt"},
        corr={},
        event_type=None,
    )
    assert event_id is not None

    def broken_boundary(_event):
        raise RuntimeError("auxiliary model unavailable")

    real_sweep = wf_engine.sweep

    def assert_failed_closed_before_sweep(target_conn, now):
        assert _row(
            target_conn, "SELECT status FROM wf_event WHERE id = ?", event_id
        )[0] == "needs_review"
        return real_sweep(target_conn, now)

    monkeypatch.setattr(wf_engine, "sweep", assert_failed_closed_before_sweep)
    try:
        wf_watcher.run_tick(conn, NOW, extractor_boundary=broken_boundary)
        assert _row(
            conn, "SELECT status FROM wf_event WHERE id = ?", event_id
        )[0] == "needs_review"
    finally:
        conn.close()


def test_match_and_apply_roll_back_together_on_failure_and_restart_recovers(
    tmp_path, monkeypatch
):
    path, conn, task_id = _board(tmp_path, monkeypatch)
    event_id = wf_engine.ingest_event(
        conn,
        source="synthetic",
        external_id="crash-between-match-and-apply",
        payload={},
        corr={"entity": "entity-1"},
        event_type="observed",
    )
    assert event_id is not None
    real_apply_event = wf_engine.apply_event

    def fail_after_match(*_args, **_kwargs):
        raise RuntimeError("synthetic process exit after match")

    monkeypatch.setattr(wf_engine, "apply_event", fail_after_match)
    with pytest.raises(RuntimeError, match="synthetic process exit"):
        wf_watcher.run_tick(conn, NOW)
    # The sweep classification committed before the synthetic exit.  A
    # durable matched row is now itself part of the watcher's restart queue.
    assert _row(
        conn, "SELECT status FROM wf_event WHERE id = ?", event_id
    )[0] == "matched"
    conn.close()

    monkeypatch.setattr(wf_engine, "apply_event", real_apply_event)
    restarted = kanban_db.connect(path)
    try:
        caught_up = wf_watcher.run_tick(restarted, NOW + 60)
        assert event_id in caught_up.applied_events
        assert _row(
            restarted, "SELECT status FROM wf_event WHERE id = ?", event_id
        )[0] == "applied"
        assert _row(
            restarted,
            "SELECT current_step_key FROM tasks WHERE id = ?",
            task_id,
        )[0] == "done"
    finally:
        restarted.close()


def test_recorrelate_rolls_match_back_for_next_sweep(tmp_path, monkeypatch):
    _path, conn, task_id = _board(tmp_path, monkeypatch)
    event_id = wf_engine.ingest_event(
        conn,
        source="synthetic",
        external_id="cas-retry-after-match",
        payload={},
        corr={"entity": "entity-1"},
        event_type="observed",
    )
    assert event_id is not None
    real_apply_event = wf_engine.apply_event

    def request_recorrelation(_conn, target_event_id, target_task_id, **_kwargs):
        return wf_engine.ApplyResult(
            kind="re_correlate",
            task_id=target_task_id,
            event_id=target_event_id,
            reason="synthetic stage race",
        )

    monkeypatch.setattr(wf_engine, "apply_event", request_recorrelation)
    first = wf_watcher.run_tick(conn, NOW)
    assert event_id not in first.applied_events
    assert _row(
        conn, "SELECT status FROM wf_event WHERE id = ?", event_id
    )[0] == "received"

    monkeypatch.setattr(wf_engine, "apply_event", real_apply_event)
    second = wf_watcher.run_tick(conn, NOW + 60)
    assert event_id in second.applied_events
    assert _row(
        conn, "SELECT status FROM wf_event WHERE id = ?", event_id
    )[0] == "applied"
    assert _row(
        conn,
        "SELECT current_step_key FROM tasks WHERE id = ?",
        task_id,
    )[0] == "done"
    conn.close()


def test_sweeper_redrives_stuck_intake_and_restart_catches_overdue_timer(
    tmp_path, monkeypatch
):
    path, conn, task_id = _board(
        tmp_path,
        monkeypatch,
        {"after": 30, "action": "advance", "advance_to": "done"},
    )
    stuck = wf_engine.ingest_event(
        conn,
        source="synthetic",
        external_id="stuck-intake",
        payload={},
        corr={"entity": "entity-1"},
        event_type="observed",
    )
    assert stuck is not None
    first = wf_watcher.run_tick(conn, NOW)
    assert stuck in first.applied_events
    conn.close()

    # A fresh connection represents a restarted gateway.  The timer lost the
    # event race and was superseded durably, so restart catch-up cannot revive
    # it or double-apply the stage.
    restarted = kanban_db.connect(path)
    try:
        after_restart = wf_watcher.run_tick(restarted, NOW + 3_600)
        assert after_restart.timers_fired == ()
        assert _row(
            restarted,
            "SELECT current_step_key FROM tasks WHERE id = ?",
            task_id,
        )[0] == "done"
        assert _row(
            restarted,
            "SELECT COUNT(*) FROM wf_transition WHERE task_id = ? AND step_key = 'wait'",
            task_id,
        )[0] == 1
    finally:
        restarted.close()


def test_restart_catchup_fires_timer_missed_while_gateway_was_down(
    tmp_path, monkeypatch
):
    path, conn, task_id = _board(
        tmp_path,
        monkeypatch,
        {"after": 30, "action": "advance", "advance_to": "done"},
    )
    conn.close()

    restarted = kanban_db.connect(path)
    try:
        catchup = wf_watcher.run_tick(restarted, NOW + 3_600)
        repeated = wf_watcher.run_tick(restarted, NOW + 3_601)
        assert len(catchup.timers_fired) == 1
        assert catchup.timer_results == ("applied",)
        assert repeated.timers_fired == ()
        assert _row(
            restarted,
            "SELECT current_step_key FROM tasks WHERE id = ?",
            task_id,
        )[0] == "done"
    finally:
        restarted.close()


def test_restart_drains_timer_event_committed_before_processing(
    tmp_path, monkeypatch
):
    path, conn, task_id = _board(
        tmp_path,
        monkeypatch,
        {"after": 30, "action": "advance", "advance_to": "done"},
    )
    due = NOW + 30
    fired = wf_engine.fire_due_timers(conn, due)
    assert len(fired) == 1
    assert _row(
        conn, "SELECT status FROM wf_event WHERE id = ?", fired[0]
    )[0] == "received"
    conn.close()

    restarted = kanban_db.connect(path)
    try:
        caught_up = wf_watcher.run_tick(restarted, due + 60)
        repeated = wf_watcher.run_tick(restarted, due + 120)
        assert caught_up.timers_fired == ()
        assert caught_up.timer_results == ("applied",)
        assert repeated.timer_results == ()
        assert _row(
            restarted,
            "SELECT current_step_key FROM tasks WHERE id = ?",
            task_id,
        )[0] == "done"
        assert _row(
            restarted,
            "SELECT COUNT(*) FROM wf_transition WHERE task_id = ? AND step_key = 'wait'",
            task_id,
        )[0] == 1
    finally:
        restarted.close()


def test_bad_timer_and_probe_do_not_block_other_work(tmp_path, monkeypatch):
    _path, conn, task_id = _board(
        tmp_path,
        monkeypatch,
        {"after": 30, "action": "advance", "advance_to": "done"},
    )
    invalid_timer = wf_engine.ingest_event(
        conn,
        source="timer",
        external_id="invalid-timer",
        payload={"wait_id": -1, "fire": 1, "action": "advance"},
        corr={"task_id": task_id},
        event_type="advance",
    )
    stuck = wf_engine.ingest_event(
        conn,
        source="synthetic",
        external_id="stuck-observation",
        payload={},
        corr={"entity": "entity-1"},
        event_type="observed",
    )
    assert invalid_timer is not None
    assert stuck is not None

    real_process_timer_event = wf_engine.process_timer_event

    def isolate_invalid_timer(target_conn, event_id):
        if event_id == invalid_timer:
            raise RuntimeError("one malformed timer")
        return real_process_timer_event(target_conn, event_id)

    def broken_probe(_targets):
        yield {"external_id": "never-consumed", "event_type": "observed"}
        raise RuntimeError("tenant probe failed after yielding")

    monkeypatch.setattr(wf_engine, "process_timer_event", isolate_invalid_timer)
    wf_watcher.register_state_probe("tenant", broken_probe, read_only=True)
    try:
        result = wf_watcher.run_tick(conn, NOW)
        assert result.timer_errors == 1
        assert result.probe_errors == 1
        assert stuck in result.applied_events
        assert _row(
            conn,
            "SELECT current_step_key FROM tasks WHERE id = ?",
            task_id,
        )[0] == "done"
        assert _row(
            conn, "SELECT status FROM wf_event WHERE id = ?", invalid_timer
        )[0] == "received"
    finally:
        wf_watcher.unregister_state_probe("tenant")
        conn.close()


def test_repeating_non_chase_timer_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(wf_engine, "_now", lambda: NOW)
    conn = kanban_db.connect(tmp_path / "workflow.sqlite")
    with pytest.raises(ValueError, match="must use action='chase'"):
        wf_engine.register_template(
            conn,
            _spec(
                {
                    "after": 30,
                    "action": "advance",
                    "advance_to": "done",
                    "max_fires": 2,
                }
            ),
        )
    conn.close()
