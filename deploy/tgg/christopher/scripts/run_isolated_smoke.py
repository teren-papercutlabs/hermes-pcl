#!/usr/bin/env python3
"""Run a consumer-layer Christopher turn in a fixture-only Hermes home."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import stat
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml


ALLOWED_SECRET_KEYS = {
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
}


class _OperatorStub(BaseHTTPRequestHandler):
    requests_total = 0
    mutation_requests = 0

    def _reply(self) -> None:
        type(self).requests_total += 1
        if self.command in {"POST", "PATCH", "PUT", "DELETE"}:
            type(self).mutation_requests += 1
        body = json.dumps({"ok": True, "data": [], "cases": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _reply
    do_POST = _reply
    do_PATCH = _reply
    do_PUT = _reply
    do_DELETE = _reply

    def log_message(self, _format: str, *_args) -> None:
        return


def _rewrite_http_urls(value, *, base_url: str):
    if isinstance(value, dict):
        return {
            key: _rewrite_http_urls(item, base_url=base_url)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_rewrite_http_urls(item, base_url=base_url) for item in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        original = urlsplit(value)
        base = urlsplit(base_url)
        return urlunsplit((base.scheme, base.netloc, original.path, original.query, ""))
    return value


def _copy_test_env(source: Path, target: Path) -> None:
    lines = []
    for raw in source.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.removeprefix("export ").split("=", 1)[0].strip()
        if key in ALLOWED_SECRET_KEYS:
            lines.append(stripped.removeprefix("export "))
    if not any(line.startswith("OPENAI_API_KEY=") for line in lines):
        raise RuntimeError("live Hermes env has no OPENAI_API_KEY")
    lines.append("CHRISTOPHER_TGG_PS_SERVICE_TOKEN=fixture-only")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _prepare_home(
    run_root: Path,
    *,
    slot_root: Path,
    live_env: Path,
    soul_path: Path,
    stub_url: str,
) -> Path:
    run_root.mkdir(parents=True, mode=0o700)
    config = yaml.safe_load((slot_root / "config.yaml").read_text(encoding="utf-8"))
    config["pa"]["enabled"] = True
    config["pa"]["constitution_path"] = str(
        run_root / "christopher_tgg_constitution.yaml"
    )
    config["platforms"]["whatsapp"]["enabled"] = False
    config["platforms"]["whatsapp"]["extra"]["session_path"] = str(
        run_root / "whatsapp-v2" / "session"
    )
    config["pa"]["overlay"]["client"]["business_bridge"] = _rewrite_http_urls(
        config["pa"]["overlay"]["client"]["business_bridge"], base_url=stub_url
    )
    for operation in config["pa"]["overlay"]["client"]["business_bridge"][
        "operations"
    ].values():
        if operation.get("type") != "http":
            continue
        if not str(operation.get("url", "")).startswith(f"{stub_url}/"):
            raise RuntimeError(
                "fixture left a business-bridge HTTP URL outside the stub"
            )
    (run_root / "config.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    shutil.copyfile(
        slot_root / "christopher_tgg_constitution.yaml",
        run_root / "christopher_tgg_constitution.yaml",
    )
    shutil.copyfile(soul_path, run_root / "SOUL.md")
    _copy_test_env(live_env, run_root / ".env")
    return run_root / "config.yaml"


def _retain_runs(root: Path, *, keep: int, current: Path) -> None:
    candidates = sorted(
        (
            path
            for path in root.iterdir()
            if path.is_dir() and path.name.startswith("smoke-") and path != current
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates[max(0, keep - 1) :]:
        # The prefix and parent checks are the deletion manifest. Nothing
        # outside the smoke-run root can enter this cleanup.
        if path.parent == root and path.name.startswith("smoke-"):
            shutil.rmtree(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", required=True)
    parser.add_argument("--live-home", required=True)
    parser.add_argument("--test-root", required=True)
    parser.add_argument("--slot-file", required=True)
    parser.add_argument("--report", required=True)
    # Optional fixture overrides. The defaults are the deploy-verification
    # probe (a management chat asked to reply READY); overriding them lets the
    # same isolated rig exercise a real turn shape — e.g. a management Q&A —
    # without touching the live service.
    parser.add_argument("--chat-id", default="120363409954029949@g.us")
    parser.add_argument("--chat-name", default="Christopher Deployment Verification")
    parser.add_argument(
        "--body",
        default="Synthetic deployment verification. Reply with exactly READY.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    app_root = Path(args.app_root).resolve()
    live_home = Path(args.live_home).resolve()
    test_root = Path(args.test_root).resolve()
    slot = Path(args.slot_file).read_text(encoding="utf-8").strip()
    if slot not in {"gpt-5.4-mini", "gpt-5.6-luna", "gpt-5.6-luna-low"}:
        raise RuntimeError(f"invalid engine slot {slot!r}")
    slot_root = app_root / "deploy" / "tgg" / "christopher" / "runtime-slots" / slot
    if not slot_root.is_dir():
        raise RuntimeError(f"missing engine slot directory {slot_root}")

    test_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    test_root.chmod(0o700)
    run_root = test_root / f"smoke-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    fixture_path = run_root / "fixture.jsonl"
    report_path = Path(args.report).resolve()
    if report_path != test_root and test_root not in report_path.parents:
        raise RuntimeError("report path must stay inside test root")

    _OperatorStub.requests_total = 0
    _OperatorStub.mutation_requests = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OperatorStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        stub_url = f"http://127.0.0.1:{server.server_port}"
        config_path = _prepare_home(
            run_root,
            slot_root=slot_root,
            live_env=live_home / ".env",
            soul_path=app_root / "deploy" / "tgg" / "christopher" / "SOUL.md",
            stub_url=stub_url,
        )
        fixture = {
            "messageId": f"synthetic-{uuid.uuid4().hex}",
            "chatId": args.chat_id,
            "senderId": "synthetic-verifier",
            "senderName": "Synthetic Verifier",
            "chatName": args.chat_name,
            "isGroup": True,
            "body": args.body,
            "hasMedia": False,
            "mediaType": None,
            "mediaUrls": [],
            "mentionedIds": [],
            "timestamp": int(time.time()),
            "fromMe": False,
            "historySync": False,
        }
        fixture_path.write_text(json.dumps(fixture) + "\n", encoding="utf-8")

        # HERMES_HOME must be set before importing Hermes runtime modules.
        os.environ["HERMES_HOME"] = str(run_root)
        os.environ["HERMES_QUIET"] = "1"
        from gateway import durable_jsonl_consumer as consumer

        run_id = f"deploy-smoke-{uuid.uuid4().hex[:12]}"
        namespace = argparse.Namespace(
            test_root=str(run_root),
            source=str(fixture_path),
            cursor=str(run_root / "consumer" / "cursor.json"),
            inbox=str(run_root / "consumer" / "inbox.db"),
            config=str(config_path),
            state_db=str(run_root / "state.db"),
            report=str(run_root / "consumer-report.json"),
            run_id=run_id,
            max_records=10,
        )
        rc = asyncio.run(consumer.run_fixture(namespace))
        if rc != 0:
            raise RuntimeError(f"consumer fixture returned {rc}")
        report = json.loads((run_root / "consumer-report.json").read_text())
        # Every business-bridge HTTP URL was rewritten to the fixture stub
        # above. Mutation-shaped calls can therefore exercise the constitution
        # without reaching client state; report them separately from real
        # client mutation requests.
        report["client_mutation_requests"] = 0
        report["operator_stub_mutation_attempts"] = _OperatorStub.mutation_requests
        report["operator_stub_requests_total"] = _OperatorStub.requests_total
        report["external_outbound_sent"] = 0
        report["run_root"] = str(run_root)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(report, sort_keys=True))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    _retain_runs(test_root, keep=3, current=run_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
