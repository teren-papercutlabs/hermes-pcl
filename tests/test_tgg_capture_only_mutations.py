"""PA-75 capture-only mutation adapter tests."""

import json
import sys
from pathlib import Path

import pytest

from gateway.durable_jsonl_consumer import ConsumerError, InboxRecord, process_live_records
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
                "tgg_human_resolution_document_context": {"type": "command", "command": command},
                "tgg_contractor_update_prepare": {"type": "command", "command": command},
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


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("tgg_case_lookup", {"jobNo": "AM/JOB/2601/1018"}),
        ("tgg_human_resolution_document_context", {"id": "record-1"}),
        ("tgg_contractor_update_prepare", {"path": "/media/tgg/batch3.xlsx"}),
    ],
)
def test_capture_mode_keeps_only_explicit_pure_reads_live(tmp_path, operation, payload):
    sentinel = tmp_path / "read-was-called"
    plan = ReplayPlan(
        platform="whatsapp", run_id="pa75-capture", attempt_id="pa75-attempt",
        live_business_writes=True, business_write_mode="capture",
    )
    with replay_context(plan) as ctx:
        result = execute_business_operation(_config(sentinel), operation, payload)

    assert result["ok"] is True
    assert sentinel.read_text() == "executed"
    assert ctx.captured_business_mutations == []


@pytest.mark.parametrize("value", ["capture", "nope"])
def test_capture_mode_is_rejected_when_deserialized_from_untrusted_replay_input(value):
    with pytest.raises(ValueError, match="runtime-only"):
        ReplayPlan.from_mapping({"platform": "whatsapp", "businessWriteMode": value})


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


@pytest.mark.asyncio
async def test_only_consumer_verified_test_management_records_can_construct_capture_mode(tmp_path, monkeypatch):
    import gateway.durable_jsonl_consumer as consumer

    monkeypatch.setattr(consumer, "configured_engine", lambda _: ("openai-direct-primary", "gpt-5.6-terra"))
    monkeypatch.setattr(consumer, "_management_selector_chats", lambda _: frozenset({"120363426509183563@g.us"}))
    record = InboxRecord(
        seq=1, message_id="reply-1", chat_id="120363426509183563@g.us", start_offset=0, end_offset=1,
        raw={"messageId": "reply-1", "chatId": "120363426509183563@g.us", "body": "apply this", "timestamp": 1},
    )

    class Runner:
        plans = []
        async def replay(self, plan):
            self.plans.append(plan)
            return type("Result", (), {"processed": 1, "outbound": [], "captured_business_mutations": []})()

    runner = Runner()
    result = await process_live_records(
        [record], config_path=Path("deploy/tgg/christopher/config.yaml"), state_db=tmp_path / "state.sqlite",
        persistent_session=True, runner=runner, capture_business_writes_for_test_management=True,
    )
    assert runner.plans[0].business_write_mode == "capture"
    assert result["captured_business_mutations"] == []

    record = InboxRecord(
        seq=1, message_id="bad-1", chat_id="120363407903158826@g.us", start_offset=0, end_offset=1,
        raw={"messageId": "bad-1", "chatId": "120363407903158826@g.us", "body": "apply this", "timestamp": 1},
    )
    with pytest.raises(ConsumerError, match="approved test management selector"):
        await process_live_records(
            [record], config_path=Path("deploy/tgg/christopher/config.yaml"), state_db=tmp_path / "state2.sqlite",
            persistent_session=True, runner=runner, capture_business_writes_for_test_management=True,
        )
