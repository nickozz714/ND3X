from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from services.system_cognition.factory import create_system_cognition_service
from services.system_cognition.system_cognition_query_service import SystemCognitionQueryService

router = APIRouter(prefix="/system-cognition", tags=["system-cognition"])

query_service = SystemCognitionQueryService()


@router.get("/overview")
async def get_system_cognition_overview():
    return await query_service.get_overview()


@router.get("/memories")
async def list_memories(
    q: Optional[str] = Query(default=None),
    type_: Optional[str] = Query(default=None, alias="type"),
    scope: Optional[str] = Query(default=None),
    thread_id: Optional[str] = Query(default=None),
    project_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    return await query_service.list_memories(
        q=q,
        type_=type_,
        scope=scope,
        thread_id=thread_id,
        project_id=project_id,
        limit=limit,
        offset=offset,
    )


@router.get("/beliefs")
async def list_beliefs(
    q: Optional[str] = Query(default=None),
    topic: Optional[str] = Query(default=None),
    domain: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    scope: Optional[str] = Query(default=None),
    thread_id: Optional[str] = Query(default=None),
    project_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    return await query_service.list_beliefs(
        q=q,
        topic=topic,
        domain=domain,
        status=status,
        scope=scope,
        thread_id=thread_id,
        project_id=project_id,
        limit=limit,
        offset=offset,
    )


@router.get("/curiosity-jobs")
async def list_curiosity_jobs(
    q: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    depth: Optional[str] = Query(default=None),
    thread_id: Optional[str] = Query(default=None),
    project_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    return await query_service.list_curiosity_jobs(
        q=q,
        status=status,
        depth=depth,
        thread_id=thread_id,
        project_id=project_id,
        limit=limit,
        offset=offset,
    )

@router.delete("/memories/{memory_id}")
async def delete_memory(memory_id: str):
    service, _dispatcher = create_system_cognition_service()
    return await service.delete_memory(memory_id)

@router.delete("/beliefs/{belief_id}")
async def delete_belief(belief_id: str):
    service, _dispatcher = create_system_cognition_service()
    return await service.delete_belief(belief_id)

@router.delete("/curiosity-jobs/{job_id}")
async def delete_curiosity_job(job_id: str):
    service, _dispatcher = create_system_cognition_service()
    return await service.delete_curiosity_job(job_id)