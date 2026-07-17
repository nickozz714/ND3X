"""image__view (TODO 5) — attachment-ref resolution: exact id/name match,
single-image fallback for hallucinated refs, and the available-images error."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import services.builtin.tools.image_tools as image_tools
from services.builtin.tools.background_tasks import current_run_thread


def _write_manifest(directory: Path, *, attachment_id: str, name: str, media_type: str = "image/png") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{attachment_id}.json").write_text(
        json.dumps({
            "id": attachment_id,
            "name": name,
            "media_type": media_type,
            "path": str(directory / f"{attachment_id}.bin"),
            "size": 10,
        }),
        encoding="utf-8",
    )


@pytest.fixture()
def thread_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(image_tools, "_thread_dir", lambda thread_id: tmp_path / thread_id)
    return tmp_path


def test_find_by_id_and_name_case_insensitive(thread_dir):
    _write_manifest(thread_dir / "t1", attachment_id="abc123", name="Foto.PNG")
    assert image_tools._find_image_record("t1", "abc123")["id"] == "abc123"
    assert image_tools._find_image_record("t1", "foto.png")["id"] == "abc123"


def test_unmatched_ref_falls_back_to_only_image(thread_dir):
    # Smaller planners pass made-up refs like "${_attachment_retrieval.0}";
    # with a single image in the thread that must still resolve.
    _write_manifest(thread_dir / "t1", attachment_id="abc123", name="foto.png")
    _write_manifest(thread_dir / "t1", attachment_id="doc1", name="notes.txt", media_type="text/plain")
    record = image_tools._find_image_record("t1", "${_attachment_retrieval.0}")
    assert record is not None and record["id"] == "abc123"


def test_unmatched_ref_with_multiple_images_returns_none(thread_dir):
    _write_manifest(thread_dir / "t1", attachment_id="img1", name="a.png")
    _write_manifest(thread_dir / "t1", attachment_id="img2", name="b.png")
    assert image_tools._find_image_record("t1", "nonsense") is None


def test_missing_thread_dir_returns_none(thread_dir):
    assert image_tools._find_image_record("nope", "anything") is None


def test_image_view_error_lists_available_images(thread_dir):
    _write_manifest(thread_dir / "t1", attachment_id="img1", name="a.png")
    _write_manifest(thread_dir / "t1", attachment_id="img2", name="b.png")
    token = current_run_thread.set("t1")
    try:
        result = asyncio.run(image_tools.image_view({"image": "nonsense", "question": "?"}))
    finally:
        current_run_thread.reset(token)
    assert result["status"] == "error"
    assert "a.png" in result["error"] and "b.png" in result["error"]
