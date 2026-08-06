"""Bounded, surface-scoped operating dials for Hermes runtimes.

The dial plane deliberately does not apply values to a running agent. It owns
the declaration, validation, on-box overlay, and mutation receipt. Runtime
adapters can resolve values from this store without making config files a
second write path.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml


DEFAULT_DEFINITION_PATH = Path(__file__).with_name("dial_plane.defaults.yaml")
DEFAULT_SCOPE = "default"


class DialPlaneRefusal(ValueError):
    """A fail-closed refusal carrying a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ScopeRegistry:
    """Exact registry of surfaces allowed to own dial overrides."""

    scopes: frozenset[str]

    @classmethod
    def from_scopes(cls, scopes: Iterable[str] = ()) -> "ScopeRegistry":
        normalized = {DEFAULT_SCOPE}
        for scope in scopes:
            if not isinstance(scope, str) or not scope.strip():
                raise DialPlaneRefusal("INVALID_SCOPE", "scope names must be non-empty strings")
            normalized.add(scope.strip())
        return cls(frozenset(normalized))

    def require(self, scope: str) -> str:
        if scope not in self.scopes:
            raise DialPlaneRefusal("UNKNOWN_SCOPE", f"unknown dial scope: {scope}")
        return scope


@dataclass(frozen=True)
class DialDefinition:
    key: str
    value_type: str
    default: Any
    authority_tier: str
    minimum: int | float | None = None
    maximum: int | float | None = None
    allowed: tuple[Any, ...] = ()
    description: str = ""

    def validate(self, value: Any) -> Any:
        if self.value_type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                self._refuse(value, "must be an integer")
        elif self.value_type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                self._refuse(value, "must be a number")
        elif self.value_type == "boolean":
            if not isinstance(value, bool):
                self._refuse(value, "must be a boolean")
        elif self.value_type == "string":
            if not isinstance(value, str):
                self._refuse(value, "must be a string")
        elif self.value_type == "enum":
            if value not in self.allowed:
                self._refuse(value, f"must be one of {list(self.allowed)!r}")
        else:  # Configuration is engine-owned; an invalid declaration fails closed.
            raise DialPlaneRefusal(
                "INVALID_SCHEMA", f"dial {self.key!r} has unsupported type {self.value_type!r}"
            )

        if self.minimum is not None and value < self.minimum:
            self._refuse(value, f"must be >= {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            self._refuse(value, f"must be <= {self.maximum}")
        return value

    def _refuse(self, value: Any, reason: str) -> None:
        raise DialPlaneRefusal(
            "INVALID_VALUE", f"invalid value for dial {self.key!r}: {value!r} {reason}"
        )


@dataclass(frozen=True)
class SlotDefinition:
    name: str
    model: str
    reasoning_effort: str | None
    cost_tag: str
    cost_rank: int


@dataclass(frozen=True)
class DialPlaneSchema:
    dials: Mapping[str, DialDefinition]
    slots: Mapping[str, SlotDefinition]
    authority_tiers: Mapping[str, Mapping[str, Any]]
    scopes: ScopeRegistry

    @classmethod
    def load(cls, path: Path | str = DEFAULT_DEFINITION_PATH) -> "DialPlaneSchema":
        source = Path(path)
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise DialPlaneRefusal("INVALID_SCHEMA", "dial definition must have version 1")

        raw_authorities = raw.get("authority_tiers")
        raw_slots = raw.get("slots")
        raw_dials = raw.get("dials")
        if not all(isinstance(item, dict) for item in (raw_authorities, raw_slots, raw_dials)):
            raise DialPlaneRefusal("INVALID_SCHEMA", "authorities, slots, and dials must be maps")

        slots: dict[str, SlotDefinition] = {}
        for name, item in raw_slots.items():
            try:
                slot = SlotDefinition(
                    name=name,
                    model=item["model"],
                    reasoning_effort=item.get("reasoning_effort"),
                    cost_tag=item["cost_tag"],
                    cost_rank=item["cost_rank"],
                )
            except (KeyError, TypeError) as exc:
                raise DialPlaneRefusal("INVALID_SCHEMA", f"invalid slot {name!r}") from exc
            if not isinstance(slot.model, str) or not slot.model or not isinstance(slot.cost_rank, int):
                raise DialPlaneRefusal("INVALID_SCHEMA", f"invalid slot {name!r}")
            slots[name] = slot

        dials: dict[str, DialDefinition] = {}
        for key, item in raw_dials.items():
            if not isinstance(item, dict):
                raise DialPlaneRefusal("INVALID_SCHEMA", f"invalid dial {key!r}")
            authority = item.get("authority_tier")
            if authority not in raw_authorities:
                raise DialPlaneRefusal(
                    "INVALID_SCHEMA", f"dial {key!r} names unknown authority tier {authority!r}"
                )
            definition = DialDefinition(
                key=key,
                value_type=item.get("type", ""),
                default=item.get("default"),
                authority_tier=authority,
                minimum=item.get("minimum"),
                maximum=item.get("maximum"),
                allowed=tuple(item.get("allowed", ())),
                description=item.get("description", ""),
            )
            definition.validate(definition.default)
            dials[key] = definition

        model_slot = dials.get("model_slot")
        if model_slot is not None:
            declared_slots = set(model_slot.allowed) - {"inherit"}
            if declared_slots != set(slots):
                raise DialPlaneRefusal(
                    "INVALID_SCHEMA", "model_slot allowed values must exactly match declared slots"
                )
            if model_slot.default != "inherit":
                raise DialPlaneRefusal(
                    "INVALID_SCHEMA", "model_slot default must be inherit (no spend-bearing default)"
                )

        return cls(
            dials=dials,
            slots=slots,
            authority_tiers=raw_authorities,
            scopes=ScopeRegistry.from_scopes(raw.get("scopes", ())),
        )

    def with_scopes(self, scopes: Iterable[str]) -> "DialPlaneSchema":
        return DialPlaneSchema(
            dials=self.dials,
            slots=self.slots,
            authority_tiers=self.authority_tiers,
            scopes=ScopeRegistry.from_scopes((*self.scopes.scopes, *scopes)),
        )


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _lock_owner_is_live(path: Path) -> bool:
    """Return True unless the lock positively belongs to a dead process."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = int(payload["pid"])
        expected_started = float(payload["process_started"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        # The owner may still be between O_EXCL creation and its metadata
        # fsync. A malformed fresh lock is not evidence that it is stale, but
        # one surviving beyond that tiny creation window is orphaned.
        try:
            return time.time() - path.stat().st_mtime < 1.0
        except OSError:
            return False
    try:
        import psutil

        process = psutil.Process(pid)
        return process.is_running() and abs(process.create_time() - expected_started) < 0.01
    except (psutil.Error, OSError):
        return False


@contextmanager
def _store_write_lock(store_path: Path, timeout_seconds: float = 10.0):
    """Serialize the store's read-modify-write cycle across processes.

    ``os.replace`` makes each file replacement atomic, but it cannot prevent
    two writers from reading the same predecessor and overwriting each other.
    The sibling O_EXCL lock closes that gap. Dead-owner recovery is PID-start-
    time checked so a crashed writer cannot strand the dial plane forever.
    """
    import psutil

    store_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = store_path.with_name(f".{store_path.name}.lock")
    deadline = time.monotonic() + timeout_seconds
    fd: int | None = None
    owned_inode: int | None = None
    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            owned_inode = os.fstat(fd).st_ino
            payload = {
                "pid": os.getpid(),
                "process_started": psutil.Process().create_time(),
                "acquired_at": datetime.now(timezone.utc).isoformat(),
            }
            os.write(fd, (json.dumps(payload) + "\n").encode("utf-8"))
            os.fsync(fd)
        except FileExistsError:
            if not _lock_owner_is_live(lock_path):
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise DialPlaneRefusal(
                    "STORE_BUSY", f"dial store is locked by another writer: {store_path}"
                )
            time.sleep(0.02)

    try:
        yield
    finally:
        os.close(fd)
        try:
            if lock_path.stat().st_ino == owned_inode:
                lock_path.unlink()
        except FileNotFoundError:
            pass


class DialOverlayStore:
    """Atomic JSON overlay with exact-scope validation and mutation receipts."""

    def __init__(self, path: Path | str, schema: DialPlaneSchema) -> None:
        self.path = Path(path)
        self.schema = schema

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "values": {}, "audit": []}
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DialPlaneRefusal("INVALID_STORE", f"cannot read dial store: {exc}") from exc
        if (
            not isinstance(state, dict)
            or state.get("version") != 1
            or not isinstance(state.get("values"), dict)
            or not isinstance(state.get("audit"), list)
        ):
            raise DialPlaneRefusal("INVALID_STORE", "dial store has an invalid shape")
        return state

    def set(self, *, key: str, scope: str, value: Any, actor: str) -> dict[str, Any]:
        definition = self.schema.dials.get(key)
        if definition is None:
            raise DialPlaneRefusal("UNKNOWN_KEY", f"unknown dial key: {key}")
        self.schema.scopes.require(scope)
        if not isinstance(actor, str) or not actor.strip():
            raise DialPlaneRefusal("INVALID_ACTOR", "actor must be a non-empty string")
        value = definition.validate(value)

        with _store_write_lock(self.path):
            state = self._read()
            values = state["values"]
            scoped_values = values.setdefault(key, {})
            if not isinstance(scoped_values, dict):
                raise DialPlaneRefusal(
                    "INVALID_STORE", f"stored dial {key!r} is not scope-mapped"
                )
            old = scoped_values.get(scope, scoped_values.get(DEFAULT_SCOPE, definition.default))
            scoped_values[scope] = value
            receipt = {
                "id": str(uuid.uuid4()),
                "changed_at": datetime.now(timezone.utc).isoformat(),
                "actor": actor.strip(),
                "key": key,
                "scope": scope,
                "old": old,
                "new": value,
                "authority_tier": definition.authority_tier,
            }
            state["audit"].append(receipt)
            _atomic_write_json(self.path, state)
        return receipt

    def resolve(self, key: str, scope: str = DEFAULT_SCOPE) -> Any:
        definition = self.schema.dials.get(key)
        if definition is None:
            raise DialPlaneRefusal("UNKNOWN_KEY", f"unknown dial key: {key}")
        self.schema.scopes.require(scope)
        state = self._read()
        scoped_values = state["values"].get(key, {})
        if not isinstance(scoped_values, dict):
            raise DialPlaneRefusal("INVALID_STORE", f"stored dial {key!r} is not scope-mapped")
        if scope in scoped_values:
            return definition.validate(scoped_values[scope])
        if DEFAULT_SCOPE in scoped_values:
            return definition.validate(scoped_values[DEFAULT_SCOPE])
        return definition.default

    def audit(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self._read()["audit"])


def _parse_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _configured_schema_and_store(args: argparse.Namespace) -> tuple[DialPlaneSchema, Path]:
    """Resolve the one declared scope registry and canonical overlay path.

    Scope names come from shipped/user configuration, never from a write-time
    ``--allow-scope`` escape hatch. That keeps UNKNOWN_SCOPE a real refusal.
    """
    from hermes_cli.config import load_config
    from hermes_constants import get_hermes_home

    config = load_config()
    dial_config = config.get("dial_plane", {})
    if not isinstance(dial_config, dict):
        raise DialPlaneRefusal("INVALID_SCHEMA", "dial_plane config must be a map")
    scopes = dial_config.get("scopes", [])
    if not isinstance(scopes, list):
        raise DialPlaneRefusal("INVALID_SCHEMA", "dial_plane.scopes must be a list")
    definitions = getattr(args, "definitions", None) or DEFAULT_DEFINITION_PATH
    configured_store = dial_config.get("store_path")
    store_path = getattr(args, "store", None) or configured_store
    if store_path:
        store_path = Path(store_path).expanduser()
    else:
        store_path = get_hermes_home() / "runtime" / "dials.json"
    return DialPlaneSchema.load(definitions).with_scopes(scopes), store_path


def dials_command(args: argparse.Namespace) -> int:
    """Execute the sole dial mutation command registered by ``hermes``."""

    try:
        schema, store_path = _configured_schema_and_store(args)
        store = DialOverlayStore(store_path, schema)
        result = store.set(
            key=args.key,
            scope=args.scope,
            value=_parse_value(args.value),
            actor=args.actor,
        )
    except DialPlaneRefusal as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": str(exc)}}))
        return 2
    print(json.dumps({"ok": True, "data": {"receipt": result, "store": str(store_path)}}, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Gated on-box write command: ``python -m hermes_cli.dial_plane set``."""
    parser = argparse.ArgumentParser(description="Mutate bounded Hermes operating dials")
    commands = parser.add_subparsers(dest="command", required=True)
    setter = commands.add_parser("set")
    setter.add_argument("--key", required=True)
    setter.add_argument("--scope", required=True)
    setter.add_argument("--value", required=True)
    setter.add_argument("--actor", required=True)
    setter.add_argument("--definitions", type=Path, default=DEFAULT_DEFINITION_PATH)
    setter.add_argument("--store", type=Path)
    return dials_command(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
