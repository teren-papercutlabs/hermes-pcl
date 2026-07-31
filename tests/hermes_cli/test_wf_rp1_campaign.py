from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts import wf_rp1_campaign as campaign


def test_plan_cli_is_offline_and_preserves_locked_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail_connect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("campaign planner attempted network access")

    monkeypatch.setattr("socket.socket.connect", fail_connect)
    output = tmp_path / "plan.json"
    assert campaign.main(
        [
            "--output",
            str(output),
            "--smtp-host",
            "smtp.invalid",
            "--remote-db",
            "ssh://staging.invalid/db",
            "--worker-profile",
            "dorm1",
        ]
    ) == 0
    assert capsys.readouterr().out == ""
    plan = json.loads(output.read_text())
    assert plan["network_performed"] is False
    assert plan["database_mutated"] is False
    assert plan["population"] == {"arcs": 12, "emails": 25}
    assert plan["workflow_template"]["mutation"] == "unchanged"
    assert plan["orchestration"]["smtp_host"] == "smtp.invalid"
    assert plan["orchestration"]["remote_db"] == "ssh://staging.invalid/db"
    assert plan["orchestration"]["worker_profile"] == "dorm1"
    assert plan["locked_fixture_hashes"] == campaign.LOCKED_FIXTURES
    assert [arc["id"] for arc in plan["arcs"]] == [
        f"RP1-A{number:02d}" for number in range(1, 13)
    ]


def test_plan_maps_wire_ids_seed_aliases_and_display_personas() -> None:
    plan = campaign.build_campaign_plan(campaign.load_locked_campaign())
    arcs = {arc["id"]: arc for arc in plan["arcs"]}
    a01_first = arcs["RP1-A01"]["emails"][0]
    assert a01_first["logical_message_id"] == "rpa01-001@rp1.synthetic.test"
    assert a01_first["wire_message_id"] == "<rpa01-001@rp1.synthetic.test>"
    assert a01_first["wire_body"].endswith(
        "Daniel Ortiz | Pacific Meridian Lines <dorm1@staging.invalid>\n"
    )
    assert a01_first["locked_body_sha256"] != a01_first["wire_body_sha256"]

    a05 = arcs["RP1-A05"]
    assert a05["seed_plan"][0]["logical_target"] == "job"
    assert a05["seed_plan"][0]["canonical_alias"] == "job:RP1-JOB-0501"
    assert (
        a05["emails"][0]["wire_message_id"]
        == "<rp1-a05-original-0501@rp1.synthetic.test>"
    )
    assert (
        a05["emails"][1]["headers"]["references"]
        == "<rp1-a05-original-0501@rp1.synthetic.test>"
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "no-at-sign",
        "bad id@example.test",
        "<half@example.test",
        "half@example.test>",
        "bad\n@example.test",
    ],
)
def test_wire_message_id_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(campaign.CampaignContractError):
        campaign.logical_to_wire_message_id(value)


def test_hash_gate_refuses_modified_fixture(tmp_path: Path) -> None:
    for filename in campaign.LOCKED_FIXTURES:
        shutil.copy(campaign.ROLEPLAY_DIR / filename, tmp_path / filename)
    altered = tmp_path / "arcs-01-04.json"
    altered.write_text(altered.read_text() + "\n")
    with pytest.raises(campaign.CampaignContractError, match="hash mismatch"):
        campaign.load_locked_campaign(tmp_path)


def test_subset_scoring_accepts_extra_fields_and_marks_limits() -> None:
    expected = {
        "verdict": "matched",
        "target": "job:1",
        "candidate_count": 2,
    }
    observed = {"verdict": "matched", "target": "job:1", "extra": "accepted"}
    result = campaign.compare_subset(expected, observed, "correlation")
    assert result["failed"] == []
    assert [entry["path"] for entry in result["unobservable"]] == [
        "correlation.candidate_count"
    ]
    assert campaign._status(result) == "evidence-limited"


def test_answer_key_scoring_fails_observable_mismatch() -> None:
    answer_key = {
        "event_type": "pickup_advice",
        "payload": {"booking_ref": "BK-1"},
        "corr": {"booking_ref": "BK-1"},
        "correlation": {"verdict": "matched", "target": "job:1"},
        "agent_action": {"kind": "resume"},
    }
    observed = {
        "event_type": "pickup_advice",
        "payload": {"booking_ref": "BK-WRONG"},
        "corr": {"booking_ref": "BK-1"},
        "correlation": {"verdict": "matched", "target": "job:1"},
    }
    result = campaign.score_answer_key(answer_key, observed)
    assert result["status"] == "fail"
    assert result["section_status"] == {
        "extraction": "fail",
        "correlation": "pass",
        "action": "fail",
    }


def test_p5a_citation_format() -> None:
    citation = campaign.evidence_citation(
        "wf_events",
        {"message_id": "<message@example.test>"},
        "select payload from wf_events where message_id = ?",
        {"event_type": "pickup_advice"},
    )
    assert list(citation) == ["table", "identity", "query", "observed"]


def test_score_cli_requires_observed_path() -> None:
    with pytest.raises(SystemExit, match="--observed-path is required"):
        campaign.main(["--mode", "score"])
