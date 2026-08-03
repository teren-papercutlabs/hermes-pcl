#!/usr/bin/env python3
"""Run one scheduled Christopher management turn without capture mutation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


MANAGEMENT_CHAT = "120363407903158826@g.us"


def _fixture(cycle: str) -> dict[str, Any]:
    now = int(time.time())
    return {
        "messageId": f"scheduled-report-{cycle}-{now}-{uuid.uuid4().hex[:8]}",
        "chatId": MANAGEMENT_CHAT,
        "senderId": "christopher-scheduler",
        "senderName": "Christopher Scheduler",
        "chatName": "Christopher x TGG Management",
        "isGroup": True,
        "body": f"run {cycle} report",
        "hasMedia": False,
        "mediaType": None,
        "mediaUrls": [],
        "mentionedIds": [],
        "timestamp": now,
        "fromMe": False,
        "historySync": False,
    }


def _record(raw: Mapping[str, Any]):
    from gateway.durable_jsonl_consumer import InboxRecord

    return InboxRecord(
        seq=1,
        message_id=str(raw["messageId"]),
        chat_id=str(raw["chatId"]),
        start_offset=0,
        end_offset=1,
        raw=dict(raw),
    )


def _intents(captured: list[Mapping[str, Any]], config_path: Path) -> list[dict[str, Any]]:
    from gateway import durable_jsonl_consumer as consumer

    sends: list[dict[str, Any]] = []
    for entry in captured:
        parsed = consumer._parse_captured_send(entry)
        if parsed is not None:
            sends.extend(consumer._expand_captured_send(parsed))
        sends.extend(consumer._parse_captured_media(entry))
    retention = consumer._retention_config(config_path)
    if retention is None:
        raise RuntimeError("retained-media config is unavailable")
    rendered: list[dict[str, Any]] = []
    for send in sends:
        if send.get("send_kind", "text") == "media":
            media_path = consumer._resolve_captured_media_path(send["path"], retention)
            media_type, _mime, file_name = consumer._validated_captured_media_type(media_path)
            if media_type != "document" or media_path.suffix.lower() != ".xlsx":
                raise RuntimeError("scheduled report output is not an XLSX document")
            rendered.append({
                "endpoint": "send-media",
                "payload": {
                    "chatId": MANAGEMENT_CHAT,
                    "filePath": str(media_path),
                    "mediaType": media_type,
                    "fileName": file_name or media_path.name,
                    **({"caption": send["caption"]} if send.get("caption") else {}),
                },
            })
        else:
            rendered.append({
                "endpoint": "send",
                "payload": {"chatId": MANAGEMENT_CHAT, "message": str(send["content"])},
            })
    attachments = [item for item in rendered if item["endpoint"] == "send-media"]
    if not attachments and len(rendered) == 1 and rendered[0]["endpoint"] == "send":
        return rendered
    if len(attachments) != 4:
        raise RuntimeError(f"scheduled run composed {len(attachments)} attachments, expected 4")
    if not any(item["payload"].get("caption") for item in attachments):
        raise RuntimeError("scheduled run omitted the report receipt")
    return rendered


def _deliver(intents: list[dict[str, Any]]) -> None:
    bridge = os.getenv("TGG_REPLY_BRIDGE_URL", "http://127.0.0.1:3011").rstrip("/")
    for intent in intents:
        request = Request(
            f"{bridge}/{intent['endpoint']}",
            data=json.dumps(intent["payload"]).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                status = int(getattr(response, "status", 0) or 0)
                payload = json.loads(response.read() or b"{}")
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise RuntimeError(f"scheduled report delivery failed: {exc}") from exc
        if status != 200 or payload.get("success") is not True:
            raise RuntimeError(f"scheduled report delivery unconfirmed: HTTP {status} {payload}")


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    from gateway.durable_jsonl_consumer import process_live_records

    raw = _fixture(args.cycle)
    result = await process_live_records(
        [_record(raw)],
        config_path=Path(args.config).resolve(),
        state_db=Path(args.state_db).resolve(),
        persistent_session=True,
    )
    if result.get("provider_errors"):
        raise RuntimeError(str(result["provider_errors"][0]))
    intents = _intents(result.get("captured_outbound") or [], Path(args.config).resolve())
    if not args.dry_run:
        _deliver(intents)
    return {
        "ok": True,
        "cycle": args.cycle,
        "dry_run": args.dry_run,
        "outbound_sent": 0 if args.dry_run else len(intents),
        "attachments": sum(item["endpoint"] == "send-media" for item in intents),
        "receipt": any(item["payload"].get("caption") for item in intents),
        "intents": intents if args.dry_run else [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle", required=True, choices=("weekly", "monthly"))
    parser.add_argument("--config", default="/home/pclaw/.hermes-christopher-tgg/config.yaml")
    parser.add_argument("--state-db", default="/home/pclaw/.hermes-christopher-tgg/state.db")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    now = datetime.now(ZoneInfo("Asia/Singapore"))
    if args.cycle == "monthly" and not 1 <= now.day <= 7:
        print(json.dumps({"ok": True, "cycle": "monthly", "skipped": "not-first-monday"}))
        return 0
    report = asyncio.run(_run(args))
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
