"""Workflow-aware dispatcher and stage-turn contract tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from hermes_cli import kanban_db as kb
from hermes_cli import wf_engine


def _workflow(conn, *, runtime=321):
    spec = {
        "id": "dispatcher-flow",
        "correlation_keys": ["ref"],
        "create_on": [{"type": "start"}],
        "steps": [
            {
                "key": "one",
                "turn": {
                    "brief": "synthetic-stage",
                    "max_runtime_seconds": runtime,
                },
                "advance_to": "two",
            },
            {
                "key": "two",
                "turn": {
                    "brief": "synthetic-stage",
                    "max_runtime_seconds": runtime + 1,
                },
                "advance_to": "three",
            },
            {
                "key": "three",
                "turn": {"brief": "synthetic-stage"},
                "advance_to": "done",
            },
            {"key": "done"},
        ],
    }
    template_id, _ = wf_engine.register_template(conn, spec)
    event_id = wf_engine.ingest_event(
        conn,
        source="synthetic",
        external_id="start-1",
        payload={"safe": "structured"},
        corr={"ref": "entity-1"},
        event_type="start",
    )
    task_id = wf_engine.create_instance(
        conn,
        template_id=template_id,
        entity_key="entity-1",
        corr={"ref": "entity-1"},
        vars={"known": True},
        source_event_id=event_id,
    )
    conn.execute(
        "UPDATE tasks SET assignee = 'some-profile' WHERE id = ?",
        (task_id,),
    )
    return task_id, event_id


def test_workflow_dispatch_stamps_step_runtime_and_preserves_nonworkflow(
    tmp_path, monkeypatch, all_assignees_spawnable,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    conn = kb.connect()
    seen = []
    try:
        workflow_id, _ = _workflow(conn)
        normal_id = kb.create_task(
            conn,
            title="ordinary task",
            assignee="some-profile",
            max_runtime_seconds=99,
        )

        def spawn(task, workspace, board=None):
            seen.append((task, workspace, board))
            return None

        result = kb.dispatch_once(conn, spawn_fn=spawn, board="default")
        assert {item[0] for item in result.spawned} == {workflow_id, normal_id}

        workflow_task = next(task for task, _, _ in seen if task.id == workflow_id)
        normal_task = next(task for task, _, _ in seen if task.id == normal_id)
        assert workflow_task.max_runtime_seconds == 321
        assert normal_task.max_runtime_seconds == 99

        run = conn.execute(
            "SELECT step_key, max_runtime_seconds FROM task_runs WHERE task_id = ?",
            (workflow_id,),
        ).fetchone()
        assert tuple(run) == ("one", 321)
    finally:
        conn.close()


def test_default_spawn_uses_ids_only_and_workflow_worker_skill(
    tmp_path, monkeypatch,
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    conn = kb.connect()
    captured = {}
    try:
        task_id, event_id = _workflow(conn)
        task = kb.get_task(conn, task_id)
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
        monkeypatch.setattr(
            "hermes_cli.profiles.resolve_profile_env",
            lambda _profile: str(home / "profiles" / "some-profile"),
        )

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs["env"]
            return SimpleNamespace(pid=4242)

        monkeypatch.setattr("subprocess.Popen", fake_popen)
        assert kb._default_spawn(task, str(workspace)) == 4242

        cmd = captured["cmd"]
        prompt = cmd[cmd.index("-q") + 1]
        assert prompt == (
            f"advance workflow instance {task_id} — step one, event {event_id}"
        )
        assert "structured" not in prompt
        assert "workflow-worker" in cmd
        assert "kanban-worker" not in cmd
        assert captured["env"]["HERMES_WORKFLOW_TASK"] == task_id
        assert captured["env"]["HERMES_WORKFLOW_EVENT_ID"] == str(event_id)
    finally:
        conn.close()


def test_step_context_contains_current_step_and_structured_event_only(tmp_path):
    conn = kb.connect(tmp_path / "board.sqlite")
    try:
        task_id, event_id = _workflow(conn)
        context = wf_engine.context(conn, task_id)
        assert context["step"]["key"] == "one"
        assert context["event"] == {
            "id": event_id,
            "source": "synthetic",
            "event_type": "start",
            "payload": {"safe": "structured"},
            "corr": {"ref": "entity-1"},
        }
        encoded = json.dumps(context)
        assert '"steps"' not in encoded
        assert "dispatcher-flow" in context["template_id"]
    finally:
        conn.close()


def test_each_workflow_stage_closes_its_run_before_requeue(tmp_path):
    conn = kb.connect(tmp_path / "board.sqlite")
    try:
        task_id, event_id = _workflow(conn)

        assert kb.claim_task(conn, task_id) is not None
        wf_engine.advance(conn, task_id, to_step="two", event_id=event_id)
        task = kb.get_task(conn, task_id)
        assert (task.status, task.current_step_key, task.current_run_id) == (
            "ready",
            "two",
            None,
        )

        assert kb.claim_task(conn, task_id) is not None
        wf_engine.advance(conn, task_id, to_step="three", event_id=event_id)
        assert kb.claim_task(conn, task_id) is not None
        wf_engine.advance(conn, task_id, to_step="done", event_id=event_id)

        runs = conn.execute(
            """
            SELECT step_key, outcome, status
              FROM task_runs
             WHERE task_id = ?
             ORDER BY id
            """,
            (task_id,),
        ).fetchall()
        assert [tuple(row) for row in runs] == [
            ("one", "completed", "done"),
            ("two", "completed", "done"),
            ("three", "completed", "done"),
        ]
        assert kb.get_task(conn, task_id).status == "done"
    finally:
        conn.close()


def test_workflow_advance_rejects_target_not_declared_by_current_step(tmp_path):
    conn = kb.connect(tmp_path / "board.sqlite")
    try:
        task_id, event_id = _workflow(conn)
        assert kb.claim_task(conn, task_id) is not None
        try:
            wf_engine.advance_stage_turn(
                conn,
                task_id,
                to_step="one",
                event_id=event_id,
                expected_step="one",
                expected_run_id=kb.get_task(conn, task_id).current_run_id,
            )
        except wf_engine.WorkflowConflictError as exc:
            assert "may advance only to 'two'" in str(exc)
        else:
            raise AssertionError("undeclared self-transition was accepted")
        task = kb.get_task(conn, task_id)
        assert (task.status, task.current_step_key) == ("running", "one")
    finally:
        conn.close()


def test_stale_stage_worker_cannot_settle_successor_run(tmp_path):
    conn = kb.connect(tmp_path / "board.sqlite")
    try:
        task_id, event_id = _workflow(conn)
        first = kb.claim_task(conn, task_id)
        assert first is not None
        wf_engine.advance_stage_turn(
            conn,
            task_id,
            to_step="two",
            event_id=event_id,
            expected_step="one",
            expected_run_id=first.current_run_id,
        )
        second = kb.claim_task(conn, task_id)
        assert second is not None

        try:
            wf_engine.exception(
                conn,
                task_id,
                "late first-stage exception",
                expected_step="one",
                expected_run_id=first.current_run_id,
            )
        except wf_engine.WorkflowConflictError as exc:
            assert "stale workflow step" in str(exc)
        else:
            raise AssertionError("stale stage worker settled the successor run")

        row = conn.execute(
            """
            SELECT t.status, t.current_step_key, i.state
              FROM tasks t JOIN wf_instance i ON i.task_id = t.id
             WHERE t.id = ?
            """,
            (task_id,),
        ).fetchone()
        assert tuple(row) == ("running", "two", "advancing")
    finally:
        conn.close()


def test_timeout_auto_block_maps_to_workflow_exception(
    tmp_path,
    monkeypatch,
    all_assignees_spawnable,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    conn = kb.connect()
    try:
        task_id, _ = _workflow(conn)
        conn.execute(
            "UPDATE tasks SET max_retries = 1, max_runtime_seconds = 1 WHERE id = ?",
            (task_id,),
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        kb._set_worker_pid(conn, task_id, 779)
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = ?",
            (int(kb.time.time()) - 10, claimed.current_run_id),
        )
        alive_checks = iter([True, False])
        monkeypatch.setattr(
            kb,
            "_pid_alive",
            lambda _pid: next(alive_checks, False),
        )
        signals = []
        monkeypatch.setattr(kb.os, "kill", lambda pid, sig: signals.append((pid, sig)))

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kw: None,
            max_spawn=0,
        )
        assert task_id in result.timed_out
        assert task_id in result.auto_blocked
        assert signals and signals[0][0] == 779
        row = conn.execute(
            """
            SELECT t.status, i.state
              FROM tasks t JOIN wf_instance i ON i.task_id = t.id
             WHERE t.id = ?
            """,
            (task_id,),
        ).fetchone()
        assert tuple(row) == ("blocked", "exception")
    finally:
        conn.close()


def test_protocol_violation_auto_block_maps_to_workflow_exception(
    tmp_path, monkeypatch, all_assignees_spawnable,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    conn = kb.connect()
    try:
        task_id, _ = _workflow(conn)
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        kb._set_worker_pid(conn, task_id, 777)
        kb._recent_worker_exits[777] = (0, kb.time.time())
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

        result = kb.dispatch_once(conn, spawn_fn=lambda *_args, **_kw: None)
        assert task_id in result.auto_blocked
        row = conn.execute(
            """
            SELECT t.status, i.state
              FROM tasks t JOIN wf_instance i ON i.task_id = t.id
             WHERE t.id = ?
            """,
            (task_id,),
        ).fetchone()
        assert tuple(row) == ("blocked", "exception")
        assert any(
            event.kind == "protocol_violation"
            for event in kb.list_events(conn, task_id)
        )
    finally:
        kb._recent_worker_exits.pop(777, None)
        conn.close()


def test_crash_breaker_maps_workflow_to_exception(
    tmp_path, monkeypatch, all_assignees_spawnable,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    conn = kb.connect()
    try:
        task_id, _ = _workflow(conn)
        conn.execute("UPDATE tasks SET max_retries = 1 WHERE id = ?", (task_id,))
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        kb._set_worker_pid(conn, task_id, 778)
        kb._recent_worker_exits[778] = (1 << 8, kb.time.time())
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

        result = kb.dispatch_once(conn, spawn_fn=lambda *_args, **_kw: None)
        assert task_id in result.auto_blocked
        state = conn.execute(
            "SELECT state FROM wf_instance WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        assert state == "exception"
        assert any(event.kind == "crashed" for event in kb.list_events(conn, task_id))
    finally:
        kb._recent_worker_exits.pop(778, None)
        conn.close()


def test_workflow_worker_skill_exists_and_bans_raw_kanban_writes():
    path = (
        Path(__file__).parents[2]
        / "skills"
        / "devops"
        / "workflow-worker"
        / "SKILL.md"
    )
    text = path.read_text(encoding="utf-8")
    assert "wf_context" in text
    assert "Exactly one" in text
    assert "Never call `kanban_complete`" in text
