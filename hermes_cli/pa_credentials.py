"""Tenant-neutral PA credential lifecycle contracts.

This module deliberately owns only the runtime contract.  Credential rows are
exported as JSON by the registry and loaded through ``PA_CREDENTIALS_FILE``;
Hermes never opens a registry database or emits credential material.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlparse


UTC = timezone.utc
CANONICAL_HANDOFF_HOST = "auth.papercut-labs.com"
CARBON_AUTH_BASE = "https://agents.papercut-labs.com"


class CredentialContractError(ValueError):
    """Raised when a runtime credential row violates the contract."""


class InvalidTransitionError(CredentialContractError):
    """Raised for a re-auth state transition that is not legal."""


class UnsafeProbeError(CredentialContractError):
    """Raised when a driver cannot prove that its probe is safe."""


class InvalidHandoffUrlError(CredentialContractError):
    """Raised when an operator handoff URL is not canonical and signed."""


class ReauthState(str, Enum):
    REQUESTED = "requested"
    PENDING_HUMAN = "pending-human"
    COMPLETED = "completed"
    TIMED_OUT = "timed-out"


_LEGAL_TRANSITIONS: dict[ReauthState, frozenset[ReauthState]] = {
    ReauthState.REQUESTED: frozenset({ReauthState.PENDING_HUMAN}),
    ReauthState.PENDING_HUMAN: frozenset(
        {ReauthState.COMPLETED, ReauthState.TIMED_OUT}
    ),
    ReauthState.COMPLETED: frozenset(),
    ReauthState.TIMED_OUT: frozenset(),
}


@dataclass
class ReauthStateMachine:
    """The human-in-loop state machine for one credential row."""

    state: ReauthState = ReauthState.REQUESTED

    def transition(self, target: ReauthState) -> ReauthState:
        target = ReauthState(target)
        if target not in _LEGAL_TRANSITIONS[self.state]:
            raise InvalidTransitionError(
                f"illegal re-auth transition: {self.state.value} -> {target.value}"
            )
        self.state = target
        return self.state


@dataclass(frozen=True)
class ByReferenceMaterial:
    """A host-bound locator resolved by the Bedrock host registry."""

    host_id: str
    reference_locator: str

    def __post_init__(self) -> None:
        if not isinstance(self.host_id, str) or not self.host_id.strip():
            raise CredentialContractError("by-reference material requires host_id")
        if not isinstance(self.reference_locator, str) or not self.reference_locator.strip():
            raise CredentialContractError(
                "by-reference material requires reference_locator"
            )

    @property
    def mode(self) -> str:
        return "by-reference"

    def public_metadata(self) -> dict[str, str]:
        return {
            "material_mode": self.mode,
            "host_id": self.host_id,
            "reference_locator": self.reference_locator,
        }

    def resolve(self, resolver: Callable[[str, str], Any]) -> Any:
        """Resolve through the injected ``pa_runtime_hosts`` read path."""
        return resolver(self.host_id, self.reference_locator)


@dataclass(frozen=True)
class ByValueMaterial:
    """An opaque serializable/encrypted envelope.

    The value is intentionally private-by-convention: public row views expose
    only ``material_mode`` and never include this field.
    """

    opaque: Any = field(repr=False)

    def __post_init__(self) -> None:
        if (
            self.opaque is None
            or self.opaque == ""
            or self.opaque == {}
            or self.opaque == []
        ):
            raise CredentialContractError("by-value material requires an opaque envelope")
        try:
            json.dumps(self.opaque)
        except (TypeError, ValueError) as exc:
            raise CredentialContractError(
                "by-value material must be JSON serializable"
            ) from exc

    @property
    def mode(self) -> str:
        return "by-value"

    def public_metadata(self) -> dict[str, str]:
        return {"material_mode": self.mode}

    def _for_driver(self) -> Any:
        """Return opaque input to a driver without making it a row output."""
        return self.opaque


CredentialMaterial = ByReferenceMaterial | ByValueMaterial


def _parse_datetime(value: Any, *, field_name: str) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CredentialContractError(f"invalid {field_name}") from exc
    else:
        raise CredentialContractError(f"invalid {field_name}")
    if parsed.tzinfo is None:
        raise CredentialContractError(f"{field_name} must include a timezone")
    return parsed.astimezone(UTC)


def _material_from_row(row: Mapping[str, Any]) -> CredentialMaterial:
    nested = row.get("material")
    if nested is not None:
        if not isinstance(nested, Mapping):
            raise CredentialContractError("material must be an object")
        source: Mapping[str, Any] = nested
        mode = source.get("mode")
        host_id = source.get("host_id")
        reference_locator = source.get("reference_locator")
        envelope = source.get("material_envelope", source.get("opaque"))
    else:
        source = row
        mode = source.get("material_mode")
        host_id = source.get("host_id")
        reference_locator = source.get("reference_locator")
        envelope = source.get("material_envelope", source.get("opaque"))

    if mode == "by-reference":
        if envelope not in (None, "", {}, []):
            raise CredentialContractError("by-reference material cannot include by-value material")
        return ByReferenceMaterial(
            host_id=host_id or "",
            reference_locator=reference_locator or "",
        )
    if mode == "by-value":
        if host_id not in (None, "") or reference_locator not in (None, ""):
            raise CredentialContractError("by-value material cannot include host-bound fields")
        return ByValueMaterial(envelope)
    raise CredentialContractError(
        "material_mode must be exactly by-reference or by-value"
    )


@dataclass
class CredentialRecord:
    """Runtime-safe credential metadata plus private driver input."""

    credential_id: str
    driver_key: str
    material: CredentialMaterial
    expires_at: datetime | None
    probe_contract: "ProbeContract"
    reauth_state: ReauthState = ReauthState.REQUESTED
    reauth_deadline_at: datetime | None = None
    reauth_request_id: str | None = None
    tenant_slug: str = ""
    agent_slug: str = ""
    credential_slug: str = ""
    portal: str = "runtime"
    reason: str = "credential requires human re-authentication"
    urgency: str = "standard"
    principal_id: str = "operator"
    agent_pubkey: str = ""
    timeout_after_seconds: float = 600.0
    last_probe_at: datetime | None = None
    last_probe_status: str | None = None
    _state_machine: ReauthStateMachine = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.reauth_state = ReauthState(self.reauth_state)
        self._state_machine = ReauthStateMachine(self.reauth_state)
        if not self.credential_id.strip():
            raise CredentialContractError("credential_id is required")
        if not self.driver_key.strip():
            raise CredentialContractError("driver_key is required")
        if self.timeout_after_seconds <= 0:
            raise CredentialContractError("timeout_after_seconds must be positive")

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "CredentialRecord":
        if not isinstance(row, Mapping):
            raise CredentialContractError("credential row must be an object")
        probe = row.get("probe") or {}
        if not isinstance(probe, Mapping):
            raise CredentialContractError("probe must be an object")
        cadence = row.get("probe_cadence_seconds", probe.get("cadence_seconds"))
        non_destructive = row.get(
            "probe_non_destructive", probe.get("non_destructive")
        )
        safety_contract = row.get(
            "probe_safety_contract", probe.get("safety_contract")
        )
        contract = ProbeContract(
            cadence_seconds=cadence,
            non_destructive=non_destructive,
            safety_contract=safety_contract,
        )
        state = ReauthState(row.get("reauth_state", ReauthState.REQUESTED.value))
        return cls(
            credential_id=str(row.get("id", row.get("credential_id", ""))),
            driver_key=str(row.get("driver_key", "")),
            material=_material_from_row(row),
            expires_at=_parse_datetime(row.get("expires_at"), field_name="expires_at"),
            probe_contract=contract,
            reauth_state=state,
            reauth_deadline_at=_parse_datetime(
                row.get("reauth_deadline_at"), field_name="reauth_deadline_at"
            ),
            reauth_request_id=row.get("reauth_request_id"),
            tenant_slug=str(row.get("tenant_slug", "")),
            agent_slug=str(row.get("agent_slug", "")),
            credential_slug=str(row.get("credential_slug", "")),
            portal=str(row.get("portal", "runtime")),
            reason=str(row.get("reason", "credential requires human re-authentication")),
            urgency=str(row.get("urgency", "standard")),
            principal_id=str(row.get("principal_id", "operator")),
            agent_pubkey=str(row.get("agent_pubkey", "")),
            timeout_after_seconds=float(row.get("timeout_after_seconds", 600.0)),
        )

    def transition(self, target: ReauthState) -> ReauthState:
        state = self._state_machine.transition(target)
        self.reauth_state = state
        return state

    def complete_auto(self) -> ReauthState:
        """Complete the bearer degenerate case without a human handoff."""
        if self.reauth_state not in {
            ReauthState.REQUESTED,
            ReauthState.COMPLETED,
        }:
            raise InvalidTransitionError(
                f"cannot auto-complete from {self.reauth_state.value}"
            )
        self.reauth_state = ReauthState.COMPLETED
        self._state_machine.state = ReauthState.COMPLETED
        return self.reauth_state

    def public_dict(self) -> dict[str, Any]:
        """Return metadata safe for logs, receipts, and status surfaces."""
        public: dict[str, Any] = {
            "id": self.credential_id,
            "driver_key": self.driver_key,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "reauth_state": self.reauth_state.value,
            "reauth_request_id": self.reauth_request_id,
            "reauth_deadline_at": (
                self.reauth_deadline_at.isoformat()
                if self.reauth_deadline_at
                else None
            ),
            "probe_cadence_seconds": self.probe_contract.cadence_seconds,
            "probe_non_destructive": self.probe_contract.non_destructive,
            "probe_safety_contract": self.probe_contract.safety_contract,
            "last_probe_at": (
                self.last_probe_at.isoformat() if self.last_probe_at else None
            ),
            "last_probe_status": self.last_probe_status,
        }
        public.update(self.material.public_metadata())
        return public


@dataclass(frozen=True)
class ProbeContract:
    cadence_seconds: float
    non_destructive: bool
    safety_contract: str

    def __post_init__(self) -> None:
        try:
            cadence = float(self.cadence_seconds)
        except (TypeError, ValueError) as exc:
            raise UnsafeProbeError("probe cadence must be numeric") from exc
        if cadence <= 0:
            raise UnsafeProbeError("probe cadence must be positive")
        if self.non_destructive is not True:
            raise UnsafeProbeError("probe must explicitly declare non-destructive safety")
        if not isinstance(self.safety_contract, str) or not self.safety_contract.strip():
            raise UnsafeProbeError("probe safety contract is required")
        object.__setattr__(self, "cadence_seconds", cadence)

    def validate(self) -> None:
        self.__post_init__()


@dataclass(frozen=True)
class ProbeResult:
    healthy: bool
    needs_reauth: bool = False
    expires_at: datetime | None = None
    status: str = "unknown"


@dataclass(frozen=True)
class ReauthResult:
    state: ReauthState
    request_id: str | None = None
    handoff_url: str | None = None


class CredentialDriver(ABC):
    """Driver interface shared by every credential kind."""

    key: str
    probe_contract: ProbeContract

    @abstractmethod
    async def probe(
        self, material: CredentialMaterial, *, now: datetime
    ) -> ProbeResult:
        """Return a health verdict without mutating or invalidating material."""

    @abstractmethod
    async def begin_reauth(self, record: CredentialRecord) -> ReauthResult:
        """Start the driver's async re-auth path."""


def _decode_jwt_payload(token: str) -> Mapping[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise CredentialContractError("bearer material is not a JWT")
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialContractError("bearer JWT payload is invalid") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("exp"), (int, float)):
        raise CredentialContractError("bearer JWT exp claim is required")
    return payload


class BearerJwtDriver(CredentialDriver):
    """Local JWT expiry checker; it never performs network I/O."""

    key = "bearer-jwt"
    probe_contract = ProbeContract(
        cadence_seconds=300.0,
        non_destructive=True,
        safety_contract="parse the local exp claim only; no request is made",
    )

    @staticmethod
    def _token_from_material(material: CredentialMaterial) -> str:
        if not isinstance(material, ByValueMaterial):
            raise CredentialContractError("bearer-jwt requires by-value material")
        opaque = material._for_driver()
        if not isinstance(opaque, str):
            raise CredentialContractError("bearer-jwt opaque material must be a JWT string")
        return opaque

    @classmethod
    def expiry(cls, material: CredentialMaterial) -> datetime:
        payload = _decode_jwt_payload(cls._token_from_material(material))
        return datetime.fromtimestamp(float(payload["exp"]), tz=UTC)

    async def probe(
        self, material: CredentialMaterial, *, now: datetime
    ) -> ProbeResult:
        expiry = self.expiry(material)
        current = now.astimezone(UTC)
        healthy = expiry > current
        return ProbeResult(
            healthy=healthy,
            needs_reauth=not healthy,
            expires_at=expiry,
            status="healthy" if healthy else "expired",
        )

    async def begin_reauth(self, record: CredentialRecord) -> ReauthResult:
        return ReauthResult(ReauthState.COMPLETED)


class HumanBrowserSessionDriver(CredentialDriver):
    """Generic no-op adopter for credentials requiring an operator session."""

    key = "human-browser-session"
    probe_contract = ProbeContract(
        cadence_seconds=30.0,
        non_destructive=True,
        safety_contract="read-only local state check; no browser or network action",
    )

    async def probe(
        self, material: CredentialMaterial, *, now: datetime
    ) -> ProbeResult:
        return ProbeResult(
            healthy=False,
            needs_reauth=True,
            status="human-required",
        )

    async def begin_reauth(self, record: CredentialRecord) -> ReauthResult:
        return ReauthResult(ReauthState.PENDING_HUMAN)


class DriverRegistry:
    """Tenant-neutral driver registry."""

    def __init__(self, drivers: Sequence[CredentialDriver] | None = None) -> None:
        self._drivers: dict[str, CredentialDriver] = {}
        for driver in drivers or (BearerJwtDriver(), HumanBrowserSessionDriver()):
            self.register(driver)

    def register(self, driver: CredentialDriver) -> None:
        if not driver.key.strip():
            raise CredentialContractError("driver key is required")
        self._drivers[driver.key] = driver

    def get(self, key: str) -> CredentialDriver:
        try:
            return self._drivers[key]
        except KeyError as exc:
            raise CredentialContractError(f"unknown credential driver: {key}") from exc

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._drivers))


@dataclass(frozen=True)
class CarbonAuthRequest:
    """The request body used by Carbon Authentication v1."""

    agent_id: str
    agent_pubkey: str
    portal: str
    reason: str
    urgency: str = "standard"
    principal_id: str = "operator"

    def as_payload(self) -> dict[str, str]:
        return {
            "agent_id": self.agent_id,
            "agent_pubkey": self.agent_pubkey,
            "portal": self.portal,
            "reason": self.reason,
            "urgency": self.urgency,
            "principal_id": self.principal_id,
        }


@dataclass(frozen=True)
class CarbonAuthResponse:
    request_id: str
    start_url: str


class CarbonAuthClient(Protocol):
    async def request(self, request: CarbonAuthRequest) -> CarbonAuthResponse:
        """POST a Carbon Auth v1 re-auth request."""


async def _post_json(url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    body = json.dumps(payload).encode("utf-8")

    def send() -> Mapping[str, Any]:
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise CredentialContractError("Carbon Auth request failed") from exc
        if not isinstance(decoded, Mapping):
            raise CredentialContractError("Carbon Auth response must be an object")
        return decoded

    return await asyncio.to_thread(send)


class CarbonAuthV1Client:
    """Small injected-adapter client for the existing Carbon Auth v1 endpoint."""

    def __init__(
        self,
        *,
        post_json: Callable[[str, Mapping[str, Any]], Awaitable[Mapping[str, Any]] | Mapping[str, Any]] | None = None,
        base_url: str | None = None,
    ) -> None:
        self._post_json = post_json or _post_json
        self._base_url = (base_url or os.getenv("PCL_CARBON_AUTH_BASE") or CARBON_AUTH_BASE).rstrip("/")

    async def request(self, request: CarbonAuthRequest) -> CarbonAuthResponse:
        raw = self._post_json(
            f"{self._base_url}/carbon-auth/request",
            request.as_payload(),
        )
        response = await raw if inspect.isawaitable(raw) else raw
        if not isinstance(response, Mapping):
            raise CredentialContractError("Carbon Auth response must be an object")
        if response.get("ok") is False:
            raise CredentialContractError("Carbon Auth request was rejected")
        data = response.get("data", response)
        if not isinstance(data, Mapping):
            raise CredentialContractError("Carbon Auth response data must be an object")
        request_id = data.get("request_id")
        start_url = data.get("start_url")
        if not isinstance(request_id, str) or not request_id:
            raise CredentialContractError("Carbon Auth response omitted request_id")
        if not isinstance(start_url, str) or not start_url:
            raise CredentialContractError("Carbon Auth response omitted start_url")
        return CarbonAuthResponse(request_id=request_id, start_url=start_url)


@dataclass(frozen=True)
class HandoffSigningRequest:
    request_id: str
    start_url: str
    credential_id: str


class HandoffUrlSigner(Protocol):
    async def sign(self, request: HandoffSigningRequest) -> str:
        """Return a signed operator handoff URL."""


def validate_handoff_url(url: str) -> str:
    if not isinstance(url, str) or not url:
        raise InvalidHandoffUrlError("handoff URL is required")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != CANONICAL_HANDOFF_HOST:
        raise InvalidHandoffUrlError(
            "handoff URL must use https://auth.papercut-labs.com/"
        )
    if not parsed.path or parsed.path == "/":
        raise InvalidHandoffUrlError("handoff URL must include a signed path")
    if "novnc" in f"{parsed.path}?{parsed.query}".lower():
        raise InvalidHandoffUrlError("raw browser handoff URLs are not accepted")
    return url


class SignedHandoffUrlSigner:
    """Injected signer wrapper with a fail-closed canonical-host check."""

    def __init__(
        self,
        signer: Callable[
            [HandoffSigningRequest], Awaitable[str] | str
        ],
    ) -> None:
        self._signer = signer

    async def sign(self, request: HandoffSigningRequest) -> str:
        value = self._signer(request)
        value = await value if inspect.isawaitable(value) else value
        return validate_handoff_url(value)


EscalationHook = Callable[[CredentialRecord, ReauthResult], Awaitable[None] | None]
TimeoutHook = Callable[[CredentialRecord], Awaitable[None] | None]


async def _invoke_hook(hook: Callable[..., Any] | None, *args: Any) -> None:
    if hook is None:
        return
    result = hook(*args)
    if inspect.isawaitable(result):
        await result


def load_runtime_credentials(path: str | os.PathLike[str]) -> list[CredentialRecord]:
    """Load exported runtime rows without exposing their opaque material."""
    source = Path(path)
    with source.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    rows = payload.get("credentials") if isinstance(payload, Mapping) else payload
    if not isinstance(rows, list):
        raise CredentialContractError("credential export must be a list or credentials object")
    return [CredentialRecord.from_row(row) for row in rows]


class CredentialWatcher:
    """Expiry/probe watcher with explicit start/stop ownership."""

    def __init__(
        self,
        credentials: Sequence[CredentialRecord],
        *,
        drivers: DriverRegistry | None = None,
        carbon_auth: CarbonAuthClient | None = None,
        handoff_signer: HandoffUrlSigner | None = None,
        on_escalation: EscalationHook | None = None,
        on_timeout: TimeoutHook | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        interval_seconds: float = 5.0,
        enabled: bool = True,
    ) -> None:
        self.credentials = list(credentials)
        self.drivers = drivers or DriverRegistry()
        self.carbon_auth = carbon_auth
        self.handoff_signer = handoff_signer
        self.on_escalation = on_escalation
        self.on_timeout = on_timeout
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep or asyncio.sleep
        self.interval_seconds = interval_seconds
        self.enabled = enabled
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    @classmethod
    def from_environment(cls, **kwargs: Any) -> "CredentialWatcher":
        path = os.getenv("PA_CREDENTIALS_FILE")
        if not path or not Path(path).is_file():
            return cls([], enabled=False, **kwargs)
        return cls(load_runtime_credentials(path), enabled=True, **kwargs)

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    async def start(self) -> asyncio.Task[None] | None:
        if not self.enabled or not self.credentials:
            return None
        if self._task is not None and not self._task.done():
            return self._task
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="pa-credentials-watcher")
        return self._task

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while True:
            await self.run_once()
            if self._stop_event is not None and self._stop_event.is_set():
                return
            await self.sleep(self.interval_seconds)

    def _due(self, record: CredentialRecord, now: datetime) -> bool:
        if record.last_probe_at is None:
            return True
        return (now - record.last_probe_at).total_seconds() >= record.probe_contract.cadence_seconds

    async def run_once(self) -> None:
        now = self.clock().astimezone(UTC)
        for record in self.credentials:
            if not self._due(record, now):
                continue
            driver = self.drivers.get(record.driver_key)
            try:
                record.probe_contract.validate()
                driver.probe_contract.validate()
                result = await driver.probe(record.material, now=now)
            except UnsafeProbeError:
                record.last_probe_at = now
                record.last_probe_status = "unsafe"
                await _invoke_hook(
                    self.on_escalation,
                    record,
                    ReauthResult(record.reauth_state),
                )
                continue
            record.last_probe_at = now
            record.last_probe_status = result.status
            if result.healthy and not result.needs_reauth:
                continue
            await self._handle_reauth(record, driver, now)

    async def _handle_reauth(
        self,
        record: CredentialRecord,
        driver: CredentialDriver,
        now: datetime,
    ) -> None:
        if record.reauth_state == ReauthState.PENDING_HUMAN:
            if record.reauth_deadline_at and now >= record.reauth_deadline_at:
                record.transition(ReauthState.TIMED_OUT)
                await _invoke_hook(self.on_timeout, record)
            return
        if record.reauth_state in {
            ReauthState.COMPLETED,
            ReauthState.TIMED_OUT,
        }:
            return

        result = await driver.begin_reauth(record)
        if result.state == ReauthState.COMPLETED:
            record.complete_auto()
            return
        if result.state != ReauthState.PENDING_HUMAN:
            raise CredentialContractError("driver returned an invalid re-auth state")

        record.transition(ReauthState.PENDING_HUMAN)
        if self.carbon_auth is None or self.handoff_signer is None:
            raise CredentialContractError(
                "human re-auth requires Carbon Auth and handoff signer adapters"
            )
        request = CarbonAuthRequest(
            agent_id=record.agent_slug or "hermes-runtime",
            agent_pubkey=record.agent_pubkey,
            portal=record.portal,
            reason=record.reason,
            urgency=record.urgency,
            principal_id=record.principal_id,
        )
        response = await self.carbon_auth.request(request)
        handoff_url = await self.handoff_signer.sign(
            HandoffSigningRequest(
                request_id=response.request_id,
                start_url=response.start_url,
                credential_id=record.credential_id,
            )
        )
        record.reauth_request_id = response.request_id
        record.reauth_deadline_at = now + timedelta(
            seconds=record.timeout_after_seconds
        )
        await _invoke_hook(
            self.on_escalation,
            record,
            ReauthResult(
                ReauthState.PENDING_HUMAN,
                request_id=response.request_id,
                handoff_url=handoff_url,
            ),
        )


def create_pa_credentials_watcher(**kwargs: Any) -> CredentialWatcher:
    """Build the watcher from the exported runtime file, if configured."""
    return CredentialWatcher.from_environment(**kwargs)


__all__ = [
    "BearerJwtDriver",
    "ByReferenceMaterial",
    "ByValueMaterial",
    "CANONICAL_HANDOFF_HOST",
    "CarbonAuthRequest",
    "CarbonAuthResponse",
    "CarbonAuthV1Client",
    "CredentialContractError",
    "CredentialDriver",
    "CredentialRecord",
    "CredentialWatcher",
    "DriverRegistry",
    "HandoffSigningRequest",
    "HandoffUrlSigner",
    "HumanBrowserSessionDriver",
    "InvalidHandoffUrlError",
    "InvalidTransitionError",
    "ProbeContract",
    "ProbeResult",
    "ReauthResult",
    "ReauthState",
    "ReauthStateMachine",
    "SignedHandoffUrlSigner",
    "UnsafeProbeError",
    "create_pa_credentials_watcher",
    "load_runtime_credentials",
    "validate_handoff_url",
]
