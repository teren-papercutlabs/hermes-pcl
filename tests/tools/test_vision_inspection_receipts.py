import hashlib
import json
from pathlib import Path

from tools import vision_inspection_receipts as receipts


def test_writes_content_addressed_local_receipt_without_analysis(tmp_path: Path, monkeypatch) -> None:
    materialized = tmp_path / "materialized"
    materialized.mkdir()
    image = materialized / "image.jpg"
    image.write_bytes(b"real-image-bytes")
    root = tmp_path / "receipts"
    monkeypatch.setattr(receipts, "_configured", lambda: (root, (materialized.resolve(),)))

    first = receipts.write_local_image_inspection_receipt(
        image_path=image, mime="image/jpeg", question="Is the work complete?", mode="native_pixels"
    )
    second = receipts.write_local_image_inspection_receipt(
        image_path=image, mime="image/jpeg", question="Is the work complete?", mode="native_pixels"
    )

    assert first is not None
    assert second == first
    digest = hashlib.sha256(b"real-image-bytes").hexdigest()
    stored = json.loads(next((root / digest).glob("*.json")).read_text())
    assert stored["image_sha256"] == digest
    assert stored["mime"] == "image/jpeg"
    assert stored["question"] == "Is the work complete?"
    assert stored["mode"] == "native_pixels"
    assert "analysis" not in stored
    assert "inspected_at" in stored


def test_skips_local_path_outside_configured_materialization_root(tmp_path: Path, monkeypatch) -> None:
    materialized = tmp_path / "materialized"
    materialized.mkdir()
    other = tmp_path / "other.jpg"
    other.write_bytes(b"image")
    monkeypatch.setattr(receipts, "_configured", lambda: (tmp_path / "receipts", (materialized.resolve(),)))

    assert receipts.write_local_image_inspection_receipt(
        image_path=other, mime="image/jpeg", question="?", mode="auxiliary_result"
    ) is None
