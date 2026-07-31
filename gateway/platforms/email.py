"""
Email platform adapter for the Hermes gateway.

Allows users to interact with Hermes by sending emails.
Uses IMAP to receive and SMTP to send messages.

Environment variables:
    EMAIL_IMAP_HOST     — IMAP server host (e.g., imap.gmail.com)
    EMAIL_IMAP_PORT     — IMAP server port (default: 993)
    EMAIL_SMTP_HOST     — SMTP server host (e.g., smtp.gmail.com)
    EMAIL_SMTP_PORT     — SMTP server port (default: 587)
    EMAIL_ADDRESS       — Email address for the agent
    EMAIL_PASSWORD      — Email password or app-specific password
    EMAIL_POLL_INTERVAL — Seconds between mailbox checks (default: 15)
    EMAIL_ALLOWED_USERS — Comma-separated list of allowed sender addresses
"""

import asyncio
import contextvars
import email as email_lib
import hashlib
import imaplib
import inspect
import logging
import os
import re
import smtplib
import stat
import ssl
import uuid
from collections import OrderedDict
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.utils import formatdate
from email import encoders
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_document_from_bytes,
    cache_image_from_bytes,
)
from gateway.config import Platform, PlatformConfig
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)
_ACTIVE_EMAIL_REPLY_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "active_email_reply_id",
    default=None,
)
# Automated sender patterns — emails from these are silently ignored
_NOREPLY_PATTERNS = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "notifications@",
    "automated@", "auto-confirm", "auto-reply", "automailer",
)

# RFC headers that indicate bulk/automated mail
_AUTOMATED_HEADERS = {
    "Auto-Submitted": lambda v: v.lower() != "no",
    "Precedence": lambda v: v.lower() in {"bulk", "list", "junk"},
    "X-Auto-Response-Suppress": lambda v: bool(v),
    "List-Unsubscribe": lambda v: bool(v),
}

# Gmail-safe max length per email body
MAX_MESSAGE_LENGTH = 50_000
_DEFAULT_WORKFLOW_BODY_MAX_BYTES = 1_048_576
_WORKFLOW_BODY_MAX_CONFIG_KEYS = (
    "workflow_ingress_max_body_bytes",
    "workflow_body_max_bytes",
    "max_body_bytes",
)

# Supported image extensions for inline detection
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

def _send_imap_id(imap: "imaplib.IMAP4") -> None:
    """Send RFC 2971 IMAP ID command identifying this client.

    Required by 163/NetEase mailbox after LOGIN: without it, every UID
    SEARCH/FETCH returns ``BYE Unsafe Login`` and disconnects.  Other
    IMAP servers either honor it silently or reject the unknown command;
    we swallow failures so non-supporting servers keep working.
    """
    try:
        try:
            from hermes_cli import __version__ as _hermes_version
        except Exception:  # noqa: BLE001 — keep ID best-effort if import fails
            _hermes_version = "0"
        imap.xatom(
            "ID",
            f'("name" "hermes-agent" "version" "{_hermes_version}" '
            '"vendor" "NousResearch" '
            '"support-email" "noreply@nousresearch.com")',
        )
    except Exception as e:  # noqa: BLE001 — best-effort, never fatal
        logger.debug("[Email] IMAP ID command not accepted: %s", e)


def _is_automated_sender(address: str, headers: dict) -> bool:
    """Return True if this email is from an automated/noreply source."""
    addr = address.lower()
    if any(pattern in addr for pattern in _NOREPLY_PATTERNS):
        return True
    for header, check in _AUTOMATED_HEADERS.items():
        value = headers.get(header, "")
        if value and check(value):
            return True
    return False
    
def check_email_requirements() -> bool:
    """Check if email platform dependencies are available."""
    addr = os.getenv("EMAIL_ADDRESS")
    pwd = os.getenv("EMAIL_PASSWORD")
    imap = os.getenv("EMAIL_IMAP_HOST")
    smtp = os.getenv("EMAIL_SMTP_HOST")
    if not all([addr, pwd, imap, smtp]):
        return False
    return True


def _decode_header_value(raw: str) -> str:
    """Decode an RFC 2047 encoded email header into a plain string."""
    parts = decode_header(raw)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def _extract_text_body(msg: email_lib.message.Message) -> str:
    """Extract the plain-text body from a potentially multipart email."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            # Skip attachments
            if "attachment" in disposition:
                continue
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # Fallback: try text/html and strip tags
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in disposition:
                continue
            if content_type == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html = payload.decode(charset, errors="replace")
                    return _strip_html(html)
        return ""
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                return _strip_html(text)
            return text
        return ""


def _strip_html(html: str) -> str:
    """Naive HTML tag stripper for fallback text extraction."""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<p[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_email_address(raw: str) -> str:
    """Extract bare email address from 'Name <addr>' format."""
    match = re.search(r"<([^>]+)>", raw)
    if match:
        return match.group(1).strip().lower()
    return raw.strip().lower()


def _extract_attachments(
    msg: email_lib.message.Message,
    skip_attachments: bool = False,
) -> List[Dict[str, Any]]:
    """Extract attachment metadata and cache files locally.

    When *skip_attachments* is True, all attachment/inline parts are ignored
    (useful for malware protection or bandwidth savings).
    """
    attachments = []
    if not msg.is_multipart():
        return attachments

    for part in msg.walk():
        disposition = str(part.get("Content-Disposition", ""))
        if skip_attachments and ("attachment" in disposition or "inline" in disposition):
            continue
        if "attachment" not in disposition and "inline" not in disposition:
            continue
        # Skip text/plain and text/html body parts
        content_type = part.get_content_type()
        if content_type in {"text/plain", "text/html"} and "attachment" not in disposition:
            continue

        filename = part.get_filename()
        if filename:
            filename = _decode_header_value(filename)
        else:
            ext = part.get_content_subtype() or "bin"
            filename = f"attachment.{ext}"

        payload = part.get_payload(decode=True)
        if not payload:
            continue

        ext = Path(filename).suffix.lower()
        if ext in _IMAGE_EXTS:
            try:
                cached_path = cache_image_from_bytes(payload, ext)
            except ValueError:
                logger.debug("Skipping non-image attachment %s (invalid magic bytes)", filename)
                continue
            attachments.append({
                "path": cached_path,
                "filename": filename,
                "type": "image",
                "media_type": content_type,
            })
        else:
            cached_path = cache_document_from_bytes(payload, filename)
            attachments.append({
                "path": cached_path,
                "filename": filename,
                "type": "document",
                "media_type": content_type,
            })

    return attachments


WorkflowIngressCallback = Callable[[Dict[str, Any]], Optional[Awaitable[None]]]


class EmailAdapter(BasePlatformAdapter):
    """Email gateway adapter using IMAP (receive) and SMTP (send)."""

    def __init__(
        self,
        config: PlatformConfig,
        workflow_ingress_callback: Optional[WorkflowIngressCallback] = None,
    ):
        super().__init__(config, Platform.EMAIL)

        self._address = os.getenv("EMAIL_ADDRESS", "")
        self._password = os.getenv("EMAIL_PASSWORD", "")
        self._imap_host = os.getenv("EMAIL_IMAP_HOST", "")
        self._imap_port = int(os.getenv("EMAIL_IMAP_PORT", "993"))
        self._smtp_host = os.getenv("EMAIL_SMTP_HOST", "")
        self._smtp_port = int(os.getenv("EMAIL_SMTP_PORT", "587"))
        self._poll_interval = int(os.getenv("EMAIL_POLL_INTERVAL", "15"))

        # Skip attachments — configured via config.yaml:
        #   platforms:
        #     email:
        #       skip_attachments: true
        extra = config.extra or {}
        self._skip_attachments = extra.get("skip_attachments", False)
        # Staging harnesses may need to ingest messages authenticated as the
        # watched mailbox. Keep the default echo guard intact and, when this
        # explicit gate is open, route self-mail to workflow intake only —
        # never to conversational handling or reply generation.
        raw_self_workflow_ingress = extra.get("allow_self_workflow_ingress")
        self._allow_self_workflow_ingress = raw_self_workflow_ingress is True
        if (
            "allow_self_workflow_ingress" in extra
            and not isinstance(raw_self_workflow_ingress, bool)
        ):
            logger.warning(
                "[Email] Ignoring non-boolean allow_self_workflow_ingress; "
                "self-message guard remains enabled"
            )
        if self._allow_self_workflow_ingress:
            logger.warning(
                "[Email] SELF WORKFLOW INGRESS ENABLED: self-addressed mail may "
                "enter workflow intake, but will never enter chat handling"
            )

        # Track message IDs we've already processed to avoid duplicates
        self._seen_uids: set = set()
        self._seen_uids_max: int = 2000   # cap to prevent unbounded memory growth
        self._poll_task: Optional[asyncio.Task] = None

        # Thread state is keyed by the inbound message being replied to.  The
        # sender-latest index exists only for legacy callers that do not supply
        # an explicit reply anchor.
        self._thread_context: "OrderedDict[str, Dict[str, str]]" = OrderedDict()
        self._sender_latest_context: "OrderedDict[str, str]" = OrderedDict()
        self._thread_context_max: int = 1000
        self._workflow_ingress_callback = workflow_ingress_callback
        configured_body_limit = next(
            (
                extra[key]
                for key in _WORKFLOW_BODY_MAX_CONFIG_KEYS
                if key in extra and extra[key] is not None
            ),
            _DEFAULT_WORKFLOW_BODY_MAX_BYTES,
        )
        try:
            self._workflow_body_max_bytes = int(configured_body_limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("workflow body size limit must be an integer") from exc
        if self._workflow_body_max_bytes <= 0:
            raise ValueError("workflow body size limit must be positive")

        logger.info("[Email] Adapter initialized for %s", self._address)

    def _trim_seen_uids(self) -> None:
        """Keep only the most recent UIDs to prevent unbounded memory growth.

        IMAP UIDs are monotonically increasing integers. When the set grows
        beyond the cap, we keep only the highest half — old UIDs are safe to
        drop because new messages always have higher UIDs and IMAP's UNSEEN
        flag prevents re-delivery regardless.
        """
        if len(self._seen_uids) <= self._seen_uids_max:
            return
        try:
            # UIDs are bytes like b'1234' — sort numerically and keep top half
            sorted_uids = sorted(self._seen_uids, key=lambda u: int(u))
            keep = self._seen_uids_max // 2
            self._seen_uids = set(sorted_uids[-keep:])
            logger.debug("[Email] Trimmed seen UIDs to %d entries", len(self._seen_uids))
        except (ValueError, TypeError):
            # Fallback: just clear old entries if sort fails
            self._seen_uids = set(list(self._seen_uids)[-self._seen_uids_max // 2:])

    @staticmethod
    def _stable_external_id(msg_data: Dict[str, Any]) -> str:
        """Return the RFC Message-ID or a deterministic fallback identifier."""
        message_id = str(msg_data.get("message_id") or "").replace("\r", "").replace("\n", "").strip()
        if re.fullmatch(r"<[^<>\s]+>", message_id):
            return message_id
        digest_input = "\0".join(
            str(msg_data.get(field) or "")
            for field in (
                "sender_addr",
                "subject",
                "date",
                "in_reply_to",
                "references",
                "body",
            )
        )
        digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
        return f"<email-sha256-{digest}@hermes.local>"

    @staticmethod
    def _reference_ids(value: str) -> List[str]:
        """Extract only syntactically safe RFC message ids from a References value."""
        return re.findall(r"<[^<>\s]+>", str(value or "").replace("\r", "").replace("\n", ""))

    def _persist_workflow_body(self, body: str) -> str:
        """Persist a bounded, immutable inbound body and return its reference."""
        body_bytes = body.encode("utf-8")
        if len(body_bytes) > self._workflow_body_max_bytes:
            raise ValueError(
                "workflow email body exceeds the configured maximum "
                f"of {self._workflow_body_max_bytes} bytes"
            )
        body_digest = hashlib.sha256(body_bytes).hexdigest()
        body_dir = get_hermes_home() / "workflow" / "ingress" / "email" / "bodies"
        body_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        body_path = body_dir / f"{body_digest}.txt"
        try:
            fd = os.open(
                body_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            existing_stat = os.stat(body_path, follow_symlinks=False)
            if not stat.S_ISREG(existing_stat.st_mode):
                raise ValueError(f"workflow body path is not a regular file: {body_path}")
            existing_fd = os.open(
                body_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                with os.fdopen(existing_fd, "rb") as existing_file:
                    if existing_file.read() != body_bytes:
                        raise ValueError(f"workflow body content mismatch: {body_path}")
                    os.fchmod(existing_file.fileno(), 0o600)
            except Exception:
                try:
                    os.close(existing_fd)
                except OSError:
                    pass
                raise
            return str(body_path)

        try:
            with os.fdopen(fd, "wb") as body_file:
                body_file.write(body_bytes)
                body_file.flush()
                os.fsync(body_file.fileno())
            os.chmod(body_path, 0o600)
        except Exception:
            try:
                body_path.unlink()
            except FileNotFoundError:
                pass
            raise
        return str(body_path)

    async def _record_workflow_ingress(
        self,
        msg_data: Dict[str, Any],
        *,
        external_id: str,
    ) -> None:
        """Persist the body and synchronously complete the optional ledger tap."""
        callback = self._workflow_ingress_callback
        if callback is None:
            return
        body_ref = self._persist_workflow_body(str(msg_data.get("body") or ""))

        envelope = {
            "source": "email",
            "external_id": external_id,
            "sender": msg_data["sender_addr"],
            "subject": msg_data.get("subject", ""),
            "date": msg_data.get("date", ""),
            "in_reply_to": msg_data.get("in_reply_to", ""),
            "references": msg_data.get("references", ""),
            "body_ref": body_ref,
        }
        try:
            result = callback(envelope)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception(
                "[Email] Workflow ingress callback failed for %s; "
                "conversational handling halted",
                external_id,
            )
            raise

    def _store_thread_context(
        self,
        *,
        sender: str,
        message_id: str,
        subject: str,
        references: str,
    ) -> None:
        """Store deterministic, bounded per-message reply context."""
        self._thread_context[message_id] = {
            "sender": sender,
            "subject": subject,
            "message_id": message_id,
            "references": references,
        }
        self._thread_context.move_to_end(message_id)
        self._sender_latest_context[sender] = message_id
        self._sender_latest_context.move_to_end(sender)

        while len(self._thread_context) > self._thread_context_max:
            evicted_id, evicted = self._thread_context.popitem(last=False)
            evicted_sender = evicted.get("sender", "")
            if self._sender_latest_context.get(evicted_sender) == evicted_id:
                self._sender_latest_context.pop(evicted_sender, None)
        while len(self._sender_latest_context) > self._thread_context_max:
            self._sender_latest_context.popitem(last=False)

    def _resolve_thread_context(
        self,
        to_addr: str,
        reply_to: Optional[Any] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        """Resolve explicit per-message context, or sender-latest for legacy calls."""
        metadata = metadata or {}
        explicit_metadata = metadata.get("reply_to")
        if isinstance(explicit_metadata, dict):
            context = {
                "subject": str(explicit_metadata.get("subject") or ""),
                "message_id": str(
                    explicit_metadata.get("message_id")
                    or explicit_metadata.get("reply_to_message_id")
                    or ""
                ),
                "references": str(explicit_metadata.get("references") or ""),
            }
            if context["message_id"] or context["subject"] or context["references"]:
                return context

        explicit_id = ""
        if isinstance(reply_to, dict):
            return {
                "subject": str(reply_to.get("subject") or ""),
                "message_id": str(
                    reply_to.get("message_id")
                    or reply_to.get("reply_to_message_id")
                    or ""
                ),
                "references": str(reply_to.get("references") or ""),
            }
        if reply_to:
            explicit_id = str(reply_to)
        else:
            explicit_id = str(
                metadata.get("reply_to_message_id")
                or ""
            )

        if explicit_id:
            context = self._thread_context.get(explicit_id)
            if context and context.get("sender") == to_addr:
                return context
            safe_id = (
                explicit_id
                if re.fullmatch(r"<[^<>\s]+>", explicit_id)
                else ""
            )
            return {"subject": "", "message_id": safe_id, "references": ""}

        active_reply_id = _ACTIVE_EMAIL_REPLY_ID.get()
        if active_reply_id:
            context = self._thread_context.get(active_reply_id)
            if context and context.get("sender") == to_addr:
                return context

        latest_id = self._sender_latest_context.get(to_addr)
        if latest_id:
            return self._thread_context.get(latest_id, {})
        return {}

    @staticmethod
    def _apply_thread_headers(msg: MIMEMultipart, context: Dict[str, str]) -> str:
        subject = re.sub(
            r"[\r\n]+",
            " ",
            context.get("subject") or "Hermes Agent",
        ).strip()
        if not subject.startswith("Re:"):
            subject = f"Re: {subject}"
        msg["Subject"] = subject

        original_msg_id = context.get("message_id", "")
        if original_msg_id:
            msg["In-Reply-To"] = original_msg_id
            reference_ids = EmailAdapter._reference_ids(
                context.get("references", "")
            )
            if original_msg_id not in reference_ids:
                reference_ids.append(original_msg_id)
            msg["References"] = " ".join(reference_ids)
        return subject

    async def connect(self) -> bool:
        """Connect to the IMAP server and start polling for new messages."""
        try:
            # Test IMAP connection
            imap = imaplib.IMAP4_SSL(self._imap_host, self._imap_port, timeout=30)
            imap.login(self._address, self._password)
            _send_imap_id(imap)
            # Mark all existing messages as seen so we only process new ones
            imap.select("INBOX")
            status, data = imap.uid("search", None, "ALL")
            if status == "OK" and data and data[0]:
                for uid in data[0].split():
                    self._seen_uids.add(uid)
            # Keep only the most recent UIDs to prevent unbounded growth
            self._trim_seen_uids()
            imap.logout()
            logger.info("[Email] IMAP connection test passed. %d existing messages skipped.", len(self._seen_uids))
        except Exception as e:
            logger.error("[Email] IMAP connection failed: %s", e)
            return False

        try:
            # Test SMTP connection
            smtp = smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=30)
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(self._address, self._password)
            smtp.quit()
            logger.info("[Email] SMTP connection test passed.")
        except Exception as e:
            logger.error("[Email] SMTP connection failed: %s", e)
            return False

        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        print(f"[Email] Connected as {self._address}")
        return True

    async def disconnect(self) -> None:
        """Stop polling and disconnect."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        logger.info("[Email] Disconnected.")

    async def _poll_loop(self) -> None:
        """Poll IMAP for new messages at regular intervals."""
        while self._running:
            try:
                await self._check_inbox()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[Email] Poll error: %s", e)
            await asyncio.sleep(self._poll_interval)

    async def _check_inbox(self) -> None:
        """Check INBOX for unseen messages and dispatch them."""
        # Run IMAP operations in a thread to avoid blocking the event loop
        loop = asyncio.get_running_loop()
        messages = await loop.run_in_executor(None, self._fetch_new_messages)
        for msg_data in messages:
            try:
                await self._dispatch_message(msg_data)
                marked_seen = await loop.run_in_executor(
                    None,
                    self._mark_message_seen,
                    msg_data["uid"],
                )
                # Dispatch already completed. Remember it in-process even if
                # the cross-restart IMAP flag update failed, or the next poll
                # would duplicate conversational handling.
                self._seen_uids.add(msg_data["uid"])
                self._trim_seen_uids()
                if not marked_seen:
                    logger.warning(
                        "[Email] UID %r handled but not marked seen on server",
                        msg_data["uid"],
                    )
            except Exception as exc:
                logger.error(
                    "[Email] Dispatch failed for UID %r; left unseen for retry: %s",
                    msg_data.get("uid"),
                    exc,
                    exc_info=True,
                )

    def _mark_message_seen(self, uid: bytes) -> bool:
        """Mark one successfully dispatched message seen on the IMAP server."""
        try:
            imap = imaplib.IMAP4_SSL(
                self._imap_host,
                self._imap_port,
                timeout=30,
            )
            try:
                imap.login(self._address, self._password)
                _send_imap_id(imap)
                imap.select("INBOX")
                status, _ = imap.uid("store", uid, "+FLAGS", r"(\Seen)")
                return status == "OK"
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass
        except Exception as exc:
            logger.error("[Email] Failed to mark UID %r seen: %s", uid, exc)
            return False

    def _fetch_new_messages(self) -> List[Dict[str, Any]]:
        """Fetch new (unseen) messages from IMAP. Runs in executor thread."""
        results = []
        try:
            imap = imaplib.IMAP4_SSL(self._imap_host, self._imap_port, timeout=30)
            try:
                imap.login(self._address, self._password)
                _send_imap_id(imap)
                imap.select("INBOX")

                status, data = imap.uid("search", None, "UNSEEN")
                if status != "OK" or not data or not data[0]:
                    return results

                for uid in data[0].split():
                    if uid in self._seen_uids:
                        continue

                    status, msg_data = imap.uid(
                        "fetch",
                        uid,
                        "(BODY.PEEK[])",
                    )
                    if status != "OK":
                        continue

                    raw_email = msg_data[0][1]
                    msg = email_lib.message_from_bytes(raw_email)

                    sender_raw = msg.get("From", "")
                    sender_addr = _extract_email_address(sender_raw)
                    sender_name = _decode_header_value(sender_raw)
                    # Remove email from name if present
                    if "<" in sender_name:
                        sender_name = sender_name.split("<")[0].strip().strip('"')

                    subject = _decode_header_value(msg.get("Subject", "(no subject)"))
                    message_id = msg.get("Message-ID", "")
                    in_reply_to = msg.get("In-Reply-To", "")
                    references = msg.get("References", "")
                    # Skip automated/noreply senders before any processing
                    msg_headers = dict(msg.items())
                    if _is_automated_sender(sender_addr, msg_headers):
                        logger.debug("[Email] Skipping automated sender: %s", sender_addr)
                        # This is an intentional terminal drop, not a retryable
                        # ingress failure. Mark it seen on the current IMAP
                        # connection and remember it in-process.
                        imap.uid("store", uid, "+FLAGS", r"(\Seen)")
                        self._seen_uids.add(uid)
                        self._trim_seen_uids()
                        continue
                    body = _extract_text_body(msg)
                    attachments = _extract_attachments(msg, skip_attachments=self._skip_attachments)

                    results.append({
                        "uid": uid,
                        "sender_addr": sender_addr,
                        "sender_name": sender_name,
                        "subject": subject,
                        "message_id": message_id,
                        "in_reply_to": in_reply_to,
                        "references": references,
                        "body": body,
                        "attachments": attachments,
                        "date": msg.get("Date", ""),
                    })
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass
        except Exception as e:
            logger.error("[Email] IMAP fetch error: %s", e)
        return results

    async def _dispatch_message(self, msg_data: Dict[str, Any]) -> None:
        """Convert a fetched email into a MessageEvent and dispatch it."""
        sender_addr = msg_data["sender_addr"]

        is_self_message = bool(self._address) and sender_addr == self._address.lower()
        if is_self_message and not self._allow_self_workflow_ingress:
            return

        if not is_self_message:
            # Never reply to automated senders
            if _is_automated_sender(sender_addr, {}):
                logger.debug("[Email] Dropping automated sender at dispatch: %s", sender_addr)
                return

            # Skip senders not in EMAIL_ALLOWED_USERS — prevents the adapter
            # from creating a MessageEvent (and thus thread context) for senders
            # that the gateway will never authorize.  Without this early guard,
            # a race between dispatch and authorization can result in the adapter
            # sending a reply even though the handler returned None.
            allowed_raw = os.getenv("EMAIL_ALLOWED_USERS", "").strip()
            if allowed_raw:
                allowed = {addr.strip().lower() for addr in allowed_raw.split(",") if addr.strip()}
                if sender_addr.lower() not in allowed:
                    logger.debug("[Email] Dropping non-allowlisted sender at dispatch: %s", sender_addr)
                    return

        subject = msg_data["subject"]
        body = msg_data["body"].strip()
        attachments = msg_data["attachments"]
        external_id = self._stable_external_id(msg_data)

        # Workflow intake is fail-closed and precedes both conversational
        # thread mutation and chat handling.  The callback receives only a
        # reference to the untrusted raw body.
        await self._record_workflow_ingress(msg_data, external_id=external_id)
        if is_self_message:
            logger.info(
                "[Email] Self-message admitted to workflow ingress only: %s",
                external_id,
            )
            return

        # Build message text: include subject as context
        text = body
        if subject and not subject.startswith("Re:"):
            text = f"[Subject: {subject}]\n\n{body}"

        # Determine message type and media
        media_urls = []
        media_types = []
        msg_type = MessageType.TEXT

        for att in attachments:
            media_urls.append(att["path"])
            media_types.append(att["media_type"])
            if att["type"] == "image":
                msg_type = MessageType.PHOTO

        self._store_thread_context(
            sender=sender_addr,
            message_id=external_id,
            subject=subject,
            references=msg_data.get("references", ""),
        )

        source = self.build_source(
            chat_id=sender_addr,
            chat_name=msg_data["sender_name"] or sender_addr,
            chat_type="dm",
            user_id=sender_addr,
            user_name=msg_data["sender_name"] or sender_addr,
        )

        event = MessageEvent(
            text=text or "(empty email)",
            message_type=msg_type,
            source=source,
            message_id=external_id,
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=msg_data["in_reply_to"] or None,
        )

        logger.info("[Email] New message from %s: %s", sender_addr, subject)
        reply_token = _ACTIVE_EMAIL_REPLY_ID.set(external_id)
        try:
            await self.handle_message(event)
        finally:
            _ACTIVE_EMAIL_REPLY_ID.reset(reply_token)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an email reply to the given address."""
        try:
            reply_to = reply_to or _ACTIVE_EMAIL_REPLY_ID.get()
            loop = asyncio.get_running_loop()
            message_id = await loop.run_in_executor(
                None, self._send_email, chat_id, content, reply_to, metadata
            )
            return SendResult(success=True, message_id=message_id)
        except Exception as e:
            logger.error("[Email] Send failed to %s: %s", chat_id, e)
            return SendResult(success=False, error=str(e))

    def _send_email(
        self,
        to_addr: str,
        body: str,
        reply_to_msg_id: Optional[Any] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Send an email via SMTP. Runs in executor thread."""
        msg = MIMEMultipart()
        msg["From"] = self._address
        msg["To"] = to_addr

        ctx = self._resolve_thread_context(to_addr, reply_to_msg_id, metadata)
        subject = self._apply_thread_headers(msg, ctx)

        msg["Date"] = formatdate(localtime=True)
        msg_id = f"<hermes-{uuid.uuid4().hex[:12]}@{self._address.split('@')[1]}>"
        msg["Message-ID"] = msg_id

        msg.attach(MIMEText(body, "plain", "utf-8"))

        smtp = smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=30)
        try:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(self._address, self._password)
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:
                smtp.close()

        logger.info("[Email] Sent reply to %s (subject: %s)", to_addr, subject)
        return msg_id

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Email has no typing indicator — no-op."""

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image URL as part of an email body."""
        text = caption or ""
        text += f"\n\nImage: {image_url}"
        return await self.send(chat_id, text.strip(), reply_to, metadata)

    async def send_multiple_images(
        self,
        chat_id: str,
        images: List[Tuple[str, str]],
        metadata: Optional[Dict[str, Any]] = None,
        human_delay: float = 0.0,
        reply_to: Optional[str] = None,
    ) -> None:
        """Send a batch of images as a single email with multiple MIME attachments.

        Local files are attached directly. URL images have their URL
        appended to the body (email adapter does not download remote
        images). No hard cap — email clients handle dozens of
        attachments fine, subject to SMTP message size limits.
        """
        if not images:
            return
        reply_to = reply_to or _ACTIVE_EMAIL_REPLY_ID.get()

        from urllib.parse import unquote as _unquote

        body_parts: List[str] = []
        local_paths: List[str] = []
        for image_url, alt_text in images:
            if alt_text:
                body_parts.append(alt_text)
            if image_url.startswith("file://"):
                local_path = _unquote(image_url[7:])
                if Path(local_path).exists():
                    local_paths.append(local_path)
                else:
                    logger.warning("[Email] Skipping missing image: %s", local_path)
            else:
                # Remote URLs just get linked in the body (parity with send_image)
                body_parts.append(f"Image: {image_url}")

        if not local_paths and not body_parts:
            return

        body = "\n\n".join(body_parts)

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                self._send_email_with_attachments,
                chat_id,
                body,
                local_paths,
                reply_to,
                metadata,
            )
        except Exception as e:
            logger.error("[Email] Multi-image send failed, falling back: %s", e, exc_info=True)
            await super().send_multiple_images(chat_id, images, metadata, human_delay)

    def _send_email_with_attachments(
        self,
        to_addr: str,
        body: str,
        file_paths: List[str],
        reply_to: Optional[Any] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Send an email with multiple file attachments via SMTP."""
        msg = MIMEMultipart()
        msg["From"] = self._address
        msg["To"] = to_addr

        ctx = self._resolve_thread_context(to_addr, reply_to, metadata)
        subject = self._apply_thread_headers(msg, ctx)

        msg["Date"] = formatdate(localtime=True)
        msg_id = f"<hermes-{uuid.uuid4().hex[:12]}@{self._address.split('@')[1]}>"
        msg["Message-ID"] = msg_id

        if body:
            msg.attach(MIMEText(body, "plain", "utf-8"))

        for file_path in file_paths:
            p = Path(file_path)
            try:
                with open(p, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header("Content-Disposition", f"attachment; filename={p.name}")
                    msg.attach(part)
            except Exception as e:
                logger.warning("[Email] Failed to attach %s: %s", file_path, e)

        smtp = smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=30)
        try:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(self._address, self._password)
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:
                smtp.close()

        logger.info("[Email] Sent multi-attachment email to %s (%d files)", to_addr, len(file_paths))
        return msg_id

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a file as an email attachment."""
        try:
            reply_to = reply_to or _ACTIVE_EMAIL_REPLY_ID.get()
            loop = asyncio.get_running_loop()
            message_id = await loop.run_in_executor(
                None,
                self._send_email_with_attachment,
                chat_id,
                caption or "",
                file_path,
                file_name,
                reply_to,
                metadata,
            )
            return SendResult(success=True, message_id=message_id)
        except Exception as e:
            logger.error("[Email] Send document failed: %s", e)
            return SendResult(success=False, error=str(e))

    def _send_email_with_attachment(
        self,
        to_addr: str,
        body: str,
        file_path: str,
        file_name: Optional[str] = None,
        reply_to: Optional[Any] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Send an email with a file attachment via SMTP."""
        msg = MIMEMultipart()
        msg["From"] = self._address
        msg["To"] = to_addr

        ctx = self._resolve_thread_context(to_addr, reply_to, metadata)
        self._apply_thread_headers(msg, ctx)

        msg["Date"] = formatdate(localtime=True)
        msg_id = f"<hermes-{uuid.uuid4().hex[:12]}@{self._address.split('@')[1]}>"
        msg["Message-ID"] = msg_id

        if body:
            msg.attach(MIMEText(body, "plain", "utf-8"))

        # Attach file
        p = Path(file_path)
        fname = file_name or p.name
        with open(p, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={fname}")
            msg.attach(part)

        smtp = smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=30)
        try:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(self._address, self._password)
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:
                smtp.close()

        return msg_id

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about the email chat."""
        ctx = self._resolve_thread_context(chat_id)
        return {
            "name": chat_id,
            "type": "dm",
            "chat_id": chat_id,
            "subject": ctx.get("subject", ""),
        }
