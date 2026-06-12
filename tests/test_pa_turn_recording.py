"""Unit tests for PA universal turn-recording (PA-portal observability, Phase 1).

Covers:
  (a) the three new tables (pa_turns / pa_tool_calls / pa_events) + indexes
      are created;
  (b) recording a synthetic turn writes pa_turns + pa_tool_calls + pa_events
      rows with the correct universal fields;
  (c) events are PURELY agent-recorded — a turn the agent records nothing for
      has zero events (no mechanical floor), tool-calls still captured;
  (d) the record_event safety wrapper swallows a recording exception and does
      NOT propagate it (the live agent path must never break).

These are pure unit tests — no gateway / event loop required.
"""

import sqlite3

import pytest

from hermes_state import SessionDB
from gateway import pa_observability as po


# ── (a) schema ──────────────────────────────────────────────────────────


def test_pa_turn_tables_and_indexes_created(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        conn = sqlite3.connect(tmp_path / "state.db")
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "pa_turns" in tables
            assert "pa_tool_calls" in tables
            assert "pa_events" in tables

            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            assert "idx_pa_turns_agent" in indexes
            assert "idx_pa_turns_chat" in indexes
            assert "idx_pa_tool_calls_turn" in indexes
            assert "idx_pa_events_turn" in indexes
        finally:
            conn.close()
    finally:
        db.close()


def test_pa_turns_columns_match_universal_shape(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        conn = sqlite3.connect(tmp_path / "state.db")
        try:
            cols = {
                row[1]
                for row in conn.execute('PRAGMA table_info("pa_turns")').fetchall()
            }
            for expected in (
                "turn_id", "agent_id", "chat_id", "session_id",
                "message_refs_json", "model", "provider", "input_tokens",
                "output_tokens", "cost_usd", "turn_status", "error_json",
                "latency_ms", "raw_turn_envelope_json", "started_at",
                "completed_at",
            ):
                assert expected in cols, f"missing pa_turns column: {expected}"
        finally:
            conn.close()
    finally:
        db.close()


# ── (b) recording a synthetic turn ──────────────────────────────────────


def test_record_pa_turn_writes_all_three_tables(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        turn_id = db.record_pa_turn(
            turn_id="paturn_unit_1",
            agent_id="christopher",
            chat_id="chat-42",
            session_id="sess-1",
            message_refs=["m1", "m2"],
            model="claude-x",
            provider="anthropic",
            input_tokens=120,
            output_tokens=45,
            cost_usd=0.0031,
            turn_status="completed",
            error=None,
            latency_ms=812,
            raw_turn_envelope={"final_response": "ok"},
            started_at=100.0,
            completed_at=100.8,
            tool_calls=[
                {
                    "tool_name": "pa_business_read",
                    "input": {"operation": "case_lookup"},
                    "result": {"jobNo": "WC-9"},
                    "cost_usd": 0.0,
                    "duration_ms": 33,
                    "client_entity_pointer": "WC-9",
                },
            ],
            events=[
                {
                    "event_type": "case_update_confirmed",
                    "reason": "confirmed update to WC-9",
                    "evidence_message_refs": ["m2"],
                    "source": "agent_recorded",
                    "recorded_at": 100.7,
                },
            ],
        )
        assert turn_id == "paturn_unit_1"

        turns = db.list_pa_turns(agent_id="christopher")
        assert len(turns) == 1
        turn = turns[0]
        assert turn["turn_id"] == "paturn_unit_1"
        assert turn["agent_id"] == "christopher"
        assert turn["chat_id"] == "chat-42"
        assert turn["session_id"] == "sess-1"
        assert turn["model"] == "claude-x"
        assert turn["provider"] == "anthropic"
        assert turn["input_tokens"] == 120
        assert turn["output_tokens"] == 45
        assert turn["turn_status"] == "completed"
        assert turn["latency_ms"] == 812
        # JSON columns decode back to objects
        assert turn["message_refs"] == ["m1", "m2"]
        assert turn["raw_turn_envelope"] == {"final_response": "ok"}

        assert len(turn["tool_calls"]) == 1
        tc = turn["tool_calls"][0]
        assert tc["tool_name"] == "pa_business_read"
        assert tc["input"] == {"operation": "case_lookup"}
        assert tc["result"] == {"jobNo": "WC-9"}
        assert tc["client_entity_pointer"] == "WC-9"

        assert len(turn["events"]) == 1
        ev = turn["events"][0]
        assert ev["event_type"] == "case_update_confirmed"
        assert ev["source"] == "agent_recorded"
        assert ev["evidence_message_refs"] == ["m2"]
    finally:
        db.close()


def test_record_pa_turn_idempotent_on_same_turn_id(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        for _ in range(2):
            db.record_pa_turn(
                turn_id="paturn_dup",
                agent_id="a1",
                chat_id="c1",
                session_id="s1",
                tool_calls=[{"tool_name": "t"}],
                events=[{"event_type": "e", "source": "agent_recorded"}],
            )
        turns = db.list_pa_turns(agent_id="a1")
        assert len(turns) == 1  # not duplicated
        assert len(turns[0]["tool_calls"]) == 1  # children not duplicated
        assert len(turns[0]["events"]) == 1
    finally:
        db.close()


def test_list_pa_turns_scopes_by_chat(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.record_pa_turn(turn_id="t1", agent_id="a", chat_id="x", started_at=1.0)
        db.record_pa_turn(turn_id="t2", agent_id="a", chat_id="y", started_at=2.0)
        x_turns = db.list_pa_turns(agent_id="a", chat_id="x")
        assert [t["turn_id"] for t in x_turns] == ["t1"]
    finally:
        db.close()


# ── per-turn delta at source (session-scoped call_id dedup) ──────────────


def _tc(call_id, name="pa_business_read"):
    return {"tool_name": name, "input": {"i": call_id}, "call_id": call_id}


def test_record_pa_turn_dedups_prior_turns_calls_by_call_id(tmp_path):
    """Cumulative payloads (extracted from full session history) persist only
    the recording turn's DELTA: call_ids already recorded for the session
    under other turns are skipped."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.record_pa_turn(
            turn_id="t1", agent_id="a", chat_id="c", session_id="s1",
            tool_calls=[_tc("call_a"), _tc("call_b")],
        )
        # Turn 2 payload re-carries turn 1's calls (cumulative extraction)
        # plus its own new call.
        db.record_pa_turn(
            turn_id="t2", agent_id="a", chat_id="c", session_id="s1",
            tool_calls=[_tc("call_a"), _tc("call_b"), _tc("call_c")],
        )
        turns = {t["turn_id"]: t for t in db.list_pa_turns(agent_id="a")}
        assert [tc["call_id"] for tc in turns["t1"]["tool_calls"]] == ["call_a", "call_b"]
        assert [tc["call_id"] for tc in turns["t2"]["tool_calls"]] == ["call_c"]
    finally:
        db.close()


def test_record_pa_turn_dedup_survives_session_rotation(tmp_path):
    """v6.3 item 5b (WB f6845320): hermes compression rotates session_id on
    every compaction while the carried transcript still contains the
    historical tool calls. The dedup high-water mark is therefore scoped to
    the CHAT, not the session — after rotation (same chat, new session id,
    carried messages re-extracted) the second turn records ONLY its own
    calls. Pre-fix this re-recorded the whole history under the new turn
    (day-30 AMK: 132 calls attributed to one 40-second turn)."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        # Day's turns before the compaction, session s1.
        db.record_pa_turn(
            turn_id="t1", agent_id="a", chat_id="c", session_id="s1",
            tool_calls=[_tc("call_a"), _tc("call_b")],
        )
        # Compaction rotates s1 -> s2. The next turn's payload (extracted
        # from the carried/compressed transcript) re-carries the historical
        # calls plus its own new one.
        db.record_pa_turn(
            turn_id="t2", agent_id="a", chat_id="c", session_id="s2",
            tool_calls=[_tc("call_a"), _tc("call_b"), _tc("call_c")],
        )
        turns = {t["turn_id"]: t for t in db.list_pa_turns(agent_id="a")}
        assert [tc["call_id"] for tc in turns["t1"]["tool_calls"]] == ["call_a", "call_b"]
        assert [tc["call_id"] for tc in turns["t2"]["tool_calls"]] == ["call_c"]
    finally:
        db.close()


def test_record_pa_turn_dedup_scoped_to_chat(tmp_path):
    """The same call_id in a DIFFERENT chat still records (chat is the scope
    boundary); without a chat_id the dedup falls back to session scope."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.record_pa_turn(
            turn_id="t1", agent_id="a", chat_id="c1", session_id="s1",
            tool_calls=[_tc("call_a")],
        )
        db.record_pa_turn(
            turn_id="t2", agent_id="a", chat_id="c2", session_id="s2",
            tool_calls=[_tc("call_a")],
        )
        # No chat_id: session-scope fallback — same session still dedups.
        db.record_pa_turn(
            turn_id="t3", agent_id="a", chat_id=None, session_id="s3",
            tool_calls=[_tc("call_x")],
        )
        db.record_pa_turn(
            turn_id="t4", agent_id="a", chat_id=None, session_id="s3",
            tool_calls=[_tc("call_x"), _tc("call_y")],
        )
        turns = {t["turn_id"]: t for t in db.list_pa_turns(agent_id="a")}
        assert len(turns["t1"]["tool_calls"]) == 1
        assert len(turns["t2"]["tool_calls"]) == 1
        assert [tc["call_id"] for tc in turns["t3"]["tool_calls"]] == ["call_x"]
        assert [tc["call_id"] for tc in turns["t4"]["tool_calls"]] == ["call_y"]
    finally:
        db.close()


def test_record_pa_turn_rerecord_same_turn_keeps_own_calls(tmp_path):
    """Idempotent re-record of the SAME turn_id keeps that turn's calls —
    the session dedup only excludes calls recorded under OTHER turn_ids."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        for _ in range(2):
            db.record_pa_turn(
                turn_id="t1", agent_id="a", chat_id="c", session_id="s1",
                tool_calls=[_tc("call_a"), _tc("call_b")],
            )
        turns = db.list_pa_turns(agent_id="a")
        assert len(turns) == 1
        assert [tc["call_id"] for tc in turns[0]["tool_calls"]] == ["call_a", "call_b"]
    finally:
        db.close()


def test_record_pa_turn_calls_without_call_id_pass_through(tmp_path):
    """Legacy payloads without call_id are not deduped (no high-water key)."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.record_pa_turn(
            turn_id="t1", agent_id="a", chat_id="c", session_id="s1",
            tool_calls=[{"tool_name": "x"}],
        )
        db.record_pa_turn(
            turn_id="t2", agent_id="a", chat_id="c", session_id="s1",
            tool_calls=[{"tool_name": "x"}],
        )
        turns = {t["turn_id"]: t for t in db.list_pa_turns(agent_id="a")}
        assert len(turns["t1"]["tool_calls"]) == 1
        assert len(turns["t2"]["tool_calls"]) == 1
    finally:
        db.close()


def test_extract_tool_calls_carries_call_id():
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_42",
                    "function": {"name": "f", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_42", "content": "ok"},
    ]
    calls = po.extract_tool_calls(messages)
    assert len(calls) == 1
    assert calls[0].call_id == "call_42"


def test_failed_turn_calls_recorded_then_not_reattributed(tmp_path):
    """A failed turn whose record carries its tool calls keeps attribution:
    the NEXT turn's cumulative payload does not re-claim them."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.record_pa_turn(
            turn_id="t_failed", agent_id="a", chat_id="c", session_id="s1",
            turn_status="failed", error={"error": "stream cut"},
            tool_calls=[_tc("call_a")],
        )
        db.record_pa_turn(
            turn_id="t_next", agent_id="a", chat_id="c", session_id="s1",
            turn_status="completed",
            tool_calls=[_tc("call_a"), _tc("call_b")],
        )
        turns = {t["turn_id"]: t for t in db.list_pa_turns(agent_id="a")}
        assert [tc["call_id"] for tc in turns["t_failed"]["tool_calls"]] == ["call_a"]
        assert [tc["call_id"] for tc in turns["t_next"]["tool_calls"]] == ["call_b"]
    finally:
        db.close()


# ── full build_turn_record + write integration (pure builder + DB) ───────


def test_build_turn_record_extracts_tool_calls_and_writes(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        agent_result = {
            "final_response": "Done — updated the case.",
            "completed": True,
            "model": "claude-x",
            "provider": "anthropic",
            "input_tokens": 200,
            "output_tokens": 50,
            "estimated_cost_usd": 0.004,
            "api_calls": 2,
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "pa_business_write",
                                "arguments": '{"operation":"update_case"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": '{"ok":true}',
                },
            ],
        }
        record = po.build_turn_record(
            session_id="sess-9",
            agent_id="christopher",
            chat_id="chat-9",
            agent_result=agent_result,
            final_response=agent_result["final_response"],
            started_at=10.0,
            completed_at=10.4,
        )
        assert record.agent_id == "christopher"
        assert record.turn_status == "completed"
        assert record.model == "claude-x"
        assert record.input_tokens == 200
        assert len(record.tool_calls) == 1
        assert record.tool_calls[0].tool_name == "pa_business_write"

        turn_id = po.write_turn_record(db, record)
        assert turn_id

        turns = db.list_pa_turns(agent_id="christopher")
        assert len(turns) == 1
        assert len(turns[0]["tool_calls"]) == 1
    finally:
        db.close()


# ── (c) pure agent-recorded events: no mechanical floor ──


def test_no_events_when_record_event_not_called(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        # No agent-recorded events staged for this session. With the pure
        # agent-recorded model (no mechanical floor) the turn records with
        # ZERO events — its tool-calls are still captured separately.
        agent_result = {
            "final_response": "Here is the answer.",
            "completed": True,
            "messages": [],
        }
        record = po.build_turn_record(
            session_id="sess-no-events",
            agent_id="christopher",
            chat_id="chat-1",
            agent_result=agent_result,
            final_response="Here is the answer.",
            started_at=1.0,
            completed_at=1.2,
        )
        assert record.events == []

        po.write_turn_record(db, record)
        turns = db.list_pa_turns(agent_id="christopher")
        # The turn still persists with zero events.
        assert len(turns) == 1
        assert turns[0]["events"] == []
    finally:
        db.close()


def test_agent_recorded_events_present(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        # Stage an agent-recorded event via the public staging API.
        po.stage_agent_event(
            "sess-mixed",
            event_type="escalated",
            reason="needs operator",
            evidence_message_refs=["m5"],
        )
        record = po.build_turn_record(
            session_id="sess-mixed",
            agent_id="christopher",
            chat_id="chat-1",
            agent_result={"final_response": "Escalating.", "completed": True, "messages": []},
            final_response="Escalating.",
            started_at=1.0,
            completed_at=1.1,
        )
        # Only the agent-recorded event — no derived/mechanical events.
        assert [e.source for e in record.events] == ["agent_recorded"]
        assert record.events[0].event_type == "escalated"

        po.write_turn_record(db, record)
        turns = db.list_pa_turns(agent_id="christopher")
        ev_sources = {e["source"] for e in turns[0]["events"]}
        assert ev_sources == {"agent_recorded"}
    finally:
        db.close()


def test_failed_turn_status_captured_without_events(tmp_path):
    # A failed turn still captures turn_status; with no agent-recorded events
    # it has zero events (no mechanical floor synthesising a turn_failed event).
    record = po.build_turn_record(
        session_id="sess-fail",
        agent_id="a",
        chat_id="c",
        agent_result={"final_response": "", "error": "boom", "messages": []},
        final_response="",
        started_at=1.0,
        completed_at=1.1,
    )
    assert record.turn_status == "failed"
    assert record.events == []


def test_drain_clears_buffer_between_turns(tmp_path):
    po.stage_agent_event("sess-drain", event_type="e1", reason=None)
    first = po.drain_agent_events("sess-drain")
    second = po.drain_agent_events("sess-drain")
    assert len(first) == 1
    assert second == []  # buffer cleared; no leak into the next turn


# ── (d) safety wrapper: recording exception is swallowed ─────────────────


def test_write_turn_record_with_none_db_returns_none():
    record = po.build_turn_record(
        session_id="s",
        agent_id="a",
        chat_id="c",
        agent_result={"final_response": "x", "completed": True, "messages": []},
        final_response="x",
        started_at=1.0,
        completed_at=1.1,
    )
    # No DB available (degraded mode) — must not raise.
    assert po.write_turn_record(None, record) is None


def test_record_event_tool_no_session_does_not_raise(monkeypatch):
    """The record_event handler must fail soft (no session) — never raise."""
    import os
    import tools.pa_record_event as pre

    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    # Force _current_session_id() to None.
    monkeypatch.setattr(pre, "_current_session_id", lambda: None)
    out = pre._handle_record_event({"event_type": "e", "reason": "r"})
    assert "no active session" in out.lower()


def test_safe_record_turn_swallows_write_exception(tmp_path):
    """A SessionDB.record_pa_turn failure must be SWALLOWED by safe_record_turn.

    This is the gateway safety-wrapper contract (hard requirement (i)): an
    exception inside the recording write does NOT propagate to the caller, so a
    broken recording can never break live processing or the agent's reply.
    """
    class _BrokenDB:
        def record_pa_turn(self, **kwargs):
            raise RuntimeError("simulated DB failure")

    record = po.build_turn_record(
        session_id="s",
        agent_id="a",
        chat_id="c",
        agent_result={"final_response": "x", "completed": True, "messages": []},
        final_response="x",
        started_at=1.0,
        completed_at=1.1,
    )
    # safe_record_turn must NOT raise — it returns None on failure.
    result = po.safe_record_turn(_BrokenDB(), record)
    assert result is None

    # The thin write_turn_record DOES propagate (documented), proving the
    # swallow happens in safe_record_turn, not by accident upstream.
    raised = False
    try:
        po.write_turn_record(_BrokenDB(), record)
    except RuntimeError:
        raised = True
    assert raised is True


# ── (e) staging key must match the drain key (sk-day26-v6 drain miss) ────


def test_record_event_stages_under_explicit_session_id_despite_env_clobber(monkeypatch):
    """REPRO of the sk-day26-v6 pa_events drain miss (turns 11-14 lost).

    Mid-run, a background AIAgent fork (bg-review) overwrote the
    process-global HERMES_SESSION_ID env var with its own fresh session id.
    record_event then staged under the fork's id while the turn-boundary
    drained the live gateway session id — the events were stranded forever.

    The fix: the agent loop threads its authoritative session_id through
    tool dispatch, and record_event MUST key staging on that explicit id,
    not the clobberable env var.  This test FAILS on pre-fix code (the
    event lands under the clobbered env key).
    """
    import tools.pa_record_event as pre
    from gateway import pa_observability as po

    # Hygiene: empty both keys in the process-global buffer.
    po.drain_agent_events("live-session")
    po.drain_agent_events("fork-session-clobber")

    # Simulate the clobber: another AIAgent.__init__ re-pointed the env var.
    monkeypatch.setenv("HERMES_SESSION_ID", "fork-session-clobber")

    out = pre._handle_record_event(
        {"event_type": "case_observation", "reason": "tap leak fixed"},
        task_id="t1",
        user_task=None,
        session_id="live-session",  # explicit id from the agent-loop dispatch
    )
    assert '"recorded": true' in out

    # Drain side keys on the LIVE session id — the event must be there...
    drained = po.drain_agent_events("live-session")
    assert [e.event_type for e in drained] == ["case_observation"]
    # ...and nothing may be stranded under the clobbered env key.
    assert po.drain_agent_events("fork-session-clobber") == []


def test_handle_function_call_threads_session_id_to_record_event(monkeypatch):
    """End-to-end through the real dispatch path: run_agent passes
    session_id into handle_function_call; the registry must deliver it to
    the handler so staging keys on it (not on the env var)."""
    from model_tools import handle_function_call
    from gateway import pa_observability as po

    po.drain_agent_events("live-dispatch-session")
    po.drain_agent_events("env-other-session")
    monkeypatch.setenv("HERMES_SESSION_ID", "env-other-session")

    result = handle_function_call(
        "record_event",
        {"event_type": "case_update_confirmed", "reason": "done"},
        "task-1",
        session_id="live-dispatch-session",
        skip_pre_tool_call_hook=True,
    )
    assert '"recorded": true' in result

    drained = po.drain_agent_events("live-dispatch-session")
    assert [e.event_type for e in drained] == ["case_update_confirmed"]
    assert po.drain_agent_events("env-other-session") == []


def test_record_event_fallback_prefers_contextvar_over_env(monkeypatch):
    """Without an explicit session_id kwarg, resolution must prefer the
    task-local ContextVar (gateway.session_context._SESSION_ID) over the
    process-global env var.  Pre-fix code imported the ContextVar from the
    wrong module (run_agent — always ImportError) so this also FAILS on
    pre-fix code."""
    import tools.pa_record_event as pre
    from gateway import pa_observability as po
    from gateway.session_context import _SESSION_ID

    po.drain_agent_events("ctx-sess")
    po.drain_agent_events("env-sess")
    monkeypatch.setenv("HERMES_SESSION_ID", "env-sess")
    token = _SESSION_ID.set("ctx-sess")
    try:
        out = pre._handle_record_event({"event_type": "escalated"})
        assert '"recorded": true' in out
        assert [e.event_type for e in po.drain_agent_events("ctx-sess")] == ["escalated"]
        assert po.drain_agent_events("env-sess") == []
    finally:
        _SESSION_ID.reset(token)


# ── (f) per-turn token counts (cumulative-counter regression) ────────────


def test_build_turn_record_prefers_turn_scoped_token_fields():
    """pa_turns token columns must be PER-TURN. run_agent's legacy
    "input_tokens"/"output_tokens" result fields are session-cumulative
    (sk-day26-v6: 80k -> 2.28M monotonic across 14 turns); the extraction
    must prefer the turn-scoped fields when present. FAILS on pre-fix code
    (extraction reads only the cumulative fields)."""
    record = po.build_turn_record(
        session_id="s",
        agent_id="christopher",
        chat_id="c",
        agent_result={
            "final_response": "ok",
            "completed": True,
            "messages": [],
            # cumulative (turn 3 of a cached agent)
            "input_tokens": 188854,
            "output_tokens": 2526,
            # this turn's delta
            "turn_input_tokens": 111899,
            "turn_output_tokens": 894,
        },
        final_response="ok",
        started_at=1.0,
        completed_at=1.1,
    )
    assert record.input_tokens == 111899
    assert record.output_tokens == 894


def test_build_turn_record_turn_zero_tokens_do_not_fall_back():
    """A genuine zero turn-delta must record 0, not the cumulative value."""
    record = po.build_turn_record(
        session_id="s",
        agent_id="a",
        chat_id="c",
        agent_result={
            "final_response": "",
            "completed": True,
            "messages": [],
            "input_tokens": 500,
            "output_tokens": 50,
            "turn_input_tokens": 0,
            "turn_output_tokens": 0,
        },
        final_response="",
        started_at=1.0,
        completed_at=1.1,
    )
    assert record.input_tokens == 0
    assert record.output_tokens == 0


def test_build_turn_record_falls_back_to_legacy_token_fields():
    """Callers that don't emit turn-scoped fields (string-only paths, replay
    backfill) keep the legacy extraction."""
    record = po.build_turn_record(
        session_id="s",
        agent_id="a",
        chat_id="c",
        agent_result={
            "final_response": "ok",
            "completed": True,
            "messages": [],
            "input_tokens": 1234,
            "output_tokens": 56,
        },
        final_response="ok",
        started_at=1.0,
        completed_at=1.1,
    )
    assert record.input_tokens == 1234
    assert record.output_tokens == 56


def test_build_turn_record_extracts_context_window_peak():
    """context_window_peak comes from run_conversation's
    turn_context_window_peak (max single-call prompt this turn)."""
    record = po.build_turn_record(
        session_id="s",
        agent_id="christopher",
        chat_id="c",
        agent_result={
            "final_response": "ok",
            "completed": True,
            "messages": [],
            "turn_input_tokens": 111899,
            "turn_output_tokens": 894,
            "turn_context_window_peak": 98342,
        },
        final_response="ok",
        started_at=1.0,
        completed_at=1.1,
    )
    assert record.context_window_peak == 98342


def test_build_turn_record_context_window_peak_absent_records_none():
    """Callers that don't emit turn_context_window_peak (string-only paths,
    replay backfill) record NULL — never a cumulative/legacy proxy."""
    record = po.build_turn_record(
        session_id="s",
        agent_id="a",
        chat_id="c",
        agent_result={
            "final_response": "ok",
            "completed": True,
            "messages": [],
            "input_tokens": 500,
            "output_tokens": 50,
        },
        final_response="ok",
        started_at=1.0,
        completed_at=1.1,
    )
    assert record.context_window_peak is None


def test_record_pa_turn_persists_context_window_peak(tmp_path):
    """The column round-trips through record_pa_turn — and the
    schema-reconcile auto-migration adds it to pre-existing DBs."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.record_pa_turn(
            turn_id="t-peak-1",
            agent_id="christopher",
            chat_id="chat",
            session_id="s",
            input_tokens=120,
            output_tokens=30,
            context_window_peak=98342,
        )
        db.record_pa_turn(
            turn_id="t-peak-2",
            agent_id="christopher",
            chat_id="chat",
            session_id="s",
        )
        conn = sqlite3.connect(tmp_path / "state.db")
        conn.row_factory = sqlite3.Row
        try:
            rows = {
                row["turn_id"]: row
                for row in conn.execute(
                    "SELECT turn_id, context_window_peak FROM pa_turns"
                ).fetchall()
            }
            assert rows["t-peak-1"]["context_window_peak"] == 98342
            assert rows["t-peak-2"]["context_window_peak"] is None
        finally:
            conn.close()
    finally:
        db.close()


# ── v6.3 item 5a: turn telemetry survives the LIVE gateway path ──────────
#
# Subagent tests passed but ALL live rows recorded NULL context_window_peak:
# gateway run_sync REBUILDS the agent_result dict with an explicit field
# whitelist (two return sites) that dropped turn_input_tokens /
# turn_output_tokens / turn_context_window_peak, and run_conversation's ~20
# early-return sites never emitted them at all. The fixes are
# gateway.run._turn_telemetry_fields (whitelist passthrough) and the
# run_conversation wrapper (stamps the keys on every dict return path).


def test_turn_telemetry_fields_passthrough():
    from gateway.run import _turn_telemetry_fields

    result = {
        "final_response": "ok",
        "turn_input_tokens": 1200,
        "turn_output_tokens": 90,
        "turn_context_window_peak": 98342,
        "input_tokens": 999999,  # cumulative — must NOT be remapped
    }
    fields = _turn_telemetry_fields(result)
    assert fields == {
        "turn_input_tokens": 1200,
        "turn_output_tokens": 90,
        "turn_context_window_peak": 98342,
    }


def test_turn_telemetry_fields_absent_or_none_keys_omitted():
    from gateway.run import _turn_telemetry_fields

    assert _turn_telemetry_fields({"final_response": "x"}) == {}
    assert _turn_telemetry_fields({"turn_context_window_peak": None}) == {}
    assert _turn_telemetry_fields("not a dict") == {}
    assert _turn_telemetry_fields(None) == {}


def test_live_shaped_rebuilt_result_records_context_window_peak(tmp_path):
    """LIVE-shaped flow: run_conversation result -> run_sync REBUILD (field
    whitelist + telemetry passthrough) -> stashed agent_result ->
    build_turn_record -> DB row. This is the path the 25 NULL day-30 rows
    took; the rebuild previously dropped the telemetry keys."""
    from gateway.run import _turn_telemetry_fields

    run_conversation_result = {
        "final_response": "recorded the observation",
        "messages": [{"role": "assistant", "content": "recorded"}],
        "api_calls": 3,
        "completed": True,
        "input_tokens": 2_280_000,  # session-cumulative (cached gateway agent)
        "output_tokens": 64_000,
        "turn_input_tokens": 141_000,
        "turn_output_tokens": 1_800,
        "turn_context_window_peak": 187_404,
        "model": "gpt-5.4-mini",
        "provider": "openai",
        "estimated_cost_usd": 0.02,
    }
    # run_sync's success-path rebuild (gateway/run.py): explicit whitelist
    # plus the telemetry passthrough under test.
    stashed_agent_result = {
        "final_response": run_conversation_result["final_response"],
        "messages": run_conversation_result["messages"],
        "api_calls": run_conversation_result["api_calls"],
        "completed": run_conversation_result["completed"],
        "input_tokens": run_conversation_result["input_tokens"],
        "output_tokens": run_conversation_result["output_tokens"],
        "model": run_conversation_result["model"],
        "provider": run_conversation_result["provider"],
        "estimated_cost_usd": run_conversation_result["estimated_cost_usd"],
        **_turn_telemetry_fields(run_conversation_result),
    }
    record = po.build_turn_record(
        agent_id="christopher",
        chat_id="chat-amk",
        session_id="sess-live",
        agent_result=stashed_agent_result,
        final_response=stashed_agent_result["final_response"],
        started_at=10.0,
        completed_at=11.0,
    )
    assert record.context_window_peak == 187_404
    # Token columns take the TURN deltas, not the cumulative counters.
    assert record.input_tokens == 141_000
    assert record.output_tokens == 1_800

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        po.write_turn_record(db, record)
        conn = sqlite3.connect(tmp_path / "state.db")
        try:
            row = conn.execute(
                "SELECT input_tokens, output_tokens, context_window_peak FROM pa_turns"
            ).fetchone()
            assert row == (141_000, 1_800, 187_404)
        finally:
            conn.close()
    finally:
        db.close()


def _bare_agent_with_turn_state():
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent.session_input_tokens = 5_000
    agent.session_output_tokens = 700
    agent._turn_input_tokens_baseline = 4_000
    agent._turn_output_tokens_baseline = 600
    agent._turn_context_window_peak = 98_342
    return agent


def test_run_conversation_wrapper_stamps_telemetry_on_early_returns():
    """Early-return impl dicts (failure / interrupt sites) get the
    turn-scoped keys stamped by the public wrapper."""
    agent = _bare_agent_with_turn_state()
    agent._run_conversation_impl = lambda *a, **kw: {
        "final_response": None,
        "messages": [],
        "api_calls": 2,
        "completed": False,
        "failed": True,
        "error": "Invalid API response after 3 retries",
    }
    result = agent.run_conversation("hello")
    assert result["turn_input_tokens"] == 1_000
    assert result["turn_output_tokens"] == 100
    assert result["turn_context_window_peak"] == 98_342


def test_run_conversation_wrapper_keeps_impl_values():
    """The happy-path result computes its own turn fields — setdefault must
    not override them."""
    agent = _bare_agent_with_turn_state()
    agent._run_conversation_impl = lambda *a, **kw: {
        "final_response": "ok",
        "turn_input_tokens": 123,
        "turn_output_tokens": 45,
        "turn_context_window_peak": 67_890,
    }
    result = agent.run_conversation("hello")
    assert result["turn_input_tokens"] == 123
    assert result["turn_output_tokens"] == 45
    assert result["turn_context_window_peak"] == 67_890


class TestTurnSourceMessageIds:
    """message_refs records the turn's INPUT WA message ids (teren 2026-06-12:
    deterministic at source, never reconstructed from content)."""

    def test_build_turn_record_prefers_injected_source_ids(self):
        from gateway import pa_observability

        record = pa_observability.build_turn_record(
            session_id="s1",
            agent_id="christopher",
            chat_id="chat@g.us",
            agent_result={
                "final_response": "ok",
                "turn_source_message_ids": ["3AAA", "3BBB"],
                "messages": [{"role": "user", "content": "x"}],
            },
            final_response="ok",
            started_at=1.0,
            completed_at=2.0,
        )
        assert record.message_refs == ["3AAA", "3BBB"]

    def test_build_turn_record_falls_back_without_injection(self):
        from gateway import pa_observability

        record = pa_observability.build_turn_record(
            session_id="s1",
            agent_id="christopher",
            chat_id="chat@g.us",
            agent_result={"final_response": "ok", "messages": [{"role": "user", "content": "x"}]},
            final_response="ok",
            started_at=1.0,
            completed_at=2.0,
        )
        assert record.message_refs is None

    def test_boundary_extracts_bundle_and_single_ids(self):
        # mirror of the gateway boundary extraction logic
        class _Ev:
            raw_message = {"bundle": True, "sourceMessageIds": ["A1", "B2"]}
            message_id = "A1+B2"

        class _Single:
            raw_message = None
            message_id = "C3"

        def extract(event):
            ids = []
            raw = getattr(event, "raw_message", None)
            if isinstance(raw, dict):
                got = raw.get("sourceMessageIds")
                if isinstance(got, list):
                    ids = [str(i) for i in got if i]
            if not ids:
                mid = getattr(event, "message_id", None)
                if mid:
                    ids = [m for m in str(mid).split("+") if m]
            return ids

        assert extract(_Ev()) == ["A1", "B2"]
        assert extract(_Single()) == ["C3"]
