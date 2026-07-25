from datetime import datetime, timezone
from types import SimpleNamespace

from gateway.run import GatewayRunner
from gateway.session_context import clear_session_vars, get_session_env, set_session_vars


def test_bundle_source_message_timestamps_are_keyed_by_exact_ref():
    event = SimpleNamespace(
        message_id="wa-a+wa-b",
        timestamp=datetime(2026, 7, 24, tzinfo=timezone.utc),
        raw_message={
            "sourceMessageIds": ["wa-a", "wa-b"],
            "messages": [
                {"messageId": "wa-a", "timestamp": 1_784_895_201},
                {"messageId": "wa-b", "timestamp": "1784895202000"},
            ],
        },
    )
    assert GatewayRunner._event_source_message_timestamps(event) == {
        "wa-a": 1_784_895_201,
        "wa-b": 1_784_895_202,
    }


def test_source_message_timestamps_accept_baileys_long_shape():
    event = SimpleNamespace(
        message_id="wa-long",
        timestamp=None,
        raw_message={
            "messageId": "wa-long",
            "timestamp": {"low": 1_784_895_203, "high": 0, "unsigned": True},
        },
    )
    assert GatewayRunner._event_source_message_timestamps(event) == {
        "wa-long": 1_784_895_203,
    }


def test_source_message_timestamps_are_session_scoped():
    tokens = set_session_vars(
        source_message_refs='["wa-a"]',
        source_message_timestamps='{"wa-a": 1784895201}',
    )
    try:
        assert get_session_env("HERMES_SESSION_SOURCE_MESSAGE_TIMESTAMPS") == (
            '{"wa-a": 1784895201}'
        )
    finally:
        clear_session_vars(tokens)
    assert get_session_env("HERMES_SESSION_SOURCE_MESSAGE_TIMESTAMPS") == ""
