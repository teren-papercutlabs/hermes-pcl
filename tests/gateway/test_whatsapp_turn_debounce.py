import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource


def _make_adapter(debounce_ms=20):
    from gateway.platforms.whatsapp import WhatsAppAdapter

    adapter = WhatsAppAdapter.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter._turn_debounce_ms = debounce_ms
    adapter._turn_buffers = {}
    adapter._turn_tasks = {}
    adapter.handle_message = AsyncMock()
    return adapter


def _event(chat_id, text, message_id, *, chat_name="Ops Group", media_urls=None):
    return MessageEvent(
        text=text,
        message_type=MessageType.PHOTO if media_urls else MessageType.TEXT,
        source=SessionSource(
            platform=Platform.WHATSAPP,
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type="group",
            user_id="60120000000@s.whatsapp.net",
            user_name="Sky",
        ),
        raw_message={"messageId": message_id, "timestamp": "2026-05-24T12:00:00Z"},
        message_id=message_id,
        media_urls=media_urls or [],
        media_types=["image/jpeg"] if media_urls else [],
    )


@pytest.mark.asyncio
async def test_debounces_same_chat_messages_into_one_turn():
    adapter = _make_adapter()

    await adapter._queue_or_handle_event(_event("120363111@g.us", "first", "m1"))
    await adapter._queue_or_handle_event(_event("120363111@g.us", "second", "m2", media_urls=["/cache/img.jpg"]))
    await asyncio.sleep(0.06)

    adapter.handle_message.assert_awaited_once()
    bundled = adapter.handle_message.await_args.args[0]
    assert bundled.source.chat_id == "120363111@g.us"
    assert bundled.source.chat_name == "Ops Group"
    assert "WhatsApp turn bundle (2 messages)" in bundled.text
    assert "first" in bundled.text
    assert "second" in bundled.text
    assert "source_message_id: m1" in bundled.text
    assert "source_message_id: m2" in bundled.text
    assert bundled.media_urls == ["/cache/img.jpg"]
    assert bundled.raw_message["sourceMessageIds"] == ["m1", "m2"]


def test_bundle_uses_neutral_replay_time_and_accepts_archived_tgg_alias():
    adapter = _make_adapter()
    neutral = _event("120363111@g.us", "neutral", "m1")
    neutral.raw_message.update(
        {
            "_pa_local_time": "neutral-time",
            "_tgg_sgt": "stale-legacy-time",
        }
    )
    archived = _event("120363111@g.us", "archived", "m2")
    archived.raw_message.pop("timestamp")
    archived.raw_message["_tgg_sgt"] = "legacy-time"

    bundled = adapter._build_turn_event([neutral, archived])

    assert "[neutral-time] Sky: neutral" in bundled.text
    assert "[legacy-time] Sky: archived" in bundled.text
    assert "stale-legacy-time" not in bundled.text


@pytest.mark.asyncio
async def test_debounce_isolated_by_chat_id():
    adapter = _make_adapter()

    await adapter._queue_or_handle_event(_event("120363aaa@g.us", "alpha one", "a1", chat_name="Alpha"))
    await adapter._queue_or_handle_event(_event("120363bbb@g.us", "bravo one", "b1", chat_name="Bravo"))
    await adapter._queue_or_handle_event(_event("120363aaa@g.us", "alpha two", "a2", chat_name="Alpha"))
    await asyncio.sleep(0.06)

    assert adapter.handle_message.await_count == 2
    delivered = [call.args[0] for call in adapter.handle_message.await_args_list]
    by_chat = {event.source.chat_id: event for event in delivered}

    assert set(by_chat) == {"120363aaa@g.us", "120363bbb@g.us"}
    assert "alpha one" in by_chat["120363aaa@g.us"].text
    assert "alpha two" in by_chat["120363aaa@g.us"].text
    assert "bravo one" not in by_chat["120363aaa@g.us"].text
    assert by_chat["120363bbb@g.us"].text == "bravo one"


@pytest.mark.asyncio
async def test_zero_debounce_preserves_existing_single_message_path():
    adapter = _make_adapter(debounce_ms=0)
    event = _event("120363111@g.us", "single", "m1")

    await adapter._queue_or_handle_event(event)

    adapter.handle_message.assert_awaited_once_with(event)
