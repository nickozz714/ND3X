from __future__ import annotations

import hashlib
from typing import Any, Dict, List

from component.config import settings
from component.logging import get_logger
from services.system_cognition.models import utc_now_iso

log = get_logger(__name__)


class SystemEmbeddingService:
    """
    Creates deterministic embedding text for system memories and beliefs.

    Keep this outside repositories so repositories remain DB-only.
    """

    def __init__(self, *, openai_service):
        self.openai = openai_service
        # None → the LLM router resolves the embeddings slot (registry). No config default.
        self.model = None
        self.dimensions = getattr(settings, "SYSTEM_COGNITION_EMBEDDING_DIMENSIONS", None)

    def content_hash(self, text: str) -> str:
        return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

    def memory_text(self, memory: Dict[str, Any]) -> str:
        metadata = memory.get("metadata_") or {}
        tags = metadata.get("tags") or []
        domain = metadata.get("domain") or ""
        reason = metadata.get("reason") or ""

        return "\n".join(
            part
            for part in [
                f"kind: memory",
                f"type: {memory.get('type') or ''}",
                f"scope: {memory.get('scope') or ''}",
                f"domain: {domain}",
                f"tags: {', '.join(str(t) for t in tags)}" if isinstance(tags, list) else "",
                f"reason: {reason}",
                f"content: {memory.get('content') or ''}",
            ]
            if part.strip()
        )

    def belief_text(self, belief: Dict[str, Any]) -> str:
        insights = belief.get("insights") or []
        future_use = belief.get("future_use") or []
        use_when = belief.get("use_when") or []

        return "\n".join(
            part
            for part in [
                f"kind: belief",
                f"topic: {belief.get('topic') or ''}",
                f"domain: {belief.get('domain') or ''}",
                f"scope: {belief.get('scope') or ''}",
                f"summary: {belief.get('summary') or ''}",
                f"content: {belief.get('content') or ''}",
                "insights: " + " | ".join(str(x) for x in insights) if isinstance(insights, list) else "",
                "future_use: " + " | ".join(str(x) for x in future_use) if isinstance(future_use, list) else "",
                "use_when: " + " | ".join(str(x) for x in use_when) if isinstance(use_when, list) else "",
            ]
            if part.strip()
        )

    def embed_text(self, text: str) -> Dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {
                "embedding": None,
                "embedding_model": self.model,
                "embedding_hash": None,
                "embedding_updated_at": None,
            }

        vector = self.openai.embed(
            text,
            model=self.model,
            dimensions=self.dimensions,
            normalize=True,
        )

        return {
            "embedding": vector,
            "embedding_model": self.model,
            "embedding_hash": self.content_hash(text),
            "embedding_updated_at": utc_now_iso(),
        }

    def embed_batch(self, texts: List[str]) -> List[Dict[str, Any]]:
        clean = [(text or "").strip() for text in texts]
        if not clean:
            return []

        vectors = self.openai.embed_batch(
            clean,
            model=self.model,
            dimensions=self.dimensions,
            normalize=True,
            batch_size=int(getattr(settings, "SYSTEM_COGNITION_EMBEDDING_BATCH_SIZE", 64)),
        )

        now = utc_now_iso()
        return [
            {
                "embedding": vector,
                "embedding_model": self.model,
                "embedding_hash": self.content_hash(text),
                "embedding_updated_at": now,
            }
            for text, vector in zip(clean, vectors)
        ]
