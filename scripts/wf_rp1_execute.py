#!/usr/bin/env python3
"""Safely execute a locked RP1 plan against PA workflow staging.

SMTP is the only email ingress. A separately deployed staging command owns
seed, observation, and state-poll operations. Dry-run validates all locked
inputs without constructing either mutation adapter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import smtplib
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path
from typing import Any, Callable, IO, Mapping, Protocol

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml

from hermes_cli import kanban_db, wf_engine, wf_watcher
from scripts import wf_rp1_campaign as campaign_lib


TARGET_SYSTEM = "pa-workflow-dev"
TARGET_ENVIRONMENT = "staging"
RECIPIENT_ENV = "WF_RP1_RECIPIENT"
STAGING_DB_ENV = "WF_RP1_STAGING_DB"
SERVICE_PROOF_ENV = "WF_RP1_SERVICE_PROOF_PATH"
SERVICE_NAME = "pa-workflow-dev-hermes.service"
RP1_BOARD = "workflow-rp1"
REMOTE_SSH_TARGET = "pa-staging@100.87.146.11"
REMOTE_EXECUTOR_PATH = (
    "/home/pa-staging/apps/hermes-pcl/current/scripts/wf_rp1_execute.py"
)
REMOTE_PYTHON_PATH = (
    "/home/pa-staging/apps/hermes-pcl/current/.venv/bin/python"
)
REMOTE_STAGING_DB_PATH = (
    "/home/pa-staging/.hermes-p0/kanban/boards/workflow-rp1/kanban.db"
)
REMOTE_SERVICE_PROOF_PATH = (
    "/home/pa-staging/.hermes-p0/kanban/wf-rp1-service-proof.json"
)
_REQUIRED_DURABLE = ("event", "instance", "proposal")
_SCORABLE_KEYS = ("event_type", "payload", "corr", "correlation", "agent_action")


class ExecutionContractError(RuntimeError):
    """The plan, target, adapter, or evidence is unsafe or incomplete."""


class RemoteStaging(Protocol):
    def request(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ExecutionSettings:
    target: str
    environment: str
    recipient: str
    smtp_user: str
    smtp_password: str
    poll_interval_seconds: float
    ingress_timeout_seconds: float
    worker_timeout_seconds: float
    pacing_seconds: float

    @classmethod
    def from_environment(
        cls, plan: Mapping[str, Any], env: Mapping[str, str]
    ) -> "ExecutionSettings":
        orchestration = _mapping(plan.get("orchestration"), "orchestration")
        target = env.get("WF_RP1_TARGET", "")
        environment = env.get("WF_RP1_ENVIRONMENT", "")
        if target != TARGET_SYSTEM or environment != TARGET_ENVIRONMENT:
            raise ExecutionContractError(
                "execution target must be pa-workflow-dev staging"
            )
        user_env = _text(orchestration.get("smtp_user_env"), "smtp_user_env")
        password_env = _text(
            orchestration.get("smtp_password_env"), "smtp_password_env"
        )
        smtp_user = env.get(user_env, "")
        smtp_password = env.get(password_env, "")
        recipient = env.get(RECIPIENT_ENV, "")
        if not smtp_user or not smtp_password or not recipient:
            raise ExecutionContractError(
                f"SMTP credentials and recipient must come from {user_env}, "
                f"{password_env}, and {RECIPIENT_ENV}"
            )
        _mailbox(smtp_user, "SMTP user")
        _mailbox(recipient, "recipient")
        recipient_local = recipient.rsplit("@", 1)[0].lower()
        planned_recipient = _text(
            orchestration.get("recipient"), "planned recipient"
        ).lower()
        if f"+{planned_recipient}" not in recipient_local:
            raise ExecutionContractError(
                "recipient mailbox does not agree with the plan recipient"
            )
        return cls(
            target=target,
            environment=environment,
            recipient=recipient,
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            poll_interval_seconds=_positive(
                orchestration.get("poll_interval_seconds"), "poll interval"
            ),
            ingress_timeout_seconds=_positive(
                orchestration.get("ingress_timeout_seconds"), "ingress timeout"
            ),
            worker_timeout_seconds=_positive(
                orchestration.get("worker_timeout_seconds"), "worker timeout"
            ),
            pacing_seconds=_positive(
                env.get("WF_RP1_PACING_SECONDS", "2"), "pacing"
            ),
        )


class ExecutionJournal:
    """Append-only execution record used to make resume non-replaying."""

    def __init__(self, path: Path, *, resume: bool) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size and not resume:
            raise ExecutionContractError(
                f"refusing non-empty journal without --resume: {self.path}"
            )
        self.entries: list[dict[str, Any]] = []
        if self.path.exists():
            for number, line in enumerate(
                self.path.read_text(encoding="utf-8").splitlines(), 1
            ):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ExecutionContractError(
                        f"invalid journal JSON on line {number}"
                    ) from exc
                if not isinstance(entry, dict):
                    raise ExecutionContractError(
                        f"invalid journal entry on line {number}"
                    )
                self.entries.append(entry)

    def append(self, event: str, **fields: Any) -> None:
        entry = {
            "version": 1,
            "ts_unix": time.time(),
            "event": event,
            **_clone(fields),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.entries.append(entry)

    def email_state(self, logical_id: str) -> tuple[bool, dict[str, Any] | None]:
        sent = False
        observed = None
        for entry in self.entries:
            if entry.get("logical_message_id") != logical_id:
                continue
            if entry.get("event") == "email_sent":
                sent = True
            elif entry.get("event") == "email_observed":
                observed = entry
        return sent, observed


class CommandRemoteStaging:
    """JSON-over-stdin boundary pinned to the deployed staging helper."""

    def __init__(
        self,
        *,
        target: str,
        environment: str,
        timeout_seconds: float,
        command: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        if target != TARGET_SYSTEM or environment != TARGET_ENVIRONMENT:
            raise ExecutionContractError(
                "remote staging target must be pa-workflow-dev staging"
            )
        expected_argv = [
            "ssh",
            "-T",
            REMOTE_SSH_TARGET,
            "env",
            f"{STAGING_DB_ENV}={REMOTE_STAGING_DB_PATH}",
            f"{SERVICE_PROOF_ENV}={REMOTE_SERVICE_PROOF_PATH}",
            REMOTE_PYTHON_PATH,
            REMOTE_EXECUTOR_PATH,
            "--staging-stdio",
        ]
        # A command override exists only to fail closed on stale deployment
        # wrappers. It may not redirect execution to a local helper or another
        # host/path.
        if command is not None and shlex.split(command) != expected_argv:
            raise ExecutionContractError(
                "staging command must be the exact pa-workflow-dev SSH helper"
            )
        self._argv = expected_argv
        self._target = target
        self._environment = environment
        self._timeout = timeout_seconds
        self._runner = runner

    def request(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        envelope = {
            "protocol": "wf-rp1-staging-v1",
            "target": self._target,
            "environment": self._environment,
            "action": action,
            "payload": _clone(payload),
        }
        try:
            result = self._runner(
                self._argv,
                input=json.dumps(envelope, sort_keys=True),
                text=True,
                capture_output=True,
                check=False,
                timeout=self._timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutionContractError(
                f"remote staging command failed for {action}"
            ) from exc
        if result.returncode != 0:
            # Never relay stderr: a remote helper may have logged credentials.
            raise ExecutionContractError(
                f"remote staging command returned {result.returncode} for {action}"
            )
        try:
            response = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ExecutionContractError(
                f"remote staging command returned invalid JSON for {action}"
            ) from exc
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise ExecutionContractError(
                f"remote staging command did not confirm {action}"
            )
        if response.get("target") != self._target:
            raise ExecutionContractError("remote staging response target mismatch")
        if response.get("environment") != self._environment:
            raise ExecutionContractError("remote staging response environment mismatch")
        return response


def _json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    value = json.loads(raw)
    return value if isinstance(value, dict) else {}


def _correlation_verdict(row: Mapping[str, Any]) -> str:
    """Recover the match verdict after apply has advanced event status."""
    status = str(row["status"])
    if status == "applied" and row["matched_task_id"]:
        return "matched"
    return status


def _citation(
    table: str, identity: str, query: str, observed: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "table": table,
        "identity": identity,
        "query": query,
        "observed": _clone(observed),
    }


def _extraction_contract_evidence(contract: Mapping[str, Any]) -> dict[str, Any]:
    event_types = contract.get("event_types")
    correlation_keys = contract.get("correlation_keys")
    disambiguators = contract.get("disambiguators")
    instruction = contract.get("instruction")
    if (
        not isinstance(contract.get("schema"), str)
        or not isinstance(event_types, Mapping)
        or not isinstance(correlation_keys, list)
        or not all(isinstance(key, str) and key for key in correlation_keys)
        or not isinstance(disambiguators, list)
        or not all(
            isinstance(key, str) and key for key in disambiguators
        )
        or not isinstance(instruction, str)
        or not instruction.strip()
    ):
        raise ExecutionContractError("email_extraction contract is incomplete")
    canonical = wf_engine._json(dict(contract))
    return {
        "schema": contract["schema"],
        "event_types": sorted(event_types),
        "correlation_keys": list(correlation_keys),
        "disambiguators": list(disambiguators),
        "contract_sha256": _sha256_text(canonical),
        "instruction_sha256": _sha256_text(instruction),
    }


class StagingHelper:
    """Tenant-neutral staging implementation over the real workflow engine."""

    def __init__(self, db_path: Path, service_proof_path: Path) -> None:
        self.db_path = db_path.resolve()
        self.service_proof_path = service_proof_path.resolve()
        self.conn = kanban_db.connect(self.db_path)
        document = yaml.safe_load(
            campaign_lib.TEMPLATE_PATH.read_text(encoding="utf-8")
        )
        self.workflow = _mapping(document, "workflow fixture").get("workflow")
        if not isinstance(self.workflow, dict):
            raise ExecutionContractError("workflow fixture has no workflow object")
        self.template_id, _version = wf_engine.register_template(
            self.conn, self.workflow
        )

    def close(self) -> None:
        self.conn.close()

    def _response(
        self,
        *,
        ready: bool,
        citations: list[dict[str, Any]],
        observed: Mapping[str, Any] | None = None,
        durable: Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "target": TARGET_SYSTEM,
            "environment": TARGET_ENVIRONMENT,
            "ready": ready,
            "durable": dict(
                durable
                or {
                    "event": "not_applicable",
                    "instance": "not_applicable",
                    "proposal": "not_applicable",
                }
            ),
            "observed": _clone(observed or {}),
            "citations": citations,
            **_clone(extra),
        }

    def _service_proof(self) -> dict[str, Any]:
        try:
            proof = json.loads(self.service_proof_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExecutionContractError("service proof is missing or invalid") from exc
        if not isinstance(proof, dict):
            raise ExecutionContractError("service proof must be an object")
        required = {
            "target": TARGET_SYSTEM,
            "environment": TARGET_ENVIRONMENT,
            "service": SERVICE_NAME,
            "database": str(self.db_path),
            "ingress_board": RP1_BOARD,
            "kanban_db_override": None,
        }
        for key, expected in required.items():
            if proof.get(key) != expected:
                raise ExecutionContractError(f"service proof {key} mismatch")
        if not isinstance(proof.get("deployed_release"), str) or not proof[
            "deployed_release"
        ]:
            raise ExecutionContractError("service proof deployed_release missing")
        executor_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        if proof.get("executor_sha256") != executor_hash:
            raise ExecutionContractError("service proof executor_sha256 mismatch")
        return proof

    def _instance_citation(self, entity_key: str) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT i.task_id, i.entity_key, i.template_id, i.state, i.corr, i.vars,
                   t.current_step_key
              FROM wf_instance i JOIN tasks t ON t.id = i.task_id
             WHERE i.entity_key = ?
            """,
            (entity_key,),
        ).fetchone()
        if row is None:
            raise ExecutionContractError(f"workflow instance missing: {entity_key}")
        return _citation(
            "wf_instance+tasks",
            f"wf_instance.entity_key={entity_key}",
            "SELECT workflow instance and current task step by entity_key",
            {
                "task_id": row["task_id"],
                "entity_key": row["entity_key"],
                "template_id": row["template_id"],
                "state": row["state"],
                "current_step_key": row["current_step_key"],
                "corr": _json_object(row["corr"]),
                "vars": _json_object(row["vars"]),
            },
        )

    def _event_by_stable_keys(
        self,
        *,
        wire_message_id: str | None,
        subject_token: str | None,
        x_token: str | None,
    ) -> tuple[sqlite3.Row, dict[str, Any]] | None:
        rows = self.conn.execute(
            """
            SELECT * FROM wf_event
             WHERE source = 'email'
             ORDER BY id DESC
             LIMIT 500
            """
        ).fetchall()
        parsed = [(row, _json_object(row["payload"])) for row in rows]
        if wire_message_id:
            for row, payload in parsed:
                message_id = str(
                    payload.get("message_id")
                    or payload.get("Message-ID")
                    or row["external_id"]
                    or ""
                )
                if message_id == wire_message_id:
                    return row, payload
        for row, payload in parsed:
            subject = str(payload.get("subject") or "")
            headers = payload.get("headers")
            header_token = ""
            if isinstance(headers, dict):
                header_token = str(
                    headers.get("X-RP1-Token")
                    or headers.get("x-rp1-token")
                    or ""
                )
            if subject_token and subject_token in subject:
                return row, payload
            if subject_token and subject_token in json.dumps(
                payload, sort_keys=True
            ):
                return row, payload
            if x_token and header_token == x_token:
                return row, payload
        return None

    def _event_capture(
        self, row: sqlite3.Row, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not row["event_type"] and row["status"] == "received":
            raise ExecutionContractError(
                "email event extraction is not yet durable"
            )
        if row["event_type"] is None:
            extraction_disposition = (
                "boundary-failure"
                if row["status"] == "needs_review"
                and row["matched_task_id"] is None
                else "declared-no-fit"
            )
        else:
            extraction_disposition = "classified"
        corr = _json_object(row["corr"])
        entity_key = None
        if row["matched_task_id"]:
            instance = self.conn.execute(
                "SELECT entity_key FROM wf_instance WHERE task_id = ?",
                (row["matched_task_id"],),
            ).fetchone()
            entity_key = instance["entity_key"] if instance else None
        approval = None
        if row["matched_task_id"]:
            approval = self.conn.execute(
                """
                SELECT action, payload, status FROM wf_approval
                 WHERE task_id = ? ORDER BY id DESC LIMIT 1
                """,
                (row["matched_task_id"],),
            ).fetchone()
        action = (
            {
                "kind": "propose",
                "proposal": {
                    "action": approval["action"],
                    "payload": _json_object(approval["payload"]),
                    "status": approval["status"],
                },
            }
            if approval
            else {
                "evidence_status": "EVIDENCE-LIMITED",
                "reason": "no durable wf_approval row for this event",
            }
        )
        observed = {
            "event_type": row["event_type"],
            "extraction_disposition": extraction_disposition,
            "payload": payload,
            "corr": corr,
            "correlation": {
                "verdict": _correlation_verdict(row),
                "target": entity_key,
                "match_method": row["match_method"],
            },
            "agent_action": action,
        }
        _validate_observed(
            observed,
            f"wf_event.id={row['id']}",
            allow_empty_classified_payload=(
                row["source"] == "email" and row["payload"] not in (None, "")
            ),
        )
        citation = _citation(
            "wf_event",
            f"wf_event.id={row['id']}",
            "SELECT email event by Message-ID, X-RP1-Token, or subject token",
            {
                "id": int(row["id"]),
                "source": row["source"],
                "external_id": row["external_id"],
                "event_type": row["event_type"],
                "status": row["status"],
                "matched_task_id": row["matched_task_id"],
                "match_method": row["match_method"],
                "payload_column_present": row["payload"] is not None
                and row["payload"] != "",
            },
        )
        return observed, citation

    def handle(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if action == "preflight":
            proof = self._service_proof()
            tick = wf_watcher.run_tick(
                self.conn, int(time.time()), extract_email=False
            )
            expected_extraction = self.workflow.get("email_extraction")
            resolved_extraction = wf_watcher.resolve_email_extraction_brief(self.conn)
            if not isinstance(expected_extraction, dict):
                raise ExecutionContractError(
                    "workflow fixture has no email_extraction contract"
                )
            if resolved_extraction is None:
                raise ExecutionContractError(
                    "registered email_extraction contract is absent or ambiguous"
                )
            if resolved_extraction != expected_extraction:
                raise ExecutionContractError(
                    "registered email_extraction contract differs from fixture"
                )
            assert isinstance(resolved_extraction, dict)
            template = self.conn.execute(
                "SELECT slug, version, content_hash, spec FROM wf_template "
                "WHERE slug = ? ORDER BY version DESC LIMIT 1",
                (self.workflow["id"],),
            ).fetchone()
            if template is None:
                raise ExecutionContractError("workflow template is not registered")
            template_count = int(
                self.conn.execute(
                    "SELECT COUNT(*) AS count FROM wf_template"
                ).fetchone()["count"]
            )
            if template_count != 1:
                raise ExecutionContractError(
                    "fresh RP1 staging board contains another workflow template"
                )
            registered_spec = wf_engine._load_json(template["spec"], None)
            registered_workflow = (
                wf_engine._workflow_spec(registered_spec)
                if isinstance(registered_spec, dict)
                else {}
            )
            registered_correlation_keys = registered_workflow.get(
                "correlation_keys"
            )
            if not isinstance(registered_correlation_keys, list):
                raise ExecutionContractError(
                    "registered workflow correlation_keys are missing"
                )
            registered_extraction = registered_workflow.get("email_extraction")
            if registered_extraction != expected_extraction:
                raise ExecutionContractError(
                    "latest RP1 template email_extraction differs from fixture"
                )
            extraction_evidence = _extraction_contract_evidence(
                resolved_extraction
            )
            if extraction_evidence["correlation_keys"] != registered_correlation_keys:
                raise ExecutionContractError(
                    "email_extraction correlation_keys differ from workflow"
                )
            if extraction_evidence["disambiguators"] != registered_workflow.get(
                "disambiguators"
            ):
                raise ExecutionContractError(
                    "email_extraction disambiguators differ from workflow"
                )
            citations = [
                _citation(
                    "runtime_service_proof",
                    f"service={proof['service']} release={proof['deployed_release']}",
                    f"read {self.service_proof_path}",
                    proof,
                ),
                _citation(
                    "wf_template",
                    f"wf_template.slug={template['slug']}@{template['version']}",
                    "SELECT latest registered workflow template",
                    {
                        "slug": template["slug"],
                        "version": int(template["version"]),
                        "content_hash": template["content_hash"],
                    },
                ),
                _citation(
                    "wf_template",
                    (
                        f"wf_template.slug={template['slug']}@{template['version']}"
                        ".email_extraction"
                    ),
                    "resolve the sole registered email_extraction contract",
                    extraction_evidence,
                ),
                _citation(
                    "wf_watcher.run_tick",
                    f"database={self.db_path}",
                    "run wf_watcher.run_tick against the staging database",
                    {
                        "probe_errors": tick.probe_errors,
                        "timer_errors": tick.timer_errors,
                        "sweep_processed": tick.sweep_processed,
                    },
                ),
            ]
            healthy = tick.probe_errors == 0 and tick.timer_errors == 0
            return self._response(
                ready=healthy,
                citations=citations,
                service_identity=proof["service"],
                deployed_release=proof["deployed_release"],
                watcher_healthy=healthy,
                extraction_contract_ready=True,
            )

        if action in {"seed_arc", "observe_seed"}:
            seeds = payload.get("seeds")
            if not isinstance(seeds, list):
                raise ExecutionContractError(f"{action} requires seeds")
            if not seeds:
                return self._response(
                    ready=True,
                    citations=[],
                    durable={
                        "event": "not_applicable",
                        "instance": "not_applicable",
                        "proposal": "not_applicable",
                    },
                )
            citations: list[dict[str, Any]] = []
            if action == "seed_arc":
                for seed in seeds:
                    seed = _mapping(seed, "seed")
                    entity_key = _text(seed.get("entity_key"), "seed entity_key")
                    event_id = wf_engine.ingest_event(
                        self.conn,
                        source="campaign_seed",
                        external_id=f"rp1:{payload.get('arc_id')}:{entity_key}",
                        payload={"entity_key": entity_key},
                        corr=dict(_mapping(seed.get("corr", {}), "seed corr")),
                        event_type="campaign_seed",
                    )
                    task_id = wf_engine.create_instance(
                        self.conn,
                        template_id=self.template_id,
                        entity_key=entity_key,
                        corr=dict(_mapping(seed.get("corr", {}), "seed corr")),
                        vars=dict(_mapping(seed.get("vars", {}), "seed vars")),
                        source_event_id=event_id,
                    )
                    requested = _text(seed.get("requested_step"), "requested_step")
                    current = self.conn.execute(
                        "SELECT current_step_key FROM tasks WHERE id = ?", (task_id,)
                    ).fetchone()["current_step_key"]
                    if current != requested:
                        if event_id is None:
                            existing = self.conn.execute(
                                "SELECT id FROM wf_event WHERE source='campaign_seed' "
                                "AND external_id=?",
                                (f"rp1:{payload.get('arc_id')}:{entity_key}",),
                            ).fetchone()
                            event_id = int(existing["id"])
                        wf_engine.advance(
                            self.conn,
                            task_id,
                            to_step=requested,
                            event_id=int(event_id),
                        )
            for seed in seeds:
                citations.append(
                    self._instance_citation(
                        _text(_mapping(seed, "seed").get("entity_key"), "entity_key")
                    )
                )
            return self._response(
                ready=True,
                citations=citations,
                durable={
                    "event": True if action == "seed_arc" else "not_applicable",
                    "instance": True,
                    "proposal": "not_applicable",
                },
            )

        if action in {"observe_preflight", "observe_email"}:
            match = self._event_by_stable_keys(
                wire_message_id=payload.get("wire_message_id"),
                subject_token=payload.get("subject_token"),
                x_token=payload.get("x_rp1_token"),
            )
            if match is None:
                return self._response(ready=False, citations=[])
            row, event_payload = match
            if not row["event_type"] and row["status"] == "received":
                if action == "observe_preflight":
                    ingress = {
                        "from": event_payload.get("sender_addr")
                        or event_payload.get("sender")
                        or event_payload.get("from"),
                        "message_id": event_payload.get("message_id")
                        or row["external_id"],
                        "subject": event_payload.get("subject"),
                        "x_rp1_token": (
                            event_payload.get("headers", {}).get("X-RP1-Token")
                            if isinstance(event_payload.get("headers"), dict)
                            else None
                        ),
                    }
                    ingress_citation = _citation(
                        "wf_event",
                        f"wf_event.id={row['id']} ingress-envelope",
                        "SELECT unclassified email envelope by stable keys",
                        {
                            "id": int(row["id"]),
                            "source": row["source"],
                            "external_id": row["external_id"],
                            "event_type": None,
                            "payload": event_payload,
                        },
                    )
                    return self._response(
                        ready=False,
                        citations=[ingress_citation],
                        observed={
                            "ingress_received": ingress,
                            "stable_keys": [
                                key
                                for key in ("message_id", "subject", "x_rp1_token")
                                if ingress.get(key)
                            ],
                        },
                    )
                return self._response(ready=False, citations=[])
            observed, citation = self._event_capture(row, event_payload)
            if action == "observe_preflight":
                received = {
                    "from": event_payload.get("sender_addr")
                    or event_payload.get("sender")
                    or event_payload.get("from"),
                    "message_id": event_payload.get("message_id")
                    or row["external_id"],
                    "subject": event_payload.get("subject"),
                    "x_rp1_token": (
                        event_payload.get("headers", {}).get("X-RP1-Token")
                        if isinstance(event_payload.get("headers"), dict)
                        else None
                    ),
                }
                stable = [
                    key
                    for key in ("message_id", "subject", "x_rp1_token")
                    if received.get(key)
                ]
                if not stable:
                    raise ExecutionContractError(
                        "preflight rewrite lost every stable observation key"
                    )
                observed = {**observed, "received": received, "stable_keys": stable}
            return self._response(
                ready=True,
                citations=[citation],
                observed=observed,
                durable={
                    "event": True,
                    "instance": bool(row["matched_task_id"]) or "not_applicable",
                    "proposal": "not_applicable",
                },
                lookup_fallback="subject_token",
            )

        if action == "execute_state_poll":
            if set(payload) != {"entity_key", "request", "result"}:
                raise ExecutionContractError(
                    "state_poll payload permits entity_key, request, result only"
                )
            entity_key = _text(payload.get("entity_key"), "probe entity_key")
            request = dict(_mapping(payload.get("request"), "probe request"))
            result = dict(_mapping(payload.get("result"), "probe result"))
            tenant = "rp1-staging"
            task = self.conn.execute(
                "SELECT task_id FROM wf_instance WHERE entity_key = ?",
                (entity_key,),
            ).fetchone()
            if task is None:
                raise ExecutionContractError("state_poll entity is not seeded")
            self.conn.execute(
                "UPDATE tasks SET tenant = ? WHERE id = ?", (tenant, task["task_id"])
            )

            def probe(
                targets: tuple[wf_watcher.ProbeTarget, ...],
            ) -> tuple[wf_watcher.ProbeObservation, ...]:
                selected = [target for target in targets if target.entity_key == entity_key]
                if not selected:
                    return ()
                field = _text(request.get("field"), "probe field")
                corr = {
                    key: result[key]
                    for key in ("job_no", "container_no")
                    if key in result
                }
                return (
                    wf_watcher.ProbeObservation(
                        external_id=f"rp1:{entity_key}:{field}",
                        event_type=field,
                        corr=corr,
                        payload=result,
                    ),
                )

            wf_watcher.register_state_probe(tenant, probe, read_only=True)
            try:
                tick = wf_watcher.run_tick(
                    self.conn, int(time.time()), extract_email=False
                )
            finally:
                wf_watcher.unregister_state_probe(tenant)
            row = self.conn.execute(
                "SELECT * FROM wf_event WHERE source='state_poll' "
                "AND external_id=? ORDER BY id DESC LIMIT 1",
                (f"rp1:{entity_key}:{request['field']}",),
            ).fetchone()
            if row is None:
                return self._response(ready=False, citations=[])
            event_payload = _json_object(row["payload"])
            observed, citation = self._event_capture(row, event_payload)
            observed["state_poll"] = result
            observed["process_local_isolation"] = (
                "probe registered and ticked in this staging helper process "
                "against the same SQLite board"
            )
            return self._response(
                ready=True,
                citations=[
                    citation,
                    _citation(
                        "wf_watcher.run_tick",
                        f"state_poll.entity_key={entity_key}",
                        "register_state_probe(read_only=True), then run_tick",
                        {
                            "poll_events": list(tick.poll_events),
                            "probe_errors": tick.probe_errors,
                            "poll_duplicates": tick.poll_duplicates,
                        },
                    ),
                ],
                observed=observed,
                durable={
                    "event": True,
                    "instance": True,
                    "proposal": (
                        True
                        if observed["agent_action"].get("kind") == "propose"
                        else "not_applicable"
                    ),
                },
            )

        if action == "observe_arc_final":
            seeds = payload.get("seeds")
            if not isinstance(seeds, list):
                raise ExecutionContractError("observe_arc_final requires seeds")
            if not seeds:
                return self._response(
                    ready=True,
                    citations=[],
                    observed={
                        "evidence_status": "EVIDENCE-LIMITED",
                        "instances": {},
                    },
                    durable={
                        "event": "not_applicable",
                        "instance": "not_applicable",
                        "proposal": "not_applicable",
                    },
                )
            citations = [
                self._instance_citation(
                    _text(_mapping(seed, "seed").get("entity_key"), "entity_key")
                )
                for seed in seeds
            ]
            observed = {
                citation["observed"]["entity_key"]: {
                    "state": citation["observed"]["state"],
                    "step": citation["observed"]["current_step_key"],
                    "corr": citation["observed"]["corr"],
                    "vars": citation["observed"]["vars"],
                }
                for citation in citations
            }
            return self._response(
                ready=True,
                citations=citations,
                observed={
                    "evidence_status": "EVIDENCE-LIMITED",
                    "instances": observed,
                },
                durable={
                    "event": "not_applicable",
                    "instance": True,
                    "proposal": "not_applicable",
                },
            )
        raise ExecutionContractError(f"unknown staging action: {action}")


def staging_stdio(
    stdin: IO[str],
    stdout: IO[str],
    *,
    env: Mapping[str, str],
) -> int:
    """Serve exactly one staging request on stdin/stdout."""
    try:
        envelope = json.loads(stdin.read())
        if not isinstance(envelope, dict):
            raise ExecutionContractError("staging request must be an object")
        if set(envelope) != {
            "protocol",
            "target",
            "environment",
            "action",
            "payload",
        }:
            raise ExecutionContractError("staging request schema mismatch")
        if (
            envelope["protocol"] != "wf-rp1-staging-v1"
            or envelope["target"] != TARGET_SYSTEM
            or envelope["environment"] != TARGET_ENVIRONMENT
        ):
            raise ExecutionContractError("staging request target gate failed")
        db_path = Path(_text(env.get(STAGING_DB_ENV), STAGING_DB_ENV))
        proof_path = Path(_text(env.get(SERVICE_PROOF_ENV), SERVICE_PROOF_ENV))
        helper = StagingHelper(db_path, proof_path)
        try:
            response = helper.handle(
                _text(envelope["action"], "action"),
                _mapping(envelope["payload"], "payload"),
            )
        finally:
            helper.close()
    except Exception as exc:
        response = {
            "ok": False,
            "target": TARGET_SYSTEM,
            "environment": TARGET_ENVIRONMENT,
            "error": type(exc).__name__,
        }
    stdout.write(json.dumps(response, sort_keys=True) + "\n")
    return 0 if response.get("ok") else 1


class SMTPIngress:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        recipient: str,
        smtp_factory: Callable[..., Any] = smtplib.SMTP,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.recipient = recipient
        self.smtp_factory = smtp_factory

    def send(self, message: EmailMessage, envelope_sender: str) -> None:
        with self.smtp_factory(self.host, self.port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(self.user, self.password)
            smtp.send_message(
                message, from_addr=envelope_sender, to_addrs=[self.recipient]
            )


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExecutionContractError(f"{label} must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionContractError(f"{label} must be a non-empty string")
    return value.strip()


def _positive(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ExecutionContractError(f"{label} must be numeric") from exc
    if result <= 0:
        raise ExecutionContractError(f"{label} must be positive")
    return result


def _mailbox(value: str, label: str) -> str:
    _display, address = parseaddr(value)
    if (
        address != value
        or not address
        or "@" not in address
        or "\r" in value
        or "\n" in value
    ):
        raise ExecutionContractError(f"{label} must be one plain mailbox")
    return address


def _plus_address(base: str, arc_id: str) -> str:
    local, domain = _mailbox(base, "SMTP user").rsplit("@", 1)
    local = local.split("+", 1)[0]
    return f"{local}+{arc_id.lower()}@{domain}"


def _same_sender_after_plus_canonicalization(
    expected: str, received: str
) -> bool:
    """Accept Gmail's exact plus-tag collapse, and no other identity drift."""
    try:
        expected = _mailbox(expected, "expected sender")
        received = _mailbox(received, "received sender")
    except ExecutionContractError:
        return False
    expected_local, expected_domain = expected.casefold().rsplit("@", 1)
    received_local, received_domain = received.casefold().rsplit("@", 1)
    if expected_local == received_local and expected_domain == received_domain:
        return True
    expected_base, separator, expected_tag = expected_local.partition("+")
    return bool(
        separator
        and expected_tag
        and expected_domain == received_domain
        and expected_base == received_local
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Validate locked bytes and plan structure without external access."""
    locked = campaign_lib.load_locked_campaign()
    if plan.get("campaign") != "rp1" or plan.get("mode") != "plan-only":
        raise ExecutionContractError("input is not an RP1 plan-only artifact")
    if (
        plan.get("network_performed") is not False
        or plan.get("database_mutated") is not False
    ):
        raise ExecutionContractError("input plan claims prior mutation")
    if plan.get("locked_fixture_hashes") != locked.fixture_hashes:
        raise ExecutionContractError("plan locked fixture hashes do not match")
    population = {
        "arcs": len(locked.arcs),
        "emails": locked.email_count,
        "probes": locked.probe_count,
    }
    if plan.get("population") != population:
        raise ExecutionContractError("plan population does not match locked campaign")
    template = _mapping(plan.get("workflow_template"), "workflow_template")
    if template.get("sha256") != campaign_lib._sha256(campaign_lib.TEMPLATE_PATH):
        raise ExecutionContractError("workflow template hash mismatch")
    orchestration = _mapping(plan.get("orchestration"), "orchestration")
    if orchestration.get("remote_db") != TARGET_SYSTEM:
        raise ExecutionContractError("plan remote_db must be pa-workflow-dev")
    if (
        orchestration.get("smtp_host") != "smtp.gmail.com"
        or orchestration.get("smtp_port") != 587
    ):
        raise ExecutionContractError("RP1 SMTP must use smtp.gmail.com:587")
    if plan.get("evidence_limited_paths") != list(campaign_lib.EVIDENCE_LIMITS):
        raise ExecutionContractError("plan evidence-limit contract changed")

    arcs = plan.get("arcs")
    if not isinstance(arcs, list) or len(arcs) != len(locked.arcs):
        raise ExecutionContractError("plan arc population mismatch")
    locked_by_id = {arc["id"]: arc for arc in locked.arcs}
    reference_plan = campaign_lib.build_campaign_plan(
        locked, remote_db=TARGET_SYSTEM
    )
    reference_by_id = {arc["id"]: arc for arc in reference_plan["arcs"]}
    probe_count = 0
    seen_arc_ids: set[str] = set()
    for planned_arc in arcs:
        arc = _mapping(planned_arc, "arc")
        arc_id = _text(arc.get("id"), "arc id")
        locked_arc = locked_by_id.get(arc_id)
        if locked_arc is None or arc_id in seen_arc_ids:
            raise ExecutionContractError(f"unknown arc {arc_id}")
        seen_arc_ids.add(arc_id)
        reference_arc = reference_by_id[arc_id]
        seeds, emails, probes = (
            arc.get("seed_plan"),
            arc.get("emails"),
            arc.get("state_probes"),
        )
        if not isinstance(seeds, list) or len(seeds) != len(
            locked_arc["initial_instances"]
        ):
            raise ExecutionContractError(f"{arc_id}: seed population mismatch")
        if not isinstance(emails, list) or len(emails) != len(locked_arc["emails"]):
            raise ExecutionContractError(f"{arc_id}: email population mismatch")
        if not isinstance(probes, list) or len(probes) != len(
            locked_arc.get("state_probes", [])
        ):
            raise ExecutionContractError(f"{arc_id}: probe population mismatch")
        if seeds != reference_arc["seed_plan"]:
            raise ExecutionContractError(f"{arc_id}: seed plan changed")
        if arc.get("expected_final") != locked_arc["expected_final"]:
            raise ExecutionContractError(f"{arc_id}: expected final changed")
        probe_count += len(probes)
        for planned_email, locked_email in zip(
            emails, locked_arc["emails"], strict=True
        ):
            if planned_email.get("logical_message_id") != locked_email["message_id"]:
                raise ExecutionContractError(f"{arc_id}: logical Message-ID changed")
            wire_id = campaign_lib.logical_to_wire_message_id(
                locked_email["message_id"]
            )
            if planned_email.get("wire_message_id") != wire_id:
                raise ExecutionContractError(f"{arc_id}: wire Message-ID changed")
            if planned_email.get("locked_body_sha256") != _sha256_text(
                locked_email["body"]
            ):
                raise ExecutionContractError(f"{arc_id}: locked body changed")
            if planned_email.get("subject") != locked_email["subject"]:
                raise ExecutionContractError(f"{arc_id}: subject changed")
            if planned_email.get("from_display") != locked_email["from_display"]:
                raise ExecutionContractError(f"{arc_id}: sender display changed")
            if planned_email.get("locked_body") != locked_email["body"]:
                raise ExecutionContractError(f"{arc_id}: locked body bytes changed")
            if planned_email.get("headers") != campaign_lib._reference_headers(
                locked_email["answer_key"]
            ):
                raise ExecutionContractError(f"{arc_id}: thread headers changed")
        ordered = sorted(
            locked_arc.get("state_probes", []),
            key=lambda item: (item["after_email_step"], item["step"]),
        )
        email_steps = {int(email["step"]) for email in emails}
        for planned_probe, locked_probe in zip(probes, ordered, strict=True):
            for key in ("step", "after_email_step", "source", "entity_key", "request"):
                if planned_probe.get(key) != locked_probe.get(key):
                    raise ExecutionContractError(
                        f"{arc_id}: state probe {key} changed"
                    )
            if int(planned_probe["after_email_step"]) not in email_steps:
                raise ExecutionContractError(
                    f"{arc_id}: state probe is not bound to an email step"
                )
    if probe_count != 1:
        raise ExecutionContractError(
            "RP1 execution requires exactly one declared state_poll"
        )
    return {
        "valid": True,
        "target_required": TARGET_SYSTEM,
        "environment_required": TARGET_ENVIRONMENT,
        "population": population,
        "locked_fixture_hashes": locked.fixture_hashes,
        "network_performed": False,
        "database_mutated": False,
    }


def _email_message(
    planned_email: Mapping[str, Any],
    *,
    arc_id: str,
    sender_base: str,
    recipient: str,
) -> tuple[EmailMessage, str]:
    sender = _plus_address(sender_base, arc_id)
    display = _text(planned_email.get("from_display"), "from_display")
    locked_body = _text(planned_email.get("locked_body"), "locked_body")
    message = EmailMessage()
    message["From"] = formataddr((display, sender))
    message["To"] = recipient
    message["Subject"] = _text(planned_email.get("subject"), "subject")
    message["Message-ID"] = campaign_lib.logical_to_wire_message_id(
        _text(planned_email.get("logical_message_id"), "logical Message-ID")
    )
    headers = _mapping(planned_email.get("headers", {}), "headers")
    if headers.get("in_reply_to"):
        message["In-Reply-To"] = campaign_lib.logical_to_wire_message_id(
            str(headers["in_reply_to"])
        )
    if headers.get("references"):
        message["References"] = " ".join(
            campaign_lib.logical_to_wire_message_id(value)
            for value in str(headers["references"]).split()
        )
    message.set_content(f"{locked_body}\n\n--\n{display} <{sender}>\n")
    return message, sender


def _validate_observed(
    observed: Mapping[str, Any],
    label: str,
    *,
    allow_empty_classified_payload: bool = False,
) -> None:
    missing = [key for key in _SCORABLE_KEYS if key not in observed]
    if missing:
        raise ExecutionContractError(
            f"{label}: observed capture missing scorable keys {missing}"
        )
    if observed["event_type"] is not None and (
        not isinstance(observed["event_type"], str)
        or not observed["event_type"]
    ):
        raise ExecutionContractError(
            f"{label}: event_type must be non-empty or null"
        )
    null_disposition = observed.get("extraction_disposition")
    durable_null = (
        observed["event_type"] is None
        and null_disposition in {"declared-no-fit", "boundary-failure"}
    )
    classified_empty_payload = (
        allow_empty_classified_payload
        and observed["event_type"] is not None
        and observed.get("payload") == {}
    )
    if observed["event_type"] is None and not durable_null:
        raise ExecutionContractError(
            f"{label}: null event_type needs a durable extraction disposition"
        )
    for key in ("payload", "corr", "correlation", "agent_action"):
        value = observed[key]
        if not isinstance(value, Mapping):
            raise ExecutionContractError(f"{label}: {key} must be an object")
        if value.get("evidence_status") == "EVIDENCE-LIMITED":
            if not isinstance(value.get("reason"), str) or not value["reason"]:
                raise ExecutionContractError(
                    f"{label}: evidence-limited {key} needs a reason"
                )
        elif not value and not (
            (durable_null and key in {"payload", "corr"})
            or (classified_empty_payload and key == "payload")
        ):
            raise ExecutionContractError(
                f"{label}: {key} must contain observed fields or an "
                "EVIDENCE-LIMITED marker"
            )


def _citations(response: Mapping[str, Any], label: str) -> list[dict[str, Any]]:
    citations = response.get("citations")
    if not isinstance(citations, list) or not citations:
        raise ExecutionContractError(f"{label}: missing P5a citations")
    required_fields = {"table", "identity", "query", "observed"}
    for citation in citations:
        if (
            not isinstance(citation, dict)
            or set(citation) != required_fields
            or not isinstance(citation["identity"], str)
        ):
            raise ExecutionContractError(f"{label}: invalid P5a citation shape")
    return _clone(citations)


def _no_seed_citations(
    response: Mapping[str, Any], label: str
) -> list[dict[str, Any]]:
    expected_durable = {
        "event": "not_applicable",
        "instance": "not_applicable",
        "proposal": "not_applicable",
    }
    if response.get("citations") != [] or response.get("durable") != expected_durable:
        raise ExecutionContractError(f"{label}: invalid zero-seed evidence shape")
    return []


def _require_extraction_contract_citation(
    citations: list[dict[str, Any]],
) -> None:
    document = yaml.safe_load(
        campaign_lib.TEMPLATE_PATH.read_text(encoding="utf-8")
    )
    workflow = _mapping(document, "workflow fixture").get("workflow")
    if not isinstance(workflow, dict):
        raise ExecutionContractError("workflow fixture has no workflow object")
    contract = workflow.get("email_extraction")
    if not isinstance(contract, dict):
        raise ExecutionContractError("workflow fixture has no email_extraction")
    expected = _extraction_contract_evidence(contract)
    expected_identity_prefix = f"wf_template.slug={workflow['id']}@"
    matching = [
        citation
        for citation in citations
        if citation["identity"].startswith(expected_identity_prefix)
        and citation["identity"].endswith(".email_extraction")
        and citation["observed"] == expected
    ]
    if len(matching) != 1:
        raise ExecutionContractError(
            "preflight did not cite the exact registered email_extraction contract"
        )
    extraction_identity = matching[0]["identity"]
    template_identity = extraction_identity.removesuffix(".email_extraction")
    if not any(
        citation["identity"] == template_identity
        for citation in citations
    ):
        raise ExecutionContractError(
            "preflight extraction citation version is not bound to template"
        )


def _durable_ready(response: Mapping[str, Any]) -> bool:
    durable = response.get("durable")
    return response.get("ready") is True and isinstance(durable, Mapping) and all(
        durable.get(key) in (True, "not_applicable") for key in _REQUIRED_DURABLE
    )


def _wait_remote(
    remote: RemoteStaging,
    *,
    action: str,
    payload: Mapping[str, Any],
    timeout_seconds: float,
    poll_interval_seconds: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    journal: ExecutionJournal | None = None,
    journal_fields: Mapping[str, Any] | None = None,
    response_hook: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    deadline = monotonic() + timeout_seconds
    while True:
        response = remote.request(action, payload)
        if response_hook is not None:
            response_hook(response)
        if _durable_ready(response):
            return response
        if monotonic() >= deadline:
            if journal is not None:
                journal.append(
                    "timeout",
                    action=action,
                    **dict(journal_fields or {}),
                )
            raise ExecutionContractError(
                f"timed out waiting for durable {action} state"
            )
        sleep(poll_interval_seconds)


def _journal_citations(
    journal: ExecutionJournal,
    citations: list[dict[str, Any]],
    **identity: Any,
) -> None:
    for citation in citations:
        journal.append("citation_captured", citation=citation, **identity)


def _preflight_message(
    *, sender_base: str, recipient: str, token: str
) -> tuple[EmailMessage, str]:
    sender = _plus_address(sender_base, "rp1-preflight")
    message = EmailMessage()
    message["From"] = formataddr(("RP1 Staging Preflight", sender))
    message["To"] = recipient
    message["Subject"] = f"[{token}] RP1 staging ingress preflight"
    message["Message-ID"] = f"<{token}@rp1.synthetic.test>"
    message["X-RP1-Token"] = token
    message.set_content(
        "TYPE=pickup_advice\n"
        f"BOOKING_REF={token}\n"
        "RESULT=preflight\n\n"
        f"--\nRP1 Staging Preflight <{sender}>\n"
    )
    return message, sender


def execute_plan(
    plan: Mapping[str, Any],
    *,
    settings: ExecutionSettings,
    remote: RemoteStaging,
    smtp: SMTPIngress,
    journal: ExecutionJournal,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Execute one campaign and wait at every mutation boundary."""
    validation = validate_plan(plan)
    if settings.target != TARGET_SYSTEM or settings.environment != TARGET_ENVIRONMENT:
        raise ExecutionContractError("execution target gate failed")
    preflight = remote.request(
        "preflight",
        {
            "campaign": "rp1",
            "workflow_template": plan["workflow_template"],
            "locked_fixture_hashes": plan["locked_fixture_hashes"],
        },
    )
    if not _durable_ready(preflight):
        raise ExecutionContractError("staging preflight is not durably ready")
    if (
        preflight.get("service_identity") != SERVICE_NAME
        or not isinstance(preflight.get("deployed_release"), str)
        or not preflight["deployed_release"]
        or preflight.get("watcher_healthy") is not True
        or preflight.get("extraction_contract_ready") is not True
    ):
        raise ExecutionContractError(
            "remote preflight did not prove service, release, watcher health, "
            "and extraction contract"
        )
    all_citations = _citations(preflight, "preflight")
    _require_extraction_contract_citation(all_citations)
    journal.append(
        "remote_preflight_observed",
        service_identity=preflight.get("service_identity"),
        deployed_release=preflight.get("deployed_release"),
        watcher_healthy=preflight.get("watcher_healthy"),
        extraction_contract_ready=preflight.get("extraction_contract_ready"),
    )
    _journal_citations(journal, all_citations, phase="remote_preflight")

    preflight_token = f"rp1-preflight-{uuid.uuid4().hex}"
    loopback_message, loopback_sender = _preflight_message(
        sender_base=settings.smtp_user,
        recipient=settings.recipient,
        token=preflight_token,
    )
    smtp.send(loopback_message, loopback_sender)
    journal.append(
        "preflight_email_sent",
        wire_message_id=loopback_message["Message-ID"],
        subject_token=preflight_token,
        envelope_sender=loopback_sender,
    )
    ingress_snapshot: dict[str, Any] = {}
    ingress_citations: list[dict[str, Any]] = []

    def capture_ingress(response: Mapping[str, Any]) -> None:
        observed = response.get("observed")
        if not isinstance(observed, Mapping):
            return
        received = observed.get("ingress_received")
        if isinstance(received, Mapping):
            ingress_snapshot.update(_clone(received))
            journal.append(
                "preflight_ingress_observed",
                received=received,
                stable_keys=observed.get("stable_keys", []),
            )
            citations = response.get("citations")
            if isinstance(citations, list) and citations:
                validated = _citations(response, "preflight ingress")
                ingress_citations.extend(validated)
                _journal_citations(
                    journal, validated, phase="loopback_ingress"
                )

    loopback = _wait_remote(
        remote,
        action="observe_preflight",
        payload={
            "wire_message_id": loopback_message["Message-ID"],
            "subject_token": preflight_token,
            "x_rp1_token": preflight_token,
            "expected_from": loopback_sender,
        },
        timeout_seconds=settings.ingress_timeout_seconds,
        poll_interval_seconds=settings.poll_interval_seconds,
        monotonic=monotonic,
        sleep=sleep,
        journal=journal,
        journal_fields={"phase": "loopback_preflight"},
        response_hook=capture_ingress,
    )
    loopback_observed = dict(
        _mapping(loopback.get("observed"), "preflight observed")
    )
    # The preflight is itself an email extraction measurement. A classified
    # empty payload is a recorded miss, not a broken observation boundary.
    _validate_observed(
        loopback_observed,
        "loopback preflight",
        allow_empty_classified_payload=True,
    )
    received_value = loopback_observed.get("received")
    if ingress_snapshot:
        merged_received = dict(ingress_snapshot)
        if isinstance(received_value, Mapping):
            merged_received.update(
                {key: value for key, value in received_value.items() if value}
            )
        loopback_observed["received"] = merged_received
        stable = set(loopback_observed.get("stable_keys") or ())
        stable.update(
            key
            for key in ("message_id", "subject", "x_rp1_token")
            if merged_received.get(key)
        )
        loopback_observed["stable_keys"] = sorted(stable)
    received = _mapping(loopback_observed.get("received"), "preflight received")
    received_from = received.get("from")
    if not isinstance(received_from, str) or not _same_sender_after_plus_canonicalization(
        loopback_sender, received_from
    ):
        raise ExecutionContractError("preflight received From does not match sender")
    stable_keys = loopback_observed.get("stable_keys")
    if (
        not received.get("message_id")
        or not isinstance(stable_keys, list)
        or not {"subject", "x_rp1_token"}.intersection(stable_keys)
    ):
        raise ExecutionContractError(
            "preflight must retain Message-ID and a subject/X-RP1 token"
        )
    loopback_citations = _citations(loopback, "loopback preflight")
    journal.append(
        "preflight_email_observed",
        observed=loopback_observed,
        display_persona_scope="display and body only",
    )
    _journal_citations(journal, loopback_citations, phase="loopback_preflight")
    all_citations.extend(loopback_citations)
    all_citations.extend(ingress_citations)
    observed_arcs: dict[str, Any] = {}

    for arc in plan["arcs"]:
        arc_id = arc["id"]
        seed = remote.request(
            "seed_arc",
            {
                "campaign": "rp1",
                "arc_id": arc_id,
                "template": plan["workflow_template"],
                "seeds": arc["seed_plan"],
            },
        )
        if not _durable_ready(seed):
            seed = _wait_remote(
                remote,
                action="observe_seed",
                payload={
                    "campaign": "rp1",
                    "arc_id": arc_id,
                    "seeds": arc["seed_plan"],
                },
                timeout_seconds=settings.worker_timeout_seconds,
                poll_interval_seconds=settings.poll_interval_seconds,
                monotonic=monotonic,
                sleep=sleep,
                journal=journal,
                journal_fields={"arc_id": arc_id},
            )
        seed_citations = (
            _citations(seed, f"{arc_id} seed")
            if arc["seed_plan"]
            else _no_seed_citations(seed, f"{arc_id} seed")
        )
        journal.append("seed_observed", arc_id=arc_id)
        _journal_citations(journal, seed_citations, arc_id=arc_id, phase="seed")
        all_citations.extend(seed_citations)
        arc_observed: dict[str, Any] = {
            "emails": {},
            "state_probes": [],
            "expected_final": {},
        }
        probes_by_after: dict[int, list[Mapping[str, Any]]] = {}
        for probe in arc["state_probes"]:
            probes_by_after.setdefault(int(probe["after_email_step"]), []).append(
                probe
            )
        for index, planned_email in enumerate(arc["emails"]):
            message, sender = _email_message(
                planned_email,
                arc_id=arc_id,
                sender_base=settings.smtp_user,
                recipient=settings.recipient,
            )
            logical_id = planned_email["logical_message_id"]
            already_sent, captured = journal.email_state(logical_id)
            if captured is None:
                if not already_sent:
                    actual_body = message.get_body().get_content()
                    smtp.send(message, sender)
                    journal.append(
                        "email_sent",
                        arc_id=arc_id,
                        logical_message_id=logical_id,
                        wire_message_id=message["Message-ID"],
                        wire_body_sha256=_sha256_text(actual_body),
                        envelope_sender=sender,
                    )
                response = _wait_remote(
                    remote,
                    action="observe_email",
                    payload={
                        "campaign": "rp1",
                        "arc_id": arc_id,
                        "logical_message_id": logical_id,
                        "wire_message_id": planned_email["wire_message_id"],
                        "subject_token": planned_email["subject"],
                    },
                    timeout_seconds=settings.ingress_timeout_seconds,
                    poll_interval_seconds=settings.poll_interval_seconds,
                    monotonic=monotonic,
                    sleep=sleep,
                    journal=journal,
                    journal_fields={
                        "arc_id": arc_id,
                        "logical_message_id": logical_id,
                    },
                )
                observed = response.get("observed")
                if not isinstance(observed, dict):
                    raise ExecutionContractError(
                        f"{arc_id} {logical_id}: observed state missing"
                    )
                _validate_observed(
                    observed,
                    f"{arc_id} {logical_id}",
                    allow_empty_classified_payload=True,
                )
                citations = _citations(response, f"{arc_id} {logical_id}")
                journal.append(
                    "email_observed",
                    arc_id=arc_id,
                    logical_message_id=logical_id,
                    observed=observed,
                    citations=citations,
                )
                _journal_citations(
                    journal,
                    citations,
                    arc_id=arc_id,
                    logical_message_id=logical_id,
                )
            else:
                observed = _mapping(captured.get("observed"), "journal observed")
                citations = captured.get("citations")
                if not isinstance(citations, list):
                    raise ExecutionContractError(
                        f"{logical_id}: journal observation has no citations"
                    )
                _validate_observed(
                    observed,
                    f"{arc_id} {logical_id} journal",
                    allow_empty_classified_payload=True,
                )
            arc_observed["emails"][logical_id] = {
                **_clone(observed),
                "_citations": citations,
                "_evidence_limited_paths": list(campaign_lib.EVIDENCE_LIMITS),
            }
            all_citations.extend(citations)
            for probe in probes_by_after.get(int(planned_email["step"]), []):
                if probe.get("source") != "state_poll":
                    raise ExecutionContractError(
                        "only the declared state_poll probe is allowed"
                    )
                probe_response = _wait_remote(
                    remote,
                    action="execute_state_poll",
                    payload={
                        "entity_key": probe["entity_key"],
                        "request": probe["request"],
                        "result": probe["result"],
                    },
                    timeout_seconds=settings.worker_timeout_seconds,
                    poll_interval_seconds=settings.poll_interval_seconds,
                    monotonic=monotonic,
                    sleep=sleep,
                    journal=journal,
                    journal_fields={"arc_id": arc_id, "phase": "state_poll"},
                )
                probe_observed = probe_response.get("observed")
                if not isinstance(probe_observed, dict):
                    raise ExecutionContractError(
                        f"{arc_id}: state_poll observation missing"
                    )
                probe_citations = _citations(
                    probe_response, f"{arc_id} state_poll"
                )
                journal.append(
                    "state_poll_observed",
                    arc_id=arc_id,
                    observed=probe_observed,
                )
                _journal_citations(
                    journal, probe_citations, arc_id=arc_id, phase="state_poll"
                )
                arc_observed["state_probes"].append(
                    {**_clone(probe_observed), "_citations": probe_citations}
                )
                all_citations.extend(probe_citations)
            if index + 1 < len(arc["emails"]):
                sleep(settings.pacing_seconds)
        final = _wait_remote(
            remote,
            action="observe_arc_final",
            payload={
                "campaign": "rp1",
                "arc_id": arc_id,
                "seeds": arc["seed_plan"],
            },
            timeout_seconds=settings.worker_timeout_seconds,
            poll_interval_seconds=settings.poll_interval_seconds,
            monotonic=monotonic,
            sleep=sleep,
            journal=journal,
            journal_fields={"arc_id": arc_id, "phase": "arc_final"},
        )
        final_observed = final.get("observed")
        if not isinstance(final_observed, dict):
            raise ExecutionContractError(f"{arc_id}: final observation missing")
        final_citations = (
            _citations(final, f"{arc_id} final")
            if arc["seed_plan"]
            else _no_seed_citations(final, f"{arc_id} final")
        )
        journal.append("arc_final_observed", arc_id=arc_id, observed=final_observed)
        _journal_citations(
            journal, final_citations, arc_id=arc_id, phase="arc_final"
        )
        arc_observed["expected_final"] = {
            **_clone(final_observed),
            "_citations": final_citations,
        }
        all_citations.extend(final_citations)
        observed_arcs[arc_id] = arc_observed

    return {
        "campaign": "rp1",
        "mode": "observed",
        "target": TARGET_SYSTEM,
        "environment": TARGET_ENVIRONMENT,
        "network_performed": True,
        "database_mutated": True,
        "population": validation["population"],
        "evidence_status": "EVIDENCE-LIMITED",
        "evidence_limited_paths": list(campaign_lib.EVIDENCE_LIMITS),
        "evidence_note": (
            "Only durable staging rows cited below are observations; scorer-"
            "designated candidate/reason fields remain EVIDENCE-LIMITED when absent."
        ),
        "citation_contract": {
            "format": ["table", "identity", "query", "observed"],
            "style": "P5a",
        },
        "citations": all_citations,
        "arcs": observed_arcs,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--staging-stdio", action="store_true")
    return parser


def _write_json(value: Mapping[str, Any], path: Path | None) -> None:
    rendered = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path is None:
        print(rendered, end="")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.staging_stdio:
        if any(
            (
                args.plan,
                args.output,
                args.dry_run,
                args.journal,
                args.resume,
            )
        ):
            raise ExecutionContractError(
                "--staging-stdio cannot be combined with campaign flags"
            )
        return staging_stdio(sys.stdin, sys.stdout, env=os.environ)
    if args.plan is None:
        raise ExecutionContractError("--plan is required")
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionContractError(f"could not read plan: {args.plan}") from exc
    if not isinstance(plan, dict):
        raise ExecutionContractError("plan must be a JSON object")
    if args.dry_run:
        if args.resume:
            raise ExecutionContractError("--resume is not valid with --dry-run")
        _write_json(validate_plan(plan), args.output)
        return 0
    if args.journal is None:
        raise ExecutionContractError("--journal is required for execution")

    settings = ExecutionSettings.from_environment(plan, os.environ)
    orchestration = _mapping(plan["orchestration"], "orchestration")
    remote = CommandRemoteStaging(
        target=settings.target,
        environment=settings.environment,
        timeout_seconds=settings.worker_timeout_seconds,
    )
    smtp = SMTPIngress(
        host=_text(orchestration.get("smtp_host"), "smtp_host"),
        port=int(orchestration.get("smtp_port")),
        user=settings.smtp_user,
        password=settings.smtp_password,
        recipient=settings.recipient,
    )
    _write_json(
        execute_plan(
            plan,
            settings=settings,
            remote=remote,
            smtp=smtp,
            journal=ExecutionJournal(args.journal, resume=args.resume),
        ),
        args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
