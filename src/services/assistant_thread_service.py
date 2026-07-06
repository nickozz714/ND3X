# services/assistant_thread_service.py

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException

from repository.assistant_project_repository import AssistantProjectRepository
from repository.assistant_thread_repository import AssistantThreadRepository


class AssistantThreadService:
    def __init__(self):
        self.repository = AssistantThreadRepository()
        self.project_repository = AssistantProjectRepository()

    async def ensure_thread(
        self,
        *,
        thread_id: str,
        project_id: Optional[str] = None,
        title: Optional[str] = None,
        metadata_: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if project_id:
            project = await self.project_repository.get(project_id)
            if not project:
                raise HTTPException(status_code=404, detail="Project not found")

        return await self.repository.ensure_thread(
            thread_id=thread_id,
            project_id=project_id,
            title=title,
            metadata_=metadata_,
        )

    async def get_thread(self, thread_id: str) -> Dict[str, Any]:
        item = await self.repository.get_thread(thread_id)
        if not item:
            raise HTTPException(status_code=404, detail="Thread not found")
        return item

    async def list_threads(
        self,
        *,
        project_id: Optional[str] = None,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        return await self.repository.list_threads(
            project_id=project_id,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )

    async def add_user_message(
        self,
        *,
        thread_id: str,
        content: str,
        turn_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        return await self.repository.add_message(
            thread_id=thread_id,
            role="user",
            content=content,
            turn_id=turn_id,
        )

    async def add_assistant_message(
        self,
        *,
        thread_id: str,
        content: str,
        turn_id: Optional[int] = None,
        steps: Optional[list] = None,
    ) -> Dict[str, Any]:
        return await self.repository.add_message(
            thread_id=thread_id,
            role="assistant",
            content=content,
            turn_id=turn_id,
            steps=steps,
        )

    async def list_messages(
        self,
        *,
        thread_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        await self.get_thread(thread_id)
        return await self.repository.list_messages(
            thread_id=thread_id,
            limit=limit,
            offset=offset,
        )

    async def mark_message_important(
        self, *, thread_id: str, message_id: str, important: bool
    ) -> Dict[str, Any]:
        data = await self.repository.set_message_important(
            thread_id=thread_id, message_id=message_id, important=important
        )
        if data is None:
            raise HTTPException(status_code=404, detail="Message not found")
        # Flagging a message forces it through the cognition pipeline (memory,
        # belief, curiosity) in the background, bypassing the triviality router.
        if important:
            import asyncio
            asyncio.create_task(self._run_forced_cognition(thread_id, data))
        return {"ok": True, "important": important, "message": data["message"]}

    async def _run_forced_cognition(self, thread_id: str, data: Dict[str, Any]) -> None:
        try:
            from services.openai_service import OpenAIResponsesService
            from services.system_cognition.factory import create_system_cognition_service
            openai = OpenAIResponsesService(model=None, embedding_model=None)
            svc, _ = create_system_cognition_service(openai_service=openai)
            await svc.post_turn(
                question=data.get("question") or "",
                answer=data.get("answer") or "",
                thread_id=thread_id,
                project_id=data.get("project_id"),
                turn_id=data.get("turn_id") or 0,
                force_important=True,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort background job
            from component.logging import get_logger
            get_logger(__name__).warningx(
                "Forced cognition for important message failed",
                thread_id=thread_id, error=str(exc),
            )

    async def delete_thread(self, thread_id: str, *, delete_memories: bool = False) -> Dict[str, Any]:
        counts = await self.repository.delete_thread(
            thread_id=thread_id,
            delete_memories=delete_memories,
        )
        if counts is None:
            raise HTTPException(status_code=404, detail="Thread not found")
        return {"ok": True, "deleted": counts}

    async def update_thread(self, thread_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        if "project_id" in data:
            project_id = data.get("project_id")

            if project_id:
                project = await self.project_repository.get(project_id)
                if not project:
                    raise HTTPException(status_code=404, detail="Project not found")
        else:
            project_id = None

        item = await self.repository.update_thread(
            thread_id=thread_id,
            title=data.get("title") if "title" in data else None,
            project_id=project_id,
            project_id_provided=("project_id" in data),
            status=data.get("status") if "status" in data else None,
            is_archived=data.get("is_archived") if "is_archived" in data else None,
            metadata_=data.get("metadata") or data.get("metadata_") if ("metadata" in data or "metadata_" in data) else None,
        )

        if not item:
            raise HTTPException(status_code=404, detail="Thread not found")

        return item