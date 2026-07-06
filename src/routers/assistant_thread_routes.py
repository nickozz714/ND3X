from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from authentication.dependencies import require_user
from services.assistant_project_service import AssistantProjectService
from services.assistant_thread_service import AssistantThreadService


router = APIRouter(
    prefix="/threads",
    tags=["Assistant threads"],
    dependencies=[Depends(require_user)],
)


@router.post("/projects")
async def create_project(payload: Dict[str, Any]):
    return await AssistantProjectService().create(payload)


@router.get("/projects")
async def list_projects(
    q: Optional[str] = None,
    include_archived: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    return await AssistantProjectService().list(
        q=q,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )


@router.get("/projects/{project_id}")
async def get_project(project_id: str):
    return await AssistantProjectService().get(project_id)


@router.patch("/projects/{project_id}")
async def update_project(project_id: str, payload: Dict[str, Any]):
    return await AssistantProjectService().update(project_id, payload)


@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: str,
    delete_threads: bool = Query(default=True),
    delete_memories: bool = Query(default=False),
):
    """Delete a project (and by default its threads + messages). Set
    delete_memories=true to also remove thread/project-scoped memories, beliefs
    and curiosity jobs — the front-end asks the user which they want."""
    return await AssistantProjectService().delete(
        project_id, delete_threads=delete_threads, delete_memories=delete_memories
    )


@router.get("/threads")
async def list_threads(
    project_id: Optional[str] = None,
    include_archived: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    return await AssistantThreadService().list_threads(
        project_id=project_id,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )


@router.get("/threads/{thread_id}")
async def get_thread(thread_id: str):
    return await AssistantThreadService().get_thread(thread_id)

@router.patch("/threads/{thread_id}")
async def update_thread(thread_id: str, payload: Dict[str, Any]):
    return await AssistantThreadService().update_thread(thread_id, payload)


@router.delete("/threads/{thread_id}")
async def delete_thread(
    thread_id: str,
    delete_memories: bool = Query(default=False),
):
    """Delete a thread + its messages. Set delete_memories=true to also remove
    the thread's memories, beliefs and curiosity jobs (the front-end asks)."""
    return await AssistantThreadService().delete_thread(thread_id, delete_memories=delete_memories)

@router.post("/threads/{thread_id}/messages/{message_id}/important")
async def set_message_important(thread_id: str, message_id: str, payload: Dict[str, Any]):
    """Flag/unflag a message as important. Flagging forces it into the cognition
    pipeline (memory/belief/curiosity), bypassing the triviality router."""
    important = bool(payload.get("important", True))
    return await AssistantThreadService().mark_message_important(
        thread_id=thread_id, message_id=message_id, important=important
    )


@router.get("/threads/{thread_id}/messages")
async def list_thread_messages(
    thread_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    return await AssistantThreadService().list_messages(
        thread_id=thread_id,
        limit=limit,
        offset=offset,
    )