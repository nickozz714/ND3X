from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from component.config import settings
from repository.skill_file_repository import SkillFileRepository
from repository.skill_repository import SkillRepository


def skill_files_base_dir() -> Path:
    return (Path(settings.FILES_DIR) / "skills").resolve()


def validate_skill_relative_path(relative_path: str) -> str:
    raw = (relative_path or "").replace("\\", "/").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="relative_path is required")
    if len(raw) > 512:
        raise HTTPException(status_code=400, detail="relative_path must be 512 characters or fewer")
    if raw.startswith("/"):
        raise HTTPException(status_code=400, detail="relative_path must not be absolute")
    posix = PurePosixPath(raw)
    if posix.is_absolute() or any(part in ("", ".", "..") for part in posix.parts):
        raise HTTPException(status_code=400, detail="relative_path must not contain path traversal")
    normalized = posix.as_posix()
    if normalized != raw:
        raise HTTPException(status_code=400, detail="relative_path must be normalized")
    return normalized


def safe_skill_key(skill: Any) -> str:
    # Skill.name is unique but mutable and not constrained to a safe slug pattern.
    # The immutable database id gives a stable, collision-free directory key.
    return str(int(getattr(skill, "id")))


class SkillFileService:
    def __init__(self, db: Session):
        self.db = db
        self.skill_repo = SkillRepository(db)
        self.repo = SkillFileRepository(db)

    def _get_skill(self, skill_id: int):
        skill = self.skill_repo.get_by_id(skill_id)
        if not skill:
            raise HTTPException(status_code=404, detail="Skill not found")
        return skill

    def skill_files_root(self, skill_id: int) -> Path:
        skill = self._get_skill(skill_id)
        root = (skill_files_base_dir() / safe_skill_key(skill)).resolve()
        base = skill_files_base_dir()
        if root != base and base not in root.parents:
            raise HTTPException(status_code=400, detail="Resolved skill files root escapes FILES_DIR")
        return root

    def resolve_skill_file_path(self, skill_id: int, relative_path: str) -> Path:
        normalized = validate_skill_relative_path(relative_path)
        root = self.skill_files_root(skill_id)
        target = (root / normalized).resolve()
        if target != root and root not in target.parents:
            raise HTTPException(status_code=400, detail="Resolved file path escapes skill directory")
        files_dir = Path(settings.FILES_DIR).resolve()
        if target != files_dir and files_dir not in target.parents:
            raise HTTPException(status_code=400, detail="Resolved file path escapes FILES_DIR")
        return target

    @staticmethod
    def compute_checksum_sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _read_metadata(path: Path, content_type: str | None = None) -> dict[str, Any]:
        data = path.read_bytes()
        return {
            "size_bytes": len(data),
            "checksum_sha256": SkillFileService.compute_checksum_sha256(data),
            "content_type": content_type,
        }

    @staticmethod
    def _atomic_write(target: Path, data: bytes, *, executable: bool) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
            if executable:
                current = target.stat().st_mode
                target.chmod(current | 0o111)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    def runtime_path_for(self, skill_id: int, relative_path: str) -> str:
        return str(self.resolve_skill_file_path(skill_id, relative_path))

    def runtime_root_for(self, skill_id: int) -> str:
        return str(self.skill_files_root(skill_id))

    def to_metadata(self, item, *, include_content: bool = False) -> dict[str, Any]:
        data = {
            "id": item.id,
            "skill_id": item.skill_id,
            "relative_path": item.relative_path,
            "filename": item.filename,
            "runtime_path": self.runtime_path_for(item.skill_id, item.relative_path),
            "content_type": item.content_type,
            "size_bytes": item.size_bytes,
            "checksum_sha256": item.checksum_sha256,
            "is_editable": item.is_editable,
            "is_executable": item.is_executable,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
        if include_content:
            path = self.resolve_skill_file_path(item.skill_id, item.relative_path)
            data["content"] = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        return data

    def manifest_metadata_for_skill(self, skill_id: int) -> dict[str, Any]:
        self._get_skill(skill_id)
        return {
            "skill_files_root": self.runtime_root_for(skill_id),
            "skill_files": [
                {
                    "relative_path": item.relative_path,
                    "runtime_path": self.runtime_path_for(item.skill_id, item.relative_path),
                    "content_type": item.content_type,
                    "size_bytes": item.size_bytes,
                    "checksum_sha256": item.checksum_sha256,
                    "is_executable": item.is_executable,
                }
                for item in self.repo.list_for_skill(skill_id)
            ],
        }

    def list_skill_files(self, skill_id: int) -> list[dict[str, Any]]:
        self._get_skill(skill_id)
        return [self.to_metadata(item, include_content=False) for item in self.repo.list_for_skill(skill_id)]

    def get_skill_file(self, skill_id: int, file_id: int, *, include_content: bool = True) -> dict[str, Any]:
        self._get_skill(skill_id)
        item = self.repo.get_for_skill_by_id(skill_id, file_id)
        if not item:
            raise HTTPException(status_code=404, detail="Skill file not found")
        return self.to_metadata(item, include_content=include_content)

    def create_or_update_skill_file(self, skill_id: int, relative_path: str, content: str, metadata: dict[str, Any] | None = None):
        self._get_skill(skill_id)
        metadata = metadata or {}
        normalized = validate_skill_relative_path(relative_path)
        target = self.resolve_skill_file_path(skill_id, normalized)
        data = (content or "").encode("utf-8")
        is_executable = bool(metadata.get("is_executable", False))
        self._atomic_write(target, data, executable=is_executable)
        written = self._read_metadata(target, content_type=metadata.get("content_type"))
        values = {
            "skill_id": skill_id,
            "relative_path": normalized,
            "filename": PurePosixPath(normalized).name,
            "content_type": written["content_type"],
            "size_bytes": written["size_bytes"],
            "checksum_sha256": written["checksum_sha256"],
            "is_editable": bool(metadata.get("is_editable", True)),
            "is_executable": is_executable,
        }
        existing = self.repo.get_by_path(skill_id, normalized)
        if existing:
            return self.repo.update(existing, **values)
        return self.repo.create(**values)

    def update_skill_file(self, skill_id: int, file_id: int, *, content: str, metadata: dict[str, Any] | None = None):
        self._get_skill(skill_id)
        item = self.repo.get_for_skill_by_id(skill_id, file_id)
        if not item:
            raise HTTPException(status_code=404, detail="Skill file not found")
        metadata = metadata or {}
        relative_path = metadata.get("relative_path") or item.relative_path
        normalized = validate_skill_relative_path(relative_path)
        old_path = self.resolve_skill_file_path(skill_id, item.relative_path)
        target = self.resolve_skill_file_path(skill_id, normalized)
        data = (content or "").encode("utf-8")
        is_executable = bool(metadata.get("is_executable", item.is_executable))
        self._atomic_write(target, data, executable=is_executable)
        if target != old_path and old_path.exists():
            old_path.unlink()
        written = self._read_metadata(target, content_type=metadata.get("content_type", item.content_type))
        return self.repo.update(
            item,
            relative_path=normalized,
            filename=PurePosixPath(normalized).name,
            content_type=written["content_type"],
            size_bytes=written["size_bytes"],
            checksum_sha256=written["checksum_sha256"],
            is_editable=bool(metadata.get("is_editable", item.is_editable)),
            is_executable=is_executable,
        )

    def delete_skill_file(self, skill_id: int, file_id: int) -> dict[str, bool]:
        self._get_skill(skill_id)
        item = self.repo.get_for_skill_by_id(skill_id, file_id)
        if not item:
            raise HTTPException(status_code=404, detail="Skill file not found")
        path = self.resolve_skill_file_path(skill_id, item.relative_path)
        self.repo.delete(item)
        if path.exists():
            path.unlink()
        return {"success": True}
