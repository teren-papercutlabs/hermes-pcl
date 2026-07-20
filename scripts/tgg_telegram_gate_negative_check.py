"""Negative check: outbound gate refuses non-allowlisted destinations.

Tested against the LIVE runtime config at ~/.hermes-christopher-tgg-telegram
(config.yaml platforms.telegram.extra — the same dict the running gateway's
adapter reads). A stub bot records any Bot API call; the assertion is that
refusal happens BEFORE any call is made.
"""
import asyncio
import os
import sys
from pathlib import Path

import yaml

WORKTREE = "/Users/pcloffice/pcl-dev/hermes-pcl-worktrees/christopher-ops-upgrade"
HERMES_HOME = os.path.expanduser("~/.hermes-christopher-tgg-telegram")
sys.path.insert(0, WORKTREE)

# Ensure env fallbacks match the live runtime (.env values, no tokens printed)
for line in Path(HERMES_HOME, ".env").read_text().splitlines():
    if line.strip() and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        if k in ("TELEGRAM_OUTBOUND_ALLOWED_CHATS", "TELEGRAM_ALLOWED_CHATS"):
            os.environ[k] = v

from gateway.config import PlatformConfig  # noqa: E402
from gateway.platforms.telegram import (  # noqa: E402
    TelegramAdapter,
    TelegramOutboundBlocked,
    _OutboundGuardedBot,
)

live = yaml.safe_load(Path(HERMES_HOME, "config.yaml").read_text())
extra = live["platforms"]["telegram"]["extra"]
print(f"LIVE config platforms.telegram.extra.outbound_allowed_chats = {extra['outbound_allowed_chats']}")
assert extra["outbound_allowed_chats"] == ["-5318895839"], "allowlist must contain exactly the new group"

cfg = PlatformConfig(enabled=True, token="TEST-STUB", extra=dict(extra))
adapter = TelegramAdapter(cfg)

configured, allowed = adapter._telegram_outbound_allowed_chats()
print(f"adapter._telegram_outbound_allowed_chats() -> ({configured}, {sorted(allowed)})")
assert configured and allowed == {"-5318895839"}, "gate must see exactly one allowed chat"


class StubBot:
    def __init__(self):
        self.calls = []

    def send_message(self, chat_id=None, **kw):
        self.calls.append(("send_message", chat_id))
        return type("M", (), {"message_id": 1})()


TARGETS = [
    ("-5318895839", True, "the witnessed test group (ONLY allowlisted chat)"),
    ("-5541405631", False, "earlier test group from this afternoon's run"),
    ("-5295904349", False, "dead Bobby TGG Management (DEV)"),
    ("276672685", False, "private DM"),
    ("-1001234567890", False, "arbitrary supergroup"),
]
print("\npolicy decisions:")
for chat, expect_allow, label in TARGETS:
    d = adapter._outbound_policy_decision(chat)
    verdict = "ALLOW " if d.allowed else "REFUSE"
    print(f"  {verdict} {chat:<16} {d.reason:<26} {label}")
    assert d.allowed == expect_allow, f"unexpected decision for {chat}"

stub = StubBot()
adapter._bot = _OutboundGuardedBot(stub, adapter._outbound_policy_decision)

res = asyncio.run(adapter.send("-5541405631", "this send must be refused"))
print("\nsend() to non-allowlisted chat -5541405631:")
print(f"  success: {res.success}")
print(f"  error  : {res.error}")
print(f"  Bot API calls made: {len(stub.calls)}")
assert not res.success and len(stub.calls) == 0

print("\nproxy backstop — direct bot call bypassing send():")
try:
    adapter._bot.send_message(chat_id="-5295904349", text="must be blocked")
    raise SystemExit("FAIL: proxy allowed a non-allowlisted send")
except TelegramOutboundBlocked as e:
    print(f"  BLOCKED reason={e.reason} chat={e.chat_id}")
print(f"  Bot API calls made: {len(stub.calls)}")
assert len(stub.calls) == 0

print("\nALL NEGATIVE CHECKS PASS — refusal occurs before any Bot API call.")
