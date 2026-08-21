"""The nightly analyzer receipt gate stays inside one live agent session."""

import sys
import threading
import types
from types import SimpleNamespace

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.session import SessionSource


class _FakeAgent:
    instances = []

    def __init__(self, *args, **kwargs):
        self.tools = []
        self.calls = []
        type(self).instances.append(self)

    def run_conversation(self, message, conversation_history=None, task_id=None):
        self.calls.append({
            "message": message,
            "conversation_history": conversation_history,
            "task_id": task_id,
        })
        if len(self.calls) == 1:
            return {
                "final_response": "first turn",
                "messages": [{"role": "assistant", "content": "first turn"}],
                "api_calls": 1,
                "completed": True,
            }
        if len(self.calls) == 2:
            return {
                "final_response": "second turn",
                "messages": [{"role": "assistant", "content": "second turn"}],
                "api_calls": 1,
                "completed": True,
            }
        return {
            "final_response": "receipt submitted",
            "messages": [{"role": "assistant", "content": "receipt submitted"}],
            "api_calls": 1,
            "completed": True,
        }


def _runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._service_tier = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._pending_model_notes = {}
    runner._pending_skills_reload_notes = {}
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._session_model_overrides = {}
    runner._queued_events = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(streaming=None)
    return runner


@pytest.mark.asyncio
async def test_nightly_analyzer_continues_same_agent_session_until_receipt(monkeypatch, tmp_path):
    _FakeAgent.instances = []
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.6")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})

    import hermes_cli.tools_config as tools_config
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda *_args: {"core"})

    status_calls = []

    def status_handler(args):
        status_calls.append(args)
        return {
            "ok": True,
            "completed_chat_ids": [] if len(status_calls) < 3 else ["amk@g.us"],
        }

    import tools.registry as tool_registry
    monkeypatch.setattr(
        tool_registry.registry,
        "get_entry",
        lambda name: SimpleNamespace(handler=status_handler) if name == "tgg_nightly_get_batch_status" else None,
    )

    runner = _runner()
    result = await runner._run_agent(
        message="nightly analyzer",
        context_prompt="",
        history=[],
        source=SessionSource(platform=Platform.WHATSAPP, chat_id="synthetic@g.us", chat_type="group"),
        session_id="nightly-session",
        session_key="nightly-session-key",
        pa_job_type="tgg_nightly_whatsapp",
        pa_context={"nightly_batch_id": "nightly:2026-08-17:test", "authoritative_chat_id": "amk@g.us"},
        suppress_delivery=True,
    )

    assert result["final_response"] == "receipt submitted"
    assert len(_FakeAgent.instances) == 1
    calls = _FakeAgent.instances[0].calls
    assert len(calls) == 3
    assert [call["task_id"] for call in calls] == ["nightly-session"] * 3
    assert calls[0]["conversation_history"] == []
    assert calls[1]["conversation_history"] == []
    assert calls[2]["conversation_history"] == []
    assert all(
        "batch_id=nightly:2026-08-17:test" in call["message"]
        and "authoritative_chat_id=amk@g.us" in call["message"]
        for call in calls[1:]
    )
    assert "first read the chat ledger" in calls[1]["message"]
    assert "process every remaining unclassified page in cursor order" in calls[1]["message"]
    assert "immediately fetch and process the next" in calls[1]["message"]
    assert len(status_calls) == 3
    assert status_calls == [{"batch_id": "nightly:2026-08-17:test"}] * 3
    assert runner._queued_events == {}


@pytest.mark.asyncio
async def test_nightly_analyzer_skips_continuation_when_receipt_already_exists(monkeypatch, tmp_path):
    _FakeAgent.instances = []
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.6")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})

    import hermes_cli.tools_config as tools_config
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda *_args: {"core"})
    import tools.registry as tool_registry
    monkeypatch.setattr(
        tool_registry.registry,
        "get_entry",
        lambda name: SimpleNamespace(handler=lambda _args: {"ok": True, "completed_chat_ids": ["amk@g.us"]}) if name == "tgg_nightly_get_batch_status" else None,
    )

    await _runner()._run_agent(
        message="nightly analyzer",
        context_prompt="",
        history=[],
        source=SessionSource(platform=Platform.WHATSAPP, chat_id="synthetic@g.us", chat_type="group"),
        session_id="nightly-session",
        session_key="nightly-session-key",
        pa_job_type="tgg_nightly_whatsapp",
        pa_context={"nightly_batch_id": "nightly:2026-08-17:test", "authoritative_chat_id": "amk@g.us"},
        suppress_delivery=True,
    )

    assert len(_FakeAgent.instances) == 1
    assert len(_FakeAgent.instances[0].calls) == 1


@pytest.mark.asyncio
async def test_nightly_analyzer_stops_after_a_continuation_with_no_durable_progress(monkeypatch, tmp_path):
    _FakeAgent.instances = []
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.6")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})

    import hermes_cli.tools_config as tools_config
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda *_args: {"core"})
    import tools.registry as tool_registry
    monkeypatch.setattr(
        tool_registry.registry,
        "get_entry",
        lambda name: SimpleNamespace(handler=lambda _args: {
            "ok": True,
            "completed_chat_ids": [],
            "chat_progress": {"amk@g.us": {"sha256": "unchanged"}},
        }) if name == "tgg_nightly_get_batch_status" else None,
    )

    result = await _runner()._run_agent(
        message="nightly analyzer",
        context_prompt="",
        history=[],
        source=SessionSource(platform=Platform.WHATSAPP, chat_id="synthetic@g.us", chat_type="group"),
        session_id="nightly-session",
        session_key="nightly-session-key",
        pa_job_type="tgg_nightly_whatsapp",
        pa_context={"nightly_batch_id": "nightly:2026-08-17:test", "authoritative_chat_id": "amk@g.us"},
        suppress_delivery=True,
    )

    assert len(_FakeAgent.instances[0].calls) == 2
    assert "no durable analyzer progress" in result["final_response"]


@pytest.mark.asyncio
async def test_nightly_analyzer_uses_elapsed_deadline_not_turn_count(monkeypatch, tmp_path):
    _FakeAgent.instances = []
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.6")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr(gateway_run, "_TGG_NIGHTLY_MAX_ANALYZER_SECONDS", 0)

    import hermes_cli.tools_config as tools_config
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda *_args: {"core"})
    import tools.registry as tool_registry
    monkeypatch.setattr(
        tool_registry.registry,
        "get_entry",
        lambda name: SimpleNamespace(
            handler=lambda _args: {"ok": True, "completed_chat_ids": []}
        ) if name == "tgg_nightly_get_batch_status" else None,
    )

    result = await _runner()._run_agent(
        message="nightly analyzer",
        context_prompt="",
        history=[],
        source=SessionSource(platform=Platform.WHATSAPP, chat_id="synthetic@g.us", chat_type="group"),
        session_id="nightly-session",
        session_key="nightly-session-key",
        pa_job_type="tgg_nightly_whatsapp",
        pa_context={"nightly_batch_id": "nightly:2026-08-17:test", "authoritative_chat_id": "amk@g.us"},
        suppress_delivery=True,
    )

    assert len(_FakeAgent.instances[0].calls) == 1
    assert "two-hour same-session deadline" in result["final_response"]
