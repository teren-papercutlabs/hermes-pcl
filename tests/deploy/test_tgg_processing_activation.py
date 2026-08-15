from __future__ import annotations

import importlib.util
import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "deploy/tgg/christopher/scripts/processing_activation_transaction.py"
)
SPEC = importlib.util.spec_from_file_location("tgg_processing_activation", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class FakeService:
    def __init__(self, config: Path, gate: Path) -> None:
        self.config = config
        self.gate = gate
        self.running = True
        self.stops = 0
        self.starts = 0

    def stop(self) -> None:
        self.running = False
        self.stops += 1

    def start(self) -> None:
        self.running = True
        self.starts += 1

    def wait(
        self,
        enabled: bool,
        *,
        generation: int,
        change_run_id: str,
        changed_at: str,
    ) -> dict[str, object]:
        assert self.running
        state = module.read_states(self.config.read_bytes(), self.gate.read_bytes())
        assert state["configEnabled"] is enabled
        assert state["gateEnabled"] is enabled
        assert generation == state["gateGeneration"]
        assert change_run_id
        assert datetime.fromisoformat(changed_at.replace("Z", "+00:00"))
        return {
            "state": "running" if enabled else "standby",
            "config_enabled": enabled,
            "gate_enabled": enabled,
            "gate_generation": state["gateGeneration"],
            "gate_change_run_id": change_run_id,
            "pid": 123,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }


def fixture(tmp_path: Path, *, enabled: bool = False) -> tuple[Path, Path, Path, FakeService]:
    config = tmp_path / "config.yaml"
    gate = tmp_path / "processing-gate.json"
    cursor = tmp_path / "capture-cursor.json"
    config.write_text(
        "pa:\n"
        f"  enabled: {'true' if enabled else 'false'}\n"
        "platforms:\n"
        "  whatsapp:\n"
        "    enabled: false\n",
        encoding="utf-8",
    )
    gate.write_text(
        json.dumps({"version": 1, "enabled": enabled, "generation": 4}) + "\n",
        encoding="utf-8",
    )
    cursor.write_text(
        json.dumps({"version": 1, "initial_offset": 100, "offset": 100}) + "\n",
        encoding="utf-8",
    )
    return config, gate, cursor, FakeService(config, gate)


def test_activate_sets_both_keys_while_consumer_is_stopped_and_preserves_cursor(tmp_path: Path) -> None:
    config, gate, cursor, service = fixture(tmp_path)
    receipt = module.apply_transition(
        mode="activate",
        config_path=config,
        gate_path=gate,
        cursor_path=cursor,
        service=service,
        metadata={"terenVerdictId": "verdict-1", "preCheckRunId": "run-1"},
    )
    state = module.read_states(config.read_bytes(), gate.read_bytes())
    assert state == {"configEnabled": True, "gateEnabled": True, "gateGeneration": 5}
    assert "platforms:\n  whatsapp:\n    enabled: false" in config.read_text()
    assert receipt["before"]["configEnabled"] is False
    assert receipt["after"]["configEnabled"] is True
    assert receipt["cursorBeforeSha256"] == receipt["cursorAfterSha256"]
    assert receipt["terenVerdictId"] == "verdict-1"
    assert service.stops == 1 and service.starts == 1


def test_deactivate_is_the_symmetric_both_key_reverse(tmp_path: Path) -> None:
    config, gate, cursor, service = fixture(tmp_path, enabled=True)
    receipt = module.apply_transition(
        mode="deactivate",
        config_path=config,
        gate_path=gate,
        cursor_path=cursor,
        service=service,
        metadata={},
    )
    state = module.read_states(config.read_bytes(), gate.read_bytes())
    assert state == {"configEnabled": False, "gateEnabled": False, "gateGeneration": 5}
    assert receipt["mode"] == "deactivate"
    assert receipt["consumerStatus"]["state"] == "standby"


def test_second_file_failure_rolls_both_keys_back_before_consumer_restarts(tmp_path: Path) -> None:
    config, gate, cursor, service = fixture(tmp_path)
    original_config = config.read_bytes()
    original_gate = gate.read_bytes()
    calls = 0

    def fail_once_on_second_write(path: Path, data: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("fixture second rename failed")
        module.atomic_replace(path, data)

    with pytest.raises(OSError, match="second rename failed"):
        module.apply_transition(
            mode="activate",
            config_path=config,
            gate_path=gate,
            cursor_path=cursor,
            service=service,
            metadata={},
            writer=fail_once_on_second_write,
        )
    assert config.read_bytes() == original_config
    assert gate.read_bytes() == original_gate
    assert service.running is True
    assert service.stops == 2 and service.starts == 1


def test_split_initial_state_refuses_before_stopping_service(tmp_path: Path) -> None:
    config, gate, cursor, service = fixture(tmp_path)
    gate.write_text(json.dumps({"version": 1, "enabled": True, "generation": 4}) + "\n")
    with pytest.raises(RuntimeError, match="already split"):
        module.apply_transition(
            mode="activate",
            config_path=config,
            gate_path=gate,
            cursor_path=cursor,
            service=service,
            metadata={},
        )
    assert service.stops == 0 and service.starts == 0


def _media_preflight_fixture(
    tmp_path: Path,
    media_value: str | None,
    *,
    media_type: str = "image",
) -> tuple[Path, Path]:
    source_root = tmp_path / "capture-media"
    systems_root = tmp_path / "systems-media"
    source_root.mkdir()
    systems_root.mkdir()
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "normalized": {
                    "messageId": "wa-1",
                    "chatId": "120363421424519051@g.us",
                    "hasMedia": True,
                    "mediaType": media_type,
                    "mediaUrls": [media_value] if media_value is not None else [],
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    config = tmp_path / "media-config.yaml"
    config.write_text(
        "pa:\n"
        "  media_retention:\n"
        "    enabled: true\n"
        f"    media_root: {systems_root}\n"
        "    source_roots:\n"
        f"    - {source_root}\n"
        "    operation: tgg_media_retention\n"
        "    min_free_percent: 0\n",
        encoding="utf-8",
    )
    cursor = tmp_path / "media-cursor.json"
    cursor.write_text(
        json.dumps({"source_path": str(events), "offset": 0, "initial_offset": 0}),
        encoding="utf-8",
    )
    return config, cursor


def test_media_backlog_preflight_proves_all_pending_images_resolvable(tmp_path: Path) -> None:
    photo = tmp_path / "capture-media" / "one.jpg"
    config, cursor = _media_preflight_fixture(tmp_path, str(photo))
    photo.write_bytes(b"\xff\xd8\xff\xe0fixture")

    result = module.media_backlog_preflight(config, cursor)

    assert result["events"] == 1
    assert result["images"] == 1
    assert result["bytes"] == photo.stat().st_size
    assert result["sourceOffset"] == 0


def test_media_backlog_preflight_uses_absolute_reserve_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    photo = tmp_path / "capture-media" / "one.jpg"
    config, cursor = _media_preflight_fixture(tmp_path, str(photo))
    photo.write_bytes(b"\xff\xd8\xff\xe0fixture")
    config.write_text(
        config.read_text().replace("min_free_percent: 0", "min_free_bytes: 50"),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        module.shutil, "disk_usage",
        lambda path: module.shutil._ntuple_diskusage(total=1000, used=940, free=60),
    )
    result = module.media_backlog_preflight(config, cursor)
    assert result["mediaVolumeFreeBytes"] == 60
    assert result["minimumFreeBytes"] == 50
    assert result["minimumFreePercent"] is None


def test_media_backlog_preflight_holds_activation_on_missing_cache_file(tmp_path: Path) -> None:
    missing = tmp_path / "capture-media" / "missing.jpg"
    config, cursor = _media_preflight_fixture(tmp_path, str(missing))

    with pytest.raises(RuntimeError, match="backlog preflight failed.*unresolved"):
        module.media_backlog_preflight(config, cursor)


def test_media_backlog_preflight_skips_normalized_video_event(tmp_path: Path) -> None:
    video = tmp_path / "capture-media" / "pending.mp4"
    config, cursor = _media_preflight_fixture(
        tmp_path, str(video), media_type="video"
    )
    video.write_bytes(b"not-an-image")

    result = module.media_backlog_preflight(config, cursor)

    assert result["events"] == 0
    assert result["images"] == 0
    assert result["bytes"] == 0


def test_media_backlog_preflight_holds_normalized_image_without_cache_path(
    tmp_path: Path,
) -> None:
    config, cursor = _media_preflight_fixture(tmp_path, None, media_type="image")

    with pytest.raises(RuntimeError, match="image event has no media paths"):
        module.media_backlog_preflight(config, cursor)


def test_activation_grant_is_signature_context_expiry_and_replay_bound(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    public_path = tmp_path / "controller.pub"
    public_path.write_bytes(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    now = datetime.now(timezone.utc)
    payload = {
        "schema": module.GRANT_SCHEMA,
        "purpose": "processing-activate",
        "transitionId": "transition-1",
        "packetHash": "a" * 64,
        "sourceEventId": "transition-1:processing-activate",
        "authoritySha256": "b" * 64,
        "hermesCommit": "c" * 40,
        "canonicalRunIds": {"preLive": "run-1", "armedDetector": "run-2"},
        "remoteTransactionSha256": "d" * 64,
        "issuedAt": now.isoformat(),
        "expiresAt": (now + timedelta(minutes=5)).isoformat(),
        "nonce": "fixture-nonce",
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    request = {
        "transitionId": "transition-1",
        "transitionPacketHash": "a" * 64,
        "authoritySha256": "b" * 64,
        "hermesCommit": "c" * 40,
        "remoteTransactionSha256": "d" * 64,
        "preCheckRunId": "run-1",
        "armedDetectorRunId": "run-2",
        "grantB64": base64.b64encode(raw).decode(),
        "grantSignatureB64": base64.b64encode(private.sign(raw)).decode(),
    }
    verified = module.verify_controller_grant(request, public_key_path=public_path, now=now)
    wrong_transaction = dict(request)
    wrong_transaction["remoteTransactionSha256"] = "e" * 64
    with pytest.raises(RuntimeError, match="remoteTransactionSha256 mismatch"):
        module.verify_controller_grant(
            wrong_transaction, public_key_path=public_path, now=now
        )
    ledger = tmp_path / "ledger"
    module.consume_controller_grant(verified, ledger=ledger)
    with pytest.raises(FileExistsError):
        module.consume_controller_grant(verified, ledger=ledger)
    request["authoritySha256"] = "d" * 64
    with pytest.raises(RuntimeError, match="authoritySha256 mismatch"):
        module.verify_controller_grant(request, public_key_path=public_path, now=now)


class FakePsResponse:
    status = 200

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakePsResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def test_ps_service_token_materializes_from_systems_and_verifies_without_receipt_leak(
    tmp_path: Path,
) -> None:
    source = tmp_path / "systems.env"
    target = tmp_path / "hermes.env"
    source.write_text("CHRISTOPHER_TGG_PS_SERVICE_TOKEN=fixture-token\n", encoding="utf-8")
    target.write_text("OPENAI_API_KEY=fixture-openai\n", encoding="utf-8")
    os.chmod(source, 0o640)
    os.chmod(target, 0o600)
    observed: list[tuple[str, str, int]] = []

    def opener(request: object, *, timeout: int) -> FakePsResponse:
        observed.append((request.full_url, request.headers.get("Authorization"), timeout))
        if request.full_url == module.PS_SERVICE_IDENTITY_URL:
            return FakePsResponse(
                {
                    "ok": True,
                    "data": {
                        "tenantSlug": "tgg",
                        "agentName": "christopher",
                        "scopes": sorted(module.EXPECTED_PS_SCOPES),
                    },
                }
            )
        return FakePsResponse({"ok": True, "data": {"total": 3397}})

    evidence = module.materialize_ps_service_token(
        source_env_path=source,
        target_env_path=target,
        opener=opener,
        expected_source_uid=os.getuid(),
    )
    assert module.read_env_value(
        target.read_text(encoding="utf-8"), module.TARGET_CREDENTIAL_ENV
    ) == "fixture-token"
    assert "OPENAI_API_KEY=fixture-openai" in target.read_text(encoding="utf-8")
    assert observed == [
        (module.PS_SERVICE_IDENTITY_URL, "Bearer fixture-token", 5),
        (module.PS_SERVICE_VERIFY_URL, "Bearer fixture-token", 5),
    ]
    assert evidence["changed"] is True
    assert evidence["verification"]["caseCount"] == 3397
    assert evidence["verification"]["agentName"] == "christopher"
    assert "fixture-token" not in json.dumps(evidence)


def test_ps_service_token_verification_failure_restores_target_env(tmp_path: Path) -> None:
    source = tmp_path / "systems.env"
    target = tmp_path / "hermes.env"
    source.write_text("CHRISTOPHER_TGG_PS_SERVICE_TOKEN=fixture-token\n", encoding="utf-8")
    original = b"OPENAI_API_KEY=fixture-openai\n"
    target.write_bytes(original)
    os.chmod(source, 0o640)
    os.chmod(target, 0o600)

    def opener(_request: object, *, timeout: int) -> FakePsResponse:
        assert timeout == 5
        return FakePsResponse({"ok": False, "error": {"code": "INVALID_TOKEN"}})

    with pytest.raises(RuntimeError, match="response was invalid"):
        module.materialize_ps_service_token(
            source_env_path=source,
            target_env_path=target,
            opener=opener,
            expected_source_uid=os.getuid(),
        )
    assert target.read_bytes() == original


def test_bobby_scoped_token_is_a_hard_activation_failure(tmp_path: Path) -> None:
    source = tmp_path / "systems.env"
    target = tmp_path / "hermes.env"
    source.write_text("CHRISTOPHER_TGG_PS_SERVICE_TOKEN=fixture-token\n", encoding="utf-8")
    original = b"OPENAI_API_KEY=fixture-openai\n"
    target.write_bytes(original)
    os.chmod(source, 0o640)
    os.chmod(target, 0o600)

    def opener(_request: object, *, timeout: int) -> FakePsResponse:
        assert timeout == 5
        return FakePsResponse(
            {
                "ok": True,
                "data": {
                    "tenantSlug": "tgg",
                    "agentName": "bobby",
                    "scopes": sorted(module.EXPECTED_PS_SCOPES),
                },
            }
        )

    with pytest.raises(RuntimeError, match="not Christopher-scoped"):
        module.materialize_ps_service_token(
            source_env_path=source,
            target_env_path=target,
            opener=opener,
            expected_source_uid=os.getuid(),
        )
    assert target.read_bytes() == original


def test_ps_service_token_refuses_insecure_systems_source(tmp_path: Path) -> None:
    source = tmp_path / "systems.env"
    target = tmp_path / "hermes.env"
    source.write_text("CHRISTOPHER_TGG_PS_SERVICE_TOKEN=fixture-token\n", encoding="utf-8")
    target.write_text("OPENAI_API_KEY=fixture-openai\n", encoding="utf-8")
    os.chmod(source, 0o644)
    os.chmod(target, 0o600)

    with pytest.raises(RuntimeError, match="0640-or-stricter"):
        module.materialize_ps_service_token(
            source_env_path=source,
            target_env_path=target,
            expected_source_uid=os.getuid(),
        )


def test_activation_failure_after_credential_materialization_restores_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "systems.env"
    target = tmp_path / "hermes.env"
    source.write_text("CHRISTOPHER_TGG_PS_SERVICE_TOKEN=fixture-token\n", encoding="utf-8")
    original = b"OPENAI_API_KEY=fixture-openai\n"
    target.write_bytes(original)
    os.chmod(source, 0o640)
    os.chmod(target, 0o600)

    def opener(request: object, *, timeout: int) -> FakePsResponse:
        assert timeout == 5
        if request.full_url == module.PS_SERVICE_IDENTITY_URL:
            return FakePsResponse(
                {
                    "ok": True,
                    "data": {
                        "tenantSlug": "tgg",
                        "agentName": "christopher",
                        "scopes": sorted(module.EXPECTED_PS_SCOPES),
                    },
                }
            )
        return FakePsResponse({"ok": True, "data": {"total": 3397}})

    real_materialize = module.materialize_ps_service_token
    monkeypatch.setattr(module, "HERMES_ENV_PATH", target)
    monkeypatch.setattr(module, "verify_controller_grant", lambda _request: {"grantId": "fixture"})
    monkeypatch.setattr(module, "consume_controller_grant", lambda _grant: None)
    monkeypatch.setattr(module, "media_backlog_preflight", lambda *_args: {"ok": True})
    monkeypatch.setattr(
        module,
        "materialize_ps_service_token",
        lambda: real_materialize(
            source_env_path=source,
            target_env_path=target,
            opener=opener,
            expected_source_uid=os.getuid(),
        ),
    )
    monkeypatch.setattr(
        module,
        "apply_transition",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("fixture transition failure")),
    )
    request = {
        "mode": "activate",
        "remoteTransactionSha256": module._sha(Path(module.__file__).read_bytes()),
    }
    with pytest.raises(RuntimeError, match="fixture transition failure"):
        module.run_fixed_remote(request)
    assert target.read_bytes() == original
