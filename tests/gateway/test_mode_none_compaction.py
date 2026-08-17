"""Compaction reality check for SessionResetPolicy mode "none".

Teren ruling (2026-07-29): Christopher runs ONE PERSISTENT SESSION PER CHAT
THAT AUTOCOMPACTS — no daily reset, no idle reset.  Mode "none" makes context
management rest ENTIRELY on the compression path ("context managed only by
compression", gateway/config.py).  These tests establish that the compression
path actually functions under transcript growth — not by stubbing
``_compress_context`` (as the hygiene wiring tests legitimately do), but by
exercising the REAL ``AIAgent._compress_context`` → ``ContextCompressor``
machinery with only the summariser's LLM call mocked at the network boundary.

Three layers:

1. Real compression machinery: ``AIAgent._compress_context`` with a real
   ``ContextCompressor`` and a real ``SessionDB`` compresses an oversized
   transcript, records the compression session chain, and stays bounded even
   when summary generation fails outright.
2. Gateway consumer path under mode "none": transcript growth past the
   hygiene threshold triggers compression through the real ContextCompressor
   and the session continues functioning (agent still runs, transcript
   rewritten smaller, session entry follows the compression chain).
3. Session persistence under mode "none": turns across a simulated 04:00 SGT
   boundary and across multi-day idle gaps REUSE the same session, while the
   old "both" policy demonstrably resets — proving the test detects resets.
"""

import importlib
import sys
import types
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock
from zoneinfo import ZoneInfo

import pytest

from agent.context_compressor import SUMMARY_PREFIX, ContextCompressor
from agent.model_metadata import estimate_messages_tokens_rough
from gateway.config import (
    GatewayConfig,
    Platform,
    PlatformConfig,
    SessionResetPolicy,
)
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.session import SessionEntry, SessionSource, SessionStore


SGT = ZoneInfo("Asia/Singapore")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_history(n_messages: int, content_size: int = 100) -> list:
    history = []
    for i in range(n_messages):
        role = "user" if i % 2 == 0 else "assistant"
        history.append(
            {"role": role, "content": f"turn {i}: " + ("x" * content_size)}
        )
    return history


def _make_source(chat_id="wa-group-1", user_id="u1", platform=Platform.TELEGRAM):
    return SessionSource(platform=platform, chat_id=chat_id, user_id=user_id)


# ---------------------------------------------------------------------------
# 1. Real compression machinery (the path mode "none" depends on)
# ---------------------------------------------------------------------------

class TestRealCompressionMachinery:
    """Exercise the REAL ``AIAgent._compress_context`` — the exact method the
    gateway hygiene path invokes — with only ``_generate_summary`` (the LLM
    network call) mocked."""

    def _make_agent(self, session_db, session_id="original-session"):
        import os
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            from run_agent import AIAgent
            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                quiet_mode=True,
                session_db=session_db,
                session_id=session_id,
                skip_context_files=True,
                skip_memory=True,
            )
        return agent

    def _big_history(self):
        # ~40 messages × ~1000 tokens each ≈ 40K tokens.  The compressor
        # protects the head and a ~20K-token tail, so a real middle window
        # exists to summarize.
        return _make_history(40, content_size=4000)

    def test_compress_context_compresses_and_records_chain(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "sessions.db")
        db.create_session(session_id="original-session", source="test")
        agent = self._make_agent(db)

        messages = self._big_history()
        original_tokens = estimate_messages_tokens_rough(messages)

        with patch.object(
            agent.context_compressor,
            "_generate_summary",
            # Real _generate_summary returns _with_summary_prefix(text)
            return_value=ContextCompressor._with_summary_prefix(
                "Goal: keep helping TGG ops.\nProgress: turns summarized."
            ),
        ):
            compressed, _ = agent._compress_context(
                messages, "", approx_tokens=original_tokens
            )

        # Context actually shrank
        assert len(compressed) < len(messages)
        compressed_tokens = estimate_messages_tokens_rough(compressed)
        assert compressed_tokens < original_tokens

        # A summary checkpoint message is present
        summary_msgs = [
            m for m in compressed
            if isinstance(m.get("content"), str)
            and m["content"].startswith(SUMMARY_PREFIX)
        ]
        assert summary_msgs, "compressed transcript must contain a summary checkpoint"
        assert "turns summarized" in summary_msgs[0]["content"]

        # No fallback / failure flags — the real summariser path succeeded
        assert agent.context_compressor._last_summary_fallback_used is False
        assert agent.context_compressor._last_summary_error is None

        # Session chain: old session ended with reason 'compression', new
        # session minted as its child, and the chain walk resolves to it.
        new_session_id = agent.session_id
        assert new_session_id != "original-session"
        assert db.get_compression_tip("original-session") == new_session_id

        # The new session persists the compressed transcript (what the
        # gateway rehydrates on the next turn) — flush like run_conversation
        # does post-compression.
        agent._flush_messages_to_session_db(compressed, None)
        new_rows = db.get_messages(new_session_id)
        assert len(new_rows) == len(compressed)

    def test_compression_bounds_context_even_when_summary_fails(self, tmp_path):
        """Aux model down → static fallback marker, but context still shrinks
        and the session keeps working.  Under mode "none" this is the last
        line of defence against unbounded prompt growth."""
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "sessions.db")
        db.create_session(session_id="original-session", source="test")
        agent = self._make_agent(db)

        messages = self._big_history()
        original_tokens = estimate_messages_tokens_rough(messages)

        with patch.object(
            agent.context_compressor, "_generate_summary", return_value=None
        ):
            compressed, _ = agent._compress_context(
                messages, "", approx_tokens=original_tokens
            )

        assert len(compressed) < len(messages)
        assert estimate_messages_tokens_rough(compressed) < original_tokens
        # Failure surfaced honestly, not silently
        assert agent.context_compressor._last_summary_fallback_used is True
        assert agent.context_compressor._last_summary_dropped_count > 0
        # Session still rotated into a functioning continuation
        assert agent.session_id != "original-session"

    def test_repeated_compression_stays_bounded(self, tmp_path):
        """Simulate a long-lived mode-"none" session: grow → compress → grow →
        compress.  Each cycle must shrink back below the previous peak — the
        no-unbounded-growth property mode "none" relies on."""
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "sessions.db")
        db.create_session(session_id="original-session", source="test")
        agent = self._make_agent(db)

        transcript = self._big_history()
        peaks = []
        for cycle in range(3):
            grown_tokens = estimate_messages_tokens_rough(transcript)
            peaks.append(grown_tokens)
            with patch.object(
                agent.context_compressor,
                "_generate_summary",
                return_value=ContextCompressor._with_summary_prefix(
                    f"Summary after cycle {cycle}."
                ),
            ):
                transcript, _ = agent._compress_context(
                    transcript, "", approx_tokens=grown_tokens
                )
            assert estimate_messages_tokens_rough(transcript) < grown_tokens
            # Grow again: new conversation turns arrive
            transcript = transcript + _make_history(30, content_size=4000)

        # Chain is walkable from the root to the live tip
        tip = db.get_compression_tip("original-session")
        assert tip == agent.session_id


# ---------------------------------------------------------------------------
# 2. Gateway consumer path: growth past threshold → compression → continue
# ---------------------------------------------------------------------------

class GatewayCaptureAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(
            PlatformConfig(enabled=True, token="fake-token"), Platform.TELEGRAM
        )
        self.sent = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append({"chat_id": chat_id, "content": content})
        return SendResult(success=True, message_id="m1")

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


@pytest.mark.asyncio
async def test_mode_none_growth_compresses_and_session_continues(monkeypatch, tmp_path):
    """Consumer-path proof: under mode "none", a transcript past the hygiene
    threshold is compressed through the REAL ContextCompressor and the turn
    still completes (agent runs on the compressed history, session entry
    follows the compression chain, no reset)."""
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    class CompressAgentWithRealCompressor:
        """Minimal agent shell whose ``_compress_context`` delegates to a REAL
        ContextCompressor (only the summariser LLM call is mocked).  The full
        real-AIAgent version of ``_compress_context`` is covered above; this
        keeps the gateway test hermetic while the compression machinery under
        test stays real."""

        last_instance = None

        def __init__(self, **kwargs):
            self.model = kwargs.get("model")
            self.session_id = kwargs.get("session_id", "sess-1")
            self._print_fn = None
            self.shutdown_memory_provider = MagicMock()
            self.close = MagicMock()
            self.context_compressor = ContextCompressor(
                model="test/model",
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                quiet_mode=True,
            )
            self.context_compressor._generate_summary = (
                lambda *a, **k: ContextCompressor._with_summary_prefix(
                    "Summary: earlier TGG ops turns."
                )
            )
            type(self).last_instance = self

        def _compress_context(self, messages, _system, *, approx_tokens=None, **_kw):
            compressed = self.context_compressor.compress(
                messages, current_tokens=approx_tokens
            )
            self.session_id = f"{self.session_id}_compressed"
            return (compressed, None)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = CompressAgentWithRealCompressor
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    adapter = GatewayCaptureAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake-token")},
        default_reset_policy=SessionResetPolicy(mode="none"),
    )
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)

    entry = SessionEntry(
        session_key="agent:main:telegram:group:-1001:17585",
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    # Oversized transcript: real middle window beyond head + ~20K-token tail
    big_history = _make_history(40, content_size=4000)
    runner.session_store.load_transcript.return_value = big_history
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context, event=None: None
    run_agent_mock = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )
    runner._run_agent = run_agent_mock

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "test-key"}
    )
    # Small context model → the ~40K-token transcript is far past 85%
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 10_000,
    )
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "795544298")

    event = MessageEvent(
        text="hello christopher",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1001",
            chat_type="group",
            thread_id="17585",
            user_id="12345",
        ),
        message_id="1",
    )

    result = await runner._handle_message(event)

    # Turn completed — the session continued functioning after compression
    assert result == "ok"
    assert run_agent_mock.await_count == 1

    # Compression really ran through the real compressor
    agent = CompressAgentWithRealCompressor.last_instance
    assert agent is not None

    # Transcript was rewritten smaller, with a summary checkpoint
    runner.session_store.rewrite_transcript.assert_called_once()
    rewrite_sid, rewritten = runner.session_store.rewrite_transcript.call_args[0]
    assert len(rewritten) < len(big_history)
    assert estimate_messages_tokens_rough(rewritten) < estimate_messages_tokens_rough(
        big_history
    )
    assert any(
        isinstance(m.get("content"), str) and m["content"].startswith(SUMMARY_PREFIX)
        for m in rewritten
    )

    # Session entry followed the compression chain (continuity, not reset)
    assert entry.session_id == agent.session_id
    assert rewrite_sid == entry.session_id
    # Reset bookkeeping never touched the transcript wholesale
    assert entry.last_prompt_tokens == 0


# ---------------------------------------------------------------------------
# 3. Mode "none": sessions persist across the 04:00 boundary and idle gaps
# ---------------------------------------------------------------------------

class TestModeNonePersistence:
    def _store(self, tmp_path, policy):
        config = GatewayConfig(default_reset_policy=policy)
        return SessionStore(sessions_dir=tmp_path, config=config)

    def test_same_chat_reuses_session_across_0400_sgt_boundary(
        self, tmp_path, monkeypatch
    ):
        store = self._store(tmp_path, SessionResetPolicy(mode="none"))
        source = _make_source()

        before = datetime(2026, 7, 29, 23, 50, tzinfo=SGT)
        after = datetime(2026, 7, 30, 4, 30, tzinfo=SGT)

        monkeypatch.setattr("gateway.session._hermes_now", lambda: before)
        first = store.get_or_create_session(source)
        first_id = first.session_id

        monkeypatch.setattr("gateway.session._hermes_now", lambda: after)
        second = store.get_or_create_session(source)

        assert second.session_id == first_id, (
            "mode 'none' must reuse the same session across the 04:00 SGT boundary"
        )
        assert second.auto_reset_reason is None

    def test_mode_none_survives_multi_day_idle(self, tmp_path, monkeypatch):
        store = self._store(tmp_path, SessionResetPolicy(mode="none"))
        source = _make_source()

        start = datetime(2026, 7, 29, 12, 0, tzinfo=SGT)
        much_later = start + timedelta(days=6)

        monkeypatch.setattr("gateway.session._hermes_now", lambda: start)
        first = store.get_or_create_session(source)
        first_id = first.session_id

        monkeypatch.setattr("gateway.session._hermes_now", lambda: much_later)
        second = store.get_or_create_session(source)

        assert second.session_id == first_id
        assert store._is_session_expired(second) is False

    def test_control_old_policy_resets_across_boundary(self, tmp_path, monkeypatch):
        """Positive control: the same clock jump DOES reset under the old
        'both' policy — proving these tests can detect a reset at all."""
        store = self._store(
            tmp_path, SessionResetPolicy(mode="both", at_hour=4, idle_minutes=1440)
        )
        source = _make_source()

        before = datetime(2026, 7, 29, 23, 50, tzinfo=SGT)
        after = datetime(2026, 7, 30, 4, 30, tzinfo=SGT)

        monkeypatch.setattr("gateway.session._hermes_now", lambda: before)
        first = store.get_or_create_session(source)
        first_id = first.session_id

        monkeypatch.setattr("gateway.session._hermes_now", lambda: after)
        second = store.get_or_create_session(source)

        assert second.session_id != first_id, (
            "control failed: 'both' policy should reset across the 04:00 boundary"
        )


# ---------------------------------------------------------------------------
# 4. The shipped Christopher config resolves to mode "none"
# ---------------------------------------------------------------------------

class TestChristopherConfigCarriesModeNone:
    """Tie the deploy artifacts to the behavior above: every committed
    Christopher config (root + all runtime slots) must carry
    ``session_reset: mode: none`` and parse to a policy that never resets."""

    def _config_paths(self):
        from pathlib import Path

        deploy_root = (
            Path(__file__).resolve().parents[2] / "deploy" / "tgg" / "christopher"
        )
        paths = [deploy_root / "config.yaml"]
        paths += sorted(
            (deploy_root / "runtime-slots").glob("*/config.yaml")
        )
        return paths

    def test_all_christopher_configs_set_mode_none(self):
        import yaml

        paths = self._config_paths()
        assert paths[0].is_file()
        assert len(paths) > 1, "expected root config plus runtime slot configs"
        for path in paths:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            assert data.get("session_reset") == {"mode": "none"}, path

    def test_session_reset_yaml_parses_to_never_reset_policy(self, tmp_path, monkeypatch):
        import yaml

        data = yaml.safe_load(self._config_paths()[0].read_text(encoding="utf-8"))
        policy = SessionResetPolicy.from_dict(data["session_reset"])
        assert policy.mode == "none"

        store = SessionStore(
            sessions_dir=tmp_path,
            config=GatewayConfig(default_reset_policy=policy),
        )
        source = _make_source()
        monkeypatch.setattr(
            "gateway.session._hermes_now",
            lambda: datetime(2026, 7, 29, 23, 50, tzinfo=SGT),
        )
        first = store.get_or_create_session(source)
        monkeypatch.setattr(
            "gateway.session._hermes_now",
            lambda: datetime(2026, 8, 5, 4, 30, tzinfo=SGT),
        )
        second = store.get_or_create_session(source)
        assert second.session_id == first.session_id
