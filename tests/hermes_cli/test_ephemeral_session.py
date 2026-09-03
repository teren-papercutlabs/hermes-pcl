from __future__ import annotations

from types import SimpleNamespace


def test_ephemeral_session_uses_runtime_and_removes_session(monkeypatch, tmp_path):
    from hermes_cli import ephemeral_session as subject
    events = []

    class DB:
        def create_session(self, *args, **kwargs): events.append(("create", args, kwargs))
        def end_session(self, *args): events.append(("end", args))
        def delete_session(self, *args, **kwargs): events.append(("delete", args)); return True
        def close(self): events.append(("db_close",))
    class Agent:
        def __init__(self, **kwargs): self.valid_tool_names = kwargs["allowed_tool_names"]; events.append(("agent", kwargs))
        def run_conversation(self, prompt): events.append(("run", prompt)); return {"final_response": "done"}
        def close(self): events.append(("agent_close",))
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"model": {"default": "configured"}})
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **_: {"provider": "openai-codex", "api_key": "secret", "credential_pool": object()})
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_state.SessionDB", DB)
    monkeypatch.setattr("run_agent.AIAgent", Agent)
    outcome, audit = subject.run_ephemeral_session(prompt="authority=secret", system_prompt="system", model="", max_iterations=24, allowed_tool_names=["only"])
    assert outcome["final_response"] == "done"
    assert audit["loaded_tools"] == ["only"]
    assert audit["cleanup"] == {"ended": True, "deleted": True, "agent_closed": True, "db_closed": True}
    assert [event[0] for event in events] == ["create", "agent", "run", "end", "agent_close", "delete", "db_close"]
    assert "authority" not in str(events[0])


def test_ephemeral_session_failure_still_ends_and_deletes(monkeypatch, tmp_path):
    from hermes_cli import ephemeral_session as subject
    events = []
    class DB:
        def create_session(self, *args, **kwargs): events.append("create")
        def end_session(self, *args): events.append("end")
        def delete_session(self, *args, **kwargs): events.append("delete"); return True
        def close(self): events.append("db_close")
    class Agent:
        def __init__(self, **kwargs): events.append("agent")
        def run_conversation(self, prompt): raise RuntimeError("provider failed")
        def close(self): events.append("agent_close")
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"model": {"default": "configured"}})
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **_: {"provider": "p"})
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_state.SessionDB", DB)
    monkeypatch.setattr("run_agent.AIAgent", Agent)
    import pytest
    with pytest.raises(RuntimeError, match="provider failed"):
        subject.run_ephemeral_session(prompt="p", system_prompt="s", model="", max_iterations=1, allowed_tool_names=[])
    assert events == ["create", "agent", "end", "agent_close", "delete", "db_close"]


def test_ephemeral_session_cleanup_failure_is_fail_closed(monkeypatch, tmp_path):
    from hermes_cli import ephemeral_session as subject
    events = []
    class DB:
        def create_session(self, *args, **kwargs): events.append("create")
        def end_session(self, *args): events.append("end")
        def delete_session(self, *args, **kwargs): events.append("delete"); return True
        def close(self): events.append("db_close")
    class Agent:
        def __init__(self, **kwargs): events.append("agent"); self.valid_tool_names = []
        def run_conversation(self, prompt): return {"final_response": "ok"}
        def close(self): events.append("agent_close"); raise OSError("close")
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"model": {"default": "configured"}})
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **_: {"provider": "p"})
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_state.SessionDB", DB)
    monkeypatch.setattr("run_agent.AIAgent", Agent)
    import pytest
    with pytest.raises(RuntimeError, match="EPHEMERAL_SESSION_CLEANUP_FAILED"):
        subject.run_ephemeral_session(prompt="p", system_prompt="s", model="", max_iterations=1, allowed_tool_names=[])
    assert events == ["create", "agent", "end", "agent_close", "delete", "db_close"]


def test_cleanup_failure_dominates_model_failure(monkeypatch, tmp_path):
    from hermes_cli import ephemeral_session as subject
    class DB:
        def create_session(self, *args, **kwargs): pass
        def end_session(self, *args): pass
        def delete_session(self, *args, **kwargs): raise OSError("delete")
        def close(self): pass
    class Agent:
        def __init__(self, **kwargs): pass
        def run_conversation(self, prompt): raise RuntimeError("model")
        def close(self): pass
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"model": {"default": "configured"}})
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **_: {"provider": "p"})
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_state.SessionDB", DB)
    monkeypatch.setattr("run_agent.AIAgent", Agent)
    import pytest
    with pytest.raises(RuntimeError, match="EPHEMERAL_SESSION_CLEANUP_FAILED") as raised:
        subject.run_ephemeral_session(prompt="p", system_prompt="s", model="", max_iterations=1, allowed_tool_names=[])
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert str(raised.value.__cause__) == "model"
