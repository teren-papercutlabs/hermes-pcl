#!/usr/bin/env python3
"""Initialize, backfill, and verify the canonical PA message store."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.pa_message_store import MessageStore, backfill_jsonl


def _append_jsonl(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)

    def append(value: Mapping[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")

    return append


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def _snapshot(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)


def run_backfill(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser().resolve()
    snapshot = Path(args.snapshot).expanduser().resolve()
    before = Path(args.before_images).expanduser().resolve()
    held = Path(args.held_conflicts).expanduser().resolve()
    report_path = Path(args.report).expanduser().resolve()
    if snapshot.exists() or before.exists() or held.exists() or report_path.exists():
        raise SystemExit("output artifacts already exist; choose a fresh run directory")
    _snapshot(db, snapshot)
    before.parent.mkdir(parents=True, exist_ok=True)
    held.parent.mkdir(parents=True, exist_ok=True)
    before.touch(exist_ok=False)
    held.touch(exist_ok=False)
    store = MessageStore(db)
    store.initialize()
    before_sink = _append_jsonl(before)
    held_sink = _append_jsonl(held)
    capture = backfill_jsonl(
        store,
        args.capture_jsonl,
        source="capture",
        before_image_sink=before_sink,
        held_sink=held_sink,
    )
    history = backfill_jsonl(
        store,
        args.history_jsonl,
        source="history-sync",
        before_image_sink=before_sink,
        held_sink=held_sink,
    )
    verification = store.verification_report()
    report = {
        "ok": (
            verification["integrity_check"] == "ok"
            and verification["rows"] == verification["fts_rows"]
            and verification["duplicate_message_ids"] == 0
            and verification["duplicate_source_keys"] == 0
        ),
        "completed_at": int(time.time()),
        "db": str(db),
        "snapshot": str(snapshot),
        "before_images": str(before),
        "held_conflicts": str(held),
        "feeds": {"capture": capture, "history_sync": history},
        "verification": verification,
    }
    _atomic_json(report_path, report)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--db", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--db", required=True)
    backfill = sub.add_parser("backfill")
    backfill.add_argument("--db", required=True)
    backfill.add_argument("--capture-jsonl", required=True)
    backfill.add_argument("--history-jsonl", required=True)
    backfill.add_argument("--snapshot", required=True)
    backfill.add_argument("--before-images", required=True)
    backfill.add_argument("--held-conflicts", required=True)
    backfill.add_argument("--report", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = MessageStore(args.db)
    if args.command == "init":
        store.initialize()
        print(json.dumps(store.verification_report(), sort_keys=True))
        return 0
    if args.command == "verify":
        report = store.verification_report()
        print(json.dumps(report, sort_keys=True))
        return 0 if report["integrity_check"] == "ok" else 2
    if args.command == "backfill":
        return run_backfill(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
