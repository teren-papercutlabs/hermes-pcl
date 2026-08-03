"""Document paths in gateway context must be directly usable in python_sandbox."""

from pathlib import PurePosixPath

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner, _session_meta_system_prompt
from gateway.session import SessionSource
from tools.python_sandbox_paths import host_path_to_python_sandbox_path


def _runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(group_sessions_per_user=True)
    runner.adapters = {}
    runner._model = "gpt-5.6-luna"
    runner._base_url = None
    runner._has_setup_skill = lambda: False
    return runner


def _sandbox_config() -> dict:
    return {
        "datasets": {
            "documents": {
                "type": "path",
                "path": "/var/lib/tgg-capture/whatsapp/media/documents",
            },
        },
    }


def test_host_path_translation_requires_declared_dataset_membership():
    config = _sandbox_config()
    assert host_path_to_python_sandbox_path(
        "/var/lib/tgg-capture/whatsapp/media/documents/report.xlsx", config
    ) == PurePosixPath("/inputs/documents/report.xlsx")
    assert host_path_to_python_sandbox_path(
        "/var/lib/tgg-capture/whatsapp/media/images/photo.jpg", config
    ) is None
    assert host_path_to_python_sandbox_path(
        "/var/lib/tgg-capture/whatsapp/media/documents/../images/photo.jpg", config
    ) is None


@pytest.mark.asyncio
async def test_document_context_names_original_filename_and_sandbox_path(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.read_raw_config",
        lambda: {"python_sandbox": _sandbox_config()},
    )
    source = SessionSource(
        platform=Platform.WHATSAPP,
        chat_id="120363111@g.us",
        chat_type="group",
        user_name="Sky",
    )
    event = MessageEvent(
        text="check this",
        message_type=MessageType.DOCUMENT,
        source=source,
        media_urls=[
            "/var/lib/tgg-capture/whatsapp/media/documents/Weekly Report.xlsx"
        ],
        media_types=[
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ],
    )

    result = await _runner()._prepare_inbound_message_text(
        event=event, source=source, history=[]
    )

    assert "Original filename: 'Weekly Report.xlsx'" in result
    assert "Sandbox path: /inputs/documents/Weekly Report.xlsx" in result
    assert "Host path: /var/lib/tgg-capture/whatsapp/media/documents/Weekly Report.xlsx" in result


def test_fresh_session_meta_exposes_cached_memory_prompt_for_jsonl_audit():
    class Agent:
        _cached_system_prompt = "MEMORY.md\nReconcile provenance comes from the master tracker."

    snapshot = _session_meta_system_prompt(Agent())

    assert "MEMORY.md" in snapshot
    assert "Reconcile provenance" in snapshot


def test_session_meta_prompt_snapshot_rejects_non_string_values():
    class Agent:
        _cached_system_prompt = {"not": "serializable prompt text"}

    assert _session_meta_system_prompt(Agent()) == ""
    assert _session_meta_system_prompt(None) == ""
