from __future__ import annotations

from services.providers.anthropic_provider import _with_retrieval_documents
from services.providers.attachment_context import native_attachment_resources
from services.providers.llm_router import LLMRouter


def test_openai_file_search_is_added_to_every_openai_thread_call():
    token = native_attachment_resources.set({"openai_vector_store_id": "vs_thread"})
    try:
        kwargs = LLMRouter._with_openai_file_search({"tools": [{"type": "web_search"}]})
    finally:
        native_attachment_resources.reset(token)

    assert kwargs["tools"] == [
        {"type": "web_search"},
        {"type": "file_search", "vector_store_ids": ["vs_thread"], "max_num_results": 8},
    ]


def test_anthropic_receives_current_files_and_retrieved_chunks_as_documents():
    token = native_attachment_resources.set({
        "anthropic_files": {"7": [{"file_id": "file_pdf", "name": "manual.pdf"}]},
        "retrieval_documents": [{"name": "notes.txt", "chunk": 1, "text": "Relevant detail"}],
    })
    try:
        messages = _with_retrieval_documents(
            [{"role": "user", "content": "Question"}], provider_id=7
        )
    finally:
        native_attachment_resources.reset(token)

    content = messages[0]["content"]
    assert content[0]["source"] == {"type": "file", "file_id": "file_pdf"}
    assert content[1]["source"]["data"] == "Relevant detail"
    assert content[-1] == {"type": "text", "text": "Question"}
