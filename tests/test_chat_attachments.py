from __future__ import annotations

from io import BytesIO

import pytest
from fastapi import HTTPException, UploadFile

from services.chat_attachment_service import (
    ChatAttachmentService,
    MAX_TEXT_CHARS_PER_FILE,
)


class _VisionResult:
    text = "A dashboard image showing a red error banner with code E42."


class _VisionLLM:
    def __init__(self):
        self.input = None

    async def ask_async(self, user_input, **_kwargs):
        self.input = user_input
        return _VisionResult()

    def embed_batch(self, texts):
        return [[float("error" in text.lower()), 1.0] for text in texts]

    def embed(self, text):
        return [float("error" in text.lower()), 1.0]


@pytest.mark.asyncio
async def test_text_attachment_is_thread_scoped_and_context_bounded(tmp_path):
    service = ChatAttachmentService(tmp_path)
    uploaded = await service.upload(
        thread_id="thread-a",
        files=[UploadFile(filename="notes.txt", file=BytesIO(b"x" * (MAX_TEXT_CHARS_PER_FILE + 100)))],
    )

    question, records = await service.enrich_question(
        question="Summarize this",
        thread_id="thread-a",
        attachment_ids=[uploaded[0]["id"]],
        model="",
        llm_service=_VisionLLM(),
    )

    assert records[0]["name"] == "notes.txt"
    assert "Content truncated to protect the context window" in question
    assert len(question) < MAX_TEXT_CHARS_PER_FILE + 500
    with pytest.raises(HTTPException) as exc:
        await service.enrich_question(
            question="Read it",
            thread_id="thread-b",
            attachment_ids=[uploaded[0]["id"]],
            model="",
            llm_service=_VisionLLM(),
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_image_is_described_once_instead_of_putting_base64_in_prompt(tmp_path, monkeypatch):
    # The describe step resolves a vision-capable model from the provider registry;
    # in a clean environment (CI) that registry is empty. Pin it so the test drives
    # the injected mock LLM instead of depending on a configured model.
    from services.providers.registry_service import ProviderRegistryService
    monkeypatch.setattr(ProviderRegistryService, "resolve_vision_model", lambda self, m=None: "vision-model")
    service = ChatAttachmentService(tmp_path)
    uploaded = await service.upload(
        thread_id="thread-a",
        files=[UploadFile(filename="screen.png", file=BytesIO(b"fake-png"), headers={"content-type": "image/png"})],
    )
    llm = _VisionLLM()

    question, _ = await service.enrich_question(
        question="What went wrong?",
        thread_id="thread-a",
        attachment_ids=[uploaded[0]["id"]],
        model="vision-model",
        llm_service=llm,
    )

    assert "red error banner with code E42" in question
    assert "base64" not in question
    assert llm.input[0]["content"][1]["type"] == "input_image"


@pytest.mark.asyncio
async def test_thread_retrieval_reuses_prior_attachment_with_configured_embeddings(tmp_path):
    service = ChatAttachmentService(tmp_path)
    uploaded = await service.upload(
        thread_id="thread-a",
        files=[UploadFile(filename="runbook.txt", file=BytesIO(b"Error E42 means the cache is unavailable."))],
    )
    llm = _VisionLLM()
    await service.index_for_local_retrieval(
        thread_id="thread-a", attachments=uploaded, embedding_service=llm
    )

    matches = await service.retrieve_thread_context(
        thread_id="thread-a", query="How do I solve error E42?", embedding_service=llm
    )

    assert matches[0]["name"] == "runbook.txt"
    assert "cache is unavailable" in matches[0]["text"]
