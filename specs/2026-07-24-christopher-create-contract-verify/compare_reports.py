#!/usr/bin/env python3
"""Mechanically compare gate reports to fixture ground truth."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EVIDENCE = ROOT / "evidence"


def load(name: str) -> dict:
    return json.loads((EVIDENCE / name).read_text())


def requests(report: dict, *, method: str | None = None, suffix: str | None = None):
    rows = report["operator"]["requests"]
    if method:
        rows = [row for row in rows if row["method"] == method]
    if suffix:
        rows = [row for row in rows if row["path"].endswith(suffix)]
    return rows


def refs(body: dict) -> list[str]:
    fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
    return list(
        body.get("sourceRefs")
        or body.get("evidenceMessageRefs")
        or fields.get("source_refs")
        or []
    )


def parse_epoch(value: str | int) -> int:
    if isinstance(value, int):
        return value
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def main() -> int:
    core = load("core-repeat.json")
    label = load("label-drift.json")
    replay = load("replay-observation.json")
    concurrency = load("concurrency.json")

    create_a = next(
        row["body"]
        for row in requests(core, method="POST")
        if row["path"] == "/api/operator/cases/create"
    )
    b_obs = next(
        row["body"]
        for row in requests(core, method="POST", suffix="/observations")
        if "8802" in row["path"]
    )
    label_items = [
        row["body"]["fields"]["work_items"]
        for row in requests(label, method="POST", suffix="/observations")
    ]
    replay_requests = requests(replay, method="POST", suffix="/observations")
    replay_obs = replay["operator"]["observations"]
    conc_obs = requests(concurrency, method="POST", suffix="/observations")

    expected_replay_epoch = 1784545200
    replay_epochs = [
        parse_epoch(
            row["body"].get("observedAt")
            or row["body"].get("fields", {}).get("observed_at")
        )
        for row in replay_requests
    ]

    turns = concurrency["pa_turns"]
    turn_refs = [json.loads(turn["message_refs_json"]) for turn in turns]
    mgmt = next(turn for turn in turns if "gate-conc-mgmt" in json.loads(turn["message_refs_json"]))
    site = [
        turn
        for turn in turns
        if "gate-conc-mgmt" not in json.loads(turn["message_refs_json"])
    ]
    mgmt_overlapped_sites = (
        sum(
            float(turn["started_at"]) < float(mgmt["completed_at"]) < float(turn["completed_at"])
            for turn in site
        )
        == 3
    )
    labels = [
        item["label"]
        for items in label_items
        for item in items
    ]

    checks = {
        "foreign_photos": {
            "pass": refs(create_a) == ["gate-a-report"]
            and refs(b_obs)
            == ["gate-b-caption", "gate-b-photo-0704", "gate-b-photo-1032"],
            "expected_a_refs": ["gate-a-report"],
            "actual_a_refs": refs(create_a),
            "actual_b_refs": refs(b_obs),
        },
        "label_drift": {
            "pass": labels == ["Toilet bi-fold door", "Toilet bi-fold door"],
            "actual_labels": labels,
        },
        "replay_dedupe": {
            "pass": len(replay_obs) == 1,
            "persisted_observations": len(replay_obs),
            "same_chat_concurrency_pass": len(conc_obs) == 1
            and refs(conc_obs[0]["body"])
            == ["gate-conc-amk-1", "gate-conc-amk-2"],
        },
        "observed_at": {
            "pass": all(value == expected_replay_epoch for value in replay_epochs),
            "expected_epoch": expected_replay_epoch,
            "actual_epochs": replay_epochs,
        },
        "priority": {
            "pass": str(create_a.get("priority") or "").lower() == "urgent",
            "expected": "urgent",
            "actual": create_a.get("priority"),
        },
        "due_date": {
            "pass": create_a.get("dueAt") == 1787068800,
            "expected": "receipt 2026-07-20 + 30d",
            "actual": create_a.get("dueAt"),
            "static_plus_7_present": False,
        },
        "contact_fields": {
            "pass": create_a.get("contactName") == "Tan Ah Kow"
            and create_a.get("contactPhone") == "91234567",
            "actual_top_level": {
                "contactName": create_a.get("contactName"),
                "contactPhone": create_a.get("contactPhone"),
            },
            "nested_evidence": create_a.get("evidence"),
        },
        "work_items": {
            "pass": labels
            == ["Toilet bi-fold door", "Toilet bi-fold door"]
            and bool(create_a.get("work_items") or create_a.get("workItems")),
            "observation_path_pass": labels
            == ["Toilet bi-fold door", "Toilet bi-fold door"],
            "create_path_pass": bool(create_a.get("work_items") or create_a.get("workItems")),
            "actual_create_top_level": create_a.get("work_items")
            or create_a.get("workItems"),
        },
        "ledger_side_effects": {
            "static_current": {
                "text": "",
                "message_kind": "image",
                "timestamp_source": "input.timestamp",
            },
            "known_open": True,
        },
        "cross_chat_concurrency": {
            "pass": len(turn_refs) == 4
            and sorted(len(values) for values in turn_refs) == [1, 1, 1, 2]
            and len({value for values in turn_refs for value in values}) == 5
            and mgmt_overlapped_sites,
            "turn_refs": turn_refs,
            "management_completed_while_three_sites_active": mgmt_overlapped_sites,
        },
        "open_hunt": {
            "pass": False,
            "findings": [],
            "healthy_fields": ["zone", "address", "existing-task canonical label", "cross-chat refs"],
        },
    }
    checks["open_hunt"]["pass"] = all(
        checks[name].get("pass") is True
        for name in (
            "foreign_photos",
            "replay_dedupe",
            "observed_at",
            "priority",
            "due_date",
            "contact_fields",
            "work_items",
        )
    )
    (EVIDENCE / "comparison.json").write_text(
        json.dumps(checks, indent=2, sort_keys=True) + "\n"
    )
    failed = [name for name, value in checks.items() if value.get("pass") is False]
    print(json.dumps({"failed_checks": failed, "comparison": str(EVIDENCE / "comparison.json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
