from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from scripts import wf_rp1_campaign as campaign


def test_workflow_fixture_declares_bounded_email_extraction_contract() -> None:
    document = yaml.safe_load(campaign.TEMPLATE_PATH.read_text(encoding="utf-8"))
    workflow = document["workflow"]
    contract = workflow["email_extraction"]
    declared_types = {
        "trucking_instruction",
        "pickup_advice",
        "container_assigned",
        "vgm_reply",
        "gate_in",
    }

    assert contract["schema"] == "synthetic-freight-email-event-v1"
    assert set(contract["event_types"]) == declared_types
    assert set(workflow["correlation_keys"]) == {
        "booking_ref",
        "job_no",
        "container_no",
    }
    assert set(contract["correlation_keys"]) == set(workflow["correlation_keys"])
    instruction = contract["instruction"]
    assert "Omit unknown values rather than guessing" in instruction
    assert "return a null event_type and an empty corr object" in instruction
    for undeclared_type in {
        "booking_instruction",
        "bill_of_lading_correction",
        "carrier_delay_notice",
        "status_chase",
        "customer_escalation",
        "gate_in_notice",
        "gate_in_claim_forward",
        "other",
    }:
        assert undeclared_type not in contract["event_types"]
        assert undeclared_type not in instruction

    assert all(config == {} for config in contract["event_types"].values())
    assert "payload_fields" not in json.dumps(contract, sort_keys=True)


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
    assert plan["population"] == {"arcs": 12, "emails": 25, "probes": 1}
    assert (
        plan["workflow_template"]["template_artifact_edited_during_execution"]
        is False
    )
    assert plan["orchestration"]["smtp_host"] == "smtp.invalid"
    assert plan["orchestration"]["remote_db"] == "ssh://staging.invalid/db"
    assert plan["orchestration"]["worker_profile"] == "dorm1"
    assert plan["locked_fixture_hashes"] == campaign.LOCKED_FIXTURES
    assert [arc["id"] for arc in plan["arcs"]] == [
        f"RP1-A{number:02d}" for number in range(1, 13)
    ]


def test_plan_maps_wire_ids_seed_aliases_and_display_personas() -> None:
    plan = campaign.build_campaign_plan(campaign.load_locked_campaign())
    assert plan["orchestration"]["remote_db"] == "pa-workflow-dev"
    arcs = {arc["id"]: arc for arc in plan["arcs"]}
    a01_first = arcs["RP1-A01"]["emails"][0]
    assert a01_first["logical_message_id"] == "rpa01-001@rp1.synthetic.test"
    assert a01_first["wire_message_id"] == "<rpa01-001@rp1.synthetic.test>"
    assert a01_first["wire_body"].endswith(
        "Daniel Ortiz | Pacific Meridian Lines <dorm1@staging.invalid>\n"
    )
    assert a01_first["locked_body_sha256"] != a01_first["wire_body_sha256"]

    a05 = arcs["RP1-A05"]
    assert a05["seed_plan"][0]["canonical_alias"] == "job:RP1-JOB-0501"
    assert a05["seed_plan"][0]["entity_key"] == "job:RP1-JOB-0501"
    assert "logical_target" not in a05["seed_plan"][0]
    assert arcs["RP1-A01"]["seed_plan"][0]["canonical_alias"] == "job:RP1-JOB-0101"
    assert arcs["RP1-A01"]["seed_plan"][0]["entity_key"] == "job:RP1-JOB-0101"
    assert "logical_target" not in arcs["RP1-A01"]["seed_plan"][0]
    assert (
        a05["emails"][0]["wire_message_id"]
        == "<rp1-a05-original-0501@rp1.synthetic.test>"
    )
    assert (
        a05["emails"][1]["headers"]["references"]
        == "<rp1-a05-original-0501@rp1.synthetic.test>"
    )
    probes = arcs["RP1-A08"]["state_probes"]
    assert [(probe["after_email_step"], probe["step"]) for probe in probes] == [(2, 3)]
    assert probes[0]["expected"]["verdict"] == "mismatch"


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


def test_answer_key_scoring_labels_declared_no_fit_as_extraction_miss() -> None:
    result = campaign.score_answer_key(
        {
            "event_type": "status_chase",
            "payload": {"booking_ref": "BK-1"},
            "corr": {"booking_ref": "BK-1"},
        },
        {
            "event_type": None,
            "extraction_disposition": "declared-no-fit",
            "payload": {"booking_ref": "BK-1"},
            "corr": {},
            "correlation": {"verdict": "unmatched"},
            "agent_action": {
                "evidence_status": "EVIDENCE-LIMITED",
                "reason": "no durable proposal for a declared no-fit event",
            },
        },
    )

    assert result["status"] == "fail"
    assert result["section_status"]["extraction"] == "fail"
    assert result["extraction_disposition"] == "declared-no-fit"
    assert result["miss_taxonomy"] == "extraction"


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


def _fully_matching_observation(
    locked: campaign.LockedCampaign,
) -> dict[str, object]:
    arcs: dict[str, object] = {}
    for arc in locked.arcs:
        arcs[arc["id"]] = {
            "emails": {
                email["message_id"]: json.loads(json.dumps(email["answer_key"]))
                for email in arc["emails"]
            },
            "state_probes": [
                json.loads(json.dumps(probe["expected"]))
                for probe in sorted(
                    arc.get("state_probes", []),
                    key=lambda item: (item["after_email_step"], item["step"]),
                )
            ],
            "expected_final": json.loads(json.dumps(arc["expected_final"])),
        }
    return {"arcs": arcs}


def test_score_campaign_counts_emails_probes_and_arc_finals() -> None:
    locked = campaign.load_locked_campaign()
    observed = _fully_matching_observation(locked)
    result = campaign.score_campaign(locked, observed)
    assert result["verdict"] == "pass"
    assert result["denominators"] == {
        "email_answer_keys": 25,
        "arc_expected_final": 12,
        "state_probes": 1,
    }
    assert result["email_counts"] == {
        "pass": 25,
        "evidence-limited": 0,
        "fail": 0,
    }
    assert result["probe_counts"] == {
        "pass": 1,
        "evidence-limited": 0,
        "fail": 0,
    }
    assert result["arc_counts"] == {
        "pass": 12,
        "evidence-limited": 0,
        "fail": 0,
    }


def test_score_campaign_fails_when_all_emails_pass_but_arc_final_fails() -> None:
    locked = campaign.load_locked_campaign()
    observed = _fully_matching_observation(locked)
    observed_arcs = observed["arcs"]
    assert isinstance(observed_arcs, dict)
    a01 = observed_arcs["RP1-A01"]
    assert isinstance(a01, dict)
    a01["expected_final"] = {}

    result = campaign.score_campaign(locked, observed)
    assert result["email_counts"]["pass"] == 25
    assert result["email_counts"]["fail"] == 0
    assert result["arc_counts"]["fail"] == 1
    assert result["verdict"] == "fail"


def test_score_campaign_fails_a_missing_state_probe() -> None:
    locked = campaign.load_locked_campaign()
    observed = _fully_matching_observation(locked)
    observed_arcs = observed["arcs"]
    assert isinstance(observed_arcs, dict)
    a08 = observed_arcs["RP1-A08"]
    assert isinstance(a08, dict)
    a08["state_probes"] = []

    result = campaign.score_campaign(locked, observed)
    assert result["probe_counts"]["fail"] == 1
    assert result["verdict"] == "fail"
