from unittest.mock import AsyncMock

import pytest

from gateway.client_surface_policy import (
    CLIENT_SAFE_FAILURE,
    client_safe_failure,
    is_client_facing_config,
    is_client_facing_home,
)
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    SessionSource,
    build_session_key,
)


class _CaptureAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
        self.sent = []

    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SendResult(success=True, message_id="1")

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


def test_pa_profile_is_client_facing_and_cannot_be_overridden():
    config = {
        "agent": {"profile": "PA"},
        "display": {
            "tool_progress": "verbose",
            "interim_assistant_messages": True,
        },
    }

    assert is_client_facing_config(config) is True
    assert client_safe_failure() == CLIENT_SAFE_FAILURE


def test_non_pa_profiles_keep_operator_gateway_behavior():
    assert is_client_facing_config({}) is False
    assert is_client_facing_config({"agent": {"profile": "developer"}}) is False


def test_client_facing_home_reads_fresh_pa_config(tmp_path):
    (tmp_path / "config.yaml").write_text("agent:\n  profile: pa\n", encoding="utf-8")
    assert is_client_facing_home(tmp_path) is True


@pytest.mark.asyncio
async def test_adapter_last_resort_error_never_sends_raw_detail_for_pa_home(
    tmp_path, monkeypatch
):
    (tmp_path / "config.yaml").write_text("agent:\n  profile: pa\n", encoding="utf-8")
    monkeypatch.setattr(
        "gateway.client_surface_policy.is_client_facing_home",
        lambda hermes_home=None: True,
    )
    adapter = _CaptureAdapter()
    adapter.set_message_handler(
        AsyncMock(side_effect=RuntimeError("401 sk-client-secret from terminal"))
    )
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="client-chat",
        chat_type="dm",
        user_id="client-user",
    )
    event = MessageEvent(
        text="help",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m1",
    )

    await adapter._process_message_background(event, build_session_key(source))

    assert adapter.sent == [CLIENT_SAFE_FAILURE]
