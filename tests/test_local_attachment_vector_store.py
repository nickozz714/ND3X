from __future__ import annotations

import json

import pytest

from services.local_attachment_vector_store import LocalAttachmentVectorStore


pytest.importorskip("faiss")


def _write_attachment(thread_dir, *, attachment_id, name, chunks, identity):
    (thread_dir / f"{attachment_id}.json").write_text(json.dumps({
        "id": attachment_id,
        "name": name,
        "embedding_identity": identity,
        "retrieval_chunks": chunks,
    }), encoding="utf-8")


def test_persistent_thread_store_searches_only_its_embedding_space(tmp_path):
    thread_dir = tmp_path / "thread-a"
    thread_dir.mkdir()
    _write_attachment(
        thread_dir,
        attachment_id="a" * 32,
        name="runbook.txt",
        identity="ollama:embeddinggemma",
        chunks=[
            {"text": "database recovery", "embedding": [1.0, 0.0, 0.0]},
            {"text": "cache recovery", "embedding": [0.0, 1.0, 0.0]},
        ],
    )
    store = LocalAttachmentVectorStore(tmp_path)

    assert store.rebuild(
        thread_dir=thread_dir, embedding_identity="ollama:embeddinggemma"
    ) == 2
    results = store.search(
        thread_dir=thread_dir,
        embedding_identity="ollama:embeddinggemma",
        query_vector=[0.0, 0.9, 0.1],
        top_k=1,
    )

    assert results[0]["text"] == "cache recovery"
    assert (thread_dir / "local_vectors").is_dir()


def test_different_embedding_models_get_different_index_files(tmp_path):
    thread_dir = tmp_path / "thread-a"
    thread_dir.mkdir()
    store = LocalAttachmentVectorStore(tmp_path)
    for identity in ("ollama:embeddinggemma", "openai:text-embedding-3-small"):
        _write_attachment(
            thread_dir,
            attachment_id=("a" if identity.startswith("ollama") else "b") * 32,
            name="notes.txt",
            identity=identity,
            chunks=[{"text": identity, "embedding": [1.0, 0.0]}],
        )
        store.rebuild(thread_dir=thread_dir, embedding_identity=identity)

    assert len(list((thread_dir / "local_vectors").glob("*.faiss"))) == 2
