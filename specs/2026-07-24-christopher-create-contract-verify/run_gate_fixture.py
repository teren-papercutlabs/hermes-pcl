#!/usr/bin/env python3
"""Run Christopher's real Hermes stack against an in-memory operator store.

The application code and deployment config are loaded from --app-root.  All
business-bridge HTTP URLs are rewritten to a loopback stub.  WhatsApp delivery
is disabled and replay delivery is capture-only.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


def _json_body(handler: BaseHTTPRequestHandler) -> dict:
    size = int(handler.headers.get("Content-Length") or 0)
    if not size:
        return {}
    value = json.loads(handler.rfile.read(size))
    return value if isinstance(value, dict) else {"value": value}


class OperatorStore:
    def __init__(self, seeds: list[dict]):
        self.lock = threading.Lock()
        self.cases = {str(case["jobNo"]): dict(case) for case in seeds}
        self.observations: list[dict] = []
        self.attention: list[dict] = []
        self.actions: list[dict] = []
        self.requests: list[dict] = []

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "cases": list(self.cases.values()),
                "observations": list(self.observations),
                "attention": list(self.attention),
                "actions": list(self.actions),
                "requests": list(self.requests),
            }


def handler_for(store: OperatorStore):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _run(self) -> None:
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            body = _json_body(self)
            entry = {
                "method": self.command,
                "path": path,
                "query": query,
                "body": body,
                "at": time.time(),
            }
            with store.lock:
                store.requests.append(entry)

                if self.command == "GET" and path == "/api/operator/cases":
                    cases = list(store.cases.values())
                    needle = str((query.get("q") or query.get("search") or [""])[0]).lower()
                    if needle:
                        cases = [
                            case
                            for case in cases
                            if needle in json.dumps(case).lower()
                        ]
                    return self._send(200, {"ok": True, "data": cases, "cases": cases})

                if self.command == "GET" and path.startswith("/api/operator/cases/"):
                    job_no = unquote(path.removeprefix("/api/operator/cases/").split("/", 1)[0])
                    case = store.cases.get(job_no)
                    if case is None:
                        return self._send(404, {"ok": False, "error": "not found"})
                    observations = [
                        item for item in store.observations if item.get("jobNo") == job_no
                    ]
                    return self._send(
                        200,
                        {"ok": True, "data": {**case, "observations": observations}},
                    )

                if self.command == "POST" and path == "/api/operator/cases/create":
                    job_no = str(body.get("jobNo") or f"WA/JOB/FIXTURE/{len(store.cases)+1:04d}")
                    created = job_no not in store.cases
                    if created:
                        store.cases[job_no] = {**body, "jobNo": job_no}
                    return self._send(
                        200,
                        {
                            "ok": True,
                            "data": {
                                "caseId": list(store.cases).index(job_no) + 1,
                                "jobNo": job_no,
                                "created": created,
                            },
                        },
                    )

                if self.command == "POST" and path.endswith("/observations"):
                    job_no = unquote(
                        path.removeprefix("/api/operator/cases/").removesuffix("/observations")
                    )
                    fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
                    refs = tuple(
                        body.get("sourceRefs")
                        or body.get("source_refs")
                        or fields.get("source_refs")
                        or fields.get("sourceRefs")
                        or []
                    )
                    normalized_refs = tuple(sorted(set(str(ref) for ref in refs)))
                    existing = next(
                        (
                            item
                            for item in store.observations
                            if item.get("jobNo") == job_no
                            and tuple(
                                sorted(
                                    set(
                                        str(ref)
                                        for ref in (
                                            item.get("sourceRefs")
                                            or item.get("source_refs")
                                            or (item.get("fields") or {}).get("source_refs")
                                            or (item.get("fields") or {}).get("sourceRefs")
                                            or []
                                        )
                                    )
                                )
                            )
                            == normalized_refs
                        ),
                        None,
                    )
                    if existing is not None and normalized_refs:
                        return self._send(
                            200,
                            {
                                "ok": True,
                                "data": {"observationId": existing["observationId"]},
                            },
                        )
                    duplicate = {
                        **body,
                        "jobNo": job_no,
                        "observationId": f"obs-{len(store.observations)+1}",
                    }
                    store.observations.append(duplicate)
                    return self._send(
                        200,
                        {"ok": True, "data": {"observationId": duplicate["observationId"]}},
                    )

                if self.command == "PATCH" and path.endswith("/state"):
                    job_no = unquote(
                        path.removeprefix("/api/operator/cases/").removesuffix("/state")
                    )
                    if job_no in store.cases:
                        store.cases[job_no].update(body)
                    return self._send(200, {"ok": True, "data": store.cases.get(job_no, body)})

                if self.command == "POST" and path == "/api/operator/attention-items":
                    item = {**body, "id": f"attn-{len(store.attention)+1}"}
                    store.attention.append(item)
                    return self._send(200, {"ok": True, "data": item})

                if self.command == "POST" and path == "/api/operator/agent-actions":
                    store.actions.append(body)
                    return self._send(200, {"ok": True, "data": body})

                # Reads outside the case spine and non-case writes are recorded
                # but deliberately return an empty successful envelope.
                return self._send(200, {"ok": True, "data": [], "cases": []})

        do_GET = _run
        do_POST = _run
        do_PATCH = _run
        do_PUT = _run
        do_DELETE = _run

        def log_message(self, _format: str, *_args) -> None:
            return

    return Handler


def _load_smoke_module(app_root: Path):
    path = app_root / "deploy/tgg/christopher/scripts/run_isolated_smoke.py"
    spec = importlib.util.spec_from_file_location("integration_smoke", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _db_rows(path: Path, table: str) -> list[dict]:
    if not path.is_file():
        return []
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            return []
        return [dict(row) for row in conn.execute(f'SELECT * FROM "{table}"')]
    finally:
        conn.close()


async def _fixture_run(
    consumer, run_root: Path, sources: list[Path], config: Path, repeat: int
):
    results = []
    state_db = config.parent / "state.db"
    sequence = sources * repeat
    for index, source in enumerate(sequence):
        step = run_root / f"fixture-pass-{index+1}"
        step.mkdir(parents=True)
        fixture_copy = step / "fixture.jsonl"
        shutil.copyfile(source, fixture_copy)
        namespace = argparse.Namespace(
            test_root=str(run_root),
            source=str(fixture_copy),
            cursor=str(step / "cursor.json"),
            inbox=str(step / "inbox.db"),
            config=str(config),
            state_db=str(state_db),
            report=str(step / "consumer-report.json"),
            run_id=f"gate-fixture-{index+1}-{uuid.uuid4().hex[:10]}",
            max_records=100,
        )
        try:
            rc = await consumer.run_fixture(namespace)
            results.append({"pass": index + 1, "rc": rc})
        except Exception as exc:
            results.append(
                {"pass": index + 1, "error": f"{type(exc).__name__}: {exc}"}
            )
    return results


async def _concurrency_run(
    consumer, run_root: Path, source: Path, config: Path, site_concurrency: int
):
    state_db = config.parent / "state.db"
    inbox_path = run_root / "concurrency-inbox.db"
    cursor = run_root / "concurrency-cursor.json"
    consumer.initialize_cursor(source, cursor, position="start")
    gate = run_root / "processing-gate.json"
    gate.write_text(
        json.dumps(
            {
                "version": 1,
                "enabled": True,
                "generation": 1,
                "changed_at": "2026-07-24T00:00:00+00:00",
                "change_run_id": "fixture-gate",
            }
        )
    )
    namespace = argparse.Namespace(
        source=str(source),
        cursor=str(cursor),
        inbox=str(inbox_path),
        config=str(config),
        state_db=str(state_db),
        processing_gate=str(gate),
        lock_file=str(run_root / "consumer.lock"),
        status_file=str(run_root / "consumer-status.json"),
        poll_seconds=0.01,
        max_records=100,
        site_concurrency=site_concurrency,
        chat_batch_size=25,
        retention_batch_size=25,
        once=True,
    )
    try:
        rc = await consumer.run_consumer(namespace)
        return {"rc": rc, "status": json.loads((run_root / "consumer-status.json").read_text())}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-root", required=True)
    parser.add_argument("--secrets-env", required=True)
    parser.add_argument("--test-root", required=True)
    parser.add_argument("--fixture-file", action="append", required=True)
    parser.add_argument("--seed-file", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--mode", choices=("fixture", "concurrency"), default="fixture")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--site-concurrency", type=int, default=3)
    args = parser.parse_args()

    app_root = Path(args.app_root).resolve()
    test_root = Path(args.test_root).resolve()
    test_root.mkdir(parents=True, exist_ok=True)
    run_root = test_root / f"gate-{args.mode}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    run_root.mkdir(mode=0o700)
    seeds = json.loads(Path(args.seed_file).read_text())
    store = OperatorStore(seeds)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(store))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        smoke = _load_smoke_module(app_root)
        secret_home = run_root / "secret-source"
        secret_home.mkdir()
        allowed = []
        for line in Path(args.secrets_env).read_text().splitlines():
            if line.startswith(("OPENAI_API_KEY=", "GEMINI_API_KEY=", "GEMINI_API_KEY_PCL_PA_SHARED=")):
                allowed.append(line)
        (secret_home / ".env").write_text("\n".join(allowed) + "\n")
        config = smoke._prepare_home(
            run_root / "home",
            slot_root=app_root
            / "deploy/tgg/christopher/runtime-slots/gpt-5.6-luna-low",
            live_env=secret_home / ".env",
            soul_path=app_root / "deploy/tgg/christopher/SOUL.md",
            stub_url=f"http://127.0.0.1:{server.server_port}",
        )
        if args.mode == "concurrency":
            import yaml

            config_data = yaml.safe_load(config.read_text())
            if isinstance((config_data.get("pa") or {}).get("media_retention"), dict):
                config_data["pa"]["media_retention"]["enabled"] = False
            config.write_text(yaml.safe_dump(config_data, sort_keys=False))
        # Fixture evidence is intentionally metadata-only; retaining dummy
        # image bytes is not part of this gate.
        sources = []
        for index, fixture_value in enumerate(args.fixture_file, 1):
            fixture_path = Path(fixture_value).resolve()
            source = run_root / f"source-{index}.jsonl"
            shutil.copyfile(fixture_path, source)
            sources.append(source)
        os.environ["HERMES_HOME"] = str(run_root / "home")
        os.environ["HERMES_QUIET"] = "1"
        from gateway import durable_jsonl_consumer as consumer

        if args.mode == "fixture":
            result = asyncio.run(
                _fixture_run(consumer, run_root, sources, config, args.repeat)
            )
        else:
            result = asyncio.run(
                _concurrency_run(
                    consumer, run_root, sources[0], config, args.site_concurrency
                )
            )
        state_db = config.parent / "state.db"
        payload = {
            "integration_commit": os.popen(
                f"git -C {json.dumps(str(app_root))} rev-parse HEAD"
            ).read().strip(),
            "mode": args.mode,
            "result": result,
            "operator": store.snapshot(),
            "pa_turns": _db_rows(state_db, "pa_turns"),
            "pa_tool_calls": _db_rows(state_db, "pa_tool_calls"),
            "run_root": str(run_root),
        }
        report = Path(args.report).resolve()
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
        print(json.dumps({"ok": True, "report": str(report), "run_root": str(run_root)}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
