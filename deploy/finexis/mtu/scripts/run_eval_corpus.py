#!/usr/bin/env python3
"""Run the MTU PA eval corpus against a disposable runtime copy."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gateway.pa_eval import PAEvalCorpus, run_pa_eval_corpus


LIVE_MTU_HOME = (Path.home() / ".hermes-mtu").resolve()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stage_runtime(source: Path, target: Path) -> dict[str, Any]:
    source = source.expanduser().resolve()
    target = target.expanduser().resolve()
    if target == LIVE_MTU_HOME or LIVE_MTU_HOME in target.parents:
        raise ValueError("runtime copy must not be ~/.hermes-mtu or a child of it")
    if source == target:
        raise ValueError("runtime source and copy must be different")
    for name in ("config.yaml", "mtu_constitution.yaml", "SOUL.md", ".env"):
        if not (source / name).is_file():
            raise ValueError(f"runtime source is missing {name}")

    target.mkdir(parents=True, exist_ok=False)
    for name in ("mtu_constitution.yaml", "SOUL.md", ".env"):
        shutil.copy2(source / name, target / name)
    (target / ".env").chmod(stat.S_IRUSR | stat.S_IWUSR)

    config = yaml.safe_load((source / "config.yaml").read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("runtime config must be a YAML mapping")
    config.setdefault("pa", {})["constitution_path"] = str(
        target / "mtu_constitution.yaml"
    )
    (target / "config.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )
    from hermes_cli.pa_compose import sync_pa_knowledge

    sync_pa_knowledge(
        source,
        target / "mtu_constitution.yaml",
        target / "knowledge",
        target / "knowledge-sync.manifest.json",
    )
    return {
        "mode": "disposable_copy",
        "source_home": str(source),
        "copy_home": str(target),
        "live_home_written": False,
        "model_provider": (config.get("model") or {}).get("provider"),
        "model": (config.get("model") or {}).get("default"),
        "source_config_sha256": _sha256(source / "config.yaml"),
        "source_constitution_sha256": _sha256(source / "mtu_constitution.yaml"),
        "copied_config_sha256": _sha256(target / "config.yaml"),
        "copied_constitution_sha256": _sha256(target / "mtu_constitution.yaml"),
    }


async def _run(args: argparse.Namespace, runtime_manifest: dict[str, Any]) -> dict[str, Any]:
    # Import after HERMES_HOME is bound so every profile-aware path resolves to
    # the disposable copy rather than the live MTU home.
    from gateway.run import GatewayRunner

    corpus = PAEvalCorpus.from_path(args.corpus)
    runner = GatewayRunner()
    try:
        return await run_pa_eval_corpus(
            corpus,
            runner=runner,
            tags=args.tag,
            honor_draws=args.honor_draws,
            platform="telegram",
            runtime_manifest=runtime_manifest,
        )
    finally:
        session_db = getattr(runner, "_session_db", None)
        if session_db is not None and hasattr(session_db, "close"):
            session_db.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run pa-eval-case corpus through native Hermes replay"
    )
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument(
        "--runtime-source",
        type=Path,
        default=LIVE_MTU_HOME,
        help="Read-only runtime home to copy (default: ~/.hermes-mtu)",
    )
    parser.add_argument(
        "--runtime-copy",
        type=Path,
        help="Disposable HERMES_HOME. Defaults to a new /tmp directory.",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        help="Run cases matching any supplied tag. Repeatable; empty runs all.",
    )
    parser.add_argument(
        "--honor-draws",
        action="store_true",
        help="Run every declared draw instead of one deterministic pass per case.",
    )
    parser.add_argument(
        "--keep-runtime-copy",
        action="store_true",
        help="Keep the disposable copy for debugging (never the default).",
    )
    parser.add_argument(
        "--instruction-placement",
        choices=("skill", "constitution"),
        default="skill",
        help="A/B axis: task-adjacent final skill or constitution-middle instruction.",
    )
    parser.add_argument(
        "--output-mode",
        choices=("marker", "verbatim"),
        default="marker",
        help="A/B axis: deterministic placeholder splice or model-authored verbatim text.",
    )
    return parser


def _configure_ab_mode(runtime_copy: Path, args: argparse.Namespace) -> None:
    path = runtime_copy / "mtu_constitution.yaml"
    constitution = yaml.safe_load(path.read_text(encoding="utf-8"))
    policy = constitution["job_briefs"]["bor_generation"]["response_policy"]["output_assembly"]
    policy["placement"] = args.instruction_placement
    policy["mode"] = args.output_mode
    path.write_text(
        yaml.safe_dump(constitution, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _deterministic_exit_code(report: dict[str, Any]) -> int:
    failed = int((report.get("deterministic_summary") or {}).get("failed") or 0)
    return 1 if failed else 0


def main() -> None:
    args = _parser().parse_args()
    runtime_copy = (
        args.runtime_copy.expanduser().resolve()
        if args.runtime_copy
        else Path(tempfile.mkdtemp(prefix="mtu-pa-eval-")).resolve()
    )
    created_by_mkdtemp = args.runtime_copy is None
    if created_by_mkdtemp:
        runtime_copy.rmdir()  # _stage_runtime owns the fail-if-exists create.

    try:
        manifest = _stage_runtime(args.runtime_source, runtime_copy)
        _configure_ab_mode(runtime_copy, args)
        manifest["instruction_placement"] = args.instruction_placement
        manifest["output_mode"] = args.output_mode
        manifest["copied_constitution_sha256"] = _sha256(runtime_copy / "mtu_constitution.yaml")
        os.environ["HERMES_HOME"] = str(runtime_copy)
        report = asyncio.run(_run(args, manifest))
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        exit_code = _deterministic_exit_code(report)
        print(json.dumps({
            "ok": exit_code == 0,
            "report": str(args.report.resolve()),
            "selected_case_count": report["corpus"]["selected_case_count"],
            "turn_count": report["execution"]["turn_count"],
            "deterministic_summary": report["deterministic_summary"],
        }, sort_keys=True))
        if exit_code:
            raise SystemExit(exit_code)
    finally:
        if runtime_copy.exists() and not args.keep_runtime_copy:
            shutil.rmtree(runtime_copy)


if __name__ == "__main__":
    main()
