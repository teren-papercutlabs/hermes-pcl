"""PA-75 capture-only mutation adapter tests."""

import json
import sys
from pathlib import Path

import pytest

from gateway.replay import ReplayPlan, replay_context
from tools.pa_business_tools import execute_business_operation


def _config(sentinel: Path):
    command = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; import json; "
            f"Path({str(sentinel)!r}).write_text('executed'); "
            "print(json.dumps({'ok': True}))"
        ),
    ]
    return {
        "pa_business": {
            "operations": {
                "tgg_case_update": {"type": "command", "command": command},
                "tgg_human_resolution_apply": {"type": "command", "command": command},
                "tgg_case_lookup": {"type": "command", "command": command},
            }
        }
    }


def test_capture_only_intercepts_every_non_read_operation_at_final_bridge(tmp_path):
    sentinel = tmp_path / "systems-was-called"
    plan = ReplayPlan(
        platform="whatsapp", run_id="pa75-capture", attempt_id="pa75-attempt",
        live_business_writes=True, business_write_mode="capture",
    )
    with replay_context(plan) as ctx:
        update = execute_business_operation(
            _config(sentinel), "tgg_case_update", {"jobNo": "AM/JOB/2601/1018", "state": "completed"}
        )
        future = execute_business_operation(
            _config(sentinel), "tgg_human_resolution_apply", {"documentId": "doc-1", "effects": [{"caseId": "case-1"}]}
        )

    assert not sentinel.exists()
    assert update["status_code"] == 202
    assert future["data"]["capture_only"] is True
    assert [(row["capture_id"], row["operation"]) for row in ctx.captured_business_mutations] == [
        ("capture-1", "tgg_case_update"),
        ("capture-2", "tgg_human_resolution_apply"),
    ]
    assert ctx.captured_business_mutations[0]["payload"] == {
        "jobNo": "AM/JOB/2601/1018", "state": "completed"
    }
    assert len(ctx.captured_business_mutations[0]["payload_sha256"]) == 64


def test_capture_mode_keeps_only_explicit_pure_reads_live(tmp_path):
    sentinel = tmp_path / "read-was-called"
    plan = ReplayPlan(
        platform="whatsapp", run_id="pa75-capture", attempt_id="pa75-attempt",
        live_business_writes=True, business_write_mode="capture",
    )
    with replay_context(plan) as ctx:
        result = execute_business_operation(
            _config(sentinel), "tgg_case_lookup", {"jobNo": "AM/JOB/2601/1018"}
        )

    assert result["ok"] is True
    assert sentinel.read_text() == "executed"
    assert ctx.captured_business_mutations == []


def test_capture_mode_is_rejected_when_deserialized_with_invalid_value():
    with pytest.raises(ValueError, match="business_write_mode"):
        ReplayPlan.from_mapping({"platform": "whatsapp", "businessWriteMode": "nope"})


def test_capture_context_has_a_normalized_serializable_receipt(tmp_path):
    sentinel = tmp_path / "systems-was-called"
    plan = ReplayPlan(
        platform="whatsapp", run_id="pa75-capture", attempt_id="pa75-attempt",
        live_business_writes=True, business_write_mode="capture",
    )
    with replay_context(plan) as ctx:
        execute_business_operation(_config(sentinel), "tgg_case_update", {"jobNo": "AM/JOB/2601/1018"})
    encoded = json.dumps(ctx.captured_business_mutations, sort_keys=True)
    assert "tgg-pa75-captured-business-mutation/v1" in encoded
    assert "systems-was-called" not in encoded
