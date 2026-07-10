import hashlib
import json
import sqlite3
from pathlib import Path

import yaml

from gateway.eval_instrument import (
    COMPARISON_SCHEMA,
    CONFIG_SCHEMA,
    SCORE_SCHEMA,
    TRACE_SCHEMA,
    audit_receipt_index,
    build_arm_plan,
    compare_receipts,
    evaluate_adaptive_trace,
    load_eval_config,
    materialize_arm_runtime,
    pin_eval_config,
    record_evaluation_invocation,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path):
    constitution = tmp_path / "constitution.yaml"
    deployment = tmp_path / "deployment.yaml"
    corpus_manifest = tmp_path / "corpus-manifest.json"
    source = tmp_path / "capture.jsonl"
    media_root = tmp_path / "media"
    seed = tmp_path / "seed.db"
    media_root.mkdir()
    constitution.write_text(
        yaml.safe_dump(
            {
                "id": "agent-a",
                "runtime": {"provider": "provider-a", "model": "model-a"},
                "job_briefs": {
                    "ops": {"runtime": {"provider": "provider-a", "model": "model-a"}}
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    deployment.write_text(
        yaml.safe_dump(
            {
                "model": {"provider": "provider-a", "default": "model-a"},
                "pa": {"enabled": True, "constitution_path": str(constitution)},
                "providers": {"provider-a": {"base_url": "https://example.invalid/v1"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    corpus_manifest.write_text(
        json.dumps({"schema": "capture-window-manifest/v1"}), encoding="utf-8"
    )
    source.write_text(
        "\n".join(
            json.dumps(
                {
                    "schema": "capture-event/v1",
                    "normalized": {
                        "messageId": message_id,
                        "chatId": "ops@g.us",
                        "senderId": "person@s.whatsapp.net",
                        "timestamp": timestamp,
                        "body": "fixture",
                        "mediaUrls": [],
                    },
                }
            )
            for message_id, timestamp in (("m-left", 100), ("m-right", 200))
        )
        + "\n",
        encoding="utf-8",
    )
    seed.write_bytes(b"seed")
    config_path = tmp_path / "eval.json"
    config = {
        "schema": CONFIG_SCHEMA,
        "instrument_id": "fixture-instrument",
        "platform": "whatsapp",
        "agent": {
            "id": "agent-a",
            "job_type": "ops",
            "constitution": str(constitution),
            "constitution_sha256": _sha(constitution),
            "deployment_manifest": str(deployment),
            "deployment_manifest_sha256": _sha(deployment),
        },
        "runtime": {
            "config_template": str(deployment),
            "operation_overrides": {
                "case_clarify": {
                    "type": "http",
                    "method": "POST",
                    "url": "https://example.invalid/v1/clarifications",
                }
            },
        },
        "corpus": {
            "manifest": str(corpus_manifest),
            "manifest_sha256": _sha(corpus_manifest),
            "sources": [
                {
                    "id": "window",
                    "source": "capture_event_jsonl",
                    "path": str(source),
                    "sha256": _sha(source),
                    "record_path": "normalized",
                    "media_root": str(media_root),
                }
            ],
        },
        "tenant": {"slug": "tenant-a", "isolation": "process_data_root"},
        "integrity": {
            "seed_boundary": {
                "cutoff": "2026-01-01T00:00:00Z",
                "snapshot": str(seed),
                "snapshot_sha256": _sha(seed),
            },
            "twin_sequences": {
                "agent_policy": "include_and_score",
                "judge_policy": "exclude_future_resolution",
            },
        },
        "arms": [
            {"id": "arm-a", "provider": "provider-a", "model": "model-a"},
            {
                "id": "arm-b",
                "provider": "provider-a",
                "model": "model-b",
                "fallback": True,
            },
        ],
        "trace": {
            "min_distinct_sequences": 2,
            "judgment_layer": {"tool_prefixes": ["case_"]},
            "paired_probes": [
                {
                    "id": "lookup-vs-create",
                    "left": {"message_refs_any": ["m-left"]},
                    "right": {"message_refs_any": ["m-right"]},
                    "left_requires_any": ["case_lookup"],
                    "right_requires_any": ["case_create"],
                }
            ],
        },
        "metrics": [
            {"key": "created", "goal": "minimize"},
            {"key": "matched", "goal": "maximize"},
        ],
    }
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config_path


def _write_trace_db(path: Path, *, run_id: str, attempt_id: str, model: str):
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE pa_turns (
              turn_id TEXT PRIMARY KEY, agent_id TEXT, replay_run_id TEXT,
              replay_attempt_id TEXT, message_refs_json TEXT, model TEXT,
              provider TEXT, raw_turn_envelope_json TEXT, started_at REAL
            );
            CREATE TABLE pa_tool_calls (
              id INTEGER PRIMARY KEY, turn_id TEXT, tool_name TEXT,
              input_json TEXT, result_json TEXT, client_entity_pointer TEXT
            );
            CREATE TABLE pa_events (
              id INTEGER PRIMARY KEY, turn_id TEXT, event_type TEXT
            );
            """
        )
        for index, (message_ref, tool_name) in enumerate(
            (("m-left", "case_lookup"), ("m-right", "case_create")), start=1
        ):
            turn_id = f"turn-{index}"
            call_id = f"call-{index}"
            envelope = {
                "api_calls": 1,
                "messages": [
                    {"role": "user", "content": "fixture"},
                    {
                        "role": "assistant",
                        "reasoning": "fixture reasoning",
                        "tool_calls": [{"id": call_id, "function": {"name": tool_name}}],
                    },
                    {"role": "tool", "tool_call_id": call_id, "content": "{}"},
                    {"role": "assistant", "content": "done"},
                ],
            }
            conn.execute(
                "INSERT INTO pa_turns VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    turn_id,
                    "agent-a",
                    run_id,
                    attempt_id,
                    json.dumps([message_ref]),
                    model,
                    "provider-a",
                    json.dumps(envelope),
                    index,
                ),
            )
            conn.execute(
                "INSERT INTO pa_tool_calls VALUES (?,?,?,?,?,?)",
                (index, turn_id, tool_name, "{}", "{}", f"case:{index}"),
            )


def _write_run_manifest(
    path: Path,
    *,
    config,
    arm_id: str,
    runtime_manifest: dict,
    session_db: Path,
):
    plan = build_arm_plan(config, arm_id, runtime_manifest=runtime_manifest)
    run_id = f"run-{arm_id}"
    attempt_id = f"attempt-{arm_id}"
    arm = next(row for row in config.data["arms"] if row["id"] == arm_id)
    _write_trace_db(session_db, run_id=run_id, attempt_id=attempt_id, model=arm["model"])
    plan_dict = plan.to_dict()
    plan_dict["target_descriptor_manifest"] = {
        "mode": "eval",
        "tenantSlug": "tenant-a",
    }
    payload = {
        "run_id": run_id,
        "target": {"descriptor": plan_dict["target_descriptor_manifest"]},
        "attempts": [
            {
                "attempt_id": attempt_id,
                "run_id": run_id,
                "plan": plan_dict,
                "session_db_path": str(session_db),
                "result": {
                    "attempt": {
                        "code_manifest": {
                            "git_commit": "abc123",
                            "git_dirty": False,
                            "runtime_files": {
                                "config_sha256": runtime_manifest["config_sha256"],
                                "constitution_sha256": runtime_manifest[
                                    "constitution_sha256"
                                ],
                            },
                        }
                    }
                },
            }
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_eval_instrument_pins_runtime_and_enforces_four_trace_assertions(tmp_path):
    config_path = _fixture(tmp_path)
    config = load_eval_config(config_path)
    pins = pin_eval_config(config)
    assert pins["corpus"]["sources"][0]["record_path"] == "normalized"
    assert pins["integrity"]["twin_sequences"]["probe_ids"] == [
        "lookup-vs-create"
    ]

    runtime = materialize_arm_runtime(config, "arm-a", tmp_path / "runtime-a")
    materialized_config = yaml.safe_load(
        (tmp_path / "runtime-a" / "config.yaml").read_text(encoding="utf-8")
    )
    assert (
        materialized_config["pa"]["overlay"]["client"]["business_bridge"][
            "operations"
        ]["case_clarify"]["method"]
        == "POST"
    )
    plan = build_arm_plan(config, "arm-a", runtime_manifest=runtime)
    assert len(plan.messages) == 2
    assert plan.config_overlay_manifest["runtime"] == {
        "config_sha256": runtime["config_sha256"],
        "constitution_sha256": runtime["constitution_sha256"],
    }

    manifest = tmp_path / "run-a.json"
    state_db = tmp_path / "state-a.db"
    _write_run_manifest(
        manifest,
        config=config,
        arm_id="arm-a",
        runtime_manifest=runtime,
        session_db=state_db,
    )
    trace = evaluate_adaptive_trace(
        config,
        "arm-a",
        run_manifest_path=manifest,
        session_db_path=state_db,
    )
    assert trace["schema"] == TRACE_SCHEMA
    assert trace["ok"] is True
    assert [check["name"] for check in trace["checks"]] == [
        "sequence-variance",
        "paired-probes",
        "reasoning-present",
        "provenance",
    ]


def test_eval_receipts_audit_and_compare_distinct_arms(tmp_path):
    config_path = _fixture(tmp_path)
    config = load_eval_config(config_path)
    receipts = []
    index = tmp_path / "receipts.jsonl"
    for arm_id, metrics in (
        ("arm-a", {"created": 2, "matched": 5}),
        ("arm-b", {"created": 1, "matched": 6}),
    ):
        runtime = materialize_arm_runtime(config, arm_id, tmp_path / f"runtime-{arm_id}")
        manifest = tmp_path / f"run-{arm_id}.json"
        state_db = tmp_path / f"state-{arm_id}.db"
        _write_run_manifest(
            manifest,
            config=config,
            arm_id=arm_id,
            runtime_manifest=runtime,
            session_db=state_db,
        )
        score = tmp_path / f"score-{arm_id}.json"
        score.write_text(
            json.dumps(
                {
                    "schema": SCORE_SCHEMA,
                    "arm_id": arm_id,
                    "eligible": True,
                    "metrics": metrics,
                    "cases": [],
                    "twin_discrimination": [
                        {
                            "probe_id": "lookup-vs-create",
                            "passed": True,
                            "outcome": "different_paths",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        receipt = record_evaluation_invocation(
            config_path=config_path,
            arm_id=arm_id,
            mode="eval",
            invocation_id=f"qualification-{arm_id}",
            run_manifest_path=manifest,
            session_db_path=state_db,
            output_dir=tmp_path / "receipt-files",
            receipt_index_path=index,
            score_manifest_path=score,
        )
        assert receipt["ok"] is True
        receipts.append(receipt["receipt_path"])

    audit = audit_receipt_index(
        index,
        instrument_id="fixture-instrument",
        mode="eval",
        expected_invocation_ids=("qualification-arm-a", "qualification-arm-b"),
    )
    assert audit["ok"] is True
    comparison = compare_receipts(config_path, receipts, output_dir=tmp_path / "compare")
    assert comparison["schema"] == COMPARISON_SCHEMA
    assert comparison["metrics"]["created"]["values"] == {
        "arm-a": 2,
        "arm-b": 1,
    }
    assert comparison["decision"]["status"] == "driver_verdict_required"
    assert comparison["decision"]["eligible_arms"] == ["arm-a", "arm-b"]

    failed_receipt = tmp_path / "failed-arm-b.json"
    failed = json.loads(Path(receipts[1]).read_text(encoding="utf-8"))
    failed["ok"] = False
    failed["mechanical_gate"] = {
        "ok": False,
        "failed_checks": ["tool-error-budget"],
    }
    failed_receipt.write_text(json.dumps(failed), encoding="utf-8")
    failed_comparison = compare_receipts(
        config_path,
        [receipts[0], failed_receipt],
        output_dir=tmp_path / "compare-failed-arm",
    )
    assert failed_comparison["qualification"]["arm-b"]["qualified"] is False
    assert failed_comparison["decision"]["eligible_arms"] == ["arm-a"]


def test_failed_eval_invocation_still_writes_an_auditable_receipt(tmp_path):
    config_path = _fixture(tmp_path)
    run_manifest = tmp_path / "run.json"
    run_manifest.write_text(json.dumps({"run_id": "broken", "attempts": []}))
    index = tmp_path / "receipts.jsonl"

    receipt = record_evaluation_invocation(
        config_path=config_path,
        arm_id="arm-a",
        mode="graduation",
        invocation_id="broken-graduation",
        run_manifest_path=run_manifest,
        session_db_path=tmp_path / "missing-state.db",
        output_dir=tmp_path / "receipt-files",
        receipt_index_path=index,
    )

    assert receipt["ok"] is False
    assert receipt["error"]["type"] == "EvalInstrumentError"
    assert Path(receipt["receipt_path"]).is_file()
    audit = audit_receipt_index(
        index,
        instrument_id="fixture-instrument",
        mode="graduation",
        expected_invocation_ids=("broken-graduation",),
    )
    assert audit["ok"] is False
    assert audit["failed_receipt_ids"] == ["broken-graduation:arm-a:graduation"]
