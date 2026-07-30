"""Focused acceptance coverage for the credential-free P5a rehearsal."""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

from scripts import wf_p5a_dress_rehearsal as rehearsal


def _run_driver(monkeypatch, capsys, root: Path, run: int) -> dict:
    db_path = root / f"run-{run}.sqlite"
    home = root / f"home-{run}"
    evidence_path = root / f"evidence-{run}.json"
    connect_calls: list[tuple] = []

    def no_network(sock, address):
        connect_calls.append(address)
        raise AssertionError(f"unexpected external network call: {address!r}")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "wf_p5a_dress_rehearsal.py",
            "--db-path",
            str(db_path),
            "--home",
            str(home),
            "--evidence-path",
            str(evidence_path),
        ],
    )
    assert rehearsal.main() == 0
    stdout = capsys.readouterr().out
    output = json.loads(stdout)
    assert output == json.loads(evidence_path.read_text(encoding="utf-8"))
    assert connect_calls == []
    return output


def _body_refs(value):
    if isinstance(value, dict):
        body_ref = value.get("body_ref")
        if isinstance(body_ref, dict):
            yield body_ref
        for child in value.values():
            yield from _body_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _body_refs(child)


def _assert_citations(evidence: dict, home: Path) -> None:
    for case_name, case in evidence["cases"].items():
        assert case["ids"], case_name
        assert case["citations"], case_name
        for citation in case["citations"]:
            assert set(citation) == {"table", "identity", "query", "observed"}
            assert citation["table"]
            assert citation["identity"]
            assert citation["query"]
            assert citation["observed"] is not None
    refs = list(_body_refs(evidence))
    assert refs
    for body_ref in refs:
        path = home / body_ref["path"]
        assert path.is_file()
        assert body_ref["exists"] is True
        assert len(body_ref["sha256"]) == 64


def test_p5a_driver_runs_twice_isolated_and_covers_frozen_cases(
    tmp_path, monkeypatch, capsys
):
    first = _run_driver(monkeypatch, capsys, tmp_path, 1)
    second = _run_driver(monkeypatch, capsys, tmp_path, 2)
    expected = set(rehearsal.CASE_KEYS)
    assert set(first["cases"]) == expected
    assert set(second["cases"]) == expected

    for evidence, run in ((first, 1), (second, 2)):
        home = tmp_path / f"home-{run}"
        _assert_citations(evidence, home)
        rendered = json.dumps(evidence, sort_keys=True)
        assert "TYPE=" not in rendered
        assert "BOOKING_REF=" not in rendered
        assert "allied" not in rendered.lower()
        assert "/Users/" not in rendered
        assert "/tmp/" not in rendered

    zero = first["cases"]["zero_match_hold_heal"]["results"]
    assert [item["status"] for item in zero["checkpoints"]] == [
        "unmatched",
        "buffered",
        "matched",
        "applied",
    ]
    assert zero["apply"] == "applied"

    shared = first["cases"]["shared_ref_two_live"]["results"]
    assert shared["first_match"] == shared["second_match"] == "matched"
    assert shared["first_apply"] == shared["second_apply"] == "applied"

    human = first["cases"]["two_match_human_pick"]["results"]
    assert human["ambiguous"] == "ambiguous"
    assert human["resolved"] == "matched"
    assert human["match_method"] == "human"
    assert human["candidate_task_ids"]

    ordered = first["cases"]["out_of_order_buffer"]["results"]
    assert ordered["before_enter"] == ordered["buffered"] == "buffered"
    assert ordered["after_enter"] == "matched"
    assert ordered["apply"] == "applied"
    assert ordered["past"] == "superseded"

    duplicates = first["cases"]["duplicates_three_layers"]["results"]
    assert duplicates["same_source_external_second_ingest_returned_none"] is True
    assert duplicates["forwarded"] == "superseded"
    assert duplicates["second_transition"] == "re_correlate"
    assert duplicates["transition_count"] == 1

    done = first["cases"]["done_instance_retention"]["results"]
    assert done["late_normal"] == "superseded"
    assert done["late_mutation"] == "needs_review"

    chase = first["cases"]["chase_caps"]["results"]
    assert chase["tick_timer_results"] == [["chase"], ["chase"], ["exception"]]
    assert chase["fourth_tick_timer_results"] == []
    assert chase["outbox_count"] == 3

    races = first["cases"]["timer_races"]["results"]
    assert races["event_wins_timer_count"] == 0
    assert races["event_wins_tick_timer_results"] == []
    assert races["timer_wins_late"] == "superseded"
    assert races["transition_count_after_late"] == races["transition_count_before_late"]

    approval = first["cases"]["propose_approve_edit_diff"]["results"]
    assert approval["plain_outbox_count"] == 1
    assert approval["edited_outbox_count"] == 1
    assert approval["edited_status"] == "edited_approved"
    assert approval["replay_result"] is None
    assert approval["token_rotated"] is True

    observation = first["cases"]["manual_observation_advance"]["results"]
    assert observation["first_resulting_step"] == "notify_customer"
    assert observation["second_tick_poll_duplicates"] == 1
    assert observation["second_target_still_waiting"] == "await_container"

    mail_cases = {
        "zero_match_hold_heal",
        "two_match_human_pick",
        "out_of_order_buffer",
        "duplicates_three_layers",
        "done_instance_retention",
    }
    for name in mail_cases:
        assert any(
            citation["table"] == "wf_event"
            and citation["observed"].get("source") == "email"
            for citation in first["cases"][name]["citations"]
        ), name
