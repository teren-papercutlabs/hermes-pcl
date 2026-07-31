#!/usr/bin/env python3
"""Materialize the narrow, secret-scan-safe Christopher runtime file set."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


TOP_LEVEL_RUNTIME_FILES = {
    "MANIFEST.in",
    "LICENSE",
    "README.md",
    "batch_runner.py",
    "cli.py",
    "hermes",
    "hermes_bootstrap.py",
    "hermes_constants.py",
    "hermes_logging.py",
    "hermes_state.py",
    "hermes_time.py",
    "model_tools.py",
    "pyproject.toml",
    "run_agent.py",
    "toolset_distributions.py",
    "toolsets.py",
    "trajectory_compressor.py",
    "utils.py",
}
PACKAGE_ROOTS = (
    "acp_adapter/",
    "agent/",
    "cron/",
    "hermes_cli/",
    "plugins/",
    "providers/",
    "tools/",
    "tui_gateway/",
)
DEPLOY_ROOT = "deploy/tgg/christopher/"
PA_DEPLOY_ROOT = "deploy/pa/"
EXCLUDED_PATHS = {
    # Not imported by the Christopher runtime. Keeping them out avoids
    # unrelated OAuth/web-dashboard surfaces and their dependencies.
    "agent/google_oauth.py",
    "hermes_cli/copilot_auth.py",
    "hermes_cli/web_server.py",
    "hermes_cli/webhook.py",
    # Explicit ownership boundary: these units belong to stream A, not this
    # Hermes deployment, and must never enter its immutable bundle.
    f"{DEPLOY_ROOT}systemd/christopher-tgg-systems.service",
    f"{DEPLOY_ROOT}systemd/singpass-pair-server.service",
    f"{DEPLOY_ROOT}inter_session_ops.md",
}
WHATSAPP_PLATFORM_FILES = {
    "gateway/platforms/__init__.py",
    "gateway/platforms/base.py",
    "gateway/platforms/helpers.py",
    "gateway/platforms/whatsapp.py",
}


def _tracked_files(app_root: Path) -> list[str]:
    output = subprocess.check_output(["git", "ls-files"], cwd=app_root, text=True)
    return [line.strip() for line in output.splitlines() if line.strip()]


def expected_runtime_files(app_root: Path) -> list[str]:
    files = _tracked_files(app_root)
    generator = f"{DEPLOY_ROOT}scripts/build_pa_agent_manifest.py"
    if (app_root / generator).is_file() and generator not in files:
        files.append(generator)

    selected: list[str] = []
    for relative in files:
        if relative in EXCLUDED_PATHS:
            continue
        keep = (
            relative in TOP_LEVEL_RUNTIME_FILES
            or relative.startswith(PACKAGE_ROOTS)
            or relative.startswith(DEPLOY_ROOT)
            or relative.startswith(PA_DEPLOY_ROOT)
            or (
                relative.startswith("gateway/")
                and not relative.startswith("gateway/platforms/")
            )
            or relative in WHATSAPP_PLATFORM_FILES
        )
        if not keep:
            continue
        # Package documentation is not executable runtime state. The top-level
        # README remains because setuptools reads it from pyproject.toml; TGG
        # deployment markdown remains because SOUL/provenance are load-bearing.
        if (
            relative.endswith(".md")
            and not relative.startswith(DEPLOY_ROOT)
            and relative not in TOP_LEVEL_RUNTIME_FILES
        ):
            continue
        selected.append(relative)
    return sorted(set(selected))


def _render(manifest_path: Path, include: list[str]) -> str:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["include"] = include
    return json.dumps(manifest, sort_keys=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    app_root = Path(args.app_root).resolve()
    manifest_path = Path(args.manifest).resolve()
    rendered = _render(manifest_path, expected_runtime_files(app_root))
    if args.check:
        if manifest_path.read_text(encoding="utf-8") != rendered:
            raise SystemExit(
                "pa-agent runtime file set drifted; run build_pa_agent_manifest.py"
            )
    else:
        manifest_path.write_text(rendered, encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "manifest": str(manifest_path),
                "file_count": len(json.loads(rendered)["include"]),
                "check": args.check,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
