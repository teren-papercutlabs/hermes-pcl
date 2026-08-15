#!/usr/bin/env python3
"""Consumer-stopped, both-or-neither transaction for Christopher processing keys."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hmac
import hashlib
import json
import os
import re
import shlex
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


SERVICE = "christopher-tgg-hermes.service"
CONFIG_PATH = Path("/home/pclaw/.hermes-christopher-tgg/config.yaml")
GATE_PATH = Path("/home/pclaw/.hermes-christopher-tgg/runtime/processing-gate.json")
CURSOR_PATH = Path("/home/pclaw/.hermes-christopher-tgg/runtime/capture-cursor.json")
STATUS_PATH = Path("/home/pclaw/.hermes-christopher-tgg/runtime/capture-consumer-status.json")
CONTROLLER_PUBLIC_KEY = Path("/etc/tgg-capture/activation-controller.pub")
GRANT_LEDGER = Path("/home/pclaw/.hermes-christopher-tgg/runtime/activation-controller-grants")
GRANT_SCHEMA = "tgg-activation-controller-grant/v1"
HERMES_ENV_PATH = Path("/home/pclaw/.hermes-christopher-tgg/.env")
SYSTEMS_ENV_PATH = Path("/etc/systems-papercut-labs/tgg.env")
SOURCE_CREDENTIAL_ENV = "CHRISTOPHER_TGG_PS_SERVICE_TOKEN"
TARGET_CREDENTIAL_ENV = "CHRISTOPHER_TGG_PS_SERVICE_TOKEN"
PS_SERVICE_IDENTITY_URL = "http://127.0.0.1:5191/api/operator/auth/identity"
PS_SERVICE_VERIFY_URL = "http://127.0.0.1:5191/api/operator/cases/count"
EXPECTED_PS_SCOPES = frozenset(
    {
        "cases:read",
        "cases:write",
        "observations:write",
        "attention:write",
        "state:write",
        "agent-config:read",
        "agent-config:write",
    }
)


class ServiceController(Protocol):
    def stop(self) -> None: ...
    def start(self) -> None: ...
    def wait(
        self,
        enabled: bool,
        *,
        generation: int,
        change_run_id: str,
        changed_at: str,
    ) -> dict[str, Any]: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_replace(path: Path, data: bytes) -> None:
    stat = path.stat()
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, stat.st_mode & 0o7777)
        os.chown(tmp, stat.st_uid, stat.st_gid)
        os.replace(tmp, path)
        _sync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def read_env_value(text: str, key: str) -> str:
    found: list[str] = []
    pattern = re.compile(rf"^(?:export\s+)?{re.escape(key)}=(.*)$")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = pattern.match(line)
        if not match:
            continue
        value = match.group(1).strip()
        try:
            pieces = shlex.split(value, comments=False, posix=True)
        except ValueError as exc:
            raise RuntimeError(f"{key} has invalid quoting") from exc
        if len(pieces) != 1 or not pieces[0] or any(char.isspace() for char in pieces[0]):
            raise RuntimeError(f"{key} has an invalid value shape")
        found.append(pieces[0])
    if len(found) != 1:
        raise RuntimeError(f"expected exactly one {key}, found {len(found)}")
    return found[0]


def patch_env_value(text: str, key: str, value: str) -> bytes:
    replacement = f"{key}={json.dumps(value)}"
    lines = text.splitlines()
    pattern = re.compile(rf"^(?:export\s+)?{re.escape(key)}=")
    matches = [index for index, line in enumerate(lines) if pattern.match(line.strip())]
    if len(matches) > 1:
        raise RuntimeError(f"destination env contains duplicate {key}")
    if matches:
        lines[matches[0]] = replacement
    else:
        lines.append(replacement)
    return ("\n".join(lines) + "\n").encode()


def verify_ps_service_token(
    token: str,
    *,
    opener: Callable[..., Any] = urlopen,
    endpoint: str = PS_SERVICE_VERIFY_URL,
) -> dict[str, Any]:
    def read(endpoint_url: str) -> tuple[int, dict[str, Any]]:
        request = Request(endpoint_url, headers={"Authorization": f"Bearer {token}"})
        try:
            with opener(request, timeout=5) as response:
                status = int(response.status)
                payload = json.loads(response.read())
        except HTTPError as exc:
            raise RuntimeError(f"PS service token verification returned HTTP {exc.code}") from None
        except Exception as exc:
            raise RuntimeError(f"PS service token verification failed ({type(exc).__name__})") from None
        if status != 200 or not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError("PS service token verification response was invalid")
        return status, payload

    identity_status, identity_payload = read(PS_SERVICE_IDENTITY_URL)
    identity = identity_payload.get("data")
    scopes = identity.get("scopes") if isinstance(identity, dict) else None
    if (
        not isinstance(identity, dict)
        or identity.get("tenantSlug") != "tgg"
        or identity.get("agentName") != "christopher"
        or not isinstance(scopes, list)
        or not EXPECTED_PS_SCOPES.issubset(set(scopes))
    ):
        raise RuntimeError("PS service token is not Christopher-scoped with the required grants")

    count_status, count_payload = read(endpoint)
    data = count_payload.get("data")
    total = data.get("total") if isinstance(data, dict) else None
    if not isinstance(total, int) or total < 0:
        raise RuntimeError("PS service token verification response was invalid")
    return {
        "identityEndpoint": PS_SERVICE_IDENTITY_URL,
        "identityHttpStatus": identity_status,
        "tenantSlug": "tgg",
        "agentName": "christopher",
        "scopes": sorted(scopes),
        "endpoint": endpoint,
        "httpStatus": count_status,
        "caseCount": total,
    }


def materialize_ps_service_token(
    *,
    source_env_path: Path = SYSTEMS_ENV_PATH,
    target_env_path: Path = HERMES_ENV_PATH,
    opener: Callable[..., Any] = urlopen,
    writer: Callable[[Path, bytes], None] = atomic_replace,
    expected_source_uid: int | None = 0,
) -> dict[str, Any]:
    for label, env_path in (("systems source", source_env_path), ("Hermes target", target_env_path)):
        stat = env_path.lstat()
        if not env_path.is_file() or env_path.is_symlink():
            raise RuntimeError(f"{label} env must be a regular non-symlink file")
        if label == "systems source":
            if expected_source_uid is not None and stat.st_uid != expected_source_uid:
                raise RuntimeError("systems source env has the wrong owner")
            if stat.st_mode & 0o027:
                raise RuntimeError("systems source env must be 0640-or-stricter")
        if label == "Hermes target" and stat.st_mode & 0o077:
            raise RuntimeError("Hermes target env must be 0600-or-stricter")
    source_token = read_env_value(
        source_env_path.read_text(encoding="utf-8"), SOURCE_CREDENTIAL_ENV
    )
    before = target_env_path.read_bytes()
    before_text = before.decode()
    try:
        prior_token = read_env_value(before_text, TARGET_CREDENTIAL_ENV)
    except RuntimeError as exc:
        if "found 0" not in str(exc):
            raise
        prior_token = None
    next_bytes = patch_env_value(before_text, TARGET_CREDENTIAL_ENV, source_token)
    changed = next_bytes != before
    if changed:
        writer(target_env_path, next_bytes)
    try:
        materialized_token = read_env_value(
            target_env_path.read_text(encoding="utf-8"), TARGET_CREDENTIAL_ENV
        )
        if not hmac.compare_digest(materialized_token, source_token):
            raise RuntimeError("materialized PS service token does not match the systems authority")
        verification = verify_ps_service_token(materialized_token, opener=opener)
    except Exception:
        if changed:
            writer(target_env_path, before)
        raise
    return {
        "sourcePath": str(source_env_path),
        "sourceKey": SOURCE_CREDENTIAL_ENV,
        "targetPath": str(target_env_path),
        "targetKey": TARGET_CREDENTIAL_ENV,
        "changed": changed,
        "replacedExisting": prior_token is not None and not hmac.compare_digest(prior_token, source_token),
        "verification": verification,
    }


def verify_controller_grant(
    request: dict[str, Any],
    *,
    public_key_path: Path = CONTROLLER_PUBLIC_KEY,
    now: datetime | None = None,
) -> dict[str, Any]:
    try:
        payload_bytes = base64.b64decode(str(request["grantB64"]), validate=True)
        signature = base64.b64decode(str(request["grantSignatureB64"]), validate=True)
        public = serialization.load_pem_public_key(public_key_path.read_bytes())
        if not isinstance(public, Ed25519PublicKey):
            raise RuntimeError("controller public key is not Ed25519")
        public.verify(signature, payload_bytes)
        payload = json.loads(payload_bytes)
    except Exception as exc:
        raise RuntimeError(f"controller grant signature/payload invalid: {exc}") from exc
    if payload.get("schema") != GRANT_SCHEMA or payload.get("purpose") != "processing-activate":
        raise RuntimeError("controller grant schema/purpose invalid")
    expected = {
        "transitionId": request.get("transitionId"),
        "packetHash": request.get("transitionPacketHash"),
        "sourceEventId": f"{request.get('transitionId')}:processing-activate",
        "authoritySha256": request.get("authoritySha256"),
        "hermesCommit": request.get("hermesCommit"),
        "remoteTransactionSha256": request.get("remoteTransactionSha256"),
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise RuntimeError(f"controller grant {key} mismatch")
    run_ids = payload.get("canonicalRunIds") or {}
    if run_ids.get("preLive") != request.get("preCheckRunId"):
        raise RuntimeError("controller grant pre-live run mismatch")
    if run_ids.get("armedDetector") != request.get("armedDetectorRunId"):
        raise RuntimeError("controller grant armed detector run mismatch")
    issued = datetime.fromisoformat(str(payload.get("issuedAt", "")).replace("Z", "+00:00"))
    expires = datetime.fromisoformat(str(payload.get("expiresAt", "")).replace("Z", "+00:00"))
    observed = now or datetime.now(timezone.utc)
    if issued.tzinfo is None or expires.tzinfo is None or expires <= issued:
        raise RuntimeError("controller grant timestamps invalid")
    if expires - issued > timedelta(minutes=10) or observed < issued - timedelta(seconds=30) or observed >= expires:
        raise RuntimeError("controller grant is expired or outside its allowed lifetime")
    nonce = str(payload.get("nonce") or "")
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", nonce):
        raise RuntimeError("controller grant nonce invalid")
    return payload


def consume_controller_grant(payload: dict[str, Any], *, ledger: Path = GRANT_LEDGER) -> Path:
    ledger.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = ledger / f"{payload['nonce']}.json"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {"schema": GRANT_SCHEMA, "consumedAt": _now(), "payload": payload},
                handle,
                sort_keys=True,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _sync_directory(ledger)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            target.unlink()
        raise
    return target


def patch_pa_enabled(text: str, enabled: bool) -> bytes:
    lines = text.splitlines(keepends=True)
    in_pa = False
    found = 0
    for index, line in enumerate(lines):
        if re.match(r"^pa:\s*(?:#.*)?$", line.rstrip("\n")):
            in_pa = True
            continue
        if in_pa and line and not line.startswith((" ", "\t", "\n")):
            in_pa = False
        if in_pa and re.match(
            r"^  enabled:\s*(?:true|false)\s*(?:#.*)?$", line.rstrip("\n")
        ):
            suffix = "\n" if line.endswith("\n") else ""
            lines[index] = f"  enabled: {'true' if enabled else 'false'}{suffix}"
            found += 1
    if found != 1:
        raise RuntimeError(f"expected one pa.enabled, found {found}")
    return "".join(lines).encode()


def read_states(config_bytes: bytes, gate_bytes: bytes) -> dict[str, Any]:
    lines = config_bytes.decode().splitlines()
    in_pa = False
    value: bool | None = None
    for line in lines:
        if re.match(r"^pa:\s*(?:#.*)?$", line):
            in_pa = True
            continue
        if in_pa and line and not line.startswith((" ", "\t")):
            break
        match = (
            re.match(r"^  enabled:\s*(true|false)\s*(?:#.*)?$", line)
            if in_pa
            else None
        )
        if match:
            value = match.group(1) == "true"
    if value is None:
        raise RuntimeError("pa.enabled unreadable")
    gate = json.loads(gate_bytes)
    if gate.get("version") != 1 or gate.get("enabled") not in {True, False}:
        raise RuntimeError("processing gate is invalid")
    generation = gate.get("generation")
    if not isinstance(generation, int) or generation < 0:
        raise RuntimeError("processing gate generation is invalid")
    return {
        "configEnabled": value,
        "gateEnabled": gate["enabled"],
        "gateGeneration": generation,
    }


def media_backlog_preflight(
    config_path: Path,
    cursor_path: Path,
) -> dict[str, Any]:
    """Fail activation if paused capture backlog cannot be retained safely."""
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    retention = (config.get("pa") or {}).get("media_retention") or {}
    if retention.get("enabled") is not True:
        raise RuntimeError("media retention must be enabled before processing activation")
    media_root = Path(str(retention.get("media_root") or "")).resolve(strict=True)
    source_roots = retention.get("source_roots") or []
    if not isinstance(source_roots, list) or not source_roots:
        raise RuntimeError("media retention source_roots are missing")
    roots = tuple(Path(str(value)).resolve(strict=True) for value in source_roots)
    usage = shutil.disk_usage(media_root)
    free_percent = usage.free / usage.total * 100 if usage.total else 0.0
    min_free_bytes = retention.get("min_free_bytes")
    if min_free_bytes is not None:
        try:
            required_bytes = int(min_free_bytes)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("media retention min_free_bytes is invalid") from exc
        if required_bytes < 0:
            raise RuntimeError("media retention min_free_bytes must be non-negative")
        if usage.free < required_bytes:
            raise RuntimeError(
                "media retention volume below activation absolute reserve: "
                f"{usage.free} B < {required_bytes} B"
            )
    else:
        # Compatibility for an older, externally supplied slot. Christopher's
        # own slots use min_free_bytes exclusively.
        min_free = float(retention.get("min_free_percent", 20))
        if free_percent < min_free:
            raise RuntimeError(
                f"media retention volume below activation floor: {free_percent:.3f}% < {min_free:.3f}%"
            )

    cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
    source_path = Path(str(cursor.get("source_path") or ""))
    offset = int(cursor.get("offset", cursor.get("initial_offset", 0)))
    if not source_path.is_file() or offset < 0 or offset > source_path.stat().st_size:
        raise RuntimeError("capture backlog source/cursor is not resolvable")
    events = images = image_bytes = 0
    failures: list[str] = []
    with source_path.open("rb") as handle:
        handle.seek(offset)
        for line_no, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                continue
            item: Any = None
            try:
                event = json.loads(raw_line)
                item = (
                    event.get("normalized")
                    or event.get("item")
                    or event.get("payload")
                    or event
                )
                if not isinstance(item, dict):
                    raise ValueError("event item is not an object")
                media_type = str(
                    item.get("mediaType") or item.get("mimeType") or ""
                ).split(";", 1)[0].strip().lower()
                is_image = media_type == "image" or media_type.startswith("image/")
                if not item.get("hasMedia") or not is_image:
                    continue
                values = item.get("mediaUrls") or item.get("media") or item.get("mediaPaths")
                if isinstance(values, (str, dict)):
                    values = [values]
                if not isinstance(values, list) or not values:
                    raise ValueError("image event has no media paths")
                events += 1
                for value in values:
                    if isinstance(value, dict):
                        value = (
                            value.get("path")
                            or value.get("filePath")
                            or value.get("localPath")
                            or value.get("url")
                        )
                    text = str(value or "")
                    if text.startswith("file://"):
                        text = text[7:]
                    path = Path(text).resolve(strict=True)
                    if not path.is_file() or not any(path.is_relative_to(root) for root in roots):
                        raise ValueError("image path escapes source roots or is missing")
                    with path.open("rb") as media_handle:
                        prefix = media_handle.read(16)
                    if not any(
                        prefix[offset : offset + len(signature)] == signature
                        for signature, offset in (
                            (b"\xff\xd8\xff", 0),
                            (b"\x89PNG\r\n\x1a\n", 0),
                            (b"GIF8", 0),
                            (b"WEBP", 8),
                        )
                    ):
                        raise ValueError("media bytes are not a supported image")
                    images += 1
                    image_bytes += path.stat().st_size
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                message_id = "unknown"
                if isinstance(item, dict):
                    message_id = str(item.get("messageId") or "unknown")
                failures.append(f"line+{line_no} message={message_id}: {exc}")
    if failures:
        raise RuntimeError(
            "capture media backlog preflight failed "
            f"({len(failures)} unresolved); first={failures[0]}"
        )
    return {
        "events": events,
        "images": images,
        "bytes": image_bytes,
        "sourceOffset": offset,
        "sourceSize": source_path.stat().st_size,
        "mediaVolumeFreePercent": round(free_percent, 3),
        "mediaVolumeFreeBytes": int(usage.free),
        "minimumFreeBytes": int(min_free_bytes) if min_free_bytes is not None else None,
        "minimumFreePercent": (
            None if min_free_bytes is not None else float(retention.get("min_free_percent", 20))
        ),
    }


def apply_transition(
    *,
    mode: str,
    config_path: Path,
    gate_path: Path,
    cursor_path: Path,
    service: ServiceController,
    metadata: dict[str, Any],
    writer: Callable[[Path, bytes], None] = atomic_replace,
) -> dict[str, Any]:
    if mode not in {"activate", "deactivate"}:
        raise ValueError("mode must be activate or deactivate")
    target_enabled = mode == "activate"
    started_at = _now()
    run_id = str(uuid.uuid4())
    before_config = config_path.read_bytes()
    before_gate = gate_path.read_bytes()
    before = read_states(before_config, before_gate)
    if before["configEnabled"] is not before["gateEnabled"]:
        raise RuntimeError("processing keys are already split; refusing mutation")
    cursor_before = cursor_path.read_bytes()

    gate = json.loads(before_gate)
    gate.update(
        {
            "enabled": target_enabled,
            "generation": int(gate["generation"]) + 1,
            "changed_at": started_at,
            "change_run_id": run_id,
            "authority_verdict_id": metadata.get("terenVerdictId"),
        }
    )
    next_config = patch_pa_enabled(before_config.decode(), target_enabled)
    next_gate = (json.dumps(gate, sort_keys=True, indent=2) + "\n").encode()

    service.stop()
    try:
        writer(config_path, next_config)
        writer(gate_path, next_gate)
        after = read_states(config_path.read_bytes(), gate_path.read_bytes())
        if (
            after["configEnabled"] is not target_enabled
            or after["gateEnabled"] is not target_enabled
        ):
            raise RuntimeError("post-write processing keys mismatch")
        service.start()
        status = service.wait(
            target_enabled,
            generation=after["gateGeneration"],
            change_run_id=run_id,
            changed_at=started_at,
        )
    except Exception:
        with contextlib.suppress(Exception):
            service.stop()
        writer(config_path, before_config)
        writer(gate_path, before_gate)
        service.start()
        raise

    cursor_after = cursor_path.read_bytes()
    return {
        "schema": "tgg-processing-activation-receipt/v1",
        "runId": run_id,
        "mode": mode,
        "startedAt": started_at,
        "finishedAt": _now(),
        "before": before,
        "after": after,
        "cursorBeforeSha256": _sha(cursor_before),
        "cursorAfterSha256": _sha(cursor_after),
        "cursorBefore": json.loads(cursor_before),
        "cursorAfter": json.loads(cursor_after),
        "consumerStatus": status,
        "terenVerdictId": metadata.get("terenVerdictId"),
        "preCheckRunId": metadata.get("preCheckRunId"),
        "changed": before["configEnabled"] is not target_enabled,
    }


@dataclass
class SystemdController:
    status_path: Path

    def _run(self, action: str) -> None:
        import subprocess

        subprocess.run(["systemctl", action, SERVICE], check=True)

    def stop(self) -> None:
        self._run("stop")

    def start(self) -> None:
        self._run("start")

    def _main_pid(self) -> int:
        import subprocess

        raw = subprocess.check_output(
            ["systemctl", "show", SERVICE, "--property=MainPID", "--value"],
            text=True,
        ).strip()
        pid = int(raw)
        if pid <= 0:
            raise RuntimeError("consumer systemd MainPID is not live")
        return pid

    def wait(
        self,
        enabled: bool,
        *,
        generation: int,
        change_run_id: str,
        changed_at: str,
    ) -> dict[str, Any]:
        deadline = time.time() + 20
        expected_state = "running" if enabled else "standby"
        expected_pid = self._main_pid()
        changed_at_dt = datetime.fromisoformat(changed_at.replace("Z", "+00:00"))
        while time.time() < deadline:
            try:
                status = json.loads(self.status_path.read_text(encoding="utf-8"))
            except Exception:
                status = None
            if (
                status
                and status.get("state") == expected_state
                and status.get("config_enabled") is enabled
                and status.get("gate_enabled") is enabled
                and status.get("gate_generation") == generation
                and status.get("gate_change_run_id") == change_run_id
                and status.get("pid") == expected_pid
                and datetime.fromisoformat(
                    str(status.get("updated_at", "")).replace("Z", "+00:00")
                ) >= changed_at_dt
            ):
                return status
            time.sleep(0.5)
        raise RuntimeError("consumer did not confirm the target processing state")


def run_fixed_remote(request: dict[str, Any]) -> dict[str, Any]:
    mode = str(request["mode"])
    if mode == "status":
        states = read_states(CONFIG_PATH.read_bytes(), GATE_PATH.read_bytes())
        status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        return {
            "schema": "tgg-processing-activation-status/v1",
            "off": states["configEnabled"] is False
            and states["gateEnabled"] is False
            and status.get("state") == "standby"
            and status.get("config_enabled") is False
            and status.get("gate_enabled") is False
            and status.get("gate_generation") == states["gateGeneration"],
            "states": states,
            "consumerStatus": status,
        }
    credential_env_before: bytes | None = None
    if mode == "activate":
        deployed_transaction_sha256 = _sha(Path(__file__).read_bytes())
        if deployed_transaction_sha256 != request.get("remoteTransactionSha256"):
            raise RuntimeError("deployed activation transaction does not match the signed grant")
        grant = verify_controller_grant(request)
        media_preflight = media_backlog_preflight(CONFIG_PATH, CURSOR_PATH)
        consume_controller_grant(grant)
        credential_env_before = HERMES_ENV_PATH.read_bytes()
        credential = materialize_ps_service_token()
    else:
        credential = None
        media_preflight = None
    try:
        result = apply_transition(
            mode=mode,
            config_path=CONFIG_PATH,
            gate_path=GATE_PATH,
            cursor_path=CURSOR_PATH,
            service=SystemdController(STATUS_PATH),
            metadata=request,
        )
    except Exception:
        if credential is not None and credential.get("changed") is True and credential_env_before is not None:
            try:
                atomic_replace(HERMES_ENV_PATH, credential_env_before)
            except Exception as rollback_error:
                raise RuntimeError("PS service credential rollback failed") from rollback_error
        raise
    if credential is not None:
        result["psServiceCredential"] = credential
        result["mediaBacklogPreflight"] = media_preflight
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-b64", required=True)
    args = parser.parse_args()
    request = json.loads(base64.b64decode(args.request_b64).decode())
    print(json.dumps(run_fixed_remote(request), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
