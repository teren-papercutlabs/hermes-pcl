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
import subprocess
import sys
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import wf_rp1_campaign as campaign_lib


TARGET_SYSTEM = "pa-workflow-dev"
TARGET_ENVIRONMENT = "staging"
STAGING_COMMAND_ENV = "WF_RP1_STAGING_COMMAND"
RECIPIENT_ENV = "WF_RP1_RECIPIENT"
_REQUIRED_DURABLE = ("event", "instance", "proposal")


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
        if "+allied-workflow-staging" not in recipient_local:
            raise ExecutionContractError(
                "recipient must use the +allied-workflow-staging mailbox"
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


class CommandRemoteStaging:
    """JSON-over-stdin staging boundary; command arguments are forbidden."""

    def __init__(
        self,
        *,
        command: str,
        target: str,
        environment: str,
        timeout_seconds: float,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        argv = shlex.split(command)
        if len(argv) != 1:
            raise ExecutionContractError(
                f"{STAGING_COMMAND_ENV} must name one executable without arguments"
            )
        self._argv = argv
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
        for planned_probe, locked_probe in zip(probes, ordered, strict=True):
            for key in ("step", "after_email_step", "source", "entity_key", "request"):
                if planned_probe.get(key) != locked_probe.get(key):
                    raise ExecutionContractError(
                        f"{arc_id}: state probe {key} changed"
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


def _citations(response: Mapping[str, Any], label: str) -> list[dict[str, Any]]:
    citations = response.get("citations")
    if not isinstance(citations, list) or not citations:
        raise ExecutionContractError(f"{label}: missing P5a citations")
    for citation in citations:
        if not isinstance(citation, dict) or list(citation) != [
            "table",
            "identity",
            "query",
            "observed",
        ]:
            raise ExecutionContractError(f"{label}: invalid P5a citation shape")
    return _clone(citations)


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
) -> dict[str, Any]:
    deadline = monotonic() + timeout_seconds
    while True:
        response = remote.request(action, payload)
        if _durable_ready(response):
            return response
        if monotonic() >= deadline:
            raise ExecutionContractError(
                f"timed out waiting for durable {action} state"
            )
        sleep(poll_interval_seconds)


def execute_plan(
    plan: Mapping[str, Any],
    *,
    settings: ExecutionSettings,
    remote: RemoteStaging,
    smtp: SMTPIngress,
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
    all_citations = _citations(preflight, "preflight")
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
                payload={"campaign": "rp1", "arc_id": arc_id},
                timeout_seconds=settings.worker_timeout_seconds,
                poll_interval_seconds=settings.poll_interval_seconds,
                monotonic=monotonic,
                sleep=sleep,
            )
        all_citations.extend(_citations(seed, f"{arc_id} seed"))
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
            smtp.send(message, sender)
            logical_id = planned_email["logical_message_id"]
            response = _wait_remote(
                remote,
                action="observe_email",
                payload={
                    "campaign": "rp1",
                    "arc_id": arc_id,
                    "logical_message_id": logical_id,
                    "wire_message_id": planned_email["wire_message_id"],
                },
                timeout_seconds=settings.ingress_timeout_seconds,
                poll_interval_seconds=settings.poll_interval_seconds,
                monotonic=monotonic,
                sleep=sleep,
            )
            observed = response.get("observed")
            if not isinstance(observed, dict):
                raise ExecutionContractError(
                    f"{arc_id} {logical_id}: observed state missing"
                )
            citations = _citations(response, f"{arc_id} {logical_id}")
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
                    payload={"campaign": "rp1", "arc_id": arc_id, "probe": probe},
                    timeout_seconds=settings.worker_timeout_seconds,
                    poll_interval_seconds=settings.poll_interval_seconds,
                    monotonic=monotonic,
                    sleep=sleep,
                )
                probe_observed = probe_response.get("observed")
                if not isinstance(probe_observed, dict):
                    raise ExecutionContractError(
                        f"{arc_id}: state_poll observation missing"
                    )
                probe_citations = _citations(
                    probe_response, f"{arc_id} state_poll"
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
            payload={"campaign": "rp1", "arc_id": arc_id},
            timeout_seconds=settings.worker_timeout_seconds,
            poll_interval_seconds=settings.poll_interval_seconds,
            monotonic=monotonic,
            sleep=sleep,
        )
        final_observed = final.get("observed")
        if not isinstance(final_observed, dict):
            raise ExecutionContractError(f"{arc_id}: final observation missing")
        final_citations = _citations(final, f"{arc_id} final")
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
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
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
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionContractError(f"could not read plan: {args.plan}") from exc
    if not isinstance(plan, dict):
        raise ExecutionContractError("plan must be a JSON object")
    if args.dry_run:
        _write_json(validate_plan(plan), args.output)
        return 0

    settings = ExecutionSettings.from_environment(plan, os.environ)
    orchestration = _mapping(plan["orchestration"], "orchestration")
    command = os.environ.get(STAGING_COMMAND_ENV, "")
    if not command:
        raise ExecutionContractError(f"{STAGING_COMMAND_ENV} is required")
    remote = CommandRemoteStaging(
        command=command,
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
        execute_plan(plan, settings=settings, remote=remote, smtp=smtp),
        args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
