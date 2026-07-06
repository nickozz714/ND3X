# services/assistant_project_service.py

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException

from repository.assistant_project_repository import AssistantProjectRepository


class AssistantProjectService:
    def __init__(self):
        self.repository = AssistantProjectRepository()

    async def create(self, data: Dict[str, Any]) -> Dict[str, Any]:
        name = (data.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="Project name is required")

        return await self.repository.create(
            name=name,
            description=data.get("description"),
            domain=data.get("domain"),
            repository_url=data.get("repository_url"),
            local_path=data.get("local_path"),
            metadata_=data.get("metadata") or data.get("metadata_") or {},
        )

    async def get(self, project_id: str) -> Dict[str, Any]:
        item = await self.repository.get(project_id)
        if not item:
            raise HTTPException(status_code=404, detail="Project not found")
        return item

    async def list(
        self,
        *,
        q: Optional[str] = None,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        return await self.repository.list(
            q=q,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )

    async def delete(self, project_id: str, *, delete_threads: bool = True, delete_memories: bool = False) -> Dict[str, Any]:
        counts = await self.repository.delete_project(
            project_id=project_id,
            delete_threads=delete_threads,
            delete_memories=delete_memories,
        )
        if counts is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return {"ok": True, "deleted": counts}

    async def update(self, project_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        item = await self.repository.update(
            project_id=project_id,
            name=data.get("name"),
            description=data.get("description"),
            domain=data.get("domain"),
            status=data.get("status"),
            is_archived=data.get("is_archived"),
            repository_url=data.get("repository_url"),
            local_path=data.get("local_path"),
            metadata_=data.get("metadata") or data.get("metadata_"),
        )
        if not item:
            raise HTTPException(status_code=404, detail="Project not found")
        return item