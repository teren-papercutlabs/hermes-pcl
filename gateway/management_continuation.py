"""Authenticated enqueue for a retained management case-list continuation.

No model or delivery occurs here. The durable consumer owns those steps.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping


def require_complete_list(envelope: Mapping[str, Any]) -> Mapping[str, Any]:
    """Read the existing registered coordinator before permitting final output.

    A failed terminal counts toward coordination progress, but is not a
    substantive answer. Do not mistake the coordinator's complete bit for one.
    """
    from tools.registry import registry
    name = "tgg_whatsapp_case_list_status"
    tool = registry.get_entry(name)
    if tool is None or tool.toolset != "tgg-per-case-whatsapp-coordinator":
        raise ValueError("CONTINUATION_COORDINATOR_UNAVAILABLE")
    response = json.loads(registry.dispatch(name, {"list_id": envelope["list_id"]}))
    state = response.get("list")
    if response.get("ok") is not True or not isinstance(state, dict):
        raise ValueError("CONTINUATION_LIST_STATUS_UNAVAILABLE")
    items = state.get("items")
    if state.get("list_id") != envelope["list_id"] \
            or state.get("management_request_id") != envelope["management_request_id"] \
            or not isinstance(items, list) \
            or [item.get("job_no") for item in items] != envelope["jobs"]:
        raise ValueError("CONTINUATION_LIST_BINDING_INVALID")
    if state.get("complete") is not True or any(
        item.get("status") not in ("completed", "use_recorded") or not item.get("result")
        for item in items
    ):
        raise ValueError("CONTINUATION_LIST_NOT_SUBSTANTIVELY_COMPLETE")
    return state


def authenticate_continuation(config: Mapping[str, Any], credential: str) -> None:
    """Shared by the entry and enqueue boundary, before opening mutable stores."""
    token_env = config.get("token_env")
    expected = os.environ.get(token_env, "") if isinstance(token_env, str) else ""
    if config.get("enabled") is not True or not expected or not isinstance(credential, str) \
            or not hmac.compare_digest(expected.encode(), credential.encode()):
        raise ValueError("CONTINUATION_UNAUTHORIZED")


def enqueue_continuation(*, config: Mapping[str, Any], request: Mapping[str, Any],
                         credential: str, inbox: Any, session_db: Any) -> dict[str, Any]:
    """Authenticate before reading retained records, then use the mailbox API.

    Config and stores are supplied by the runtime, never by the request body.
    The authenticated owner binds the existing list to the original inbound.
    """
    authenticate_continuation(config, credential)
    fields = {"original_message_id", "management_request_id", "list_id"}
    if set(request) != fields or any(not isinstance(request[key], str) or not request[key]
                                     or len(request[key]) > 256 for key in fields):
        raise ValueError("CONTINUATION_REQUEST_INVALID")
    list_id = request["list_id"]
    if not re.fullmatch(r"case-list-[0-9]{14}-[a-f0-9]{10}", list_id):
        raise ValueError("CONTINUATION_LIST_INVALID")
    records = inbox.message_id_selection([request["original_message_id"]])
    if len(records) != 1 or records[0].chat_id != config.get("management_chat_id"):
        raise ValueError("CONTINUATION_ORIGINAL_REQUEST_INVALID")
    root = Path(config["coordinator_receipt_root"])
    manifest_path = root / "lists" / list_id / "manifest.json"
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    if manifest.get("contract") != "tgg-per-case-whatsapp-list/v1" \
            or manifest.get("list_id") != list_id \
            or manifest.get("management_request_id") != request["management_request_id"]:
        raise ValueError("CONTINUATION_LIST_BINDING_INVALID")
    items = manifest.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise ValueError("CONTINUATION_LIST_ITEMS_INVALID")
    jobs = [item.get("job_no") if isinstance(item, dict) else None for item in items]
    if any(not isinstance(job, str) or not re.fullmatch(r"(?:AM|SK|PG)/JOB/\d{4}/\d{1,5}", job)
           for job in jobs) or len(set(jobs)) != len(jobs):
        raise ValueError("CONTINUATION_LIST_ITEMS_INVALID")
    envelope = {"contract": "management-list-continuation/v1", **request,
                "chat_id": records[0].chat_id, "jobs": jobs,
                "manifest_sha256": hashlib.sha256(raw).hexdigest()}
    identity = json.dumps([records[0].chat_id, request["original_message_id"], list_id,
                           "followup"], separators=(",", ":"))
    return session_db.create_session_mailbox_message(
        agent_id=config["agent_id"], from_session_name=config["from_peer"],
        to_session_name=config["to_peer"],
        body=json.dumps(envelope, sort_keys=True, separators=(",", ":")),
        source_message_id=request["original_message_id"], idempotency_key=identity,
    )
