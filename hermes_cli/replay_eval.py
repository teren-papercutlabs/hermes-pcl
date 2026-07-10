"""CLI for the shared PA replay-evaluation instrument."""

from __future__ import annotations

import argparse
import json
from typing import Any, Mapping

from gateway.eval_instrument import (
    EvalInstrumentError,
    audit_receipt_index,
    compare_receipts,
    load_eval_config,
    materialize_arm_runtime,
    pin_eval_config,
    record_evaluation_invocation,
    write_arm_plan,
)


def _print(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def add_replay_eval_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "replay-eval",
        help="Pin, run, and audit model qualification replays",
        description=(
            "Wrap native Hermes replay with immutable agent/corpus/tenant pins, "
            "adaptive-trace checks, receipts, and side-by-side comparison."
        ),
    )
    commands = parser.add_subparsers(dest="replay_eval_command", required=True)

    validate = commands.add_parser("validate", help="validate and pin an eval config")
    validate.add_argument("--config", required=True)

    materialize = commands.add_parser(
        "materialize", help="materialize one isolated HERMES_HOME model arm"
    )
    materialize.add_argument("--config", required=True)
    materialize.add_argument("--arm", required=True)
    materialize.add_argument("--output-dir", required=True)
    materialize.add_argument("--business-base-url")

    plan = commands.add_parser(
        "plan", help="write a native ReplayPlan for one pinned model arm"
    )
    plan.add_argument("--config", required=True)
    plan.add_argument("--arm", required=True)
    plan.add_argument("--output", required=True)
    plan.add_argument(
        "--runtime-manifest",
        help="Manifest emitted by materialize; binds loaded runtime hashes into provenance.",
    )

    check = commands.add_parser(
        "check", help="write an immutable eval/graduation receipt and fail closed"
    )
    check.add_argument("--config", required=True)
    check.add_argument("--arm", required=True)
    check.add_argument("--mode", choices=("eval", "graduation"), required=True)
    check.add_argument("--invocation-id", required=True)
    check.add_argument("--run-manifest", required=True)
    check.add_argument("--session-db", required=True)
    check.add_argument("--output-dir", required=True)
    check.add_argument("--receipt-index", required=True)
    check.add_argument("--score-manifest")

    compare = commands.add_parser(
        "compare", help="compare distinct model-arm receipts on invariant pins"
    )
    compare.add_argument("--config", required=True)
    compare.add_argument("--receipt", action="append", required=True)
    compare.add_argument("--output-dir", required=True)

    audit = commands.add_parser(
        "audit", help="prove zero missing/failed receipts in an operating-loop slice"
    )
    audit.add_argument("--index", required=True)
    audit.add_argument("--instrument-id")
    audit.add_argument("--mode", choices=("eval", "graduation"))
    audit.add_argument("--expect-invocation", action="append", default=[])

    parser.set_defaults(func=cmd_replay_eval)
    return parser


def cmd_replay_eval(args) -> None:
    try:
        command = args.replay_eval_command
        if command == "validate":
            config = load_eval_config(args.config)
            result = {"ok": True, "pins": pin_eval_config(config)}
        elif command == "materialize":
            config = load_eval_config(args.config)
            result = {
                "ok": True,
                "runtime": materialize_arm_runtime(
                    config,
                    args.arm,
                    args.output_dir,
                    business_base_url=args.business_base_url,
                ),
            }
        elif command == "plan":
            config = load_eval_config(args.config)
            result = write_arm_plan(
                config,
                args.arm,
                args.output,
                runtime_manifest=args.runtime_manifest,
            )
        elif command == "check":
            result = record_evaluation_invocation(
                config_path=args.config,
                arm_id=args.arm,
                mode=args.mode,
                invocation_id=args.invocation_id,
                run_manifest_path=args.run_manifest,
                session_db_path=args.session_db,
                output_dir=args.output_dir,
                receipt_index_path=args.receipt_index,
                score_manifest_path=args.score_manifest,
            )
        elif command == "compare":
            result = compare_receipts(
                args.config,
                args.receipt,
                output_dir=args.output_dir,
            )
        elif command == "audit":
            result = audit_receipt_index(
                args.index,
                instrument_id=args.instrument_id,
                mode=args.mode,
                expected_invocation_ids=args.expect_invocation,
            )
        else:  # pragma: no cover - argparse rejects this first
            raise EvalInstrumentError(f"unknown replay-eval command: {command}")
    except Exception as exc:
        _print(
            {
                "ok": False,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        )
        raise SystemExit(1) from exc
    _print(result)
    if not result.get("ok"):
        raise SystemExit(1)


__all__ = ["add_replay_eval_parser", "cmd_replay_eval"]
