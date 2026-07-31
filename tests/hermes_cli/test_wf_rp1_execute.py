from __future__ import annotations

import json
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from scripts import wf_rp1_campaign as campaign
from scripts import wf_rp1_execute as execute


def _plan() -> dict[str, Any]:
    return campaign.build_campaign_plan(
        campaign.load_locked_campaign(), remote_db=execute.TARGET_SYSTEM
    )


def _citation(label: str) -> dict[str, Any]:
    return {
        "table": "wf_event",
        "identity": {"label": label},
        "query": "SELECT * FROM wf_event WHERE external_id = ?",
        "observed": {"label": label},
    }


def _ready(label: str, observed: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "ok": True,
        "target": execute.TARGET_SYSTEM,
        "environment": execute.TARGET_ENVIRONMENT,
        "ready": True,
        "durable": {
            "event": True,
            "instance": True,
            "proposal": "not_applicable",
        },
        "observed": observed or {},
        "citations": [_citation(label)],
    }


def test_dry_run_validates_without_network_or_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = tmp_path / "plan.json"
    output = tmp_path / "validated.json"
    plan_path.write_text(json.dumps(_plan()))

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("dry-run attempted external access")

    monkeypatch.setattr("socket.socket.connect", fail)
    monkeypatch.setattr(execute.subprocess, "run", fail)
    monkeypatch.setattr(execute.smtplib, "SMTP", fail)
    assert execute.main(
        ["--plan", str(plan_path), "--output", str(output), "--dry-run"]
    ) == 0
    result = json.loads(output.read_text())
    assert result["valid"] is True
    assert result["network_performed"] is False
    assert result["database_mutated"] is False
    assert result["population"] == {"arcs": 12, "emails": 25, "probes": 1}


def test_plan_gate_rejects_any_non_staging_target() -> None:
    plan = _plan()
    plan["orchestration"]["remote_db"] = "live-pa"
    with pytest.raises(execute.ExecutionContractError, match="pa-workflow-dev"):
        execute.validate_plan(plan)


def test_settings_fail_closed_and_read_credentials_from_named_envs() -> None:
    plan = _plan()
    with pytest.raises(execute.ExecutionContractError, match="pa-workflow-dev staging"):
        execute.ExecutionSettings.from_environment(plan, {})
    settings = execute.ExecutionSettings.from_environment(
        plan,
        {
            "WF_RP1_TARGET": execute.TARGET_SYSTEM,
            "WF_RP1_ENVIRONMENT": execute.TARGET_ENVIRONMENT,
            "E1_STAGING_MAIL_USER": "runner@example.test",
            "E1_DORM1_APP_PASSWORD": "not-printed",
            "WF_RP1_RECIPIENT": "ingress+allied-workflow-staging@example.test",
        },
    )
    assert settings.smtp_user == "runner@example.test"
    assert settings.smtp_password == "not-printed"
    assert (
        settings.recipient == "ingress+allied-workflow-staging@example.test"
    )


def test_remote_command_uses_stdin_and_never_argv_for_payload_or_secrets() -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(argv: list[str], **kwargs: Any) -> Any:
        calls.append((argv, kwargs))
        return execute.subprocess.CompletedProcess(
            argv, 0, json.dumps(_ready("preflight")), ""
        )

    remote = execute.CommandRemoteStaging(
        command="/opt/pcl/bin/wf-rp1-stage",
        target=execute.TARGET_SYSTEM,
        environment=execute.TARGET_ENVIRONMENT,
        timeout_seconds=10,
        runner=runner,
    )
    assert remote.request("preflight", {"secret_like": "payload-only"})["ok"]
    argv, kwargs = calls[0]
    assert argv == ["/opt/pcl/bin/wf-rp1-stage"]
    assert "payload-only" not in argv
    envelope = json.loads(kwargs["input"])
    assert envelope["target"] == execute.TARGET_SYSTEM
    assert envelope["payload"] == {"secret_like": "payload-only"}
    with pytest.raises(execute.ExecutionContractError, match="without arguments"):
        execute.CommandRemoteStaging(
            command="/opt/stage --password leaked",
            target=execute.TARGET_SYSTEM,
            environment=execute.TARGET_ENVIRONMENT,
            timeout_seconds=10,
        )


def test_smtp_message_has_plus_sender_signature_and_thread_headers() -> None:
    a05 = next(arc for arc in _plan()["arcs"] if arc["id"] == "RP1-A05")
    message, sender = execute._email_message(
        a05["emails"][1],
        arc_id="RP1-A05",
        sender_base="dorm1@example.test",
        recipient="workflow+allied-workflow-staging@example.test",
    )
    assert isinstance(message, EmailMessage)
    assert sender == "dorm1+rp1-a05@example.test"
    assert message["From"] == (
        f"{a05['emails'][1]['from_display']} "
        "<dorm1+rp1-a05@example.test>"
    )
    assert message["Message-ID"] == "<rp1-a05-forwarded-0501@rp1.synthetic.test>"
    assert message["In-Reply-To"] == "<rp1-a05-original-0501@rp1.synthetic.test>"
    assert message["References"] == "<rp1-a05-original-0501@rp1.synthetic.test>"
    assert message.get_content().endswith(
        f"{a05['emails'][1]['from_display']} "
        "<dorm1+rp1-a05@example.test>\n"
    )


class FakeSMTPIngress:
    def __init__(self) -> None:
        self.sent: list[tuple[EmailMessage, str]] = []

    def send(self, message: EmailMessage, envelope_sender: str) -> None:
        self.sent.append((message, envelope_sender))


class FakeRemote:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.email_attempts: dict[str, int] = {}
        self.locked = campaign.load_locked_campaign()

    def request(self, action: str, payload: Any) -> dict[str, Any]:
        payload = dict(payload)
        self.calls.append((action, payload))
        if action == "observe_email":
            logical_id = payload["logical_message_id"]
            count = self.email_attempts.get(logical_id, 0)
            self.email_attempts[logical_id] = count + 1
            if count == 0:
                response = _ready(logical_id)
                response["ready"] = False
                response["durable"]["event"] = False
                return response
            for arc in self.locked.arcs:
                for email in arc["emails"]:
                    if email["message_id"] == logical_id:
                        return _ready(logical_id, email["answer_key"])
            raise AssertionError(logical_id)
        if action == "execute_state_poll":
            return _ready("state-poll", payload["probe"]["expected"])
        if action == "observe_arc_final":
            locked_arc = next(
                arc for arc in self.locked.arcs if arc["id"] == payload["arc_id"]
            )
            return _ready(f"{payload['arc_id']}-final", locked_arc["expected_final"])
        return _ready(action)


def test_execute_uses_smtp_waits_and_runs_one_declared_probe() -> None:
    plan = _plan()
    settings = execute.ExecutionSettings(
        target=execute.TARGET_SYSTEM,
        environment=execute.TARGET_ENVIRONMENT,
        recipient="workflow+allied-workflow-staging@example.test",
        smtp_user="dorm1@example.test",
        smtp_password="never-output",
        poll_interval_seconds=0.01,
        ingress_timeout_seconds=5,
        worker_timeout_seconds=5,
        pacing_seconds=0.01,
    )
    remote = FakeRemote()
    smtp = FakeSMTPIngress()
    clock = iter(range(10000))
    sleeps: list[float] = []
    observed = execute.execute_plan(
        plan,
        settings=settings,
        remote=remote,
        smtp=smtp,  # type: ignore[arg-type]
        sleep=sleeps.append,
        monotonic=lambda: float(next(clock)),
    )
    assert len(smtp.sent) == 25
    actions = [action for action, _payload in remote.calls]
    assert actions.count("seed_arc") == 12
    assert actions.count("observe_email") == 50
    assert actions.count("execute_state_poll") == 1
    probe_call = remote.calls[actions.index("execute_state_poll")][1]
    assert probe_call["arc_id"] == "RP1-A08"
    assert probe_call["probe"]["after_email_step"] == 2
    assert not any(action in {"ingest_event", "wf_event"} for action in actions)
    assert observed["evidence_status"] == "EVIDENCE-LIMITED"
    assert observed["evidence_limited_paths"] == list(campaign.EVIDENCE_LIMITS)
    assert len(observed["arcs"]) == 12
    assert observed["arcs"]["RP1-A08"]["state_probes"][0]["verdict"] == "mismatch"
    assert all(
        list(citation) == ["table", "identity", "query", "observed"]
        for citation in observed["citations"]
    )
    assert "never-output" not in json.dumps(observed)
    assert sleeps
    assert campaign.score_campaign(campaign.load_locked_campaign(), observed)[
        "verdict"
    ] == "pass"


def test_missing_citations_fail_closed() -> None:
    response = _ready("x")
    response["citations"] = []
    with pytest.raises(execute.ExecutionContractError, match="missing P5a citations"):
        execute._citations(response, "test")
