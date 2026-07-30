from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_cli.pa_credentials import (
    BearerJwtDriver,
    ByReferenceMaterial,
    ByValueMaterial,
    CarbonAuthRequest,
    CarbonAuthResponse,
    CarbonAuthV1Client,
    CredentialContractError,
    CredentialDriver,
    CredentialRecord,
    CredentialWatcher,
    DriverRegistry,
    HandoffSigningRequest,
    HumanBrowserSessionDriver,
    InvalidHandoffUrlError,
    InvalidTransitionError,
    ProbeContract,
    ProbeResult,
    ReauthResult,
    ReauthState,
    ReauthStateMachine,
    SignedHandoffUrlSigner,
    UnsafeProbeError,
    validate_handoff_url,
    load_runtime_credentials,
)


UTC = timezone.utc
EXPIRY = datetime(2026, 10, 29, tzinfo=UTC)


def _jwt(expiry: datetime = EXPIRY) -> str:
    def part(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    return ".".join(
        [
            part(b'{"alg":"none","typ":"JWT"}'),
            part(json.dumps({"exp": expiry.timestamp()}).encode()),
            "signature",
        ]
    )


def _record(
    *,
    driver_key: str = "bearer-jwt",
    material: ByValueMaterial | ByReferenceMaterial | None = None,
    cadence: float = 1.0,
    state: ReauthState = ReauthState.REQUESTED,
    deadline: datetime | None = None,
) -> CredentialRecord:
    return CredentialRecord(
        credential_id="credential-1",
        driver_key=driver_key,
        material=material or ByValueMaterial(_jwt()),
        expires_at=EXPIRY,
        probe_contract=ProbeContract(cadence, True, "local read only"),
        reauth_state=state,
        reauth_deadline_at=deadline,
        agent_slug="runtime-agent",
        portal="portal",
        agent_pubkey="synthetic-public-key",
        timeout_after_seconds=5,
    )


def test_reauth_state_machine_allows_only_contract_transitions():
    machine = ReauthStateMachine()
    assert machine.transition(ReauthState.PENDING_HUMAN) is ReauthState.PENDING_HUMAN
    # A driver can complete directly only after it has supplied fresh expiry
    # evidence.  The watcher enforces that evidence; the state graph permits it.
    assert ReauthStateMachine().transition(ReauthState.COMPLETED) is ReauthState.COMPLETED
    assert machine.transition(ReauthState.COMPLETED) is ReauthState.COMPLETED
    with pytest.raises(InvalidTransitionError):
        machine.transition(ReauthState.TIMED_OUT)


def test_material_union_rejects_empty_and_mixed_branches():
    by_ref = CredentialRecord.from_row(
        {
            "id": "ref",
            "driver_key": "human-browser-session",
            "material_mode": "by-reference",
            "host_id": "host-1",
            "reference_locator": "profile/runtime",
            "probe_cadence_seconds": 5,
            "probe_non_destructive": True,
            "probe_safety_contract": "read only",
        }
    )
    assert by_ref.material.public_metadata() == {
        "material_mode": "by-reference",
        "host_id": "host-1",
        "reference_locator": "profile/runtime",
    }
    assert by_ref.material.resolve(lambda host, locator: (host, locator)) == (
        "host-1",
        "profile/runtime",
    )
    by_value = CredentialRecord.from_row(
        {
            "id": "value",
            "driver_key": "bearer-jwt",
            "material_mode": "by-value",
            "material_envelope": "opaque-envelope",
            "probe_cadence_seconds": 5,
            "probe_non_destructive": True,
            "probe_safety_contract": "local read only",
        }
    )
    assert by_value.public_dict()["material_mode"] == "by-value"
    assert "opaque-envelope" not in json.dumps(by_value.public_dict())
    with pytest.raises(CredentialContractError):
        CredentialRecord.from_row(
            {
                "id": "mixed",
                "driver_key": "bearer-jwt",
                "material_mode": "by-reference",
                "host_id": "host-1",
                "reference_locator": "x",
                "material_envelope": "also-present",
                "probe_cadence_seconds": 5,
                "probe_non_destructive": True,
                "probe_safety_contract": "read only",
            }
        )
    with pytest.raises(CredentialContractError):
        CredentialRecord.from_row(
            {
                "id": "empty",
                "driver_key": "bearer-jwt",
                "material_mode": "by-value",
                "material_envelope": {},
                "probe_cadence_seconds": 5,
                "probe_non_destructive": True,
                "probe_safety_contract": "read only",
            }
        )


def test_probe_contract_is_first_class_and_unsafe_fails_closed():
    contract = ProbeContract(30, True, "non-destructive local read")
    assert contract.cadence_seconds == 30
    assert contract.non_destructive is True
    object_contract = ProbeContract(
        30,
        True,
        {"operation": "local-read", "must_not": ["mutate", "invalidate"]},
    )
    assert object_contract.safety_contract["operation"] == "local-read"
    with pytest.raises(UnsafeProbeError):
        ProbeContract(30, False, "mutates a session")
    with pytest.raises(UnsafeProbeError):
        ProbeContract(0, True, "read only")
    with pytest.raises(UnsafeProbeError):
        ProbeContract(30, True, {})


def test_bearer_jwt_is_local_and_unrenewable_expiry_escalates():
    async def run():
        driver = BearerJwtDriver()
        record = _record()
        result = await driver.probe(
            record.material,
            now=datetime(2026, 10, 28, tzinfo=UTC),
        )
        assert result.healthy is True
        assert result.expires_at == EXPIRY

        escalated: list[str] = []
        watcher = CredentialWatcher(
            [record],
            clock=lambda: datetime(2026, 10, 30, tzinfo=UTC),
            on_escalation=lambda row, result: escalated.append(row.credential_id),
        )
        await watcher.run_once()

        # A local expiry parser has no renewal mechanism.  It must not claim
        # success merely because no human handoff is available.
        assert record.reauth_state is not ReauthState.COMPLETED
        assert record.last_probe_status == "expired"
        assert escalated == [record.credential_id]

    asyncio.run(run())


def test_human_driver_is_a_zero_side_effect_pending_stub():
    async def run():
        driver = HumanBrowserSessionDriver()
        record = _record(
            driver_key=driver.key,
            material=ByReferenceMaterial("host-1", "profile/runtime"),
        )
        result = await driver.probe(record.material, now=datetime.now(UTC))
        assert result.needs_reauth is True
        assert (await driver.begin_reauth(record)).state is ReauthState.PENDING_HUMAN
        assert DriverRegistry().keys() == ("bearer-jwt", "human-browser-session")

    asyncio.run(run())


def test_carbon_auth_v1_posts_exact_request_shape():
    async def run():
        calls: list[tuple[str, dict]] = []

        async def post(url, payload):
            calls.append((url, dict(payload)))
            return {
                "ok": True,
                "data": {
                    "request_id": "request-1",
                    "start_url": "https://agents.papercut-labs.com/carbon-auth/start/request-1",
                },
            }

        client = CarbonAuthV1Client(post_json=post, base_url="https://example.invalid")
        request = CarbonAuthRequest(
            agent_id="runtime-agent",
            agent_pubkey="public-key",
            portal="portal",
            reason="expired",
            urgency="standard",
            principal_id="operator",
        )
        response = await client.request(request)
        assert response == CarbonAuthResponse(
            "request-1", "https://agents.papercut-labs.com/carbon-auth/start/request-1"
        )
        assert calls == [
            (
                "https://example.invalid/carbon-auth/request",
                {
                    "agent_id": "runtime-agent",
                    "agent_pubkey": "public-key",
                    "portal": "portal",
                    "reason": "expired",
                    "urgency": "standard",
                    "principal_id": "operator",
                },
            )
        ]

    asyncio.run(run())


def test_handoff_urls_are_canonical_and_reject_unsafe_hosts():
    good = "https://auth.papercut-labs.com/handoff/request-1?sig=abc"
    assert validate_handoff_url(good) == good
    for url in (
        "https://runtime.ts.net/handoff/request-1",
        "http://auth.papercut-labs.com/handoff/request-1",
        "http://127.0.0.1:6080/vnc.html",
        "https://auth.papercut-labs.com/novnc/request-1",
        "https://auth.papercut-labs.com/handoff/request-1",
        "https://auth.papercut-labs.com/handoff/request-1?sig=",
        "https://auth.papercut-labs.com/handoff/request-1?token=",
        "https://auth.papercut-labs.com:8443/handoff/request-1?sig=abc",
        "https://evil@auth.papercut-labs.com/handoff/request-1?sig=abc",
        "https://agents.papercut-labs.com/carbon-auth/start/request-1",
        "https://other.example/handoff/request-1",
    ):
        with pytest.raises(InvalidHandoffUrlError):
            validate_handoff_url(url)


def test_credential_driver_has_no_sync_refresh_contract():
    # Re-auth must be an async lifecycle; introducing refresh() re-opens the
    # synchronous human-in-loop path this watcher replaced.
    assert not hasattr(CredentialDriver, "refresh")


def test_signer_wraps_injected_signer_and_enforces_host():
    async def run():
        request = HandoffSigningRequest("request-1", "start", "credential-1")
        signer = SignedHandoffUrlSigner(
            lambda _: "https://auth.papercut-labs.com/handoff/request-1?sig=abc"
        )
        assert (await signer.sign(request)).startswith("https://auth.papercut-labs.com/")
        bad = SignedHandoffUrlSigner(lambda _: "https://runtime.ts.net/handoff/request-1")
        with pytest.raises(InvalidHandoffUrlError):
            await bad.sign(request)

    asyncio.run(run())


def test_pending_human_is_committed_only_after_carbon_request_and_sign():
    async def run():
        now = datetime(2026, 10, 30, tzinfo=UTC)
        record = _record(
            driver_key="human-browser-session",
            material=ByReferenceMaterial("host-1", "profile/runtime"),
        )

        class CarbonFails:
            async def request(self, request):
                raise CredentialContractError("synthetic Carbon Auth outage")

        class Signer:
            async def sign(self, request):
                return "https://auth.papercut-labs.com/handoff/request-1?sig=abc"

        watcher = CredentialWatcher(
            [record],
            carbon_auth=CarbonFails(),
            handoff_signer=Signer(),
            clock=lambda: now,
        )
        await watcher.run_once()
        assert record.reauth_state is ReauthState.REQUESTED
        assert record.reauth_request_id is None
        assert record.reauth_deadline_at is None

        class Carbon:
            async def request(self, request):
                return CarbonAuthResponse("request-1", "https://agents.invalid/start/request-1")

        class SignerFails:
            async def sign(self, request):
                raise InvalidHandoffUrlError("synthetic signer failure")

        record = _record(
            driver_key="human-browser-session",
            material=ByReferenceMaterial("host-1", "profile/runtime"),
        )
        watcher = CredentialWatcher(
            [record],
            carbon_auth=Carbon(),
            handoff_signer=SignerFails(),
            clock=lambda: now,
        )
        await watcher.run_once()
        assert record.reauth_state is ReauthState.REQUESTED
        assert record.reauth_request_id is None
        assert record.reauth_deadline_at is None

    asyncio.run(run())


def test_pending_human_completes_only_when_the_next_probe_is_healthy():
    async def run():
        current = [datetime(2026, 10, 30, tzinfo=UTC)]
        healthy = [False]
        handoffs: list[str | None] = []

        class Driver(CredentialDriver):
            key = "controlled-human"
            probe_contract = ProbeContract(1, True, "synthetic local read")

            async def probe(self, material, *, now):
                return ProbeResult(
                    healthy=healthy[0],
                    needs_reauth=not healthy[0],
                    status="healthy" if healthy[0] else "human-required",
                )

            async def begin_reauth(self, record):
                return ReauthResult(ReauthState.PENDING_HUMAN)

        class Carbon:
            async def request(self, request):
                return CarbonAuthResponse("request-1", "https://agents.invalid/start/request-1")

        class Signer:
            async def sign(self, request):
                return "https://auth.papercut-labs.com/handoff/request-1?sig=abc"

        record = _record(
            driver_key="controlled-human",
            material=ByReferenceMaterial("host-1", "profile/runtime"),
            cadence=1,
        )
        watcher = CredentialWatcher(
            [record],
            drivers=DriverRegistry([Driver()]),
            carbon_auth=Carbon(),
            handoff_signer=Signer(),
            on_escalation=lambda row, result: handoffs.append(result.handoff_url),
            clock=lambda: current[0],
        )
        await watcher.run_once()
        assert record.reauth_state is ReauthState.PENDING_HUMAN
        assert record.reauth_request_id == "request-1"
        assert handoffs == ["https://auth.papercut-labs.com/handoff/request-1?sig=abc"]

        healthy[0] = True
        current[0] += timedelta(seconds=1)
        await watcher.run_once()
        assert record.reauth_state is ReauthState.COMPLETED

    asyncio.run(run())


def test_human_expiry_calls_carbon_signer_and_escalates_then_times_out():
    async def run():
        now = datetime(2026, 10, 30, tzinfo=UTC)
        current = [now]
        requests: list[CarbonAuthRequest] = []
        escalations: list[tuple[str, str | None]] = []
        timeouts: list[str] = []

        class Carbon:
            async def request(self, request):
                requests.append(request)
                return CarbonAuthResponse(
                    "request-1", "https://agents.papercut-labs.com/carbon-auth/start/request-1"
                )

        class Signer:
            async def sign(self, request):
                assert request.request_id == "request-1"
                return "https://auth.papercut-labs.com/handoff/request-1?sig=abc"

        async def escalate(record, result):
            escalations.append((record.credential_id, result.handoff_url))

        async def timeout(record):
            timeouts.append(record.credential_id)

        record = _record(
            driver_key="human-browser-session",
            material=ByReferenceMaterial("host-1", "profile/runtime"),
            cadence=1,
        )
        watcher = CredentialWatcher(
            [record],
            carbon_auth=Carbon(),
            handoff_signer=Signer(),
            on_escalation=escalate,
            on_timeout=timeout,
            clock=lambda: current[0],
        )
        await watcher.run_once()
        assert record.reauth_state is ReauthState.PENDING_HUMAN
        assert requests[0].as_payload()["principal_id"] == "operator"
        assert escalations == [("credential-1", "https://auth.papercut-labs.com/handoff/request-1?sig=abc")]
        # The driver's 30-second rate floor wins over this row's 1-second
        # cadence, so timeout is observed on the first eligible probe after it.
        current[0] = record.reauth_deadline_at + timedelta(seconds=30)
        await watcher.run_once()
        assert record.reauth_state is ReauthState.TIMED_OUT
        assert timeouts == ["credential-1"]

    asyncio.run(run())


def test_unsafe_driver_probe_escalates_without_calling_probe():
    async def run():
        class UnsafeDriver(HumanBrowserSessionDriver):
            key = "unsafe"
            probe_contract = ProbeContract(1, True, "safe")

            async def probe(self, material, *, now):
                raise UnsafeProbeError("probe cannot prove safety")

        record = _record(driver_key="unsafe", material=ByValueMaterial("opaque"))
        escalated: list[str] = []
        watcher = CredentialWatcher(
            [record],
            drivers=DriverRegistry([UnsafeDriver()]),
            on_escalation=lambda row, result: escalated.append(row.credential_id),
        )
        await watcher.run_once()
        assert record.last_probe_status == "unsafe"
        assert escalated == ["credential-1"]

    asyncio.run(run())


def test_due_uses_the_maximum_of_row_and_driver_cadence():
    now = datetime(2026, 10, 30, tzinfo=UTC)

    class SlowDriver(CredentialDriver):
        key = "slow"
        probe_contract = ProbeContract(60, True, "synthetic local read")

        async def probe(self, material, *, now):
            return ProbeResult(healthy=True, status="healthy")

        async def begin_reauth(self, record):
            raise AssertionError("healthy credential must not reauthenticate")

    record = _record(driver_key="slow", cadence=1)
    driver = SlowDriver()
    watcher = CredentialWatcher([record], drivers=DriverRegistry([driver]))
    record.last_probe_at = now - timedelta(seconds=59)
    assert watcher._due(record, now, driver) is False
    record.last_probe_at = now - timedelta(seconds=60)
    assert watcher._due(record, now, driver) is True


def test_one_bad_row_cannot_stop_other_credential_probes():
    async def run():
        now = datetime(2026, 10, 30, tzinfo=UTC)

        class BadResultDriver(CredentialDriver):
            key = "bad-result"
            probe_contract = ProbeContract(1, True, "synthetic local read")

            async def probe(self, material, *, now):
                return object()

            async def begin_reauth(self, record):
                raise AssertionError("malformed probe result must fail before re-auth")

        class HealthyDriver(CredentialDriver):
            key = "healthy"
            probe_contract = ProbeContract(1, True, "synthetic local read")

            async def probe(self, material, *, now):
                return ProbeResult(healthy=True, status="healthy")

            async def begin_reauth(self, record):
                raise AssertionError("healthy credential must not reauthenticate")

        bad = _record(driver_key="bad-result")
        good = _record(driver_key="healthy")
        good.credential_id = "credential-2"
        failures: list[str] = []
        watcher = CredentialWatcher(
            [bad, good],
            drivers=DriverRegistry([BadResultDriver(), HealthyDriver()]),
            on_escalation=lambda row, result: failures.append(row.credential_id),
            clock=lambda: now,
        )
        await watcher.run_once()
        assert good.last_probe_at == now
        assert good.last_probe_status == "healthy"
        assert bad.last_probe_status == "error"
        assert bad.credential_id in failures

    asyncio.run(run())


def test_watcher_loads_json_file_and_missing_file_disables_cleanly(
    tmp_path: Path, monkeypatch
):
    async def run():
        absent = tmp_path / "missing.json"
        monkeypatch.setenv("PA_CREDENTIALS_FILE", str(absent))
        disabled = CredentialWatcher.from_environment()
        assert disabled.enabled is False
        assert await disabled.start() is None

        source = tmp_path / "credentials.json"
        source.write_text(
            json.dumps(
                {
                    "ok": True,
                    "data": [
                        {
                            "id": "row-1",
                            "driver_key": "bearer-jwt",
                            "material_mode": "by-value",
                            "material_envelope": "opaque-only",
                            "expires_at": "2026-10-29T00:00:00Z",
                            "probe_cadence_seconds": 5,
                            "probe_non_destructive": True,
                            "probe_safety_contract": {
                                "operation": "local-read",
                                "must_not": ["mutate", "invalidate"],
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("PA_CREDENTIALS_FILE", str(source))
        loaded = CredentialWatcher.from_environment()
        assert loaded.enabled is True
        assert loaded.credentials[0].public_dict()["id"] == "row-1"
        assert "opaque-only" not in json.dumps(loaded.credentials[0].public_dict())

    asyncio.run(run())


def test_runtime_loader_rejects_non_success_or_unexpected_envelopes(
    tmp_path: Path,
):
    cases = (
        [],
        {"ok": False, "data": []},
        {"ok": True},
        {"ok": True, "data": {}},
        {"ok": True, "data": [], "unexpected": "field"},
        {"credentials": []},
    )
    for index, payload in enumerate(cases):
        source = tmp_path / f"invalid-{index}.json"
        source.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(CredentialContractError):
            load_runtime_credentials(source)


def test_exact_runtime_export_envelope_preserves_metadata_and_watches(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "runtime-export.json"
    token = _jwt(datetime(2026, 10, 29, tzinfo=UTC))
    source.write_text(
        json.dumps(
            {
                "ok": True,
                "data": [
                    {
                        "id": "row-1",
                        "tenant_slug": "tenant-a",
                        "agent_slug": "runtime-agent",
                        "credential_slug": "primary",
                        "driver_key": "bearer-jwt",
                        "material_mode": "by-value",
                        "material_envelope": token,
                        "expires_at": "2026-10-29T00:00:00Z",
                        "reauth_state": "requested",
                        "reauth_requested_at": "2026-10-28T23:55:00Z",
                        "last_transition_at": "2026-10-28T23:55:01Z",
                        "escalation_policy": {"channel": "operator", "priority": "high"},
                        "timeout_policy": {"seconds": 17},
                        "timeout_after_seconds": 999,
                        "probe_cadence_seconds": 300,
                        "probe_non_destructive": True,
                        "probe_safety_contract": {
                            "operation": "local-read",
                            "must_not": ["mutate", "invalidate"],
                        },
                        "last_probe_at": "2026-10-29T00:00:00Z",
                        "last_probe_status": "stale",
                    }
                ],
                "meta": {"source": "synthetic-export"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PA_CREDENTIALS_FILE", str(source))
    escalated: list[str] = []
    watcher = CredentialWatcher.from_environment(
        clock=lambda: datetime(2026, 10, 29, 0, 6, tzinfo=UTC),
        on_escalation=lambda row, result: escalated.append(row.credential_id),
    )
    record = watcher.credentials[0]
    assert record.reauth_requested_at == datetime(2026, 10, 28, 23, 55, tzinfo=UTC)
    assert record.last_transition_at == datetime(2026, 10, 28, 23, 55, 1, tzinfo=UTC)
    assert record.last_probe_at == datetime(2026, 10, 29, tzinfo=UTC)
    assert record.last_probe_status == "stale"
    assert record.escalation_policy == {"channel": "operator", "priority": "high"}
    assert record.timeout_policy == {"seconds": 17}
    assert record.timeout_after_seconds == 17
    public = record.public_dict()
    assert public["last_probe_status"] == "stale"
    assert public["timeout_policy"] == {"seconds": 17}
    assert token not in json.dumps(public)

    asyncio.run(watcher.run_once())
    assert record.reauth_state is not ReauthState.COMPLETED
    assert record.last_probe_status == "expired"
    assert escalated == ["row-1"]


def test_start_stop_owns_task_without_leak():
    async def run():
        stop_sleep = asyncio.Event()

        async def sleep(_seconds):
            await stop_sleep.wait()

        record = _record()
        watcher = CredentialWatcher([record], sleep=sleep)
        task = await watcher.start()
        assert task is not None
        await asyncio.sleep(0)
        assert not task.done()
        await watcher.stop()
        assert task.done()
        assert watcher.task is None

    asyncio.run(run())


def test_gateway_start_path_injects_configured_pa_adapters(monkeypatch):
    # This invokes GatewayRunner's actual lifecycle method.  The adapter
    # constructors are replaced before it runs, so the test cannot touch
    # Carbon Auth or any client surface.
    pytest.importorskip("httpx")
    gateway_run = pytest.importorskip("gateway.run")
    import hermes_cli.pa_credentials as credentials

    captured: dict[str, object] = {}
    carbon = object()
    signer = object()

    class Watcher:
        started = False

        async def start(self):
            self.started = True
            return None

    watcher = Watcher()

    def create(**kwargs):
        captured.update(kwargs)
        return watcher

    monkeypatch.setattr(credentials, "CarbonAuthV1Client", lambda: carbon)
    monkeypatch.setattr(
        credentials,
        "configured_handoff_signer_from_environment",
        lambda: signer,
    )
    monkeypatch.setattr(credentials, "create_pa_credentials_watcher", create)

    runner = gateway_run.GatewayRunner.__new__(gateway_run.GatewayRunner)
    runner._background_tasks = set()
    asyncio.run(runner._start_pa_credentials_watcher())

    assert watcher.started is True
    assert runner._pa_credentials_watcher is watcher
    assert captured["carbon_auth"] is carbon
    assert captured["handoff_signer"] is signer
    assert callable(captured["on_escalation"])
    assert callable(captured["on_timeout"])
