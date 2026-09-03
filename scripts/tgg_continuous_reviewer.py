#!/usr/bin/env python3
"""Run one controller-owned, fresh TGG semantic-review child.

The continuous controller invokes this executable only after it has sealed a
candidate.  This process deliberately creates no gateway event, persistent
chat, service, or controller state.  It gives the fresh AIAgent a fixed,
exact tool allowlist and requires the child to call the capability's narrow
``tgg_nightly_review_submit`` tool.  The capability owns durable artifacts;
this adapter owns only the child launch/result record.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


CONTRACT = "tgg-continuous-reviewer-launch/v1"
RESULT_CONTRACT = "tgg-continuous-reviewer-child-result/v1"


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("PA97_REVIEWER_REQUEST_OBJECT_REQUIRED")
    return value


def create_or_equal(path: Path, value: Mapping[str, Any]) -> None:
    raw = canonical(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != raw:
            raise RuntimeError("PA97_REVIEWER_RESULT_CONFLICT")
        return
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def validate_launch(value: Mapping[str, Any]) -> tuple[str, list[str]]:
    batch_id = value.get("batch_id")
    tools = value.get("allowed_tools")
    if (
        value.get("contract") != CONTRACT
        or not isinstance(batch_id, str)
        or not batch_id
        or not isinstance(value.get("candidate_sha256"), str)
        or not isinstance(value.get("candidate_path"), str)
        or not isinstance(tools, list)
        or not tools
        or len(tools) != len(set(tools))
        or any(not isinstance(tool, str) or not tool for tool in tools)
        or "tgg_nightly_review_submit" not in tools
        or value.get("reviewer_provider") != "openai-codex"
    ):
        raise RuntimeError("PA97_REVIEWER_LAUNCH_INVALID")
    return batch_id, list(tools)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args(argv)
    launch_path = args.launch.resolve(strict=True)
    result_path = args.result.resolve().absolute()
    launch = read_object(launch_path)
    batch_id, allowed_tools = validate_launch(launch)
    candidate_path = Path(str(launch["candidate_path"])).resolve(strict=True)
    candidate_bytes = candidate_path.read_bytes()
    if hashlib.sha256(candidate_bytes).hexdigest() != launch["candidate_sha256"]:
        raise RuntimeError("PA97_REVIEWER_CANDIDATE_HASH_MISMATCH")
    candidate = json.loads(candidate_bytes)
    if not isinstance(candidate, dict):
        raise RuntimeError("PA97_REVIEWER_CANDIDATE_OBJECT_REQUIRED")

    # Import only after launch validation so a malformed controller artifact
    # cannot cause plugin/tool discovery or any model request.
    from hermes_cli.ephemeral_session import run_ephemeral_session

    child_id = f"pa97-review-{uuid.uuid4().hex}"
    prompt = (
        "Independently review this sealed TGG WhatsApp interval using existing WhatsApp rules only. "
        "First read the candidate and pinned policy with tgg_nightly_review_get_candidate. Audit every inventory group: identity/case association; requested versus worker-only scope; physical versus scheduling/defer/cancel/not-required status; evidence/media role and workItemIds; and human-routing completeness. "
        "Record exactly one retained, corrected, or routed judgment with a reason for each group, then submit once. "
        "For a correction, deep-copy the returned candidate and alter only adjudicated groups. Do not use tools outside the allowlist. "
        + json.dumps({"batch_id": batch_id, "candidate_sha256": launch["candidate_sha256"],
                      "review_authority": launch["review_authority"]}, sort_keys=True)
    )
    outcome, lifecycle = run_ephemeral_session(
        prompt=prompt, system_prompt="PA-97 fresh reviewer child. Use only the supplied allowlist and the sealed candidate reader.",
        model=str(launch.get("model") or ""), max_iterations=int(launch.get("max_iterations") or 24),
        allowed_tool_names=allowed_tools, provider=str(launch["reviewer_provider"]), session_prefix="pa97-review",
    )
    reviewed_path = candidate_path.with_name("continuous-reviewed-final.json")
    submitted = reviewed_path.is_file()
    result = {
        "contract": RESULT_CONTRACT,
        "batch_id": batch_id,
        "launch_sha256": hashlib.sha256(canonical(launch)).hexdigest(),
        "child_id": lifecycle["session_id"],
        "allowed_tools": allowed_tools,
        "loaded_tools": lifecycle["loaded_tools"],
        "reviewer_provider": launch["reviewer_provider"],
        "lifecycle": lifecycle,
        "status": "completed" if submitted else "submission_missing",
        "final_response_sha256": hashlib.sha256(str(outcome.get("final_response") or "").encode()).hexdigest(),
        "final_response": str(outcome.get("final_response") or "")[:4000],
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    create_or_equal(result_path, result)
    if not submitted:
        raise RuntimeError("PA97_REVIEWER_SUBMISSION_MISSING")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"PA97_REVIEWER_FAILED:{exc}", file=sys.stderr)
        raise SystemExit(1)
