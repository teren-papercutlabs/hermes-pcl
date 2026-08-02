#!/usr/bin/env python3
"""Run the full MTU regression corpus and publish its durable nightly verdict."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from mtu_eval_policy import DEFAULT_POLICY, load_policy, read_json

MTU_ROOT = Path(__file__).resolve().parents[1]


def _write_json_atomic(path: Path, value: dict) -> None:
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--runtime-source", type=Path, default=Path.home() / ".hermes-mtu")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--no-post", action="store_true")
    args = parser.parse_args()
    policy = load_policy(args.policy)
    state_dir = (args.state_dir or Path(policy["nightly"]["state_dir"])).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    report_path = state_dir / "latest-report.json"
    corpus = (MTU_ROOT / policy["corpus"]["path"]).resolve()
    baseline = (MTU_ROOT / policy["corpus"]["accepted_baseline"]).resolve()
    cmd = [
        sys.executable, str(MTU_ROOT / "scripts/run_eval_corpus.py"),
        "--corpus", str(corpus), "--report", str(report_path),
        "--runtime-source", str(args.runtime_source.expanduser().resolve()),
        "--baseline-report", str(baseline),
    ]
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    report = read_json(report_path) if report_path.exists() else {}
    regression = report.get("regression") or {"status": "red", "error": proc.stderr[:1000]}
    summary = {
        "schema_version": 1,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "status": regression.get("status", "red"),
        "runner_exit_code": proc.returncode,
        "corpus_digest": (report.get("corpus") or {}).get("source_digest"),
        "runtime": (report.get("execution") or {}).get("runtime"),
        "case_count": (report.get("corpus") or {}).get("selected_case_count"),
        "turn_count": (report.get("execution") or {}).get("turn_count"),
        "regression": regression,
        "judge_status": "calibration_pending",
        "report": str(report_path),
    }
    latest = state_dir / "latest.json"
    _write_json_atomic(latest, summary)
    if not args.no_post:
        message = state_dir / "wb-message.txt"
        message.write_text(
            "MTU NIGHTLY " + summary["status"].upper()
            + f": {summary['case_count']} cases / {summary['turn_count']} turns; "
            + f"new deterministic regressions={regression.get('new_failure_count', 'unknown')}; "
            + "judge calibration pending. summary=" + str(latest),
            encoding="utf-8",
        )
        post = subprocess.run(
            ["pcl", "whiteboard", "comment", "--id", str(policy["nightly"]["consumer_wb"]),
             "--message-file", str(message)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        summary["wb_post"] = {"exit_code": post.returncode, "stdout": post.stdout[-1000:]}
        _write_json_atomic(latest, summary)
        if post.returncode:
            raise SystemExit(f"nightly ran but WB publication failed: {post.stderr[:1000]}")
    print(json.dumps(summary, sort_keys=True))
    if summary["status"] != "green":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
