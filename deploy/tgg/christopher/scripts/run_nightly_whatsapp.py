#!/usr/bin/env python3
"""Append one idempotent shadow-only Christopher nightly batch trigger."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


INTERNAL_CHAT = "900000000000000001@g.us"
DEFAULT_SOURCE = "/var/lib/tgg-capture/whatsapp/capture/events.jsonl"
DEFAULT_RECEIPT_ROOT = "/home/pclaw/.hermes-christopher-tgg/runtime/nightly-trigger-receipts"
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def build_record(day: str, *, now: int) -> dict[str, object]:
    if not DATE.fullmatch(day):
        raise ValueError("nightly date must be YYYY-MM-DD")
    return {
        "messageId": f"SYSNIGHTLY-{day}-{hashlib.sha256(day.encode()).hexdigest()[:12]}",
        "chatId": INTERNAL_CHAT,
        "senderId": "system@internal",
        "senderName": "system",
        "chatName": "Christopher TGG Nightly Internal",
        "isGroup": True,
        "body": f"[system] process TGG WhatsApp batch for {day}",
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


def _write_receipt(path: Path, value: dict[str, object]) -> None:
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o640)
    try:
        written = os.write(fd, encoded)
        if written != len(encoded):
            raise RuntimeError("nightly receipt write was incomplete")
        os.fsync(fd)
    finally:
        os.close(fd)


def append_once(source: Path, receipt_root: Path, record: dict[str, object]) -> bool:
    source = source.resolve()
    if source != Path(DEFAULT_SOURCE):
        raise RuntimeError(f"nightly source must be {DEFAULT_SOURCE}")
    receipt_root.mkdir(parents=True, exist_ok=True, mode=0o750)
    day = str(record["body"]).rsplit(" ", 1)[-1]
    receipt = receipt_root / f"{day}.json"
    lock_path = receipt_root / ".trigger.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o640)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if receipt.exists():
            existing = json.loads(receipt.read_text(encoding="utf-8"))
            if existing.get("message_id") != record["messageId"]:
                raise RuntimeError("nightly trigger receipt conflicts")
            return False
        encoded = (json.dumps(record, separators=(",", ":")) + "\n").encode()
        source_fd = os.open(source, os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC)
        try:
            written = os.write(source_fd, encoded)
            if written != len(encoded):
                raise RuntimeError("nightly trigger append was incomplete")
            os.fsync(source_fd)
        finally:
            os.close(source_fd)
        _write_receipt(receipt, {
            "contract": "tgg-christopher-nightly-trigger/v1",
            "date": day,
            "message_id": record["messageId"],
            "source": str(source),
            "external_outbound_sent": 0,
        })
        directory_fd = os.open(receipt_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True
    finally:
        os.close(lock_fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date")
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--receipt-root", default=DEFAULT_RECEIPT_ROOT)
    parser.add_argument("--timestamp", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    now = datetime.now(ZoneInfo("Asia/Singapore"))
    day = args.date or (now.date() - timedelta(days=1)).isoformat()
    if not DATE.fullmatch(day):
        parser.error("--date must be YYYY-MM-DD")
    record = build_record(day, now=args.timestamp or int(time.time()))
    appended = False if args.dry_run else append_once(Path(args.source), Path(args.receipt_root), record)
    print(json.dumps({
        "ok": True, "date": day, "dry_run": args.dry_run,
        "records_appended": 1 if appended else 0,
        "already_triggered": not args.dry_run and not appended,
        "external_outbound_sent": 0,
        "record": record if args.dry_run else None,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
