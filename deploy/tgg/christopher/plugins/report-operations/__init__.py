"""Christopher-local typed adapter for the configured report API.

This plugin is installed into Christopher's private Hermes home by the TGG
deployment bootstrap.  It deliberately contains no generator or canonical
store logic: every tool is transport, response validation, and (for signed
report references) download into the configured retained-media directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from hermes_cli.config import read_raw_config


TOOLSET = "report-operations"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CYCLES = {"weekly", "monthly"}


def _tool_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _tool_error(error: Any) -> str:
    return json.dumps({"error": str(error)}, ensure_ascii=False)


def _section() -> Mapping[str, Any]:
    config = read_raw_config()
    pa = config.get("pa") if isinstance(config, Mapping) else None
    section = pa.get("report_operations") if isinstance(pa, Mapping) else None
    return section if isinstance(section, Mapping) else {}


def _available() -> bool:
    section = _section()
    auth = section.get("auth") if isinstance(section, Mapping) else None
    token_env = str((auth or {}).get("token_env") or "") if isinstance(auth, Mapping) else ""
    return bool(section.get("enabled") and section.get("base_url") and token_env and os.getenv(token_env))


def _identifier(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not _RUN_ID_RE.fullmatch(text):
        raise ValueError(f"{label} is missing or invalid")
    return text


def _cycle(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text not in _CYCLES:
        raise ValueError("cycle must be weekly or monthly")
    return text


def _operation(name: str) -> Mapping[str, Any]:
    operations = _section().get("operations")
    operation = operations.get(name) if isinstance(operations, Mapping) else None
    if not isinstance(operation, Mapping):
        raise ValueError(f"report operation {name!r} is not configured")
    return operation


def _headers() -> dict[str, str]:
    section = _section()
    auth = section.get("auth")
    if not isinstance(auth, Mapping):
        raise ValueError("report API auth is not configured")
    token_env = str(auth.get("token_env") or "").strip()
    token = os.getenv(token_env, "") if token_env else ""
    if not token:
        raise ValueError(f"report API credential {token_env or '<unset>'} is unavailable")
    header = str(auth.get("header") or "Authorization")
    scheme = str(auth.get("scheme") or "Bearer").strip()
    value = f"{scheme} {token}" if scheme else token
    configured = section.get("headers")
    headers = {
        str(key): str(item)
        for key, item in (configured.items() if isinstance(configured, Mapping) else ())
    }
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        **headers,
        header: value,
    }


def _request(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    section = _section()
    operation = _operation(name)
    base_url = str(section.get("base_url") or "").rstrip("/")
    path = str(operation.get("path") or "").strip()
    if not base_url or not path.startswith("/"):
        raise ValueError(f"report operation {name!r} has no valid endpoint")
    method = str(operation.get("method") or "POST").upper()
    if method == "GET" and payload:
        path = f"{path}?{urllib.parse.urlencode(dict(payload))}"
    body = json.dumps(dict(payload)).encode("utf-8") if method != "GET" else None
    request = urllib.request.Request(
        f"{base_url}{path}", data=body, headers=_headers(), method=method
    )
    timeout = float(section.get("timeout_seconds") or 60)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            parsed = json.loads(raw or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"{name} failed with HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"{name} transport failed: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} returned a non-object response")
    if parsed.get("ok") is False or parsed.get("error"):
        raise RuntimeError(f"{name} refused: {parsed.get('error') or parsed}")
    data = parsed.get("data", parsed)
    if not isinstance(data, dict):
        raise ValueError(f"{name} returned no typed result object")
    return data


def _require_keys(data: Mapping[str, Any], *keys: str) -> None:
    missing = [key for key in keys if key not in data]
    if missing:
        raise ValueError(f"report API response missing: {', '.join(missing)}")


def _fetch_sources(args: Mapping[str, Any], **_: Any) -> str:
    try:
        data = _request("fetch-sources", {"cycle": _cycle(args.get("cycle"))})
        _require_keys(data, "fetch_id", "sources", "preview_rows")
        if not isinstance(data["sources"], list):
            raise ValueError("fetch-sources sources must be a list")
        for source in data["sources"]:
            if not isinstance(source, Mapping):
                raise ValueError("fetch-sources returned a malformed source")
            _require_keys(source, "name", "hash", "bytes", "fetched_at", "sheet_tabs")
        return _tool_result(data)
    except Exception as exc:
        return _tool_error(exc)


def _preview_reconcile(args: Mapping[str, Any], **_: Any) -> str:
    try:
        data = _request("preview-reconcile", {"fetch_id": _identifier(args.get("fetch_id"), "fetch_id")})
        _require_keys(data, "run_id", "delta", "warnings")
        delta = data["delta"]
        if not isinstance(delta, Mapping):
            raise ValueError("preview-reconcile delta must be an object")
        _require_keys(delta, "new_cases", "updates", "closure_events", "per_zone")
        return _tool_result(data)
    except Exception as exc:
        return _tool_error(exc)


def _apply_reconcile(args: Mapping[str, Any], **_: Any) -> str:
    try:
        data = _request("apply-reconcile", {"run_id": _identifier(args.get("run_id"), "run_id")})
        _require_keys(data, "applied", "backup", "audit_batch_id")
        backup = data["backup"]
        if not isinstance(backup, Mapping):
            raise ValueError("apply-reconcile backup must be an object")
        _require_keys(backup, "path", "hash", "verified")
        if backup.get("verified") is not True:
            raise ValueError("apply-reconcile returned an unverified backup")
        return _tool_result(data)
    except Exception as exc:
        return _tool_error(exc)


def _generate(args: Mapping[str, Any], **_: Any) -> str:
    try:
        payload = {"cycle": _cycle(args.get("cycle"))}
        window = str(args.get("window") or "auto").strip().lower()
        if window != "auto":
            raise ValueError("window must be auto; Christopher never computes dates")
        payload["window"] = "auto"
        data = _request("generate", payload)
        _require_keys(data, "run_id", "verdict", "checks", "reports")
        if data["verdict"] not in {"pass", "fail"}:
            raise ValueError("generate verdict must be pass or fail")
        if not isinstance(data["reports"], list) or len(data["reports"]) != 4:
            raise ValueError("generate must return exactly four reports")
        for report in data["reports"]:
            if not isinstance(report, Mapping):
                raise ValueError("generate returned a malformed report")
            _require_keys(report, "zone", "ref", "hash")
        return _tool_result(data)
    except Exception as exc:
        return _tool_error(exc)


def _safe_filename(raw: Any, ordinal: int) -> str:
    name = Path(str(raw or f"report-{ordinal}.xlsx")).name
    if not name.lower().endswith(".xlsx"):
        name += ".xlsx"
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def _download_ref(item: Mapping[str, Any], *, run_id: str, ordinal: int) -> dict[str, Any]:
    ref = str(item.get("ref") or item.get("url") or "").strip()
    if ref.startswith("/"):
        ref = f"{str(_section().get('base_url') or '').rstrip('/')}{ref}"
    if not ref.startswith(("http://", "https://")):
        raise ValueError("get-reports returned an invalid signed ref")
    root = Path(str(_section().get("download_root") or "")).expanduser().resolve()
    if not str(root):
        raise ValueError("report download_root is not configured")
    target_dir = root / run_id
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
    filename = _safe_filename(item.get("file_name") or item.get("filename") or item.get("zone"), ordinal)
    target = target_dir / filename
    request = urllib.request.Request(ref, headers={"Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"})
    with urllib.request.urlopen(request, timeout=float(_section().get("timeout_seconds") or 60)) as response:
        payload = response.read()
    digest = hashlib.sha256(payload).hexdigest()
    expected = str(item.get("hash") or item.get("sha256") or "").lower().removeprefix("sha256:")
    if expected and digest != expected:
        raise ValueError(f"signed report hash mismatch for {filename}")
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(target)
    return {**dict(item), "hash": digest, "local_path": str(target), "bytes": len(payload)}


def _get_reports(args: Mapping[str, Any], **_: Any) -> str:
    try:
        run_id = _identifier(args.get("run_id"), "run_id")
        data = _request("get-reports", {"run_id": run_id})
        _require_keys(data, "files", "receipt")
        if not isinstance(data["files"], list) or len(data["files"]) != 4:
            raise ValueError("get-reports must return exactly four signed refs")
        files = []
        for ordinal, item in enumerate(data["files"], 1):
            if not isinstance(item, Mapping):
                raise ValueError("get-reports returned a malformed signed ref")
            files.append(_download_ref(item, run_id=run_id, ordinal=ordinal))
        return _tool_result({**data, "files": files})
    except Exception as exc:
        return _tool_error(exc)


def _status(args: Mapping[str, Any], **_: Any) -> str:
    try:
        payload = {}
        if args.get("run_id") not in {None, ""}:
            payload["run_id"] = _identifier(args.get("run_id"), "run_id")
        data = _request("status", payload)
        _require_keys(data, "stage", "ok", "populations_touched")
        if not isinstance(data["ok"], bool):
            raise ValueError("status ok must be boolean")
        return _tool_result(data)
    except Exception as exc:
        return _tool_error(exc)


def _schema(name: str, description: str, properties: Mapping[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": dict(properties),
            "required": required,
            "additionalProperties": False,
        },
    }


def register(ctx) -> None:
    cycle = {"type": "string", "enum": ["weekly", "monthly"]}
    run_id = {"type": "string", "description": "Opaque run id returned by this API."}
    fetch_id = {"type": "string", "description": "Opaque fetch id returned by fetch-sources."}
    definitions = (
        ("report_fetch_sources", "Fetch source snapshots for inspection before any import.", {"cycle": cycle}, ["cycle"], _fetch_sources),
        ("report_preview_reconcile", "Preview a fetched snapshot reconciliation without writing live state.", {"fetch_id": fetch_id}, ["fetch_id"], _preview_reconcile),
        ("report_apply_reconcile", "Apply a reviewed reconcile run through the guarded API.", {"run_id": run_id}, ["run_id"], _apply_reconcile),
        ("report_generate", "Generate four verified workbooks for an automatic window.", {"cycle": cycle, "window": {"type": "string", "enum": ["auto"], "default": "auto"}}, ["cycle"], _generate),
        ("report_get_reports", "Download four signed workbook refs into retained media.", {"run_id": run_id}, ["run_id"], _get_reports),
        ("report_status", "Read the current stage and touched populations for a run.", {"run_id": run_id}, [], _status),
    )
    for name, description, properties, required, handler in definitions:
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=_schema(name, description, properties, required),
            handler=handler,
            check_fn=_available,
            requires_env=["CHRISTOPHER_TGG_PS_SERVICE_TOKEN"],
            description=description,
        )
