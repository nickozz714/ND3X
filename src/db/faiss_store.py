"""
db/faiss_store.py

Dunne wrapper om een FAISS flat-L2 index met persistentie.
Index wordt opgeslagen in FILES_DIR/faiss/index.bin.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

import numpy as np

from component.logging import get_logger

log = get_logger(__name__)

_DIM = 1536  # text-embedding-ada-002 / text-embedding-3-small


class FaissStore:
    def __init__(self, index_path: str):
        import faiss

        self._faiss = faiss
        self._path = Path(index_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

        if self._path.exists():
            log.infox("FAISS index laden", path=str(self._path))
            self._index = faiss.read_index(str(self._path))
        else:
            log.infox("Nieuwe FAISS index aanmaken", path=str(self._path))
            base = faiss.IndexFlatL2(_DIM)
            self._index = faiss.IndexIDMap(base)

    def add(self, vector: List[float], *, embedding_id: int) -> None:
        vec = np.array([vector], dtype=np.float32)
        ids = np.array([embedding_id], dtype=np.int64)
        self._index.add_with_ids(vec, ids)

    def search(self, vector: List[float], *, top_k: int = 5) -> Tuple[List[int], List[float]]:
        vec = np.array([vector], dtype=np.float32)
        distances, ids = self._index.search(vec, top_k)
        valid_ids = [int(i) for i in ids[0] if i != -1]
        valid_scores = [float(distances[0][idx]) for idx, i in enumerate(ids[0]) if i != -1]
        return valid_ids, valid_scores

    def remove_ids(self, ids: List[int]) -> int:
        if not ids:
            return 0
        id_arr = np.array(ids, dtype=np.int64)
        selector = self._faiss.IDSelectorArray(id_arr)
        removed = self._index.remove_ids(selector)
        return int(removed)

    def persist(self) -> None:
        self._faiss.write_index(self._index, str(self._path))
        log.debugx("FAISS index opgeslagen", path=str(self._path))

    @property
    def ntotal(self) -> int:
        return self._index.ntotal