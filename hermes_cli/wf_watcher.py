"""Gateway workflow watcher: timers, state probes, and intake recovery.

The watcher is deliberately tenant-neutral.  Tenant integrations register a
read-only probe which receives immutable snapshots of open workflow instances
and returns structured observations.  The shared frame owns event ingestion,
matching, application, timer catch-up, and sweep recovery.
"""

from __future__ import annotations

import inspect
import logging
import sqlite3
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from hermes_cli import kanban_db, wf_engine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProbeTarget:
    """Immutable input handed to a tenant state probe."""

    task_id: str
    tenant: str
    template_id: str
    step_key: str
    entity_key: str
    corr: Mapping[str, Any]
    vars: Mapping[str, Any]
    parked_since: int | None


@dataclass(frozen=True)
class ProbeObservation:
    """One structured, deduplicable observation returned by a probe."""

    external_id: str
    event_type: str
    corr: Mapping[str, Any]
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractionRequest:
    """Engine inputs resolved for one raw workflow event."""

    brief: dict[str, Any] | str
    extractor: Callable[[dict[str, Any] | str, Mapping[str, Any]], dict[str, Any]]
    schema_validator: Any


@dataclass(frozen=True)
class WatchTickResult:
    """Countable output of one watcher tick."""

    timers_fired: tuple[int, ...] = ()
    timer_results: tuple[str, ...] = ()
    poll_events: tuple[int, ...] = ()
    poll_duplicates: int = 0
    probe_errors: int = 0
    timer_errors: int = 0
    sweep_processed: int = 0
    applied_events: tuple[int, ...] = ()


StateProbe = Callable[
    [tuple[ProbeTarget, ...]],
    Iterable[ProbeObservation | Mapping[str, Any]],
]
ExtractorBoundary = Callable[[Mapping[str, Any]], ExtractionRequest]

_STATE_PROBES: dict[str, StateProbe] = {}


def register_state_probe(
    tenant: str,
    probe: StateProbe,
    *,
    read_only: bool,
) -> None:
    """Register one tenant probe under the E7 read-only contract.

    The callback receives values only: no SQLite connection, engine handle, or
    mutation function.  ``read_only=True`` is an explicit capability
    declaration and registrations that do not make it are refused.
    """

    normalized = str(tenant).strip()
    if not normalized:
        raise ValueError("workflow state probe tenant is required")
    if not callable(probe):
        raise TypeError("workflow state probe must be callable")
    if read_only is not True:
        raise ValueError("workflow state probes must declare read_only=True")
    parameters = tuple(inspect.signature(probe).parameters.values())
    if len(parameters) != 1:
        raise TypeError("workflow state probe must accept exactly one targets argument")
    _STATE_PROBES[normalized] = probe


def unregister_state_probe(tenant: str) -> None:
    """Remove a tenant probe, primarily for plugin unload and tests."""

    _STATE_PROBES.pop(str(tenant).strip(), None)


def registered_state_probes() -> tuple[str, ...]:
    """Return registered tenants without exposing callbacks."""

    return tuple(sorted(_STATE_PROBES))


def _json_object(raw: str | None) -> dict[str, Any]:
    value = wf_engine._load_json(raw, {}) or {}
    return value if isinstance(value, dict) else {}


def _probe_targets(conn: sqlite3.Connection) -> dict[str, tuple[ProbeTarget, ...]]:
    rows = conn.execute(
        """
        SELECT i.task_id, i.entity_key, i.template_id, i.corr, i.vars,
               i.parked_since, t.tenant, t.current_step_key
          FROM wf_instance i
          JOIN tasks t ON t.id = i.task_id
         WHERE i.state != 'done'
           AND t.current_step_key IS NOT NULL
         ORDER BY t.tenant, i.task_id
        """
    ).fetchall()
    grouped: dict[str, list[ProbeTarget]] = {}
    for row in rows:
        tenant = str(row["tenant"] or "").strip()
        if tenant not in _STATE_PROBES:
            continue
        grouped.setdefault(tenant, []).append(
            ProbeTarget(
                task_id=row["task_id"],
                tenant=tenant,
                template_id=row["template_id"],
                step_key=row["current_step_key"],
                entity_key=row["entity_key"],
                corr=MappingProxyType(_json_object(row["corr"])),
                vars=MappingProxyType(_json_object(row["vars"])),
                parked_since=(
                    int(row["parked_since"])
                    if row["parked_since"] is not None
                    else None
                ),
            )
        )
    return {tenant: tuple(targets) for tenant, targets in grouped.items()}


def _observation(value: ProbeObservation | Mapping[str, Any]) -> ProbeObservation:
    if isinstance(value, ProbeObservation):
        result = value
    elif isinstance(value, Mapping):
        result = ProbeObservation(
            external_id=str(value.get("external_id") or ""),
            event_type=str(value.get("event_type") or ""),
            corr=value.get("corr") or {},
            payload=value.get("payload") or {},
        )
    else:
        raise TypeError("workflow state probe observations must be mappings")
    if not result.external_id or not result.event_type:
        raise ValueError("workflow state probe observation needs external_id and event_type")
    if not isinstance(result.corr, Mapping) or not isinstance(result.payload, Mapping):
        raise TypeError("workflow state probe corr and payload must be mappings")
    return result


def _drive_matched_event(
    conn: sqlite3.Connection,
    event_id: int,
) -> bool:
    # ``matched`` is a terminal intake status.  Keep classification and
    # application in one outer transaction so a process exit or exception
    # cannot strand a newly matched event.  The engine calls nest through
    # savepoints under this transaction.
    with kanban_db.write_txn(conn):
        result = wf_engine.match_event(conn, int(event_id))
        if result.kind != "matched" or not result.task_id:
            return False
        task = conn.execute(
            "SELECT current_step_key FROM tasks WHERE id = ?", (result.task_id,)
        ).fetchone()
        if task is None or not task["current_step_key"]:
            conn.execute(
                """
                UPDATE wf_event
                   SET status = 'received',
                       matched_task_id = NULL,
                       match_method = NULL
                 WHERE id = ? AND status = 'matched'
                """,
                (int(event_id),),
            )
            return False
        applied = wf_engine.apply_event(
            conn,
            int(event_id),
            result.task_id,
            expected_step=task["current_step_key"],
        )
        if applied.kind in {"re_correlate", "unmatched"}:
            conn.execute(
                """
                UPDATE wf_event
                   SET status = 'received',
                       matched_task_id = NULL,
                       match_method = NULL
                 WHERE id = ? AND status = 'matched'
                """,
                (int(event_id),),
            )
            return False
        if applied.kind != "applied":
            logger.warning(
                "workflow watcher: matched event %s produced apply result %s",
                event_id,
                applied.kind,
            )
            return False
        return True



def resolve_email_extraction_brief(conn: sqlite3.Connection) -> dict | str | None:
    """Resolve the sole catalog extraction contract; never inspect instances."""
    contracts: dict[str, dict | str] = {}
    for row in conn.execute("SELECT spec FROM wf_template ORDER BY slug, version").fetchall():
        try:
            spec = wf_engine._load_json(row["spec"], None)
            workflow = wf_engine._workflow_spec(spec) if isinstance(spec, dict) else {}
            if "email_extraction" not in workflow:
                continue
            brief = workflow["email_extraction"]
            if not isinstance(brief, (dict, str)):
                return None
            wf_engine._extraction_schema(brief)
            contracts[wf_engine._json(brief)] = brief
        except Exception:
            return None
    return next(iter(contracts.values())) if len(contracts) == 1 else None

def _raw_received_email_events(
    conn: sqlite3.Connection,
) -> tuple[sqlite3.Row, ...]:
    """Return raw email intake that must be extracted before matching."""

    return tuple(
        conn.execute(
            """
            SELECT *
              FROM wf_event
             WHERE source = 'email'
               AND status = 'received'
               AND event_type IS NULL
             ORDER BY id
            """
        ).fetchall()
    )


def _event_view(event: sqlite3.Row) -> Mapping[str, Any]:
    """Expose only immutable event data to the extraction-plan resolver."""

    return MappingProxyType(
        {
            "id": int(event["id"]),
            "source": event["source"],
            "external_id": event["external_id"],
            "event_type": event["event_type"],
            "payload": wf_engine._load_json(event["payload"], None),
            "corr": wf_engine._load_json(event["corr"], None),
        }
    )


def _failed_extraction_request(reason: str) -> ExtractionRequest:
    """Build an engine request which durably records boundary failure."""

    def fail(_brief, _event):
        raise RuntimeError(reason)

    return ExtractionRequest(
        brief={"schema": None},
        extractor=fail,
        schema_validator=lambda _schema, _payload: False,
    )


def _extract_raw_email_events(
    conn: sqlite3.Connection,
    extractor_boundary: ExtractorBoundary | None,
) -> None:
    """Extract raw email intake before the generic sweeper can classify it."""

    for event in _raw_received_email_events(conn):
        if extractor_boundary is None:
            request = _failed_extraction_request(
                "workflow watcher has no email extractor boundary"
            )
        else:
            try:
                request = extractor_boundary(_event_view(event))
                if not isinstance(request, ExtractionRequest):
                    raise TypeError(
                        "extractor boundary must return an ExtractionRequest"
                    )
            except Exception as exc:
                logger.exception(
                    "workflow watcher: extractor boundary failed for event %s",
                    event["id"],
                )
                request = _failed_extraction_request(
                    f"extractor boundary failed: {exc}"
                )
        try:
            wf_engine.extract_event(
                conn,
                int(event["id"]),
                request.brief,
                request.extractor,
                request.schema_validator,
            )
        except Exception as exc:
            # A successful extractor can still hit a matching conflict after
            # its classified write commits.  Re-enter through the engine so
            # one poison email closes durably instead of starving every tick.
            logger.exception(
                "workflow watcher: extraction failed for event %s",
                event["id"],
            )
            failed = _failed_extraction_request(
                f"workflow extraction failed: {exc}"
            )
            wf_engine.extract_event(
                conn,
                int(event["id"]),
                failed.brief,
                failed.extractor,
                failed.schema_validator,
            )


def _sweep_after_email_extraction(
    conn: sqlite3.Connection,
    now: int,
) -> wf_engine.SweepResult:
    """Sweep only while no concurrently-ingested raw email is visible."""

    with kanban_db.write_txn(conn):
        # BEGIN IMMEDIATE prevents a new email write between this guard and
        # sweep's own intake query.  If ingress won the race before the lock,
        # leave every intake row for the next tick rather than sweep raw mail.
        if _raw_received_email_events(conn):
            return wf_engine.SweepResult()
        return wf_engine.sweep(conn, int(now))


def run_tick(
    conn: sqlite3.Connection,
    now: int,
    extractor_boundary: ExtractorBoundary | None = None,
    *,
    email_extractor: Callable[[dict | str, Mapping[str, Any]], dict] | None = None,
    email_schema_validator: Any = None,
) -> WatchTickResult:
    """Run one complete timer/probe/sweeper cycle against one board."""

    # Compatibility seam for existing host callers while they move to the
    # explicit boundary. The brief remains catalog-derived here.
    if extractor_boundary is None and email_extractor is not None:
        extractor_boundary = lambda _event: ExtractionRequest(
            brief=resolve_email_extraction_brief(conn) or {},
            extractor=email_extractor,
            schema_validator=email_schema_validator,
        )

    timers = tuple(wf_engine.fire_due_timers(conn, int(now)))
    pending_timers = tuple(
        int(row["id"])
        for row in conn.execute(
            """
            SELECT id FROM wf_event
             WHERE source = 'timer' AND status = 'received'
             ORDER BY id
            """
        ).fetchall()
    )
    timer_results: list[str] = []
    timer_errors = 0
    applied: list[int] = []
    for event_id in pending_timers:
        try:
            result = wf_engine.process_timer_event(conn, event_id)
        except Exception:
            timer_errors += 1
            logger.exception("workflow watcher: timer event %s failed", event_id)
            continue
        timer_results.append(result.kind)
        if result.kind in {"applied", "chase", "exception"}:
            applied.append(event_id)

    poll_events: list[int] = []
    poll_duplicates = 0
    probe_errors = 0
    # Probe calls happen outside a write transaction and receive immutable
    # values only.  A slow tenant probe cannot hold the SQLite writer lock.
    for tenant, targets in _probe_targets(conn).items():
        try:
            observations = tuple(_STATE_PROBES[tenant](targets) or ())
        except Exception:
            probe_errors += 1
            logger.exception(
                "workflow watcher: state probe failed for tenant %s", tenant
            )
            continue
        for raw in observations:
            try:
                observation = _observation(raw)
            except Exception:
                probe_errors += 1
                logger.exception(
                    "workflow watcher: invalid state probe observation for tenant %s",
                    tenant,
                )
                continue
            event_id = wf_engine.ingest_event(
                conn,
                source="state_poll",
                external_id=observation.external_id,
                payload=dict(observation.payload),
                corr=dict(observation.corr),
                event_type=observation.event_type,
            )
            if event_id is None:
                poll_duplicates += 1
                continue
            poll_events.append(event_id)
            if _drive_matched_event(conn, event_id):
                applied.append(event_id)

    _extract_raw_email_events(conn, extractor_boundary)
    swept = _sweep_after_email_extraction(conn, int(now))
    # ``sweep`` classifies before returning.  Include every durable
    # non-create match, not only matches produced in this process, so a
    # gateway restart after classification but before application catches up.
    # Create matches already birthed their instance during classification and
    # deliberately have no event-wait application phase.
    matched_ids = tuple(
        int(row["id"])
        for row in conn.execute(
            """
            SELECT id
              FROM wf_event
             WHERE source != 'timer'
               AND status = 'matched'
               AND COALESCE(match_method, '') != 'deterministic:create'
             ORDER BY id
            """
        ).fetchall()
    )
    for event_id in matched_ids:
        if _drive_matched_event(conn, event_id):
            applied.append(event_id)
    return WatchTickResult(
        timers_fired=timers,
        timer_results=tuple(timer_results),
        poll_events=tuple(poll_events),
        poll_duplicates=poll_duplicates,
        probe_errors=probe_errors,
        timer_errors=timer_errors,
        sweep_processed=swept.processed,
        applied_events=tuple(dict.fromkeys(applied)),
    )


__all__ = [
    "ExtractionRequest",
    "ExtractorBoundary",
    "ProbeObservation",
    "ProbeTarget",
    "StateProbe",
    "WatchTickResult",
    "register_state_probe",
    "registered_state_probes",
    "run_tick",
    "unregister_state_probe",
]
