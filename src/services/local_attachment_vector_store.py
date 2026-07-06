from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class LocalAttachmentVectorStore:
    """Persistent, dimension-aware FAISS stores scoped by thread and embedding space."""

    def __init__(self, root: Path):
        self.root = root

    @staticmethod
    def _key(embedding_identity: str) -> str:
        return hashlib.sha256(embedding_identity.encode("utf-8")).hexdigest()[:20]

    def _paths(self, thread_dir: Path, embedding_identity: str) -> tuple[Path, Path]:
        store_dir = thread_dir / "local_vectors"
        store_dir.mkdir(parents=True, exist_ok=True)
        key = self._key(embedding_identity)
        return store_dir / f"{key}.faiss", store_dir / f"{key}.json"

    @staticmethod
    def _manifest_records(thread_dir: Path, embedding_identity: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for manifest in thread_dir.glob("*.json"):
            if manifest.name == "vector_store.json" or manifest.name.startswith("gemini_file_search_"):
                continue
            try:
                attachment = json.loads(manifest.read_text(encoding="utf-8"))
            except Exception:
                continue
            if attachment.get("embedding_identity") != embedding_identity:
                continue
            for chunk_index, chunk in enumerate(attachment.get("retrieval_chunks") or []):
                vector = chunk.get("embedding")
                text = chunk.get("text") or ""
                if not vector or not text:
                    continue
                records.append({
                    "attachment_id": attachment.get("id"),
                    "name": attachment.get("name") or "attachment",
                    "chunk": chunk_index,
                    "text": text,
                    "embedding": vector,
                })
        return records

    def rebuild(self, *, thread_dir: Path, embedding_identity: str) -> int:
        import faiss
        import numpy as np

        records = self._manifest_records(thread_dir, embedding_identity)
        index_path, metadata_path = self._paths(thread_dir, embedding_identity)
        if not records:
            index_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            return 0

        dimension = len(records[0]["embedding"])
        consistent = [record for record in records if len(record["embedding"]) == dimension]
        matrix = np.asarray([record.pop("embedding") for record in consistent], dtype="float32")
        faiss.normalize_L2(matrix)
        index = faiss.IndexFlatIP(dimension)
        index.add(matrix)

        tmp_index = index_path.with_suffix(".faiss.tmp")
        tmp_metadata = metadata_path.with_suffix(".json.tmp")
        faiss.write_index(index, str(tmp_index))
        tmp_metadata.write_text(json.dumps({
            "embedding_identity": embedding_identity,
            "dimension": dimension,
            "items": consistent,
        }), encoding="utf-8")
        tmp_index.replace(index_path)
        tmp_metadata.replace(metadata_path)
        return len(consistent)

    def exists(self, *, thread_dir: Path, embedding_identity: str) -> bool:
        index_path, metadata_path = self._paths(thread_dir, embedding_identity)
        return index_path.is_file() and metadata_path.is_file()

    def search(
        self,
        *,
        thread_dir: Path,
        embedding_identity: str,
        query_vector: list[float],
        top_k: int,
    ) -> list[dict[str, Any]]:
        import faiss
        import numpy as np

        index_path, metadata_path = self._paths(thread_dir, embedding_identity)
        if not index_path.exists() or not metadata_path.exists():
            return []
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if int(metadata.get("dimension") or 0) != len(query_vector):
            return []
        index = faiss.read_index(str(index_path))
        vector = np.asarray([query_vector], dtype="float32")
        faiss.normalize_L2(vector)
        scores, positions = index.search(vector, min(top_k, index.ntotal))
        items = metadata.get("items") or []
        results: list[dict[str, Any]] = []
        for score, position in zip(scores[0], positions[0]):
            if position < 0 or position >= len(items):
                continue
            results.append({**items[position], "score": round(float(score), 6)})
        return results
