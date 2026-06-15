#!/usr/bin/env python3
"""Build a gap-replay bridge DB from live WhatsApp message-store.json.

This is an adaptation of the 2026-06-09 replay artifact's
`ingest_store_to_bridge.py`: it keeps the same bridge_message_log row shape,
but uses the existing tenant DB's chat metadata instead of a four-chat hardcode
and supports a precise cutoff timestamp.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

SGT = dt.timezone(dt.timedelta(hours=8))
SKIP_KEYS = {"reactionMessage", "protocolMessage"}
META_KEYS = {"messageContextInfo", "senderKeyDistributionMessage"}
MEDIA_KEYS = {"imageMessage", "videoMessage", "documentMessage", "audioMessage"}


def epoch_seconds(ts: Any) -> int:
    if isinstance(ts, dict):
        low = int(ts.get("low") or 0)
        high = int(ts.get("high") or 0)
        if low < 0:
            low += 2**32
        return (high << 32) + low
    return int(ts or 0)


def parse_sgt(value: str) -> int:
    text = value.strip()
    if text.endswith(" SGT"):
        text = text[:-4]
    if len(text) == 10:
        text += " 00:00:00"
    return int(dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SGT).timestamp())


def first_text(msg: dict[str, Any]) -> tuple[str, str, int, dict[str, Any] | None]:
    if "imageMessage" in msg:
        primary = msg["imageMessage"] or {}
        return "image", primary.get("caption") or "", 1, primary
    if "videoMessage" in msg:
        primary = msg["videoMessage"] or {}
        return "video", primary.get("caption") or "", 1, primary
    if "documentMessage" in msg:
        primary = msg["documentMessage"] or {}
        return "document", primary.get("caption") or primary.get("fileName") or "", 1, primary
    if "audioMessage" in msg:
        primary = msg["audioMessage"] or {}
        return "audio", "", 1, primary
    if "conversation" in msg:
        return "text", msg.get("conversation") or "", 0, None
    if "extendedTextMessage" in msg:
        primary = msg["extendedTextMessage"] or {}
        return "text", primary.get("text") or "", 0, primary
    # Album containers and other Baileys submessages are kept as unknown, matching
    # the old ingestion behavior. The raw_json stays available to the harness.
    primary = None
    for key in msg:
        if key not in META_KEYS:
            primary = msg.get(key) if isinstance(msg.get(key), dict) else None
            break
    return "unknown", "", 0, primary


def quoted_from_ctx(ctx: dict[str, Any] | None) -> str:
    qm = (ctx or {}).get("quotedMessage") or {}
    if qm.get("conversation"):
        return qm["conversation"]
    for key in ("extendedTextMessage", "imageMessage", "videoMessage", "documentMessage"):
        val = qm.get(key) or {}
        if val.get("text"):
            return val["text"]
        if val.get("caption"):
            return val["caption"]
        if val.get("fileName"):
            return val["fileName"]
    return ""


def load_chat_meta(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    meta: dict[str, tuple[str, str]] = {}
    rows = conn.execute(
        """
        SELECT chat_jid, chat_name, zone, MAX(ts) AS max_ts
        FROM bridge_message_log
        GROUP BY chat_jid
        """
    ).fetchall()
    for jid, chat_name, zone, _max_ts in rows:
        if jid:
            meta[str(jid)] = (str(chat_name or jid), str(zone or "UNKNOWN"))
    return meta


def load_selectors(path: Path | None) -> set[str]:
    if not path:
        return set()
    import yaml

    data = yaml.safe_load(path.read_text()) or {}
    selectors = set()
    for item in data.get("selectors") or []:
        if not isinstance(item, dict):
            continue
        match = item.get("match") if isinstance(item.get("match"), dict) else {}
        if match.get("source.platform") == "whatsapp" and match.get("source.chat_id"):
            selectors.add(str(match["source.chat_id"]))
    return selectors


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-db", required=True)
    ap.add_argument("--out-db", required=True)
    ap.add_argument("--store", required=True)
    ap.add_argument("--since-sgt", required=True)
    ap.add_argument("--until-sgt")
    ap.add_argument("--selectors-yaml", help="constitution yaml; only matching WhatsApp selector JIDs are inserted")
    ap.add_argument("--source", default="message-store-gap-20260613")
    args = ap.parse_args()

    base = Path(args.base_db)
    out = Path(args.out_db)
    out.parent.mkdir(parents=True, exist_ok=True)
    if base.resolve() != out.resolve():
        shutil.copy2(base, out)
    since_ts = parse_sgt(args.since_sgt)
    until_ts = parse_sgt(args.until_sgt) if args.until_sgt else None
    selectors = load_selectors(Path(args.selectors_yaml)) if args.selectors_yaml else set()

    store = json.load(open(args.store, encoding="utf-8"))
    messages = store.get("messages") if isinstance(store, dict) else store
    if not isinstance(messages, list):
        raise SystemExit("message store does not contain a list")

    conn = sqlite3.connect(out)
    meta = load_chat_meta(conn)
    added = Counter()
    skipped = Counter()
    unknown_chats = Counter()
    jid_seen = Counter()

    for m in messages:
        if not isinstance(m, dict):
            skipped["not_object"] += 1
            continue
        key = m.get("key") if isinstance(m.get("key"), dict) else {}
        jid = str(m.get("remoteJid") or key.get("remoteJid") or "")
        if not jid:
            skipped["no_jid"] += 1
            continue
        ts = epoch_seconds(m.get("messageTimestamp") or 0)
        if not ts or ts <= since_ts or (until_ts is not None and ts >= until_ts):
            skipped["outside_window"] += 1
            continue
        jid_seen[jid] += 1
        if selectors and jid not in selectors:
            skipped["non_selector_jid"] += 1
            continue
        if jid not in meta:
            unknown_chats[jid] += 1
        chat_name, zone = meta.get(jid, (jid, "UNKNOWN"))

        mm = m.get("message") or {}
        if not isinstance(mm, dict) or not mm:
            skipped["empty_message"] += 1
            continue
        if SKIP_KEYS & set(mm.keys()):
            skipped["reaction_or_protocol"] += 1
            continue
        content_keys = set(mm.keys()) - META_KEYS
        if not content_keys:
            skipped["no_content_keys"] += 1
            continue

        kind, text, has_media, primary = first_text(mm)
        ctx = (primary or {}).get("contextInfo") or {}
        stanza = ctx.get("stanzaId")
        reply_ref = f"{jid}::{stanza}" if stanza else None
        quoted_text = quoted_from_ctx(ctx)
        msg_id = str(m.get("id") or key.get("id") or "")
        if not msg_id:
            skipped["no_id"] += 1
            continue
        sender = m.get("participant") or key.get("participant")
        source_ref = f"{jid}::{msg_id}"
        sgt = dt.datetime.fromtimestamp(ts, SGT).strftime("%Y-%m-%d %H:%M:%S SGT")
        cur = conn.execute(
            """INSERT OR IGNORE INTO bridge_message_log
               (source, source_ref, chat_jid, chat_name, zone, channel_type,
                sender_id, from_me, ts, sgt, text, message_kind, has_media,
                media_refs, quoted_text, reply_to_source_ref, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                args.source,
                source_ref,
                jid,
                chat_name,
                zone,
                "whatsapp",
                sender,
                int(bool(m.get("fromMe") or key.get("fromMe"))),
                ts,
                sgt,
                text,
                kind,
                has_media,
                "[]",
                quoted_text,
                reply_ref,
                json.dumps(m, ensure_ascii=False),
            ),
        )
        if cur.rowcount:
            added[(jid, chat_name, zone)] += 1
        else:
            skipped["duplicate_source_ref"] += 1

    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM bridge_message_log").fetchone()[0]
    conn.close()

    summary = {
        "since_sgt": args.since_sgt,
        "until_sgt": args.until_sgt,
        "since_ts_exclusive": since_ts,
        "selector_count": len(selectors),
        "message_store_rows": len(messages),
        "window_jid_seen": dict(jid_seen.most_common()),
        "added_total": sum(added.values()),
        "added_by_chat": [
            {"chat_jid": jid, "chat_name": chat, "zone": zone, "added": n}
            for (jid, chat, zone), n in added.most_common()
        ],
        "skipped": dict(skipped),
        "unknown_chats": dict(unknown_chats),
        "bridge_message_log_total": total,
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
