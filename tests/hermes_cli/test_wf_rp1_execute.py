from __future__ import annotations

import io
import json
import sys
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import wf_rp1_campaign as campaign
from scripts import wf_rp1_execute as execute


def _plan() -> dict[str, Any]:
    return campaign.build_campaign_plan(
        campaign.load_locked_campaign(), remote_db=execute.TARGET_SYSTEM
    )


def _citation(label: str) -> dict[str, Any]:
    return {
        "table": "wf_event",
        "identity": f"wf_event.external_id={label}",
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
        target=execute.TARGET_SYSTEM,
        environment=execute.TARGET_ENVIRONMENT,
        timeout_seconds=10,
        runner=runner,
    )
    assert remote.request("preflight", {"secret_like": "payload-only"})["ok"]
    argv, kwargs = calls[0]
    assert argv == [
        "ssh",
        "-T",
        execute.REMOTE_SSH_TARGET,
        "env",
        f"{execute.STAGING_DB_ENV}={execute.REMOTE_STAGING_DB_PATH}",
        f"{execute.SERVICE_PROOF_ENV}={execute.REMOTE_SERVICE_PROOF_PATH}",
        execute.REMOTE_PYTHON_PATH,
        execute.REMOTE_EXECUTOR_PATH,
        "--staging-stdio",
    ]
    assert "payload-only" not in argv
    envelope = json.loads(kwargs["input"])
    assert envelope["target"] == execute.TARGET_SYSTEM
    assert envelope["payload"] == {"secret_like": "payload-only"}
    with pytest.raises(execute.ExecutionContractError, match="exact"):
        execute.CommandRemoteStaging(
            command=f"{sys.executable} {execute.__file__} --staging-stdio",
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


def test_applied_event_retains_matched_correlation_verdict() -> None:
    assert execute._correlation_verdict(
        {"status": "applied", "matched_task_id": "task-1"}
    ) == "matched"
    assert execute._correlation_verdict(
        {"status": "routed_out", "matched_task_id": None}
    ) == "routed_out"


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

    @staticmethod
    def _extraction_citation() -> dict[str, Any]:
        document = yaml.safe_load(campaign.TEMPLATE_PATH.read_text())
        workflow = document["workflow"]
        return {
            "table": "wf_template",
            "identity": (
                f"wf_template.slug={workflow['id']}@1.email_extraction"
            ),
            "query": "resolve the sole registered email_extraction contract",
            "observed": execute._extraction_contract_evidence(
                workflow["email_extraction"]
            ),
        }

    @staticmethod
    def _template_citation() -> dict[str, Any]:
        document = yaml.safe_load(campaign.TEMPLATE_PATH.read_text())
        workflow = document["workflow"]
        return {
            "table": "wf_template",
            "identity": f"wf_template.slug={workflow['id']}@1",
            "query": "SELECT latest registered workflow template",
            "observed": {"slug": workflow["id"], "version": 1},
        }

    def request(self, action: str, payload: Any) -> dict[str, Any]:
        payload = dict(payload)
        self.calls.append((action, payload))
        if action == "preflight":
            response = {
                **_ready("preflight"),
                "service_identity": execute.SERVICE_NAME,
                "deployed_release": "test-release",
                "watcher_healthy": True,
                "extraction_contract_ready": True,
            }
            response["citations"].extend(
                [self._template_citation(), self._extraction_citation()]
            )
            return response
        if action == "observe_preflight":
            return _ready(
                "preflight-email",
                {
                    "event_type": "pickup_advice",
                    "payload": {"booking_ref": payload["subject_token"]},
                    "corr": {"booking_ref": payload["subject_token"]},
                    "correlation": {"verdict": "unmatched"},
                    "agent_action": {
                        "evidence_status": "EVIDENCE-LIMITED",
                        "reason": "preflight has no proposal",
                    },
                    "received": {
                        "from": payload["expected_from"],
                        "message_id": payload["wire_message_id"],
                        "subject": payload["subject_token"],
                        "x_rp1_token": payload["x_rp1_token"],
                    },
                    "stable_keys": ["message_id", "subject", "x_rp1_token"],
                },
            )
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
                        return _ready(
                            logical_id,
                            {
                                "event_type": email["answer_key"]["event_type"],
                                "payload": {"captured": True},
                                "corr": {"source_message": logical_id},
                                "correlation": {
                                    "verdict": "received",
                                    "target": None,
                                },
                                "agent_action": {
                                    "evidence_status": "EVIDENCE-LIMITED",
                                    "reason": "fake has no durable proposal row",
                                },
                            },
                        )
            raise AssertionError(logical_id)
        if action == "execute_state_poll":
            assert set(payload) == {"entity_key", "request", "result"}
            return _ready(
                "state-poll",
                {
                    "event_type": payload["request"]["field"],
                    "payload": payload["result"],
                    "corr": {"entity_key": payload["entity_key"]},
                    "correlation": {"verdict": "buffered"},
                    "agent_action": {
                        "evidence_status": "EVIDENCE-LIMITED",
                        "reason": "fake has no durable proposal row",
                    },
                    "state_poll": payload["result"],
                },
            )
        if action == "observe_arc_final":
            return _ready(
                f"{payload['arc_id']}-final",
                {
                    "evidence_status": "EVIDENCE-LIMITED",
                    "instances": {
                        seed["entity_key"]: {"state": "observed"}
                        for seed in payload["seeds"]
                    },
                },
            )
        return _ready(action)


def test_execute_uses_smtp_waits_and_runs_one_declared_probe(
    tmp_path: Path,
) -> None:
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
        journal=execute.ExecutionJournal(
            tmp_path / "rp1-journal.jsonl", resume=False
        ),
        sleep=sleeps.append,
        monotonic=lambda: float(next(clock)),
    )
    assert len(smtp.sent) == 26
    actions = [action for action, _payload in remote.calls]
    assert actions.count("seed_arc") == 12
    assert actions.count("observe_email") == 50
    assert actions.count("observe_preflight") == 1
    assert actions.count("execute_state_poll") == 1
    probe_call = remote.calls[actions.index("execute_state_poll")][1]
    assert set(probe_call) == {"entity_key", "request", "result"}
    assert probe_call["entity_key"] == "job:RP1-JOB-0801"
    assert probe_call["request"]["field"] == "gate_in"
    assert not any(action in {"ingest_event", "wf_event"} for action in actions)
    assert observed["evidence_status"] == "EVIDENCE-LIMITED"
    assert observed["evidence_limited_paths"] == list(campaign.EVIDENCE_LIMITS)
    assert len(observed["arcs"]) == 12
    assert observed["arcs"]["RP1-A08"]["state_probes"][0]["event_type"] == "gate_in"
    assert all(
        list(citation) == ["table", "identity", "query", "observed"]
        for citation in observed["citations"]
    )
    assert "never-output" not in json.dumps(observed)
    assert sleeps
    journal_entries = [
        json.loads(line)
        for line in (tmp_path / "rp1-journal.jsonl").read_text().splitlines()
    ]
    sent_entries = [
        entry for entry in journal_entries if entry["event"] == "email_sent"
    ]
    assert len(sent_entries) == 25
    first_campaign_message = smtp.sent[1][0]
    assert sent_entries[0]["wire_body_sha256"] == execute._sha256_text(
        first_campaign_message.get_body().get_content()
    )


def test_execute_refuses_remote_preflight_without_extraction_contract(
    tmp_path: Path,
) -> None:
    class MissingContractRemote(FakeRemote):
        def request(
            self, action: str, payload: dict[str, object]
        ) -> dict[str, object]:
            response = super().request(action, payload)
            if action == "preflight":
                response["extraction_contract_ready"] = False
            return response

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
    smtp = FakeSMTPIngress()
    with pytest.raises(
        execute.ExecutionContractError,
        match="extraction contract",
    ):
        execute.execute_plan(
            _plan(),
            settings=settings,
            remote=MissingContractRemote(),
            smtp=smtp,  # type: ignore[arg-type]
            journal=execute.ExecutionJournal(
                tmp_path / "rp1-journal.jsonl", resume=False
            ),
        )
    assert smtp.sent == []


def test_execute_refuses_uncited_extraction_contract(
    tmp_path: Path,
) -> None:
    class UncitedContractRemote(FakeRemote):
        def request(
            self, action: str, payload: dict[str, object]
        ) -> dict[str, object]:
            response = super().request(action, payload)
            if action == "preflight":
                response["citations"] = [
                    citation
                    for citation in response["citations"]
                    if not citation["identity"].endswith(".email_extraction")
                ]
            return response

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
    smtp = FakeSMTPIngress()
    with pytest.raises(
        execute.ExecutionContractError,
        match="did not cite the exact registered",
    ):
        execute.execute_plan(
            _plan(),
            settings=settings,
            remote=UncitedContractRemote(),
            smtp=smtp,  # type: ignore[arg-type]
            journal=execute.ExecutionJournal(
                tmp_path / "rp1-journal.jsonl", resume=False
            ),
        )
    assert smtp.sent == []


def test_missing_citations_fail_closed() -> None:
    response = _ready("x")
    response["citations"] = []
    with pytest.raises(execute.ExecutionContractError, match="missing P5a citations"):
        execute._citations(response, "test")


def test_citations_accept_exact_schema_after_sorted_json_transport() -> None:
    response = json.loads(json.dumps(_ready("x"), sort_keys=True))

    citations = execute._citations(response, "test")

    assert set(citations[0]) == {"table", "identity", "query", "observed"}


@pytest.mark.parametrize("field", ["table", "identity", "query", "observed"])
def test_citations_reject_missing_schema_field(field: str) -> None:
    response = _ready("x")
    del response["citations"][0][field]

    with pytest.raises(execute.ExecutionContractError, match="invalid P5a citation"):
        execute._citations(response, "test")


def test_citations_reject_extra_schema_field() -> None:
    response = _ready("x")
    response["citations"][0]["extra"] = True

    with pytest.raises(execute.ExecutionContractError, match="invalid P5a citation"):
        execute._citations(response, "test")


def test_timeout_is_journaled_before_failure(tmp_path: Path) -> None:
    class NeverReady:
        def request(self, action: str, payload: Any) -> dict[str, Any]:
            return _ready("pending") | {"ready": False}

    journal = execute.ExecutionJournal(tmp_path / "timeout.jsonl", resume=False)
    clock = iter((0.0, 2.0))
    with pytest.raises(execute.ExecutionContractError, match="timed out"):
        execute._wait_remote(
            NeverReady(),
            action="observe_email",
            payload={},
            timeout_seconds=1,
            poll_interval_seconds=0.01,
            monotonic=lambda: next(clock),
            sleep=lambda _seconds: None,
            journal=journal,
            journal_fields={"logical_message_id": "message@example.test"},
        )
    assert journal.entries[-1]["event"] == "timeout"
    assert (
        journal.entries[-1]["logical_message_id"] == "message@example.test"
    )


def test_resume_skips_every_already_observed_campaign_email(tmp_path: Path) -> None:
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
    journal_path = tmp_path / "resume.jsonl"
    first_remote, first_smtp = FakeRemote(), FakeSMTPIngress()
    clock = iter(range(100000))
    execute.execute_plan(
        plan,
        settings=settings,
        remote=first_remote,
        smtp=first_smtp,  # type: ignore[arg-type]
        journal=execute.ExecutionJournal(journal_path, resume=False),
        sleep=lambda _seconds: None,
        monotonic=lambda: float(next(clock)),
    )
    resumed_remote, resumed_smtp = FakeRemote(), FakeSMTPIngress()
    execute.execute_plan(
        plan,
        settings=settings,
        remote=resumed_remote,
        smtp=resumed_smtp,  # type: ignore[arg-type]
        journal=execute.ExecutionJournal(journal_path, resume=True),
        sleep=lambda _seconds: None,
        monotonic=lambda: float(next(clock)),
    )
    assert len(first_smtp.sent) == 26
    assert len(resumed_smtp.sent) == 1  # fresh loopback preflight only
    assert not any(
        action == "observe_email" for action, _payload in resumed_remote.calls
    )


def test_scorer_distinguishes_evidence_limited_from_real_mismatch() -> None:
    answer = {
        "event_type": "pickup_advice",
        "payload": {"booking_ref": "BK-1"},
        "corr": {"booking_ref": "BK-1"},
        "correlation": {
            "verdict": "matched",
            "target": "job:1",
            "candidate_count": 2,
        },
        "agent_action": {"kind": "resume"},
    }
    evidence_limited = {
        "event_type": "pickup_advice",
        "payload": {"booking_ref": "BK-1"},
        "corr": {"booking_ref": "BK-1"},
        "correlation": {"verdict": "matched", "target": "job:1"},
        "agent_action": {"kind": "resume"},
    }
    assert campaign.score_answer_key(answer, evidence_limited)["status"] == (
        "evidence-limited"
    )
    mismatch = json.loads(json.dumps(evidence_limited))
    mismatch["payload"]["booking_ref"] = "BK-WRONG"
    assert campaign.score_answer_key(answer, mismatch)["status"] == "fail"


def _service_proof(tmp_path: Path, db_path: Path) -> Path:
    proof = tmp_path / "service-proof.json"
    proof.write_text(
        json.dumps(
            {
                "target": execute.TARGET_SYSTEM,
                "environment": execute.TARGET_ENVIRONMENT,
                "service": execute.SERVICE_NAME,
                "database": str(db_path.resolve()),
                "deployed_release": "test-release-123",
                "executor_sha256": execute.hashlib.sha256(
                    Path(execute.__file__).read_bytes()
                ).hexdigest(),
            }
        )
    )
    return proof


def test_staging_helper_uses_real_workflow_primitives(tmp_path: Path) -> None:
    db_path = tmp_path / "workflow.db"
    helper = execute.StagingHelper(db_path, _service_proof(tmp_path, db_path))
    plan = _plan()
    a08 = next(arc for arc in plan["arcs"] if arc["id"] == "RP1-A08")
    try:
        preflight = helper.handle("preflight", {})
        assert preflight["ready"] is True
        assert preflight["service_identity"] == execute.SERVICE_NAME
        assert preflight["watcher_healthy"] is True
        assert preflight["extraction_contract_ready"] is True
        extraction_citation = next(
            citation
            for citation in preflight["citations"]
            if str(citation["identity"]).endswith(".email_extraction")
        )
        assert extraction_citation["observed"]["schema"] == (
            "synthetic-freight-email-event-v1"
        )
        assert extraction_citation["observed"]["correlation_keys"] == [
            "container_no",
            "job_no",
            "booking_ref",
        ]

        seeded = helper.handle(
            "seed_arc",
            {"arc_id": "RP1-A08", "seeds": a08["seed_plan"]},
        )
        assert seeded["durable"]["instance"] is True
        seed_observed = helper.handle(
            "observe_seed",
            {"arc_id": "RP1-A08", "seeds": a08["seed_plan"]},
        )
        assert seed_observed["ready"] is True
        row = helper.conn.execute(
            "SELECT state FROM wf_instance WHERE entity_key=?",
            ("job:RP1-JOB-0801",),
        ).fetchone()
        assert row is not None

        token = "rp1-loopback-token"
        wire_id = "<rp1-loopback@rp1.synthetic.test>"
        event_id = execute.wf_engine.ingest_event(
            helper.conn,
            source="email",
            external_id=wire_id,
            event_type="pickup_advice",
            payload={
                "sender": "dorm1+rp1-preflight@example.test",
                "message_id": wire_id,
                "subject": f"[{token}] RP1 staging ingress preflight",
                "headers": {"X-RP1-Token": token},
                "booking_ref": token,
            },
            corr={"booking_ref": token},
        )
        assert event_id is not None
        observed_email = helper.handle(
            "observe_preflight",
            {
                "wire_message_id": wire_id,
                "subject_token": token,
                "x_rp1_token": token,
            },
        )
        assert observed_email["observed"]["event_type"] == "pickup_advice"
        assert observed_email["observed"]["received"]["from"] == (
            "dorm1+rp1-preflight@example.test"
        )
        assert observed_email["observed"]["stable_keys"] == [
            "message_id",
            "subject",
            "x_rp1_token",
        ]
        campaign_email = helper.handle(
            "observe_email",
            {
                "wire_message_id": wire_id,
                "subject_token": token,
                "x_rp1_token": token,
            },
        )
        assert set(campaign_email["observed"]) >= {
            "event_type",
            "payload",
            "corr",
            "correlation",
            "agent_action",
        }

        probe = a08["state_probes"][0]
        poll = helper.handle(
            "execute_state_poll",
            {
                "entity_key": probe["entity_key"],
                "request": probe["request"],
                "result": probe["result"],
            },
        )
        assert poll["ready"] is True
        assert poll["observed"]["state_poll"]["gate_in"] is False
        assert "same SQLite board" in poll["observed"]["process_local_isolation"]
        assert execute.wf_watcher.registered_state_probes() == ()

        final = helper.handle(
            "observe_arc_final",
            {"arc_id": "RP1-A08", "seeds": a08["seed_plan"]},
        )
        assert final["ready"] is True
        assert final["observed"]["evidence_status"] == "EVIDENCE-LIMITED"
        assert all(
            isinstance(citation["identity"], str)
            for citation in final["citations"]
        )
    finally:
        helper.close()


def test_staging_observe_email_returns_durable_declared_no_fit(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "workflow.db"
    helper = execute.StagingHelper(db_path, _service_proof(tmp_path, db_path))
    try:
        event_id = execute.wf_engine.ingest_event(
            helper.conn,
            source="email",
            external_id="<noise@rp1.synthetic.test>",
            payload={
                "message_id": "<noise@rp1.synthetic.test>",
                "subject": "[RP1-NOISE] cafeteria menu",
                "body_ref": "/synthetic/noise.txt",
            },
            corr={},
            event_type=None,
        )
        assert event_id is not None
        helper.conn.execute(
            "UPDATE wf_event SET status = 'unmatched', payload = '{}' WHERE id = ?",
            (event_id,),
        )

        response = helper.handle(
            "observe_email",
            {
                "wire_message_id": "<noise@rp1.synthetic.test>",
                "subject_token": "RP1-NOISE",
                "x_rp1_token": None,
            },
        )

        assert response["ready"] is True
        assert response["observed"]["event_type"] is None
        assert response["observed"]["payload"] == {}
        assert response["observed"]["corr"] == {}
        assert (
            response["observed"]["extraction_disposition"]
            == "declared-no-fit"
        )
        assert execute._citations(response, "declared no-fit")
    finally:
        helper.close()


def test_staging_observe_email_distinguishes_extraction_boundary_failure(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "workflow.db"
    helper = execute.StagingHelper(db_path, _service_proof(tmp_path, db_path))
    try:
        event_id = execute.wf_engine.ingest_event(
            helper.conn,
            source="email",
            external_id="<broken@rp1.synthetic.test>",
            payload={
                "message_id": "<broken@rp1.synthetic.test>",
                "subject": "[RP1-BROKEN] extractor failed",
                "body_ref": "/synthetic/broken.txt",
            },
            corr={},
            event_type=None,
        )
        assert event_id is not None
        helper.conn.execute(
            "UPDATE wf_event SET status = 'needs_review' WHERE id = ?",
            (event_id,),
        )

        response = helper.handle(
            "observe_email",
            {
                "wire_message_id": "<broken@rp1.synthetic.test>",
                "subject_token": "RP1-BROKEN",
                "x_rp1_token": None,
            },
        )

        assert response["ready"] is True
        assert response["observed"]["event_type"] is None
        assert response["observed"]["extraction_disposition"] == "boundary-failure"
        score = campaign.score_answer_key(
            {"event_type": "pickup_advice", "payload": {}, "corr": {}},
            response["observed"],
        )
        assert "engine-defect" in score["miss_taxonomy"]
        assert score["extraction_disposition"] == "boundary-failure"
    finally:
        helper.close()


def test_staging_preflight_refuses_ambiguous_extraction_contracts(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "workflow.db"
    helper = execute.StagingHelper(db_path, _service_proof(tmp_path, db_path))
    conflicting = {
        "id": "conflicting-email-flow",
        "entity": "case",
        "correlation_keys": ["case_ref"],
        "disambiguators": [],
        "create_on": [],
        "email_extraction": {
            "schema": "conflicting-email-v1",
            "instruction": "Extract a conflicting fixture event.",
        },
        "steps": [{"key": "start"}],
    }
    try:
        execute.wf_engine.register_template(helper.conn, conflicting)
        with pytest.raises(
            execute.ExecutionContractError,
            match="email_extraction contract is absent or ambiguous",
        ):
            helper.handle("preflight", {})
    finally:
        helper.close()


def test_staging_stdio_enforces_schema_and_returns_real_preflight(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "stdio.db"
    proof = _service_proof(tmp_path, db_path)
    request = {
        "protocol": "wf-rp1-staging-v1",
        "target": execute.TARGET_SYSTEM,
        "environment": execute.TARGET_ENVIRONMENT,
        "action": "preflight",
        "payload": {},
    }
    stdout = io.StringIO()
    assert execute.staging_stdio(
        io.StringIO(json.dumps(request)),
        stdout,
        env={
            execute.STAGING_DB_ENV: str(db_path),
            execute.SERVICE_PROOF_ENV: str(proof),
        },
    ) == 0
    response = json.loads(stdout.getvalue())
    assert response["ok"] is True
    assert response["deployed_release"] == "test-release-123"
    assert execute._citations(response, "preflight")


def test_remote_client_rejects_local_helper_even_when_shipped() -> None:
    with pytest.raises(execute.ExecutionContractError, match="SSH helper"):
        execute.CommandRemoteStaging(
            command=f"{sys.executable} {execute.__file__} --staging-stdio",
            target=execute.TARGET_SYSTEM,
            environment=execute.TARGET_ENVIRONMENT,
            timeout_seconds=20,
        )
