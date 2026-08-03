#!/usr/bin/env python3
"""Emit one scheduled report trigger into Christopher's canonical inbox source."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


MANAGEMENT_CHAT = "120363407903158826@g.us"
DEFAULT_SOURCE = "/var/lib/tgg-capture/whatsapp/capture/events.jsonl"


def build_record(cycle: str, *, now: int) -> dict[str, object]:
    return {
        "messageId": f"SYSREPORT-{cycle}-{now}-{uuid.uuid4().hex[:8]}",
        "chatId": MANAGEMENT_CHAT,
        "senderId": "system@internal",
        "senderName": "system",
        "chatName": "Christopher x TGG Management",
        "isGroup": True,
        "body": f"[system] scheduled {cycle} report run",
        "hasMedia": False,
        "mediaType": None,
        "mediaUrls": [],
        "mentionedIds": [],
        "quotedMessageId": None,
        "quotedText": "",
        "timestamp": now,
        "fromMe": False,
        "historySync": False,
    }


def append_record(path: Path, record: dict[str, object]) -> None:
    path = path.resolve()
    if path != Path(DEFAULT_SOURCE):
        raise RuntimeError(f"scheduled source must be {DEFAULT_SOURCE}")
    encoded = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        written = os.write(fd, encoded)
        if written != len(encoded):
            raise RuntimeError("scheduled trigger append was incomplete")
        os.fsync(fd)
    finally:
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle", required=True, choices=("weekly", "monthly"))
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timestamp", type=int)
    args = parser.parse_args()

    now = datetime.now(ZoneInfo("Asia/Singapore"))
    if args.cycle == "monthly" and not 1 <= now.day <= 7:
        print(json.dumps({"ok": True, "cycle": "monthly", "skipped": "not-first-monday"}))
        return 0

    record = build_record(args.cycle, now=args.timestamp or int(time.time()))
    if not args.dry_run:
        append_record(Path(args.source), record)
    print(json.dumps({
        "ok": True,
        "cycle": args.cycle,
        "dry_run": args.dry_run,
        "records_appended": 0 if args.dry_run else 1,
        "external_outbound_sent": 0,
        "record": record if args.dry_run else None,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
