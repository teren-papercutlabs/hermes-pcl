"""Immutable receipts for configured local-image vision inspections.

This is deliberately mechanical.  It records that Hermes successfully gave a
specific materialized image to the normal ``vision_analyze`` path; it does not
record or interpret what the model saw.  TGG uses the receipt as coverage
bookkeeping while Christopher remains responsible for the judgement.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


CONTRACT = "hermes-vision-inspection-receipt/v1"


def _canonical(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _configured() -> tuple[Path, tuple[Path, ...]] | None:
    """Return the opt-in receipt root and exact local materialization roots."""
    try:
        from hermes_cli.config import load_config

        config = load_config()
        pa = config.get("pa") if isinstance(config, Mapping) else None
        section = pa.get("vision_inspection_receipts") if isinstance(pa, Mapping) else None
        if not isinstance(section, Mapping) or section.get("enabled") is not True:
            return None
        root = Path(str(section.get("receipt_root") or "")).expanduser()
        raw_roots = section.get("materialized_roots")
        if not root.is_absolute() or not isinstance(raw_roots, list) or not raw_roots:
            return None
        roots = tuple(Path(str(value)).expanduser().resolve(strict=True) for value in raw_roots)
        return root.resolve(), roots
    except Exception:
        # Vision must remain available if an optional audit sink is misconfigured.
        return None


def write_local_image_inspection_receipt(*, image_path: Path, mime: str, question: str, mode: str) -> dict[str, Any] | None:
    """Create once a content-addressed receipt for a successful local inspection.

    ``mode`` says whether the active model received native pixels or Hermes got
    an auxiliary result.  The auxiliary result itself is intentionally not
    retained here.
    """
    configured = _configured()
    if configured is None:
        return None
    root, materialized_roots = configured
    resolved = image_path.expanduser().resolve(strict=True)
    if not resolved.is_file() or not any(resolved.is_relative_to(candidate) for candidate in materialized_roots):
        return None
    if mode not in {"native_pixels", "auxiliary_result"}:
        raise ValueError("VISION_INSPECTION_MODE_INVALID")
    image_sha256 = hashlib.sha256(resolved.read_bytes()).hexdigest()
    identity = {
        "contract": CONTRACT,
        "image_sha256": image_sha256,
        "mime": mime,
        "question": question,
        "mode": mode,
    }
    receipt_id = hashlib.sha256(_canonical(identity)).hexdigest()
    receipt_path = root / image_sha256 / f"{receipt_id}.json"
    receipt = {
        **identity,
        "receipt_id": receipt_id,
        "materialized_image_path": str(resolved),
        "inspected_at": datetime.now(timezone.utc).isoformat(),
    }
    payload = _canonical(receipt)
    receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    try:
        fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    except FileExistsError:
        existing = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not isinstance(existing, Mapping) or any(existing.get(key) != value for key, value in identity.items()):
            raise ValueError("VISION_INSPECTION_RECEIPT_CONFLICT")
        return dict(existing)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    directory_fd = os.open(receipt_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return receipt
