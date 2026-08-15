from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
DEPLOY_ROOT = ROOT / "deploy" / "tgg" / "christopher"
VERIFY = DEPLOY_ROOT / "scripts" / "verify_runtime.sh"


def _status(
    *,
    state: str,
    enabled: bool,
    retention_held: int = 0,
    retention_quarantined: int = 0,
    retention_quarantine_status: dict[str, int] | None = None,
    retention_quarantine_message_ids: list[str] | None = None,
    retention_hold: str | None = None,
    **overrides: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "state": state,
        "processing_enabled": enabled,
        "config_enabled": enabled,
        "gate_enabled": enabled,
        "retention_held": retention_held,
        "retention_quarantined": retention_quarantined,
        "retention_quarantine_status": (
            retention_quarantine_status
            if retention_quarantine_status is not None
            else ({"quarantined": retention_quarantined} if retention_quarantined else {})
        ),
        "retention_quarantine_message_ids": (
            retention_quarantine_message_ids
            if retention_quarantine_message_ids is not None
            else [f"quarantined-{index}" for index in range(retention_quarantined)]
        ),
        "retention_hold": retention_hold,
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    ("enabled", "gate_enabled", "status", "expected_ok"),
    [
        pytest.param(True, True, _status(state="running", enabled=True), True, id="running"),
        pytest.param(
            True,
            True,
            _status(
                state="running",
                enabled=True,
                retention_quarantined=1,
                retention_quarantine_status={"quarantined": 1},
            ),
            True,
            id="running-with-queryable-quarantine",
        ),
        pytest.param(
            True,
            True,
            _status(
                state="running",
                enabled=True,
                retention_quarantined=1,
                retention_quarantine_status={},
            ),
            False,
            id="quarantine-count-status-must-match",
        ),
        pytest.param(
            False, False, _status(state="standby", enabled=False), True, id="standby"
        ),
        pytest.param(
            False,
            False,
            _status(
                state="standby",
                enabled=False,
                retention_held=2,
                retention_hold="historical media retention holds awaiting reconciliation",
            ),
            True,
            id="standby-preserves-historical-retention-holds",
        ),
        pytest.param(
            True,
            True,
            _status(
                state="held-pending",
                enabled=True,
                retention_held=1,
                retention_hold="mandatory media has no capture path",
            ),
            True,
            id="held-pending-with-retention-evidence",
        ),
        pytest.param(
            True,
            True,
            _status(
                state="held-pending",
                enabled=True,
                retention_hold="mandatory media has no capture path",
            ),
            False,
            id="held-pending-without-held-count",
        ),
        pytest.param(
            True,
            True,
            _status(state="held-pending", enabled=True, retention_held=1),
            False,
            id="held-pending-without-error",
        ),
        pytest.param(
            False,
            False,
            _status(
                state="held-pending",
                enabled=False,
                retention_held=1,
                retention_hold="mandatory media has no capture path",
            ),
            False,
            id="disabled-cannot-be-held-pending",
        ),
        pytest.param(
            True,
            True,
            _status(
                state="running",
                enabled=True,
                retention_held=1,
                retention_hold="mandatory media has no capture path",
            ),
            False,
            id="running-cannot-have-retention-error",
        ),
        pytest.param(
            False,
            False,
            _status(
                state="standby",
                enabled=False,
                processing_enabled=True,
            ),
            False,
            id="status-flags-cannot-contradict-config",
        ),
        pytest.param(
            True,
            False,
            _status(state="running", enabled=True),
            False,
            id="processing-gate-cannot-contradict-config",
        ),
    ],
)
def test_consumer_status_contract(
    tmp_path: Path,
    enabled: bool,
    gate_enabled: bool,
    status: dict[str, object],
    expected_ok: bool,
) -> None:
    config_path = tmp_path / "config.yaml"
    gate_path = tmp_path / "processing-gate.json"
    status_path = tmp_path / "capture-consumer-status.json"
    config_path.write_text(yaml.safe_dump({
        "pa": {
            "enabled": enabled,
            "media_retention": {
                "max_attempts": 5,
                "retry_interval_seconds": 60,
            },
        }
    }))
    gate_path.write_text(json.dumps({"enabled": gate_enabled}))
    status_path.write_text(json.dumps(status))

    result = subprocess.run(
        [
            str(VERIFY),
            "--verify-status-contract",
            str(config_path),
            str(gate_path),
            str(status_path),
        ],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "VERIFY_PYTHON": sys.executable},
    )

    assert (result.returncode == 0) is expected_ok, result.stderr


def test_fatal_held_producer_shape_reports_fatal_message(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    gate_path = tmp_path / "processing-gate.json"
    status_path = tmp_path / "capture-consumer-status.json"
    config_path.write_text(yaml.safe_dump({"pa": {"enabled": True}}))
    gate_path.write_text(json.dumps({"enabled": True}))
    status_path.write_text(
        json.dumps(
            _status(
                state="held",
                enabled=True,
                processing_enabled=False,
                retention_held=1,
                retention_hold="mandatory media has no capture path",
            )
        )
    )

    result = subprocess.run(
        [
            str(VERIFY),
            "--verify-status-contract",
            str(config_path),
            str(gate_path),
            str(status_path),
        ],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "VERIFY_PYTHON": sys.executable},
    )

    assert result.returncode != 0
    assert "fatal consumer state: held" in result.stderr


@pytest.mark.parametrize(
    ("pending_chat", "updated_at", "expected_ok"),
    [
        pytest.param("historical-ops@g.us", "2026-08-08T09:14:06+00:00", True, id="historical-non-management-backlog-is-inert"),
        pytest.param("canary-management@g.us", "2026-08-08T09:14:06+00:00", False, id="pending-canary-work-fails-closed"),
        pytest.param("historical-ops@g.us", "2026-08-11T03:00:01+00:00", False, id="post-standby-mutation-fails-closed"),
    ],
)
def test_standby_inbox_contract_preserves_only_inert_historical_backlog(
    tmp_path: Path, pending_chat: str, updated_at: str, expected_ok: bool,
) -> None:
    constitution_path = tmp_path / "constitution.yaml"
    constitution_path.write_text(yaml.safe_dump({"selectors": [{
        "job_type": "tgg_management",
        "match": {"source.platform": "whatsapp", "source.chat_id": "canary-management@g.us"},
    }]}))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"pa": {"enabled": False, "constitution_path": str(constitution_path)}}))
    gate_path = tmp_path / "processing-gate.json"
    gate_path.write_text(json.dumps({"enabled": False, "changed_at": "2026-08-11T03:00:00+00:00"}))
    inbox_path = tmp_path / "capture-inbox.db"
    conn = sqlite3.connect(inbox_path)
    try:
        conn.execute("CREATE TABLE ingress_events (chat_id TEXT, status TEXT, updated_at TEXT)")
        conn.execute("INSERT INTO ingress_events VALUES (?, 'pending', ?)", (pending_chat, updated_at))
        conn.commit()
    finally:
        conn.close()
    status_path = tmp_path / "capture-consumer-status.json"
    status_path.write_text(json.dumps({
        **_status(state="standby", enabled=False),
        "source_opened": False,
        "cursor_advanced": False,
        "active_management_chats": [],
        "active_site_chats": [],
        "state_total": 1,
        "inbox": {"pending": 1, "processing": 0, "completed": 0, "skipped": 0, "failed": 0},
    }))
    result = subprocess.run(
        [str(VERIFY), "--verify-standby-inbox-contract", str(config_path), str(gate_path), str(status_path), str(inbox_path)],
        text=True, capture_output=True, check=False, env={**os.environ, "VERIFY_PYTHON": sys.executable},
    )
    assert (result.returncode == 0) is expected_ok, result.stderr


@pytest.mark.parametrize(
    ("initial_offset", "offset", "cursor_updated_at", "expected_ok"),
    [
        pytest.param(100, 100, "2026-08-11T02:59:59+00:00", True, id="virgin-disabled-cursor"),
        pytest.param(100, 200, "2026-08-11T02:59:59+00:00", True, id="historically-advanced-before-disabled-boundary"),
        pytest.param(100, 200, "2026-08-11T03:00:06+00:00", False, id="advanced-after-disabled-boundary"),
        pytest.param(200, 100, "2026-08-11T02:59:59+00:00", False, id="cursor-cannot-move-backwards"),
    ],
)
def test_disabled_cursor_contract_uses_latest_gate_boundary(
    tmp_path: Path,
    initial_offset: int,
    offset: int,
    cursor_updated_at: str,
    expected_ok: bool,
) -> None:
    gate_path = tmp_path / "processing-gate.json"
    gate_path.write_text(json.dumps({
        "enabled": False,
        "changed_at": "2026-08-11T03:00:00+00:00",
    }))
    cursor_path = tmp_path / "capture-cursor.json"
    cursor_path.write_text(json.dumps({
        "initial_offset": initial_offset,
        "offset": offset,
        "updated_at": cursor_updated_at,
    }))
    result = subprocess.run(
        [str(VERIFY), "--verify-disabled-cursor-contract", str(gate_path), str(cursor_path)],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "VERIFY_PYTHON": sys.executable},
    )
    assert (result.returncode == 0) is expected_ok, result.stderr


def test_deploy_selects_canonical_manifest_and_current_units() -> None:
    assert not (ROOT / "pa-agent.manifest.json").exists()

    spec = yaml.safe_load((DEPLOY_ROOT / "client-agent-deployment.yaml").read_text())
    media_retention = spec["spec"]["channels"]["whatsapp"]["mediaRetention"]
    assert media_retention["maxAttempts"] == 5
    assert media_retention["failureDisposition"] == (
        "retry-up-to-max-attempts-then-quarantine-full-envelope-and-"
        "failure-history-and-bypass-for-business-processing"
    )
    assert {"retention_quarantined", "retention_quarantine_status"} <= set(
        spec["spec"]["channels"]["whatsapp"]["consumer"]["statusMediaFields"]
    )
    manifest_ref = spec["spec"]["deploy"]["manifestRef"]
    assert manifest_ref == "deploy/tgg/christopher/pa-agent.hermes.manifest.json"

    manifest = json.loads((ROOT / manifest_ref).read_text())
    bootstrap_hook = manifest["services"][0]["preRestartHooks"][0]["command"]
    assert "CHRISTOPHER_ENGINE_SLOT=gpt-5.6-terra-medium" in bootstrap_hook
    bootstrap = (DEPLOY_ROOT / "scripts" / "bootstrap_runtime.sh").read_text()
    assert 'slot_args=(--slot "$CHRISTOPHER_ENGINE_SLOT")' in bootstrap
    assert '"${slot_args[@]}"' in bootstrap
    assert "christopher-tgg-retention-cleanup.service" in bootstrap
    assert "christopher-tgg-retention-cleanup.timer" in bootstrap
    assert (
        "systemctl enable --now christopher-tgg-retention-cleanup.timer"
        in bootstrap
    )

    deploy_script = (DEPLOY_ROOT / "scripts" / "deploy_runtime.sh").read_text()
    assert 'd["spec"]["deploy"]["manifestRef"]' in deploy_script
    assert '--manifest "$manifest_path"' in deploy_script
    assert "output_quality_eval.py" in deploy_script
    assert '"--trigger",\n            "deploy"' in deploy_script
    assert '"--maker-session-id",' in deploy_script
    assert 'f"deploy:{head_sha}"' in deploy_script
    assert "set +e" in deploy_script
    assert '"evaluator_ok": evaluator_ok' in deploy_script
    assert '"evaluator_ok":%s' in deploy_script

    verify_script = VERIFY.read_text()
    assert 'expected_config["model"] = selected_slot_config["model"]' in verify_script
    assert (
        'expected_config["pa"]["media_retention"] = '
        'selected_slot_config["pa"]["media_retention"]'
        in verify_script
    )
    assert "for _ in range(30):" in verify_script
    assert 'current_status["active_management_chats"]' in verify_script
    assert 'set(datasets) == {"cases", "documents", "media"}' in verify_script
    assert 'for name in ("cases", "documents", "media")' in verify_script
    assert "systemctl is-active --quiet systems-papercut-labs.service" in verify_script
    assert "christopher-tgg-systems.service" not in verify_script
    assert "singpass-pair-server.service" not in verify_script
    assert (
        'APP_ROOT="$APP_ROOT" VERIFY_PYTHON="$APP_ROOT/.venv/bin/python" \\\n'
        '  "$0" --verify-status-contract \\\n'
        '  "$HERMES_HOME/config.yaml" \\\n'
        '  "$RUNTIME_ROOT/processing-gate.json" \\\n'
        '  "$RUNTIME_ROOT/capture-consumer-status.json"'
    ) in verify_script
    main_pid_offset = verify_script.index(
        'main_pid="$(systemctl show -p MainPID --value '
        'christopher-tgg-hermes.service)"'
    )
    assert verify_script.index(
        'if python3 - "$RUNTIME_ROOT/capture-consumer-status.json"'
    ) < main_pid_offset
    assert verify_script.index("exit 34") < main_pid_offset
    assert '"state": status["state"]' in verify_script
    assert '"retention_held": status["retention_held"]' in verify_script
    assert '"retention_quarantined": status["retention_quarantined"]' in verify_script
    assert '"retention_quarantine_status": status["retention_quarantine_status"]' in verify_script
    assert '"retention_hold": status["retention_hold"]' in verify_script
    assert 'required_message_columns.issubset(rows[0])' in verify_script
    assert 'manifest["corpus"]' not in verify_script
    assert 'row["cases"] == systems["canonical_cases"]' not in verify_script
    assert 'row["messages"] >= systems["message_rows"]' not in verify_script

    manifest = json.loads((ROOT / manifest_ref).read_text())
    assert [service["name"] for service in manifest["services"]] == [
        "christopher-tgg-hermes.service"
    ]
    manifest_text = json.dumps(manifest)
    assert "christopher-tgg-systems.service" not in manifest_text
    assert "singpass-pair-server.service" not in manifest_text
    assert manifest["verifyHooks"] == [
        {
            "name": "christopher-hermes-full-consumer-check",
            "command": (
                "/home/pclaw/apps/hermes-pcl/deploy/tgg/christopher/"
                "scripts/verify_runtime.sh --full"
            ),
            "timeoutMs": 600000,
        }
    ]


def _run_preserve_env_key(
    current: Path,
    staged: Path,
    *,
    key: str = "CHRISTOPHER_TGG_PS_SERVICE_TOKEN",
    allow_staged_fallback: bool = False,
) -> subprocess.CompletedProcess[str]:
    args = [
        sys.executable,
        str(DEPLOY_ROOT / "scripts" / "preserve_env_key.py"),
        str(current),
        str(staged),
        key,
    ]
    if allow_staged_fallback:
        args.append("--allow-staged-fallback")
    return subprocess.run(
        args,
        text=True,
        capture_output=True,
        check=False,
    )


def test_secret_refresh_preserves_migrated_christopher_token(tmp_path: Path) -> None:
    current = tmp_path / "current.env"
    staged = tmp_path / "staged.env"
    current.write_text(
        'OPENAI_API_KEY="old"\n'
        'CHRISTOPHER_TGG_PS_SERVICE_TOKEN="christopher-scoped"\n',
        encoding="utf-8",
    )
    staged.write_text('OPENAI_API_KEY="fresh"\n', encoding="utf-8")

    result = _run_preserve_env_key(current, staged)

    assert result.returncode == 0, result.stderr
    assert staged.read_text(encoding="utf-8").splitlines() == [
        'OPENAI_API_KEY="fresh"',
        'CHRISTOPHER_TGG_PS_SERVICE_TOKEN="christopher-scoped"',
    ]


def test_secret_refresh_prefers_live_openai_key_over_staged_fallback(
    tmp_path: Path,
) -> None:
    current = tmp_path / "current.env"
    staged = tmp_path / "staged.env"
    current.write_text('OPENAI_API_KEY="live"\n', encoding="utf-8")
    staged.write_text(
        'OPENAI_API_KEY="studio-fallback"\nTGG_DEMO_MANAGEMENT_ONLY=true\n',
        encoding="utf-8",
    )

    result = _run_preserve_env_key(
        current,
        staged,
        key="OPENAI_API_KEY",
        allow_staged_fallback=True,
    )

    assert result.returncode == 0, result.stderr
    assert staged.read_text(encoding="utf-8").splitlines() == [
        'OPENAI_API_KEY="live"',
        "TGG_DEMO_MANAGEMENT_ONLY=true",
    ]


def test_secret_refresh_keeps_staged_openai_fallback_on_fresh_host(
    tmp_path: Path,
) -> None:
    current = tmp_path / "missing.env"
    staged = tmp_path / "staged.env"
    staged.write_text('OPENAI_API_KEY="studio-fallback"\n', encoding="utf-8")

    result = _run_preserve_env_key(
        current,
        staged,
        key="OPENAI_API_KEY",
        allow_staged_fallback=True,
    )

    assert result.returncode == 0, result.stderr
    assert staged.read_text(encoding="utf-8") == 'OPENAI_API_KEY="studio-fallback"\n'


@pytest.mark.parametrize(
    ("current_text", "staged_text", "error"),
    [
        (
            'CHRISTOPHER_TGG_PS_SERVICE_TOKEN="one"\n'
            'CHRISTOPHER_TGG_PS_SERVICE_TOKEN="two"\n',
            'OPENAI_API_KEY="fresh"\n',
            "destination env contains duplicate",
        ),
        (
            'OPENAI_API_KEY="old"\n',
            'OPENAI_API_KEY="fresh"\n'
            'CHRISTOPHER_TGG_PS_SERVICE_TOKEN="studio-value"\n',
            "staged env unexpectedly contains",
        ),
        (
            'export CHRISTOPHER_TGG_PS_SERVICE_TOKEN="old"\n',
            'OPENAI_API_KEY="fresh"\n',
            "destination env contains non-canonical",
        ),
    ],
)
def test_secret_refresh_refuses_unsafe_token_shapes_without_mutating_staged(
    tmp_path: Path, current_text: str, staged_text: str, error: str
) -> None:
    current = tmp_path / "current.env"
    staged = tmp_path / "staged.env"
    current.write_text(current_text, encoding="utf-8")
    staged.write_text(staged_text, encoding="utf-8")
    before = staged.read_bytes()

    result = _run_preserve_env_key(current, staged)

    assert result.returncode != 0
    assert error in result.stderr
    assert staged.read_bytes() == before


@pytest.mark.parametrize("current_exists", [False, True])
def test_secret_refresh_tokenless_state_leaves_staged_byte_identical(
    tmp_path: Path, current_exists: bool
) -> None:
    current = tmp_path / "current.env"
    staged = tmp_path / "staged.env"
    if current_exists:
        current.write_text('OPENAI_API_KEY="old"\n', encoding="utf-8")
    staged.write_text('OPENAI_API_KEY="fresh"\n', encoding="utf-8")
    before = staged.read_bytes()

    result = _run_preserve_env_key(current, staged)

    assert result.returncode == 0, result.stderr
    assert staged.read_bytes() == before


def test_prepare_script_streams_helper_and_cleans_remote_staging() -> None:
    script = (DEPLOY_ROOT / "scripts" / "prepare_host_secrets.sh").read_text()
    assert (
        'OPENAI_API_KEY \\\n'
        '  --allow-staged-fallback < "$SCRIPT_DIR/preserve_env_key.py"'
    ) in script
    assert 'CHRISTOPHER_TGG_PS_SERVICE_TOKEN < "$SCRIPT_DIR/preserve_env_key.py"' in script
    assert "trap cleanup EXIT" in script
    assert "rm -f /root/.pcl-secret-staging/christopher.env" in script
