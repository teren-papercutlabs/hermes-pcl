"""Management-selector reply delivery (teren 2026-07-21 ruling) tests.

The contract under test:
- delivery keys on the inbound chat's SELECTOR class: management only —
  a site/ingest-selector response must NEVER deliver (negative test);
- at-most-once per inbound WhatsApp message via a durable pre-send claim;
- a bridge refusal / transport failure marks undelivered and never raises
  into the ingest path, and never retries.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.durable_jsonl_consumer import (
    DurableInbox,
    InboxRecord,
    _management_typing_presence,
    _management_selector_chats,
    _internal_management_document_message,
    _parse_captured_send,
    _run_management_document_turn,
    deliver_management_replies,
    process_management_document_events,
)

MGMT_CHAT = "120363426509183563@g.us"
SITE_CHAT = "120363421424519051@g.us"
GATE_CHANGED_AT = "2026-07-21T04:00:00+00:00"
FRESH_TS = "2026-07-21T04:01:00+00:00"
STALE_TS = "2026-07-21T03:59:00+00:00"


@pytest.fixture()
def config_path(tmp_path: Path) -> Path:
    constitution = tmp_path / "constitution.yaml"
    constitution.write_text(
        "selectors:\n"
        "- job_type: tgg_ops_ingest\n"
        "  match:\n"
        "    source.platform: whatsapp\n"
        f"    source.chat_id: {SITE_CHAT}\n"
        "- job_type: tgg_management\n"
        "  match:\n"
        "    source.platform: whatsapp\n"
        f"    source.chat_id: {MGMT_CHAT}\n"
        "- job_type: tgg_management\n"
        "  match:\n"
        "    source.platform: telegram\n"
        "    source.chat_id: '-5295904349'\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        f"pa:\n  enabled: true\n  constitution_path: {constitution}\n",
        encoding="utf-8",
    )
    return config


@pytest.fixture()
def inbox(tmp_path: Path) -> DurableInbox:
    return DurableInbox(tmp_path / "inbox.db")


def _captured(chat_id: str, content: str = "reply text", reply_to: str | None = "MSG1") -> dict:
    return {
        "message_id": "replay-1",
        "kind": "send",
        "args": [chat_id, content],
        "kwargs": {"reply_to": reply_to} if reply_to else {},
        "delivery_mode": "capture",
    }


def _record(
    chat_id: str, message_id: str = "MSG1", timestamp: str = FRESH_TS
) -> InboxRecord:
    return InboxRecord(
        seq=1,
        message_id=message_id,
        chat_id=chat_id,
        start_offset=0,
        end_offset=1,
        raw={"messageId": message_id, "chatId": chat_id, "timestamp": timestamp},
    )


def _handled(*message_ids: str) -> list[dict]:
    return [{"message_ids": list(message_ids), "turn_id": "turn-current"}]


def _enable_media_retention(
    config_path: Path, media_root: Path, *, ref_prefix: str = "/media"
) -> None:
    import yaml
    data = yaml.safe_load(config_path.read_text())
    data["pa"]["media_retention"] = {
        "enabled": True,
        "media_root": str(media_root),
        "source_roots": [str(media_root)],
        "operation": "tgg_media_retention",
        "media_ref_prefix": ref_prefix,
    }
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _captured_images(chat_id: str, paths: list[Path], reply_to: str = "MSG1") -> dict:
    return {
        "message_id": "replay-media",
        "kind": "send_multiple_images",
        "args": [chat_id, [[f"file://{path}", f"photo {i}"] for i, path in enumerate(paths)]],
        "kwargs": {"reply_to": reply_to},
        "delivery_mode": "capture",
    }


def _captured_document(
    chat_id: str, path: Path, *, caption: str | None = None, reply_to: str = "MSG1"
) -> dict:
    return {
        "message_id": "replay-document",
        "kind": "send_document",
        "args": [chat_id, str(path)],
        "kwargs": {"caption": caption, "reply_to": reply_to},
        "delivery_mode": "capture",
    }


def test_selector_chats_are_whatsapp_management_only(config_path: Path) -> None:
    chats = _management_selector_chats(config_path)
    assert chats == frozenset({MGMT_CHAT})


def test_parse_extracts_send_and_rejects_other_kinds() -> None:
    parsed = _parse_captured_send(_captured(MGMT_CHAT))
    assert parsed == {"chat_id": MGMT_CHAT, "content": "reply text", "reply_to": "MSG1"}
    assert _parse_captured_send({**_captured(MGMT_CHAT), "kind": "send_image"}) is None
    assert _parse_captured_send({"kind": "send", "args": [], "kwargs": {}}) is None


def _document_entry(entry_id: str, *, created_at: int = 100, kind: str = "initial_default") -> dict:
    return {
        "id": entry_id,
        "recordId": "record-1",
        "entryKind": kind,
        "createdAt": created_at,
        "body": {"statement": "I need a decision."},
        "effects": [],
    }


def _enable_document_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TGG_MANAGEMENT_DOCUMENT_API_URL", "http://systems.test")
    monkeypatch.setenv("TGG_MANAGEMENT_DOCUMENT_CHAT_ID", MGMT_CHAT)
    monkeypatch.setenv("CHRISTOPHER_TGG_PS_SERVICE_TOKEN", "test-token")


@pytest.mark.asyncio
async def test_document_event_drains_typed_outbox_without_creating_whatsapp_ingress(
    inbox: DurableInbox, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_document_events(monkeypatch)
    calls: list[tuple[str, dict | None]] = []

    def fake_urlopen(request, timeout=0):
        body = json.loads(request.data) if request.data else None
        calls.append((request.full_url, body))
        if request.full_url.startswith("http://systems.test/"):
            return _FakeResponse({"data": {"contract": "tgg-human-resolution-document-entry/v1", "entries": [_document_entry("entry-1")]}})
        return _FakeResponse({"success": True, "messageId": "WA-notice-1"})

    async def fake_turn(entry, **kwargs):
        assert entry["id"] == "entry-1"
        assert kwargs["destination_chat_id"] == MGMT_CHAT
        return [_captured(MGMT_CHAT, "Could you confirm which case this workbook updates?", reply_to=None)]

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("gateway.durable_jsonl_consumer._run_management_document_turn", fake_turn)
    result = await process_management_document_events(
        inbox, config_path=config_path, runner=object()
    )
    assert result == {"examined": 1, "delivered": 1, "undelivered": 0, "skipped": 0}
    assert inbox.management_document_cursor() == (100, "entry-1")
    assert inbox.total() == 0
    assert calls == [
        ("http://systems.test/api/operator/human-resolution-document-entries?limit=100", None),
        ("http://127.0.0.1:3011/send", {"chatId": MGMT_CHAT, "message": "Could you confirm which case this workbook updates?"}),
    ]
    with inbox.connect() as conn:
        row = conn.execute("SELECT delivery_key,reply_to_message_id,status,bridge_message_id FROM reply_deliveries").fetchone()
    assert tuple(row) == ("human-resolution:entry-1", None, "delivered", "WA-notice-1")


@pytest.mark.asyncio
async def test_document_event_restart_does_not_repeat_prior_send(
    inbox: DurableInbox, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_document_events(monkeypatch)
    sends = 0
    document_requests: list[str] = []

    def fake_urlopen(request, timeout=0):
        nonlocal sends
        if request.full_url.startswith("http://systems.test/"):
            document_requests.append(request.full_url)
            return _FakeResponse({"data": {"contract": "tgg-human-resolution-document-entry/v1", "entries": [_document_entry("entry-1", created_at=100)]}})
        sends += 1
        return _FakeResponse({"success": True, "messageId": "WA-notice-1"})

    async def fake_turn(entry, **kwargs):
        return [_captured(MGMT_CHAT, "A natural question", reply_to=None)]

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("gateway.durable_jsonl_consumer._run_management_document_turn", fake_turn)
    assert (await process_management_document_events(inbox, config_path=config_path, runner=object()))["delivered"] == 1
    # A new process sees the persisted cursor and gets an empty exclusive page.
    def empty_page(request, timeout=0):
        assert "after_created_at=100" in request.full_url and "after_id=entry-1" in request.full_url
        return _FakeResponse({"data": {"contract": "tgg-human-resolution-document-entry/v1", "entries": []}})
    monkeypatch.setattr("urllib.request.urlopen", empty_page)
    assert await process_management_document_events(inbox, config_path=config_path, runner=object()) == {
        "examined": 0, "delivered": 0, "undelivered": 0, "skipped": 0,
    }
    assert sends == 1


@pytest.mark.asyncio
async def test_document_event_unknown_bridge_outcome_is_terminal_and_never_retried(
    inbox: DurableInbox, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_document_events(monkeypatch)
    bridge_calls = 0

    def fake_urlopen(request, timeout=0):
        nonlocal bridge_calls
        if request.full_url.startswith("http://systems.test/"):
            return _FakeResponse({"data": {"contract": "tgg-human-resolution-document-entry/v1", "entries": [_document_entry("entry-202")]}})
        bridge_calls += 1
        return _FakeResponse({"success": False, "outcome": "unknown", "retrySafe": False}, status=202)

    async def fake_turn(entry, **kwargs):
        return [_captured(MGMT_CHAT, "A natural question", reply_to=None)]

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("gateway.durable_jsonl_consumer._run_management_document_turn", fake_turn)
    result = await process_management_document_events(inbox, config_path=config_path, runner=object())
    assert result == {"examined": 1, "delivered": 0, "undelivered": 1, "skipped": 0}
    assert inbox.management_document_cursor() == (100, "entry-202")
    assert inbox.reply_delivery_status("human-resolution:entry-202") == "undelivered"
    assert bridge_calls == 1


def test_document_event_model_input_is_internal_not_a_capture_envelope() -> None:
    message = _internal_management_document_message(_document_entry("entry-1"), chat_id=MGMT_CHAT)
    assert message["senderId"] == "system@internal"
    assert message["metadata"]["contract"] == "tgg_management_document_event/v1"
    assert "sourceKey" not in message and "source_key" not in message
    assert message["messageId"].startswith("human-resolution-document-entry:")


@pytest.mark.asyncio
async def test_document_event_recreates_the_same_persistent_management_session(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gateway.durable_jsonl_consumer.configured_engine", lambda _: ("openai-codex", "gpt-5.6-terra"))

    class Runner:
        def __init__(self): self.plans = []
        async def replay(self, plan):
            self.plans.append(plan)
            return type("Result", (), {"outbound": []})()

    runner = Runner()
    await _run_management_document_turn(_document_entry("entry-1"), config_path=config_path, destination_chat_id=MGMT_CHAT, runner=runner)
    await _run_management_document_turn(_document_entry("entry-2", created_at=101), config_path=config_path, destination_chat_id=MGMT_CHAT, runner=runner)
    assert [plan.replay_namespace for plan in runner.plans] == [
        "agent:live-drain:persistent-chat:openai-codex:gpt-5.6-terra",
        "agent:live-drain:persistent-chat:openai-codex:gpt-5.6-terra",
    ]
    assert all(plan.live_business_writes is False for plan in runner.plans)


def test_multi_photo_delivery_uses_send_media_and_distinct_durable_keys(
    inbox: DurableInbox, config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_root = tmp_path / "retained"
    media_root.mkdir()
    paths = [media_root / "a.png", media_root / "b.png"]
    for index, path in enumerate(paths):
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([index]))
    _enable_media_retention(config_path, media_root)
    sent: list[tuple[str, dict]] = []
    def fake_urlopen(request, timeout=0):
        sent.append((request.full_url, json.loads(request.data)))
        return _FakeResponse({"success": True, "messageId": f"WA-{len(sent)}"})
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    kwargs = dict(
        config_path=config_path,
        captured_outbound=[_captured_images(MGMT_CHAT, paths)],
        batch_records=[_record(MGMT_CHAT)],
        gate_changed_at=GATE_CHANGED_AT,
        handled_groups=_handled("MSG1"),
    )
    first = deliver_management_replies(inbox, **kwargs)
    second = deliver_management_replies(inbox, **kwargs)
    assert first["delivered"] == 2
    assert second["duplicate"] == 2
    assert len(sent) == 2
    assert all(url.endswith("/send-media") for url, _ in sent)
    assert [body["filePath"] for _, body in sent] == [str(path) for path in paths]
    assert all(body["chatId"] == MGMT_CHAT and body["replyTo"] == "MSG1" for _, body in sent)
    with inbox.connect() as conn:
        keys = [row[0] for row in conn.execute(
            "SELECT delivery_key FROM reply_deliveries ORDER BY delivery_key"
        )]
    assert len(keys) == 2 and all(key.startswith(f"media::{MGMT_CHAT}::MSG1::") for key in keys)


def test_zip_document_delivery_uses_send_media_with_filename_and_caption(
    inbox: DurableInbox, config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_root = tmp_path / "retained"
    media_root.mkdir()
    report = media_root / "weekly-reports.zip"
    report.write_bytes(b"PK\x03\x04weekly-report")
    _enable_media_retention(config_path, media_root)
    sent: list[tuple[str, dict]] = []

    def fake_urlopen(request, timeout=0):
        sent.append((request.full_url, json.loads(request.data)))
        return _FakeResponse({"success": True, "messageId": "WA-ZIP"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    summary = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[
            _captured_document(
                MGMT_CHAT,
                report,
                caption="Weekly report for 10–15 August 2026.",
            )
        ],
        batch_records=[_record(MGMT_CHAT)],
        gate_changed_at=GATE_CHANGED_AT,
        handled_groups=_handled("MSG1"),
    )

    assert summary == {
        "delivered": 1,
        "undelivered": 0,
        "suppressed": 0,
        "duplicate": 0,
    }
    assert sent == [
        (
            "http://127.0.0.1:3011/send-media",
            {
                "chatId": MGMT_CHAT,
                "replyTo": "MSG1",
                "filePath": str(report),
                "mediaType": "document",
                "fileName": "weekly-reports.zip",
                "caption": "Weekly report for 10–15 August 2026.",
            },
        )
    ]


def test_composite_bundle_anchor_resolves_to_handled_component(
    inbox: DurableInbox, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A multi-message turn bundle anchors its reply to a synthetic composite
    id ("MSG1+MSG2"). The gate must resolve it to a handled component instead
    of silently suppressing (2026-08-04 incident: every multi-message ask
    composed a reply that never reached the bridge)."""
    sent: list[tuple[str, dict]] = []

    def fake_urlopen(request, timeout=0):
        sent.append((request.full_url, json.loads(request.data)))
        return _FakeResponse({"success": True, "messageId": "WA-1"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    summary = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[_captured(MGMT_CHAT, reply_to="MSG1+MSG2")],
        batch_records=[
            _record(MGMT_CHAT, message_id="MSG1"),
            _record(MGMT_CHAT, message_id="MSG2"),
        ],
        gate_changed_at=GATE_CHANGED_AT,
        handled_groups=_handled("MSG1", "MSG2"),
    )
    assert summary["delivered"] == 1
    assert summary["suppressed"] == 0
    # newest component wins the anchor
    assert sent and sent[0][1]["replyTo"] == {"messageId": "MSG2"}


def test_composite_anchor_with_no_handled_component_stays_suppressed(
    inbox: DurableInbox, config_path: Path
) -> None:
    summary = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[_captured(MGMT_CHAT, reply_to="MSGX+MSGY")],
        batch_records=[_record(MGMT_CHAT, message_id="MSG1")],
        gate_changed_at=GATE_CHANGED_AT,
        handled_groups=_handled("MSG1"),
    )
    assert summary["delivered"] == 0
    assert summary["suppressed"] == 1


def test_streamed_media_directive_becomes_one_native_media_send(
    inbox: DurableInbox, config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_root = tmp_path / "retained"
    media_root.mkdir()
    photo = media_root / "case-photo.jpg"
    photo.write_bytes(b"\xff\xd8\xffcase-photo")
    _enable_media_retention(config_path, media_root)
    sent: list[tuple[str, dict]] = []

    def fake_urlopen(request, timeout=0):
        sent.append((request.full_url, json.loads(request.data)))
        return _FakeResponse({"success": True, "messageId": "WA-MEDIA-DIRECTIVE"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    summary = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[
            _captured(
                MGMT_CHAT,
                f"SK/JOB/2606/2372 — first retained photo:\n\nMEDIA:{photo}",
            )
        ],
        batch_records=[_record(MGMT_CHAT)],
        gate_changed_at=GATE_CHANGED_AT,
        handled_groups=_handled("MSG1"),
    )

    assert summary == {
        "delivered": 1,
        "undelivered": 0,
        "suppressed": 0,
        "duplicate": 0,
    }
    assert sent == [
        (
            "http://127.0.0.1:3011/send-media",
            {
                "chatId": MGMT_CHAT,
                "replyTo": "MSG1",
                "filePath": str(photo),
                "mediaType": "image",
                "caption": "SK/JOB/2606/2372 — first retained photo:",
            },
        )
    ]


def test_failed_media_send_delivers_answer_as_separate_text_fallback(
    inbox: DurableInbox, config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib.error import HTTPError

    media_root = tmp_path / "retained"
    media_root.mkdir()
    photo = media_root / "case-photo.jpg"
    photo.write_bytes(b"\xff\xd8\xffcase-photo")
    _enable_media_retention(config_path, media_root)
    sent: list[tuple[str, dict]] = []

    def fake_urlopen(request, timeout=0):
        payload = json.loads(request.data)
        sent.append((request.full_url, payload))
        if request.full_url.endswith("/send-media"):
            raise HTTPError(request.full_url, 403, "unreadable", {}, None)
        return _FakeResponse({"success": True, "messageId": "WA-TEXT-FALLBACK"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    answer = "PG/JOB/2607/0702 — WhatsApp says the basin was replaced."
    summary = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[_captured(MGMT_CHAT, f"{answer}\n\nMEDIA:{photo}")],
        batch_records=[_record(MGMT_CHAT)],
        gate_changed_at=GATE_CHANGED_AT,
        handled_groups=_handled("MSG1"),
    )

    assert summary == {
        "delivered": 1,
        "undelivered": 1,
        "suppressed": 0,
        "duplicate": 0,
    }
    assert [url.rsplit("/", 1)[-1] for url, _ in sent] == ["send-media", "send"]
    assert sent[1][1]["message"] == (
        answer + "\n\nI couldn't send one or more of the selected images."
    )
    with inbox.connect() as conn:
        rows = conn.execute(
            "SELECT delivery_key,status FROM reply_deliveries ORDER BY delivery_key"
        ).fetchall()
    assert {(row["delivery_key"].split("::", 1)[0], row["status"]) for row in rows} == {
        ("media", "undelivered"),
        ("media-fallback", "delivered"),
    }


def test_later_media_failure_sends_only_attachment_note_when_caption_arrived(
    inbox: DurableInbox, config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib.error import HTTPError

    media_root = tmp_path / "retained"
    media_root.mkdir()
    photos = [media_root / "a.jpg", media_root / "b.jpg"]
    for path in photos:
        path.write_bytes(b"\xff\xd8\xffcase-photo" + path.name.encode())
    _enable_media_retention(config_path, media_root)
    sent: list[tuple[str, dict]] = []

    def fake_urlopen(request, timeout=0):
        payload = json.loads(request.data)
        sent.append((request.full_url, payload))
        if request.full_url.endswith("/send-media") and len(sent) == 2:
            raise HTTPError(request.full_url, 403, "unreadable", {}, None)
        return _FakeResponse({"success": True, "messageId": f"WA-{len(sent)}"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    answer = "PG/JOB/2607/0702 — these are the useful photos."
    summary = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[
            _captured(
                MGMT_CHAT,
                answer + "\n\n" + "\n".join(f"MEDIA:{path}" for path in photos),
            )
        ],
        batch_records=[_record(MGMT_CHAT)],
        gate_changed_at=GATE_CHANGED_AT,
        handled_groups=_handled("MSG1"),
    )

    assert summary["delivered"] == 2
    assert summary["undelivered"] == 1
    assert sent[-1][0].endswith("/send")
    assert sent[-1][1]["message"] == "I couldn't send one or more of the selected images."


def test_streamed_case_media_ref_resolves_to_retained_file_and_native_send(
    inbox: DurableInbox, config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_root = tmp_path / "retained"
    media_root.mkdir()
    basename = "export_backfill_hg_7F263A59C4E0C211C37C29A1.jpg"
    photo = media_root / basename
    photo.write_bytes(b"\xff\xd8\xffcase-photo")
    _enable_media_retention(
        config_path, media_root, ref_prefix="/media/tgg/hermes"
    )
    sent: list[tuple[str, dict]] = []

    def fake_urlopen(request, timeout=0):
        sent.append((request.full_url, json.loads(request.data)))
        return _FakeResponse({"success": True, "messageId": "WA-CASE-MEDIA-REF"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    summary = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[
            _captured(
                MGMT_CHAT,
                "SK/JOB/2606/2372 — first retained photo:\n\n"
                f"MEDIA:/media/tgg/hermes/{basename}",
            )
        ],
        batch_records=[_record(MGMT_CHAT)],
        gate_changed_at=GATE_CHANGED_AT,
        handled_groups=_handled("MSG1"),
    )

    assert summary == {
        "delivered": 1,
        "undelivered": 0,
        "suppressed": 0,
        "duplicate": 0,
    }
    assert sent == [
        (
            "http://127.0.0.1:3011/send-media",
            {
                "chatId": MGMT_CHAT,
                "replyTo": "MSG1",
                "filePath": str(photo),
                "mediaType": "image",
                "caption": "SK/JOB/2606/2372 — first retained photo:",
            },
        )
    ]


@pytest.mark.parametrize(
    "media_ref",
    [
        "/media/tgg/hermes/../case-photo.jpg",
        "/media/tgg/hermes/nested/case-photo.jpg",
        "/media/tgg/hermes/case-photo.jpg?download=1",
        "/media/tgg/hermes/case-photo.jpg#fragment",
        "/media/tgg/other/case-photo.jpg",
    ],
)
def test_streamed_case_media_ref_refuses_non_opaque_or_mismatched_reference(
    media_ref: str,
    inbox: DurableInbox,
    config_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_root = tmp_path / "retained"
    media_root.mkdir()
    (media_root / "case-photo.jpg").write_bytes(b"\xff\xd8\xffcase-photo")
    _enable_media_retention(
        config_path, media_root, ref_prefix="/media/tgg/hermes"
    )
    calls: list = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append(a))

    summary = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[_captured(MGMT_CHAT, f"MEDIA:{media_ref}")],
        batch_records=[_record(MGMT_CHAT)],
        gate_changed_at=GATE_CHANGED_AT,
        handled_groups=_handled("MSG1"),
    )

    assert summary == {
        "delivered": 0,
        "undelivered": 0,
        "suppressed": 1,
        "duplicate": 0,
    }
    assert not calls


def test_streamed_media_directive_outside_retained_root_is_not_sent_as_text(
    inbox: DurableInbox, config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_root = tmp_path / "retained"
    media_root.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"\xff\xd8\xffoutside")
    _enable_media_retention(config_path, media_root)
    calls: list = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append(a))

    summary = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[_captured(MGMT_CHAT, f"MEDIA:{outside}")],
        batch_records=[_record(MGMT_CHAT)],
        gate_changed_at=GATE_CHANGED_AT,
        handled_groups=_handled("MSG1"),
    )

    assert summary == {
        "delivered": 0,
        "undelivered": 0,
        "suppressed": 1,
        "duplicate": 0,
    }
    assert not calls


def test_media_delivery_refuses_non_management_missing_and_path_escape(
    inbox: DurableInbox, config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_root = tmp_path / "retained"
    media_root.mkdir()
    valid = media_root / "valid.png"
    valid.write_bytes(b"\x89PNG\r\n\x1a\nvalid")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\noutside")
    _enable_media_retention(config_path, media_root)
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append(a))
    captured = [
        _captured_images(SITE_CHAT, [valid]),
        _captured_images(MGMT_CHAT, [outside]),
        _captured_images(MGMT_CHAT, [media_root / "missing.png"]),
    ]
    summary = deliver_management_replies(
        inbox, config_path=config_path, captured_outbound=captured,
        batch_records=[_record(SITE_CHAT), _record(MGMT_CHAT)],
        gate_changed_at=GATE_CHANGED_AT, handled_groups=_handled("MSG1"),
    )
    assert summary["suppressed"] == 3
    assert not calls


def test_media_unknown_outcome_is_not_retried(
    inbox: DurableInbox, config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_root = tmp_path / "retained"
    media_root.mkdir()
    path = media_root / "photo.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nphoto")
    _enable_media_retention(config_path, media_root)
    calls = []
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=0: calls.append(request) or _FakeResponse(
            {"outcome": "unknown", "retrySafe": False}, status=202
        ),
    )
    kwargs = dict(
        config_path=config_path, captured_outbound=[_captured_images(MGMT_CHAT, [path])],
        batch_records=[_record(MGMT_CHAT)], gate_changed_at=GATE_CHANGED_AT,
        handled_groups=_handled("MSG1"),
    )
    assert deliver_management_replies(inbox, **kwargs)["undelivered"] == 1
    assert deliver_management_replies(inbox, **kwargs)["duplicate"] == 1
    assert len(calls) == 1
    with inbox.connect() as conn:
        row = conn.execute(
            "SELECT status,provider_outcome FROM reply_deliveries"
        ).fetchone()
    assert (row["status"], row["provider_outcome"]) == ("undelivered", "unknown")


def test_site_selector_response_never_delivers(
    inbox: DurableInbox, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list = []
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("no send expected")),
    )
    summary = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[_captured(SITE_CHAT)],
        batch_records=[_record(SITE_CHAT)],
        gate_changed_at=GATE_CHANGED_AT,
        handled_groups=_handled("MSG1"),
    )
    assert summary == {"delivered": 0, "undelivered": 0, "suppressed": 1, "duplicate": 0}
    assert not calls


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = json.dumps(payload).encode()
        self.status = status

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_mgmt_delivery_is_at_most_once(
    inbox: DurableInbox, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list = []

    def fake_urlopen(request, timeout=0):
        sent.append(json.loads(request.data))
        return _FakeResponse({"success": True, "messageId": "WAMSG9"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    kwargs = dict(
        config_path=config_path,
        captured_outbound=[_captured(MGMT_CHAT)],
        batch_records=[_record(MGMT_CHAT)],
        gate_changed_at=GATE_CHANGED_AT,
        handled_groups=_handled("MSG1"),
    )
    first = deliver_management_replies(inbox, **kwargs)
    second = deliver_management_replies(inbox, **kwargs)
    assert first["delivered"] == 1 and second["delivered"] == 0
    assert second["duplicate"] == 1
    assert len(sent) == 1
    assert sent[0] == {
        "chatId": MGMT_CHAT,
        "message": "reply text",
        "replyTo": {"messageId": "MSG1"},
    }


def test_reply_quote_payload_carries_anchor_sender_and_body(
    inbox: DurableInbox, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bridge renders the quote from replyTo.participant/body; a bare
    messageId produces a phantom "[message]" quote (2026-08-06 incident)."""
    sent: list = []

    def fake_urlopen(request, timeout=0):
        sent.append(json.loads(request.data))
        return _FakeResponse({"success": True, "messageId": "WAMSG10"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    record = InboxRecord(
        seq=1,
        message_id="MSG1",
        chat_id=MGMT_CHAT,
        start_offset=0,
        end_offset=1,
        raw={
            "messageId": "MSG1",
            "chatId": MGMT_CHAT,
            "timestamp": FRESH_TS,
            "senderId": "230407865937940@lid",
            "body": "idk what to eat for lunch",
            "fromMe": False,
        },
    )
    summary = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[_captured(MGMT_CHAT)],
        batch_records=[record],
        gate_changed_at=GATE_CHANGED_AT,
        handled_groups=_handled("MSG1"),
    )
    assert summary["delivered"] == 1
    assert sent[0]["replyTo"] == {
        "messageId": "MSG1",
        "participant": "230407865937940@lid",
        "body": "idk what to eat for lunch",
    }


def test_media_anchor_body_falls_back_to_media_type(
    inbox: DurableInbox, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list = []

    def fake_urlopen(request, timeout=0):
        sent.append(json.loads(request.data))
        return _FakeResponse({"success": True, "messageId": "WAMSG11"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    record = InboxRecord(
        seq=1,
        message_id="MSG1",
        chat_id=MGMT_CHAT,
        start_offset=0,
        end_offset=1,
        raw={
            "messageId": "MSG1",
            "chatId": MGMT_CHAT,
            "timestamp": FRESH_TS,
            "senderId": "230407865937940@lid",
            "body": "",
            "mediaType": "image",
        },
    )
    summary = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[_captured(MGMT_CHAT)],
        batch_records=[record],
        gate_changed_at=GATE_CHANGED_AT,
        handled_groups=_handled("MSG1"),
    )
    assert summary["delivered"] == 1
    assert sent[0]["replyTo"] == {
        "messageId": "MSG1",
        "participant": "230407865937940@lid",
        "body": "[image]",
    }


def test_distinct_renderings_for_same_anchor_still_deliver_only_once(
    inbox: DurableInbox, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list = []

    def fake_urlopen(request, timeout=0):
        sent.append(json.loads(request.data))
        return _FakeResponse({"success": True, "messageId": f"WAMSG{len(sent)}"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    summary = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[
            _captured(MGMT_CHAT, content="first answer"),
            _captured(MGMT_CHAT, content="second answer"),
        ],
        batch_records=[_record(MGMT_CHAT)],
        gate_changed_at=GATE_CHANGED_AT,
        handled_groups=_handled("MSG1"),
    )
    assert summary["delivered"] == 1 and summary["duplicate"] == 1
    assert len(sent) == 1
    with inbox.connect() as conn:
        row = conn.execute(
            "SELECT delivery_key FROM reply_deliveries"
        ).fetchone()
    assert row["delivery_key"] == f"{MGMT_CHAT}::MSG1"


def test_indeterminate_202_outcome_marks_undelivered(
    inbox: DurableInbox, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=0: _FakeResponse(
            {"outcome": "unknown", "retrySafe": False}, status=202
        ),
    )
    summary = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[_captured(MGMT_CHAT)],
        batch_records=[_record(MGMT_CHAT)],
        gate_changed_at=GATE_CHANGED_AT,
        handled_groups=_handled("MSG1"),
    )
    assert summary == {"delivered": 0, "undelivered": 1, "suppressed": 0, "duplicate": 0}
    # claim consumed: no retry ever re-sends the indeterminate message
    retry = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[_captured(MGMT_CHAT)],
        batch_records=[_record(MGMT_CHAT)],
        gate_changed_at=GATE_CHANGED_AT,
        handled_groups=_handled("MSG1"),
    )
    assert retry["duplicate"] == 1 and retry["delivered"] == 0


def test_bridge_refusal_marks_undelivered_and_never_raises(
    inbox: DurableInbox, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib.error import HTTPError

    def refusing_urlopen(request, timeout=0):
        raise HTTPError(
            "http://bridge/send", 403, "refused", {}, None  # type: ignore[arg-type]
        )

    monkeypatch.setattr("urllib.request.urlopen", refusing_urlopen)
    summary = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[_captured(MGMT_CHAT)],
        batch_records=[_record(MGMT_CHAT)],
        gate_changed_at=GATE_CHANGED_AT,
        handled_groups=_handled("MSG1"),
    )
    assert summary["undelivered"] == 1
    # the claim consumed the key: a retry never re-sends
    retry = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[_captured(MGMT_CHAT)],
        batch_records=[_record(MGMT_CHAT)],
        gate_changed_at=GATE_CHANGED_AT,
        handled_groups=_handled("MSG1"),
    )
    assert retry == {"delivered": 0, "undelivered": 0, "suppressed": 0, "duplicate": 1}


def test_same_content_uncited_anchor_is_suppressed_but_fresh_cited_anchor_delivers(
    inbox: DurableInbox, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list = []

    def fake_urlopen(request, timeout=0):
        sent.append(json.loads(request.data))
        return _FakeResponse({"success": True, "messageId": "WAMSG-FRESH"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    summary = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[
            _captured(MGMT_CHAT, content="same answer", reply_to="STALE-UNCITED"),
            _captured(MGMT_CHAT, content="same answer", reply_to="FRESH-CITED"),
        ],
        batch_records=[
            _record(MGMT_CHAT, "STALE-UNCITED"),
            _record(MGMT_CHAT, "FRESH-CITED"),
        ],
        gate_changed_at=GATE_CHANGED_AT,
        handled_groups=_handled("FRESH-CITED"),
    )
    assert summary == {"delivered": 1, "undelivered": 0, "suppressed": 1, "duplicate": 0}
    assert sent == [
        {
            "chatId": MGMT_CHAT,
            "message": "same answer",
            "replyTo": {"messageId": "FRESH-CITED"},
        }
    ]


def test_preactivation_cited_anchor_is_suppressed(
    inbox: DurableInbox, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list = []
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("no send expected")),
    )
    summary = deliver_management_replies(
        inbox,
        config_path=config_path,
        captured_outbound=[_captured(MGMT_CHAT, reply_to="PREACTIVATION")],
        batch_records=[_record(MGMT_CHAT, "PREACTIVATION", timestamp=STALE_TS)],
        gate_changed_at=GATE_CHANGED_AT,
        handled_groups=_handled("PREACTIVATION"),
    )
    assert summary == {"delivered": 0, "undelivered": 0, "suppressed": 1, "duplicate": 0}
    assert not calls


def test_fresh_cited_anchor_delivers_exactly_once(
    inbox: DurableInbox, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list = []

    def fake_urlopen(request, timeout=0):
        sent.append(json.loads(request.data))
        return _FakeResponse({"success": True, "messageId": "WAMSG-ONCE"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    kwargs = {
        "config_path": config_path,
        "captured_outbound": [_captured(MGMT_CHAT, reply_to="FRESH")],
        "batch_records": [_record(MGMT_CHAT, "FRESH")],
        "gate_changed_at": GATE_CHANGED_AT,
        "handled_groups": _handled("FRESH"),
    }
    assert deliver_management_replies(inbox, **kwargs)["delivered"] == 1
    assert deliver_management_replies(inbox, **kwargs)["duplicate"] == 1
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_typing_presence_wraps_only_fresh_management_processing(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    presence: list[dict] = []

    def fake_urlopen(request, timeout=0):
        presence.append(json.loads(request.data))
        return _FakeResponse({"success": True})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    records = [
        _record(MGMT_CHAT, "FRESH-MGMT"),
        _record(MGMT_CHAT, "STALE-MGMT", timestamp=STALE_TS),
        _record(SITE_CHAT, "FRESH-SITE"),
    ]
    async with _management_typing_presence(
        records,
        config_path=config_path,
        gate_changed_at=GATE_CHANGED_AT,
        reassert_seconds=60,
    ):
        assert presence == [{"chatId": MGMT_CHAT, "presence": "composing"}]
    assert presence == [
        {"chatId": MGMT_CHAT, "presence": "composing"},
        {"chatId": MGMT_CHAT, "presence": "paused"},
    ]


@pytest.mark.asyncio
async def test_typing_reasserts_and_clears_on_failure(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    presence: list[dict] = []

    def fake_urlopen(request, timeout=0):
        presence.append(json.loads(request.data))
        return _FakeResponse({"success": True})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="delivery exploded"):
        async with _management_typing_presence(
            [_record(MGMT_CHAT, "FRESH-MGMT")],
            config_path=config_path,
            gate_changed_at=GATE_CHANGED_AT,
            reassert_seconds=0.01,
        ):
            import asyncio

            await asyncio.sleep(0.025)
            raise RuntimeError("delivery exploded")
    assert [item["presence"] for item in presence].count("composing") >= 2
    assert presence[-1] == {"chatId": MGMT_CHAT, "presence": "paused"}


def test_typing_presence_default_reassert_is_six_seconds() -> None:
    import inspect

    assert (
        inspect.signature(_management_typing_presence)
        .parameters["reassert_seconds"]
        .default
        == 6
    )
