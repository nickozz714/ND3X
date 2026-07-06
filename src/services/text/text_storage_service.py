"""
services/text/text_storage_service.py
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional

from sqlalchemy.orm import Session

from repository.text_repository import TextRepository


@dataclass(frozen=True)
class IncomingText:
    source: str
    title: Optional[str]
    content: str
    subdir: str = "inbox"


@dataclass(frozen=True)
class IncomingCode:
    source: str
    title: Optional[str]
    content: str
    language: str
    subdir: str = "inbox"


_EXT_MAP = {
    "python": ".py", "javascript": ".js", "java": ".java",
    "html": ".html", "css": ".css", "typescript": ".tsx", "other": ".txt",
}


def slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "untitled"


class TextStorageService:
    def __init__(self, files_root: str):
        self.files_root = Path(files_root)
        self.files_root.mkdir(parents=True, exist_ok=True)

    def save_markdown(self, item: IncomingText) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        slug = slugify(item.title or "note")
        dir_path = self.files_root / item.subdir
        dir_path.mkdir(parents=True, exist_ok=True)
        path = dir_path / f"{ts}_{slug}.md"
        md = []
        if item.title:
            md.append(f"# {item.title}\n")
        md.append(item.content.rstrip() + "\n")
        path.write_text("\n".join(md), encoding="utf-8")
        return path

    def save_code(self, item: IncomingCode) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        slug = slugify(item.title or "code")
        ext = _EXT_MAP.get(item.language.lower(), ".txt")
        dir_path = self.files_root / item.subdir
        dir_path.mkdir(parents=True, exist_ok=True)
        path = dir_path / f"{ts}_{slug}{ext}"
        path.write_text(item.content, encoding="utf-8")
        return path

    def list_files(self, db: Session) -> List[Dict]:
        root = self.files_root.resolve()
        results = []
        try:
            for f in root.rglob("*"):
                if not f.is_file():
                    continue
                repo = TextRepository(db)
                row = repo.get_doc_by_file_path(str(f))
                results.append({
                    "name": f.name,
                    "path": str(f.relative_to(root)),
                    "doc_id": row["id"] if row else None,
                })
            return sorted(results, key=lambda x: x["path"])
        except Exception as e:
            return [{"error": str(e)}]

    def get_file(self, file_path: str) -> str:
        root = self.files_root.resolve()
        path = (root / file_path).resolve()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError("File not found")
        return path.read_text(encoding="utf-8")