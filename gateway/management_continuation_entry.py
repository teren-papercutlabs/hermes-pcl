"""Queue one retained management continuation; never invoke a model or send.

Run with the configured Hermes profile and its service environment. Input is
one JSON object on stdin with credential and request. No secrets go in argv.
"""
from __future__ import annotations

import json
import sys
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping

from gateway.management_continuation import authenticate_continuation, enqueue_continuation


def submit(config: Mapping[str, Any], packet: Mapping[str, Any]) -> dict[str, Any]:
    if set(packet) != {"credential", "request"} or not isinstance(packet["request"], dict):
        raise ValueError("CONTINUATION_PACKET_INVALID")
    authenticate_continuation(config, packet["credential"])
    # Do not silently create a new empty inbox/session database on a bad path.
    inbox_path = Path(config["inbox_db"])
    state_path = Path(config["state_db"])
    if not inbox_path.is_file() or not state_path.is_file():
        raise ValueError("CONTINUATION_EXISTING_STORES_REQUIRED")
    from gateway.durable_jsonl_consumer import DurableInbox
    from hermes_state import SessionDB
    inbox = DurableInbox(inbox_path)
    with closing(SessionDB(db_path=state_path)) as db:
        return enqueue_continuation(config=config, request=packet["request"],
                                    credential=packet["credential"], inbox=inbox, session_db=db)


def main() -> int:
    try:
        import yaml
        from hermes_constants import get_hermes_home
        # Config is profile-owned, not a caller-supplied request path.
        raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text()) or {}
        config = raw.get("pa", {}).get("management_continuation", {})
        encoded = sys.stdin.buffer.read(16385)
        if len(encoded) > 16384:
            raise ValueError("CONTINUATION_PACKET_TOO_LARGE")
        packet = json.loads(encoded)
        if not isinstance(packet, dict):
            raise ValueError("CONTINUATION_PACKET_INVALID")
        result = submit(config, packet)
        print(json.dumps({"ok": True, "mailbox": result}, sort_keys=True))
        return 0
    except Exception as exc:
        # Input and credential values must not travel in error output.
        code = str(exc)
        if not code.startswith("CONTINUATION_") or not code.replace("_", "").isalnum():
            code = "CONTINUATION_ENTRY_FAILED"
        print(json.dumps({"ok": False, "error": code}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
