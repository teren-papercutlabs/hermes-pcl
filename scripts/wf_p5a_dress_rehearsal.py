"""Run the credential-free P5a workflow dress rehearsal.

This driver deliberately extends the integration shape established by
``test_p4_synthetic_full_loop.py``: email bodies cross the real adapter as
immutable body references, while extraction and workflow decisions stay
deterministic and use the real engine, watcher, approvals, and SQLite board.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import yaml

# A direct ``python scripts/...`` invocation puts ``scripts/`` ahead of the
# checkout on ``sys.path``.  Pin imports to this worker checkout so the driver
# and its focused test exercise the same engine revision.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.config import PlatformConfig
from gateway.platforms.email import EmailAdapter
from hermes_cli import kanban_db, wf_engine, wf_watcher


FIXED_NOW = 2_000_000_000
TIMER_CADENCE = 72 * 60 * 60
WORKFLOW_FIXTURE = ROOT / "tests" / "fixtures" / "workflow" / "synthetic_allied_like.yaml"
CASE_KEYS = (
    "shared_ref_two_live",
    "zero_match_hold_heal",
    "two_match_human_pick",
    "out_of_order_buffer",
    "duplicates_three_layers",
    "done_instance_retention",
    "chase_caps",
    "timer_races",
    "propose_approve_edit_diff",
    "manual_observation_advance",
)
CORRELATION_FIELDS = {"booking_ref", "container_no", "job_no"}
VALUE_FIELDS = {
    "booking_ref",
    "container_no",
    "job_no",
    "customer",
    "vessel",
    "direction",
    "operation",
    "mutation",
    "result",
    "value",
    "field",
}


@contextmanager
def _fixed_clock():
    """Keep both engine and kanban timestamps stable without touching stdlib time."""

    original_engine_now = wf_engine._now
    original_kanban_time = kanban_db.time
    wf_engine._now = lambda: FIXED_NOW
    kanban_db.time = SimpleNamespace(time=lambda: FIXED_NOW)
    try:
        yield
    finally:
        wf_engine._now = original_engine_now
        kanban_db.time = original_kanban_time


def _read_template() -> dict:
    document = yaml.safe_load(WORKFLOW_FIXTURE.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"workflow"}:
        raise ValueError("workflow fixture must contain exactly one workflow object")
    workflow = document["workflow"]
    if not isinstance(workflow, dict):
        raise ValueError("workflow fixture object is required")
    return workflow


def _non_empty(path: Path) -> bool:
    if not path.exists():
        return False
    if path.is_file():
        return path.stat().st_size > 0
    if path.is_dir():
        return any(path.iterdir())
    return True


def _json_value(value):
    if isinstance(value, sqlite3.Row):
        return {key: _json_value(value[key]) for key in value.keys()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


def _row_citation(
    conn: sqlite3.Connection,
    *,
    table: str,
    query: str,
    params: tuple = (),
    identity: str,
    fields: tuple[str, ...],
    extra: dict | None = None,
) -> dict:
    row = conn.execute(query, params).fetchone()
    if row is None:
        raise AssertionError(f"citation query returned no row: {identity}")
    observed = {field: _json_value(row[field]) for field in fields}
    if extra:
        observed.update(_json_value(extra))
    return {
        "table": table,
        "identity": identity,
        "query": query,
        "observed": observed,
    }


def _count_citation(
    conn: sqlite3.Connection,
    *,
    table: str,
    query: str,
    params: tuple,
    identity: str,
    field: str = "count",
    extra: dict | None = None,
) -> dict:
    row = conn.execute(query, params).fetchone()
    if row is None:
        raise AssertionError(f"count query returned no row: {identity}")
    observed = {field: int(row[field])}
    if extra:
        observed.update(_json_value(extra))
    return {
        "table": table,
        "identity": identity,
        "query": query,
        "observed": observed,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DressRehearsal:
    """One isolated, fixed-clock rehearsal against a real SQLite board."""

    extraction_brief = {
        "schema": "synthetic_event_v1",
        "instruction": "Read the persisted body reference and return typed event fields.",
    }

    def __init__(self, *, db_path: Path, home: Path):
        self.db_path = db_path
        self.home = home
        self.conn: sqlite3.Connection | None = None
        self.template_id = ""
        self.body_refs: dict[int, dict] = {}
        self.email_ingest_results: list[int | None] = []
        self._sequence = 0
        self._adapter: EmailAdapter | None = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self.conn is None:
            raise RuntimeError("rehearsal connection is closed")
        return self.conn

    def _next(self, label: str) -> str:
        self._sequence += 1
        return f"p5a-{label}-{self._sequence}"

    def _email_message(self, message_id: str, subject: str, body: str) -> dict:
        return {
            "uid": message_id.encode("utf-8"),
            "sender_addr": "operator@example.test",
            "sender_name": "Synthetic Operator",
            "subject": subject,
            "message_id": message_id,
            "in_reply_to": "",
            "references": "",
            "body": body,
            "attachments": [],
            "date": "Tue, 1 Jan 2030 00:00:00 +0000",
        }

    def _record_email(self, envelope: dict) -> int | None:
        event_id = wf_engine.ingest_event(
            self.connection,
            source="email",
            external_id=envelope["external_id"],
            payload=envelope,
            corr={},
            event_type=None,
        )
        self.email_ingest_results.append(event_id)
        if event_id is None:
            return None
        body_path = Path(envelope["body_ref"]).resolve()
        home_path = self.home.resolve()
        if home_path not in body_path.parents:
            raise AssertionError("email body reference escaped the isolated home")
        self.body_refs[int(event_id)] = {
            "path": str(body_path.relative_to(home_path)),
            "sha256": _sha256(body_path),
            "exists": body_path.is_file(),
        }
        return int(event_id)

    def _dispatch_email(self, body: str, *, label: str, message_id: str | None = None) -> int:
        if self._adapter is None:
            raise RuntimeError("email adapter is not initialized")
        external_id = message_id or f"<{self._next(label)}@synthetic.test>"
        asyncio.run(
            self._adapter._dispatch_message(
                self._email_message(external_id, f"Synthetic {label}", body)
            )
        )
        row = self.connection.execute(
            "SELECT id FROM wf_event WHERE source = 'email' AND external_id = ?",
            (external_id,),
        ).fetchone()
        if row is None:
            raise AssertionError(f"email dispatch did not ledger {external_id}")
        return int(row["id"])

    @staticmethod
    def _parse_scalar(value: str):
        normalized = value.strip()
        if normalized.lower() == "true":
            return True
        if normalized.lower() == "false":
            return False
        return normalized

    def _extractor(self, _brief: dict, event: dict) -> dict:
        if "candidates" in event:
            raise AssertionError("candidate instances crossed the extraction boundary")
        payload = event.get("payload") or {}
        body_ref = payload.get("body_ref")
        if not isinstance(body_ref, str):
            raise ValueError("email event has no body reference")
        values: dict[str, object] = {}
        for raw_line in Path(body_ref).read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if "=" not in line:
                raise ValueError("synthetic event body must use key=value lines")
            key, raw_value = line.split("=", 1)
            normalized_key = key.strip().lower()
            if normalized_key == "type":
                values["type"] = raw_value.strip()
            elif normalized_key in VALUE_FIELDS:
                values[normalized_key] = self._parse_scalar(raw_value)
            else:
                raise ValueError(f"unknown synthetic event field: {key.strip()}")
        event_type = values.pop("type", None)
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("synthetic event body needs TYPE")
        corr = {
            field: values[field]
            for field in CORRELATION_FIELDS
            if field in values
        }
        return {
            "event_type": event_type,
            "payload": values,
            "corr": corr,
        }

    def _extract_email(self, event_id: int):
        return wf_engine.extract_event(
            self.connection,
            event_id,
            self.extraction_brief,
            self._extractor,
            {"synthetic_event_v1": lambda payload: isinstance(payload, dict)},
        )

    def _mail(self, body: str, *, label: str, message_id: str | None = None):
        event_id = self._dispatch_email(body, label=label, message_id=message_id)
        return event_id, self._extract_email(event_id)

    def _direct_event(
        self,
        *,
        event_type: str,
        corr: dict,
        payload: dict | None = None,
        source: str = "synthetic",
        label: str = "event",
    ) -> tuple[int, wf_engine.MatchResult]:
        event_id = wf_engine.ingest_event(
            self.connection,
            source=source,
            external_id=self._next(label),
            payload=payload or {},
            corr=corr,
            event_type=event_type,
        )
        if event_id is None:
            raise AssertionError("synthetic event unexpectedly deduplicated")
        return event_id, wf_engine.match_event(self.connection, event_id)

    def _new_instance(
        self,
        *,
        entity: str,
        booking_ref: str,
        direction: str | None = None,
        customer: str = "synthetic-customer",
        vessel: str = "synthetic-vessel",
        vars_extra: dict | None = None,
    ) -> str:
        values = {"customer": customer, "vessel": vessel}
        if direction is not None:
            values["direction"] = direction
        if vars_extra:
            values.update(vars_extra)
        return wf_engine.create_instance(
            self.connection,
            template_id=self.template_id,
            entity_key=entity,
            corr={"booking_ref": booking_ref},
            vars=values,
            source_event_id=None,
        )

    def _enter(self, task_id: str, step_key: str, *, label: str) -> int:
        setup_id = wf_engine.ingest_event(
            self.connection,
            source="synthetic_setup",
            external_id=self._next(label),
            payload={},
            corr={},
            event_type="stage_setup",
        )
        if setup_id is None:
            raise AssertionError("stage setup unexpectedly deduplicated")
        wf_engine.advance(
            self.connection,
            task_id,
            to_step=step_key,
            event_id=setup_id,
        )
        return setup_id

    def _apply(self, event_id: int, task_id: str, step_key: str):
        return wf_engine.apply_event(
            self.connection,
            event_id,
            task_id,
            expected_step=step_key,
        )

    def _task_cite(self, task_id: str, identity: str | None = None) -> dict:
        return _row_citation(
            self.connection,
            table="tasks",
            query="SELECT id, title, status, current_step_key, completed_at FROM tasks WHERE id = ?",
            params=(task_id,),
            identity=identity or f"tasks.id={task_id}",
            fields=("id", "title", "status", "current_step_key", "completed_at"),
        )

    def _instance_cite(self, task_id: str, identity: str | None = None) -> dict:
        return _row_citation(
            self.connection,
            table="wf_instance",
            query="SELECT task_id, entity_key, state, corr, vars, parked_since FROM wf_instance WHERE task_id = ?",
            params=(task_id,),
            identity=identity or f"wf_instance.task_id={task_id}",
            fields=("task_id", "entity_key", "state", "corr", "vars", "parked_since"),
        )

    def _wait_cite(self, wait_id: int, identity: str | None = None) -> dict:
        return _row_citation(
            self.connection,
            table="wf_wait",
            query="SELECT id, task_id, step_key, kind, timer_action, fires_used, status, timer_at FROM wf_wait WHERE id = ?",
            params=(wait_id,),
            identity=identity or f"wf_wait.id={wait_id}",
            fields=("id", "task_id", "step_key", "kind", "timer_action", "fires_used", "status", "timer_at"),
        )

    def _event_cite(self, event_id: int, identity: str | None = None) -> dict:
        extra = self.body_refs.get(event_id)
        return _row_citation(
            self.connection,
            table="wf_event",
            query="SELECT id, source, external_id, event_type, status, matched_task_id, match_method FROM wf_event WHERE id = ?",
            params=(event_id,),
            identity=identity or f"wf_event.id={event_id}",
            fields=("id", "source", "external_id", "event_type", "status", "matched_task_id", "match_method"),
            extra={"body_ref": extra} if extra else None,
        )

    def _transition_cite(self, task_id: str, event_id: int) -> dict:
        return _row_citation(
            self.connection,
            table="wf_transition",
            query="SELECT task_id, step_key, to_step, event_id, applied_at FROM wf_transition WHERE task_id = ? AND event_id = ?",
            params=(task_id, event_id),
            identity=f"wf_transition.task_id={task_id},event_id={event_id}",
            fields=("task_id", "step_key", "to_step", "event_id", "applied_at"),
        )

    def _suppress_timers(self, except_wait_id: int | None = None) -> None:
        if except_wait_id is None:
            self.connection.execute(
                "UPDATE wf_wait SET timer_at = ? WHERE kind = 'timer' AND status = 'armed'",
                (FIXED_NOW + 10**9,),
            )
            return
        self.connection.execute(
            "UPDATE wf_wait SET timer_at = ? WHERE kind = 'timer' AND status = 'armed' AND id != ?",
            (FIXED_NOW + 10**9, except_wait_id),
        )

    def case_shared_ref_two_live(self) -> dict:
        first = self._new_instance(entity="shared-first", booking_ref="BOOK-SHARED", direction="export")
        second = self._new_instance(entity="shared-second", booking_ref="BOOK-SHARED", direction="import")
        self._enter(first, "await_pickup", label="shared-first-pickup")
        self._enter(second, "await_vgm", label="shared-second-vgm")

        first_event, first_match = self._direct_event(
            event_type="pickup_advice",
            corr={"booking_ref": "BOOK-SHARED"},
            payload={"booking_ref": "BOOK-SHARED"},
            label="shared-first-event",
        )
        first_apply = self._apply(first_event, first, "await_pickup")
        self._enter(first, "await_vgm", label="shared-first-vgm")
        second_event, second_match = self._direct_event(
            event_type="vgm_reply",
            corr={"booking_ref": "BOOK-SHARED"},
            payload={"booking_ref": "BOOK-SHARED", "direction": "export"},
            label="shared-second-event",
        )
        second_apply = self._apply(second_event, first, "await_vgm")
        return {
            "ids": {"first_task": first, "second_task": second, "first_event": first_event, "second_event": second_event},
            "results": {
                "first_match": first_match.kind,
                "first_apply": first_apply.kind,
                "second_match": second_match.kind,
                "second_apply": second_apply.kind,
            },
            "citations": [
                self._task_cite(first, "candidate task shared-first"),
                self._task_cite(second, "candidate task shared-second"),
                self._wait_cite(
                    self.connection.execute(
                        "SELECT id FROM wf_wait WHERE task_id = ? AND step_key = 'await_vgm' ORDER BY id DESC LIMIT 1",
                        (first,),
                    ).fetchone()["id"],
                    "chosen task current await_vgm wait",
                ),
                self._wait_cite(
                    self.connection.execute(
                        "SELECT id FROM wf_wait WHERE task_id = ? AND step_key = 'await_vgm' ORDER BY id DESC LIMIT 1",
                        (second,),
                    ).fetchone()["id"],
                    "other candidate current await_vgm wait",
                ),
                self._event_cite(first_event, "first event current compatibility match"),
                self._transition_cite(first, first_event),
                self._event_cite(second_event, "second event direction discriminator match"),
                self._transition_cite(first, second_event),
            ],
        }

    def case_zero_match_hold_heal(self) -> dict:
        event_id, initial = self._mail(
            "TYPE=pickup_advice\nBOOKING_REF=BOOK-ZERO\nRESULT=ready\n",
            label="zero-match",
        )
        unmatched_checkpoint = self._event_cite(event_id, "unmatched before instance")
        task_id = self._new_instance(entity="zero-instance", booking_ref="BOOK-ZERO", direction="export")
        healed_checkpoint = self._event_cite(event_id, "buffered after instance creation")
        checkpoints = [{"label": "unmatched before instance", "status": unmatched_checkpoint["observed"]["status"]}, {"label": "buffered after instance creation", "status": healed_checkpoint["observed"]["status"]}]
        self._enter(task_id, "await_pickup", label="zero-await-pickup")
        checkpoints.append({"label": "matched after wait armed", "status": self.connection.execute("SELECT status FROM wf_event WHERE id = ?", (event_id,)).fetchone()[0]})
        applied = self._apply(event_id, task_id, "await_pickup")
        checkpoints.append({"label": "applied", "status": self.connection.execute("SELECT status FROM wf_event WHERE id = ?", (event_id,)).fetchone()[0]})
        transition = self._transition_cite(task_id, event_id)
        return {
            "ids": {"event": event_id, "task": task_id, "transition_event": transition["observed"]["event_id"]},
            "results": {"initial_match": initial.kind, "apply": applied.kind, "checkpoints": checkpoints},
            "citations": [unmatched_checkpoint, healed_checkpoint, self._event_cite(event_id, "email event across matched-applied"), self._task_cite(task_id), self._instance_cite(task_id), transition],
        }

    def case_two_match_human_pick(self) -> dict:
        first = self._new_instance(entity="human-first", booking_ref="BOOK-HUMAN", direction="export")
        second = self._new_instance(entity="human-second", booking_ref="BOOK-HUMAN", direction="import")
        self._enter(first, "await_pickup", label="human-first-pickup")
        self._enter(second, "await_pickup", label="human-second-pickup")
        event_id, ambiguous = self._mail(
            "TYPE=pickup_advice\nBOOKING_REF=BOOK-HUMAN\nRESULT=ready\n",
            label="two-match",
        )
        resolved = wf_engine.resolve_event(
            self.connection,
            event_id,
            second,
            decided_by="p5a-reviewer",
        )
        resolution = self.connection.execute(
            "SELECT id FROM wf_event WHERE source = 'human_resolution' AND external_id = ?",
            (f"event:{event_id}:{second}",),
        ).fetchone()
        if resolution is None:
            raise AssertionError("human resolution ledger row missing")
        return {
            "ids": {"first_task": first, "second_task": second, "event": event_id, "human_resolution": int(resolution["id"])},
            "results": {"ambiguous": ambiguous.kind, "candidate_task_ids": list(ambiguous.candidate_task_ids), "resolved": resolved.kind, "match_method": resolved.match_method},
            "citations": [self._task_cite(first, "retained candidate first"), self._task_cite(second, "human-selected candidate second"), self._wait_cite(self.connection.execute("SELECT id FROM wf_wait WHERE task_id = ? AND step_key = 'await_pickup'", (first,)).fetchone()["id"]), self._wait_cite(self.connection.execute("SELECT id FROM wf_wait WHERE task_id = ? AND step_key = 'await_pickup'", (second,)).fetchone()["id"]), self._event_cite(event_id, "ambiguous event resolved by human"), _row_citation(self.connection, table="wf_event", query="SELECT id, source, external_id, event_type, status, corr FROM wf_event WHERE id = ?", params=(int(resolution["id"]),), identity=f"wf_event.id={int(resolution['id'])} human_resolution", fields=("id", "source", "external_id", "event_type", "status", "corr"))],
        }

    def case_out_of_order_buffer(self) -> dict:
        task_id = self._new_instance(entity="out-of-order", booking_ref="BOOK-ORDER", direction="export")
        self._enter(task_id, "await_vgm", label="order-await-vgm")
        gate_event, buffered = self._mail(
            "TYPE=gate_in\nBOOKING_REF=BOOK-ORDER\nVALUE=yard-7\n",
            label="out-of-order-gate",
        )
        before = self.connection.execute("SELECT status FROM wf_event WHERE id = ?", (gate_event,)).fetchone()[0]
        self._enter(task_id, "await_gatein", label="order-await-gatein")
        after_enter = self.connection.execute("SELECT status FROM wf_event WHERE id = ?", (gate_event,)).fetchone()[0]
        applied = self._apply(gate_event, task_id, "await_gatein")
        past_event, past = self._mail(
            "TYPE=vgm_reply\nBOOKING_REF=BOOK-ORDER\nRESULT=duplicate\n",
            label="out-of-order-past",
        )
        return {
            "ids": {"task": task_id, "buffered_event": gate_event, "past_event": past_event},
            "results": {"buffered": buffered.kind, "before_enter": before, "after_enter": after_enter, "apply": applied.kind, "past": past.kind},
            "citations": [self._task_cite(task_id), self._event_cite(gate_event, "gate_in buffered then consumed"), self._transition_cite(task_id, gate_event), self._event_cite(past_event, "past vgm duplicate superseded"), _count_citation(self.connection, table="wf_transition", query="SELECT COUNT(*) AS count FROM wf_transition WHERE task_id = ?", params=(task_id,), identity=f"transitions for task {task_id}", extra={"expected_after_gate_in": True})],
        }

    def case_duplicates_three_layers(self) -> dict:
        task_id = self._new_instance(entity="duplicate-layers", booking_ref="BOOK-DUP", direction="export")
        self._enter(task_id, "await_pickup", label="duplicate-await-pickup")
        message_id = "<p5a-layer-one@synthetic.test>"
        body = "TYPE=pickup_advice\nBOOKING_REF=BOOK-DUP\nRESULT=ready\n"
        first_id = self._dispatch_email(body, label="layer-one", message_id=message_id)
        duplicate_id = self._dispatch_email(body, label="layer-one-replay", message_id=message_id)
        duplicate_ingest_return = self.email_ingest_results[-1]
        layer_one_count = self.connection.execute("SELECT COUNT(*) FROM wf_event WHERE source = 'email' AND external_id = ?", (message_id,)).fetchone()[0]
        self._extract_email(first_id)
        first_apply = self._apply(first_id, task_id, "await_pickup")
        forwarded_id, forwarded = self._mail(body, label="layer-two-forwarded")

        replay_task = self._new_instance(entity="duplicate-transition", booking_ref="BOOK-DUP-TRANS", direction="export")
        self._enter(replay_task, "await_pickup", label="duplicate-transition-await")
        transition_event, matched = self._direct_event(event_type="pickup_advice", corr={"booking_ref": "BOOK-DUP-TRANS"}, payload={"booking_ref": "BOOK-DUP-TRANS"}, label="duplicate-transition-event")
        first_transition = self._apply(transition_event, replay_task, "await_pickup")
        second_transition = self._apply(transition_event, replay_task, "await_pickup")
        transition_count = self.connection.execute("SELECT COUNT(*) FROM wf_transition WHERE task_id = ? AND event_id = ?", (replay_task, transition_event)).fetchone()[0]
        return {
            "ids": {"task": task_id, "first_event": first_id, "duplicate_event": duplicate_id, "forwarded_event": forwarded_id, "replay_task": replay_task, "transition_event": transition_event},
            "results": {"same_source_external_second_ingest_returned_none": duplicate_ingest_return is None, "first_apply": first_apply.kind, "forwarded": forwarded.kind, "first_transition": first_transition.kind, "second_transition": second_transition.kind, "transition_count": transition_count},
            "citations": [_count_citation(self.connection, table="wf_event", query="SELECT COUNT(*) AS count FROM wf_event WHERE source = 'email' AND external_id = ?", params=(message_id,), identity=f"email source/external_id {message_id}", extra={"second_ingest_return": duplicate_ingest_return}), self._event_cite(first_id, "layer-one email event"), self._event_cite(forwarded_id, "forwarded copy no-op"), _count_citation(self.connection, table="wf_transition", query="SELECT COUNT(*) AS count FROM wf_transition WHERE task_id = ? AND event_id = ?", params=(replay_task, transition_event), identity=f"one transition primary key for task {replay_task},event {transition_event}", extra={"second_apply_result": second_transition.kind}), self._event_cite(transition_event, "same event applied twice"), self._task_cite(replay_task)],
        }

    def _complete_instance(self, *, entity: str, booking_ref: str) -> str:
        task_id = self._new_instance(entity=entity, booking_ref=booking_ref, direction="export")
        self._enter(task_id, "await_pickup", label=f"{entity}-pickup")
        self._enter(task_id, "await_pickup", label=f"{entity}-pickup")
        event_id, result = self._direct_event(event_type="pickup_advice", corr={"booking_ref": booking_ref}, payload={"booking_ref": booking_ref}, label=f"{entity}-pickup-event")
        if result.kind != "matched":
            raise AssertionError(result)
        self._apply(event_id, task_id, "await_pickup")
        self._enter(task_id, "await_container", label=f"{entity}-container")
        self._enter(task_id, "notify_customer", label=f"{entity}-notify")
        self._enter(task_id, "await_vgm", label=f"{entity}-vgm")
        vgm_id, vgm_result = self._direct_event(event_type="vgm_reply", corr={"booking_ref": booking_ref}, payload={"booking_ref": booking_ref}, label=f"{entity}-vgm-event")
        if vgm_result.kind != "matched":
            raise AssertionError(vgm_result)
        self._apply(vgm_id, task_id, "await_vgm")
        self._enter(task_id, "await_gatein", label=f"{entity}-gatein")
        gate_id, gate_result = self._direct_event(event_type="gate_in", corr={"booking_ref": booking_ref}, payload={"booking_ref": booking_ref}, label=f"{entity}-gate-event")
        if gate_result.kind != "matched":
            raise AssertionError(gate_result)
        self._apply(gate_id, task_id, "await_gatein")
        self._enter(task_id, "done", label=f"{entity}-done")
        return task_id

    def case_done_instance_retention(self) -> dict:
        task_id = self._complete_instance(entity="done-retention", booking_ref="BOOK-DONE")
        late_id, late = self._mail("TYPE=pickup_advice\nBOOKING_REF=BOOK-DONE\nRESULT=late\n", label="done-late-normal")
        mutation_id, mutation = self._mail("TYPE=pickup_advice\nBOOKING_REF=BOOK-DONE\nOPERATION=update\n", label="done-late-mutation")
        return {
            "ids": {"task": task_id, "late_event": late_id, "mutation_event": mutation_id},
            "results": {"late_normal": late.kind, "late_mutation": mutation.kind},
            "citations": [self._task_cite(task_id, "completed task retained for late correlation"), self._instance_cite(task_id, "done workflow instance"), self._event_cite(late_id, "late non-mutation email softly superseded"), self._event_cite(mutation_id, "late mutation email needs review")],
        }

    def case_chase_caps(self) -> dict:
        self._suppress_timers()
        task_id = self._new_instance(entity="chase-cap", booking_ref="BOOK-CHASE", direction="export")
        self._enter(task_id, "await_pickup", label="chase-cap-await")
        wait = self.connection.execute("SELECT id FROM wf_wait WHERE task_id = ? AND step_key = 'await_pickup' AND kind = 'timer'", (task_id,)).fetchone()
        if wait is None:
            raise AssertionError("chase wait missing")
        wait_id = int(wait["id"])
        self.connection.execute("UPDATE wf_wait SET timer_at = ? WHERE id = ?", (FIXED_NOW - 1, wait_id))
        ticks = [wf_watcher.run_tick(self.connection, FIXED_NOW)]
        ticks.append(wf_watcher.run_tick(self.connection, FIXED_NOW + TIMER_CADENCE))
        ticks.append(wf_watcher.run_tick(self.connection, FIXED_NOW + (2 * TIMER_CADENCE)))
        fourth = wf_watcher.run_tick(self.connection, FIXED_NOW + (3 * TIMER_CADENCE))
        timer_events = self.connection.execute("SELECT id FROM wf_event WHERE source = 'timer' AND matched_task_id = ? ORDER BY id", (task_id,)).fetchall()
        return {
            "ids": {"task": task_id, "wait": wait_id, "timer_events": [int(row["id"]) for row in timer_events]},
            "results": {"tick_timer_results": [list(tick.timer_results) for tick in ticks], "fourth_tick_timer_results": list(fourth.timer_results), "outbox_count": self.connection.execute("SELECT COUNT(*) FROM wf_outbox WHERE task_id = ? AND action = 'chase'", (task_id,)).fetchone()[0]},
            "citations": [self._wait_cite(wait_id, "chase wait reached max_fires=3"), *[self._event_cite(int(row["id"]), f"chase timer event {index}") for index, row in enumerate(timer_events, 1)], _count_citation(self.connection, table="wf_outbox", query="SELECT COUNT(*) AS count FROM wf_outbox WHERE task_id = ? AND action = 'chase'", params=(task_id,), identity=f"chase outbox rows for {task_id}", extra={"fourth_tick_created_no_event": len(fourth.timers_fired) == 0}), self._instance_cite(task_id, "resumable exception after chase cap"), self._task_cite(task_id)],
        }

    def case_timer_races(self) -> dict:
        first = self._new_instance(entity="timer-event-wins", booking_ref="BOOK-RACE-EVENT", direction="export")
        self._enter(first, "await_pickup", label="race-event-await")
        first_wait = self.connection.execute("SELECT id FROM wf_wait WHERE task_id = ? AND kind = 'timer'", (first,)).fetchone()["id"]
        self._suppress_timers(except_wait_id=int(first_wait))
        event_id, event_result = self._direct_event(event_type="pickup_advice", corr={"booking_ref": "BOOK-RACE-EVENT"}, payload={"booking_ref": "BOOK-RACE-EVENT"}, label="race-event-first")
        self._apply(event_id, first, "await_pickup")
        first_tick = wf_watcher.run_tick(self.connection, FIXED_NOW)
        first_wait_status = self.connection.execute("SELECT status FROM wf_wait WHERE id = ?", (first_wait,)).fetchone()[0]
        first_timer_count = self.connection.execute("SELECT COUNT(*) FROM wf_event WHERE source = 'timer' AND json_extract(payload, '$.wait_id') = ?", (first_wait,)).fetchone()[0]

        second = self._new_instance(entity="timer-wins", booking_ref="BOOK-RACE-TIMER", direction="export")
        self._enter(second, "await_vgm", label="race-timer-await")
        second_wait = self.connection.execute("SELECT id FROM wf_wait WHERE task_id = ? AND kind = 'timer'", (second,)).fetchone()["id"]
        self._suppress_timers(except_wait_id=int(second_wait))
        self.connection.execute("UPDATE wf_wait SET timer_at = ? WHERE id = ?", (FIXED_NOW - 1, second_wait))
        timer_tick = wf_watcher.run_tick(self.connection, FIXED_NOW)
        timer_event = self.connection.execute("SELECT id FROM wf_event WHERE source = 'timer' AND json_extract(payload, '$.wait_id') = ?", (second_wait,)).fetchone()["id"]
        self._enter(second, "await_gatein", label="race-timer-advance")
        transition_before = self.connection.execute("SELECT COUNT(*) FROM wf_transition WHERE task_id = ?", (second,)).fetchone()[0]
        late_id, late = self._direct_event(event_type="vgm_reply", corr={"booking_ref": "BOOK-RACE-TIMER"}, payload={"booking_ref": "BOOK-RACE-TIMER"}, label="race-late-event")
        transition_after = self.connection.execute("SELECT COUNT(*) FROM wf_transition WHERE task_id = ?", (second,)).fetchone()[0]
        return {
            "ids": {"event_wins_task": first, "event_wins_event": event_id, "event_wins_wait": int(first_wait), "timer_wins_task": second, "timer_event": int(timer_event), "late_event": late_id, "timer_wins_wait": int(second_wait)},
            "results": {"event_wins_match": event_result.kind, "event_wins_wait_status": first_wait_status, "event_wins_timer_count": first_timer_count, "event_wins_tick_timer_results": list(first_tick.timer_results), "timer_wins_tick_timer_results": list(timer_tick.timer_results), "timer_wins_late": late.kind, "transition_count_before_late": transition_before, "transition_count_after_late": transition_after},
            "citations": [self._event_cite(event_id, "event won before due timer"), self._wait_cite(int(first_wait), "event-winning timer superseded"), _count_citation(self.connection, table="wf_event", query="SELECT COUNT(*) AS count FROM wf_event WHERE source = 'timer' AND json_extract(payload, '$.wait_id') = ?", params=(first_wait,), identity=f"no timer event for event-winning wait {first_wait}"), self._event_cite(int(timer_event), "timer event won first"), self._wait_cite(int(second_wait), "timer-winning wait invalidated on later advance"), self._transition_cite(second, int(timer_event)) if self.connection.execute("SELECT 1 FROM wf_transition WHERE task_id = ? AND event_id = ?", (second, int(timer_event))).fetchone() else self._transition_cite(second, self.connection.execute("SELECT event_id FROM wf_transition WHERE task_id = ? ORDER BY applied_at DESC, rowid DESC LIMIT 1", (second,)).fetchone()[0]), self._event_cite(late_id, "later vgm event is past and superseded"), _count_citation(self.connection, table="wf_transition", query="SELECT COUNT(*) AS count FROM wf_transition WHERE task_id = ?", params=(second,), identity=f"no second transition after late event for {second}", extra={"before_late": transition_before, "after_late": transition_after})],
        }

    def case_propose_approve_edit_diff(self) -> dict:
        plain_task = self._new_instance(entity="approval-plain", booking_ref="BOOK-APPROVE-PLAIN", direction="export")
        plain_id = wf_engine.propose(self.connection, plain_task, "send_email", {"to": "recipient@example.test", "body": "original"})
        plain_token = self.connection.execute("SELECT resume_token FROM wf_approval WHERE id = ?", (plain_id,)).fetchone()[0]
        plain_event = wf_engine.decide_approval(self.connection, plain_id, plain_token, "approved", decided_by="p5a-reviewer")
        plain_outbox_count = self.connection.execute("SELECT COUNT(*) FROM wf_outbox WHERE task_id = ?", (plain_task,)).fetchone()[0]

        edited_task = self._new_instance(entity="approval-edited", booking_ref="BOOK-APPROVE-EDIT", direction="export")
        edited_id = wf_engine.propose(self.connection, edited_task, "send_email", {"to": "recipient@example.test", "body": "original"})
        edited_token = self.connection.execute("SELECT resume_token FROM wf_approval WHERE id = ?", (edited_id,)).fetchone()[0]
        edited_payload = {"to": "recipient@example.test", "body": "edited", "field": "added"}
        edited_event = wf_engine.decide_approval(self.connection, edited_id, edited_token, "edited_approved", decided_by="p5a-reviewer", payload=edited_payload)
        replay = wf_engine.decide_approval(self.connection, edited_id, edited_token, "edited_approved", decided_by="p5a-reviewer", payload=edited_payload)
        edited_approval = self.connection.execute("SELECT resume_token FROM wf_approval WHERE id = ?", (edited_id,)).fetchone()[0]
        return {
            "ids": {"plain_task": plain_task, "plain_approval": plain_id, "plain_event": plain_event, "edited_task": edited_task, "edited_approval": edited_id, "edited_event": edited_event},
            "results": {"plain_outbox_count": plain_outbox_count, "plain_status": self.connection.execute("SELECT status FROM wf_approval WHERE id = ?", (plain_id,)).fetchone()[0], "edited_status": self.connection.execute("SELECT status FROM wf_approval WHERE id = ?", (edited_id,)).fetchone()[0], "replay_result": replay, "token_rotated": edited_approval != edited_token, "edited_outbox_count": self.connection.execute("SELECT COUNT(*) FROM wf_outbox WHERE task_id = ?", (edited_task,)).fetchone()[0]},
            "citations": [_row_citation(self.connection, table="wf_approval", query="SELECT id, task_id, action, status, decided_by, decision_diff, resume_token FROM wf_approval WHERE id = ?", params=(plain_id,), identity=f"wf_approval.id={plain_id}", fields=("id", "task_id", "action", "status", "decided_by", "decision_diff"), extra={"outbox_count": plain_outbox_count}), _row_citation(self.connection, table="wf_outbox", query="SELECT id, task_id, action, payload, status FROM wf_outbox WHERE task_id = ?", params=(plain_task,), identity=f"wf_outbox task_id={plain_task}", fields=("id", "task_id", "action", "payload", "status")), _row_citation(self.connection, table="wf_approval", query="SELECT id, task_id, action, status, decided_by, decision_diff, resume_token FROM wf_approval WHERE id = ?", params=(edited_id,), identity=f"wf_approval.id={edited_id}", fields=("id", "task_id", "action", "status", "decided_by", "decision_diff"), extra={"token_rotated": edited_approval != edited_token, "replay_result": replay}), _row_citation(self.connection, table="wf_outbox", query="SELECT id, task_id, action, payload, status FROM wf_outbox WHERE task_id = ?", params=(edited_task,), identity=f"wf_outbox task_id={edited_task}", fields=("id", "task_id", "action", "payload", "status"))],
        }

    def case_manual_observation_advance(self) -> dict:
        tenant = "synthetic-dress"
        first = self._new_instance(entity="manual-observation-first", booking_ref="BOOK-POLL", direction="export")
        second = self._new_instance(entity="manual-observation-second", booking_ref="BOOK-POLL-SECOND", direction="export")
        self._enter(first, "await_container", label="poll-first-container")
        self._enter(second, "await_container", label="poll-second-container")
        self.connection.execute("UPDATE tasks SET tenant = ? WHERE id IN (?, ?)", (tenant, first, second))
        calls = {"count": 0}

        def probe(targets):
            calls["count"] += 1
            if calls["count"] == 1:
                target = next(item for item in targets if item.task_id == first)
                return [wf_watcher.ProbeObservation(external_id=f"probe:{first}:container", event_type="container_assigned", corr={"booking_ref": "BOOK-POLL"}, payload={"container_no": "CONT-POLL"})]
            return [wf_watcher.ProbeObservation(external_id=f"probe:{first}:container", event_type="container_assigned", corr={"booking_ref": "BOOK-POLL"}, payload={"container_no": "CONT-POLL"})]

        wf_watcher.register_state_probe(tenant, probe, read_only=True)
        try:
            first_tick = wf_watcher.run_tick(self.connection, FIXED_NOW)
            observed_event = self.connection.execute("SELECT id FROM wf_event WHERE source = 'state_poll' AND external_id = ?", (f"probe:{first}:container",)).fetchone()["id"]
            second_tick = wf_watcher.run_tick(self.connection, FIXED_NOW)
        finally:
            wf_watcher.unregister_state_probe(tenant)
        return {
            "ids": {"task": first, "held_probe_target": second, "observation_event": int(observed_event)},
            "results": {"first_tick_poll_events": list(first_tick.poll_events), "first_tick_applied": list(first_tick.applied_events), "second_tick_poll_duplicates": second_tick.poll_duplicates, "first_resulting_step": self.connection.execute("SELECT current_step_key FROM tasks WHERE id = ?", (first,)).fetchone()[0], "second_target_still_waiting": self.connection.execute("SELECT current_step_key FROM tasks WHERE id = ?", (second,)).fetchone()[0]},
            "citations": [_row_citation(self.connection, table="wf_event", query="SELECT id, source, external_id, event_type, status, matched_task_id, match_method FROM wf_event WHERE id = ?", params=(int(observed_event),), identity=f"wf_event.id={int(observed_event)} state observation", fields=("id", "source", "external_id", "event_type", "status", "matched_task_id", "match_method"), extra={"probe_read_only": True, "second_observation_duplicate": second_tick.poll_duplicates}), self._transition_cite(first, int(observed_event)), self._task_cite(first, "container observation advanced task"), _count_citation(self.connection, table="wf_event", query="SELECT COUNT(*) AS count FROM wf_event WHERE source = 'state_poll' AND external_id = ?", params=(f"probe:{first}:container",), identity=f"poll duplicate ledger for probe:{first}:container", extra={"poll_duplicates": second_tick.poll_duplicates})],
        }

    def run(self) -> dict:
        self.home.mkdir(parents=True, exist_ok=False)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        os.environ["HERMES_HOME"] = str(self.home)
        os.environ["HERMES_KANBAN_DB"] = str(self.db_path)
        os.environ["EMAIL_ADDRESS"] = "hermes@example.test"
        os.environ["EMAIL_PASSWORD"] = "synthetic-only"
        os.environ["EMAIL_IMAP_HOST"] = "imap.invalid"
        os.environ["EMAIL_SMTP_HOST"] = "smtp.invalid"
        os.environ["EMAIL_ALLOWED_USERS"] = "operator@example.test"
        self.conn = kanban_db.connect(self.db_path)
        workflow = _read_template()
        self.template_id, _version = wf_engine.register_template(self.connection, workflow)

        def callback(envelope: dict) -> None:
            self._record_email(envelope)

        self._adapter = EmailAdapter(
            PlatformConfig(enabled=True),
            workflow_ingress_callback=callback,
        )
        self._adapter.handle_message = AsyncMock()
        try:
            with patch("gateway.platforms.email.get_hermes_home", return_value=self.home):
                cases = {
                    "shared_ref_two_live": self.case_shared_ref_two_live(),
                    "zero_match_hold_heal": self.case_zero_match_hold_heal(),
                    "two_match_human_pick": self.case_two_match_human_pick(),
                    "out_of_order_buffer": self.case_out_of_order_buffer(),
                    "duplicates_three_layers": self.case_duplicates_three_layers(),
                    "done_instance_retention": self.case_done_instance_retention(),
                    "chase_caps": self.case_chase_caps(),
                    "timer_races": self.case_timer_races(),
                    "propose_approve_edit_diff": self.case_propose_approve_edit_diff(),
                    "manual_observation_advance": self.case_manual_observation_advance(),
                }
                if tuple(cases) != CASE_KEYS:
                    raise AssertionError("P5a case key order/shape changed")
                return {"cases": cases}
        finally:
            self.connection.close()


def run_dress_rehearsal(*, db_path: Path, home: Path, evidence_path: Path | None = None) -> dict:
    """Run once, refusing contaminated input paths, and optionally persist evidence."""

    db_path = Path(db_path).expanduser()
    home = Path(home).expanduser()
    if home.exists() and not home.is_dir():
        raise ValueError(f"home path is not a directory: {home}")
    if _non_empty(home):
        raise ValueError(f"refusing non-empty home: {home}")
    if db_path.exists() and db_path.is_dir():
        raise ValueError(f"db path is a directory: {db_path}")
    if _non_empty(db_path):
        raise ValueError(f"refusing non-empty db: {db_path}")
    if evidence_path is not None and _non_empty(Path(evidence_path)):
        raise ValueError(f"refusing non-empty evidence path: {evidence_path}")

    old_env = {key: os.environ.get(key) for key in ("HERMES_HOME", "HERMES_KANBAN_DB", "EMAIL_ADDRESS", "EMAIL_PASSWORD", "EMAIL_IMAP_HOST", "EMAIL_SMTP_HOST", "EMAIL_ALLOWED_USERS")}
    try:
        with _fixed_clock():
            evidence = DressRehearsal(db_path=db_path, home=home).run()
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    rendered = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    if evidence_path is not None:
        evidence_path = Path(evidence_path).expanduser()
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(rendered, encoding="utf-8")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the synthetic workflow dress rehearsal.")
    parser.add_argument("--db-path", required=True, type=Path)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--evidence-path", required=True, type=Path)
    args = parser.parse_args()
    evidence = run_dress_rehearsal(
        db_path=args.db_path,
        home=args.home,
        evidence_path=args.evidence_path,
    )
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
