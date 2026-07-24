"""Read-only PA message retrieval tools backed by the canonical store."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from gateway.pa_message_store import MessageStore, MessageStoreError
from tools.registry import registry, tool_error, tool_result


DESCRIPTION_PROMPT = (
    "Describe this operational photo in one factual sentence. Include visible "
    "objects, condition, damage, work state, and readable identifiers when "
    "present. Do not infer facts that are not visible."
)


def _store_path() -> Path | None:
    env = os.getenv("HERMES_PA_MESSAGE_STORE_DB", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    try:
        from hermes_cli.config import read_raw_config

        config = read_raw_config()
        pa = config.get("pa") if isinstance(config, Mapping) else None
        section = pa.get("message_store") if isinstance(pa, Mapping) else None
        value = section.get("db_path") if isinstance(section, Mapping) else None
        if value:
            return Path(str(value)).expanduser().resolve()
    except Exception:
        return None
    return None


def _available() -> bool:
    path = _store_path()
    return bool(path and path.is_file())


def _store() -> MessageStore:
    path = _store_path()
    if not path or not path.is_file():
        raise MessageStoreError("PA message store is not configured")
    return MessageStore(path)


def _epoch(value: Any, name: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise MessageStoreError(f"{name} must be an epoch or ISO timestamp")
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.replace(".", "", 1).isdigit():
        return int(float(text))
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise MessageStoreError(f"{name} ISO timestamp must include a timezone")
    return int(parsed.timestamp())


async def _describe_candidate(store: MessageStore, message_id: str) -> bool:
    candidate = store.image_description_candidate(message_id)
    if not candidate:
        return False
    image_path = store.first_local_image(candidate)
    if not image_path:
        return False
    from tools.vision_tools import vision_analyze_tool

    raw = await vision_analyze_tool(image_path, DESCRIPTION_PROMPT)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MessageStoreError("vision descriptor returned invalid JSON") from exc
    description = str(result.get("analysis") or "").strip()
    if not result.get("success") or not description:
        raise MessageStoreError(
            f"vision descriptor failed: {result.get('error') or description or 'unknown'}"
        )
    return store.set_description_once(message_id, description)


async def _handle_search(args: Mapping[str, Any], **_kwargs: Any) -> str:
    try:
        store = _store()
        rows = store.search(
            str(args.get("query") or ""),
            chat=str(args.get("chat") or "").strip() or None,
            sender=str(args.get("sender") or "").strip() or None,
            from_ts=_epoch(args.get("from"), "from"),
            to_ts=_epoch(args.get("to"), "to"),
            limit=int(args.get("limit") or 20),
        )
        changed = False
        for row in rows:
            if row.get("has_media") and not row.get("description"):
                changed = await _describe_candidate(store, str(row["message_id"])) or changed
        if changed:
            rows = store.search(
                str(args.get("query") or ""),
                chat=str(args.get("chat") or "").strip() or None,
                sender=str(args.get("sender") or "").strip() or None,
                from_ts=_epoch(args.get("from"), "from"),
                to_ts=_epoch(args.get("to"), "to"),
                limit=int(args.get("limit") or 20),
            )
        return tool_result(
            {
                "ok": True,
                "count": len(rows),
                "messages": rows,
                "citation_field": "message_id",
            }
        )
    except Exception as exc:
        return tool_error(exc)


async def _handle_context(args: Mapping[str, Any], **_kwargs: Any) -> str:
    try:
        store = _store()
        message_id = str(args.get("message_id") or "").strip()
        if not message_id:
            raise MessageStoreError("message_id is required")
        rows = store.context(message_id, window=int(args.get("window") or 3))
        changed = False
        for row in rows:
            if row.get("has_media") and not row.get("description"):
                changed = await _describe_candidate(store, str(row["message_id"])) or changed
        if changed:
            rows = store.context(message_id, window=int(args.get("window") or 3))
        return tool_result(
            {
                "ok": True,
                "count": len(rows),
                "messages": rows,
                "citation_field": "message_id",
            }
        )
    except Exception as exc:
        return tool_error(exc)


MESSAGES_SEARCH_SCHEMA = {
    "name": "messages_search",
    "description": (
        "Search the canonical PA message corpus with BM25 ranking. Returns "
        "message ids for citation and never returns raw image data."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Words or phrase to search."},
            "chat": {"type": "string", "description": "Exact chat id filter."},
            "sender": {"type": "string", "description": "Exact sender id filter."},
            "from": {"description": "Inclusive epoch or timezone-aware ISO timestamp."},
            "to": {"description": "Inclusive epoch or timezone-aware ISO timestamp."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

MESSAGE_CONTEXT_SCHEMA = {
    "name": "message_context",
    "description": (
        "Return messages around one cited message id from the same chat. "
        "Descriptions are text-only; raw image data is never returned."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message_id": {"type": "string", "description": "Cited message id."},
            "window": {
                "type": "integer",
                "minimum": 0,
                "maximum": 20,
                "description": "Messages before and after the cited row.",
            },
        },
        "required": ["message_id"],
        "additionalProperties": False,
    },
}


registry.register(
    name="messages_search",
    toolset="pa-business",
    schema=MESSAGES_SEARCH_SCHEMA,
    handler=_handle_search,
    check_fn=_available,
    is_async=True,
)

registry.register(
    name="message_context",
    toolset="pa-business",
    schema=MESSAGE_CONTEXT_SCHEMA,
    handler=_handle_context,
    check_fn=_available,
    is_async=True,
)
