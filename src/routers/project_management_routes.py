from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional, Any

from fastapi import APIRouter, Query, Body, HTTPException
from routers._mcp_proxy import mcp_proxy_call, mcp_proxy_health

from component.config import settings
from services.mcp.mcp_client import MCPClient

router = APIRouter(prefix="/pm", tags=["pm"])
mcp = MCPClient(mcp_url=settings.MCP_URL, bearer=settings.MCP_BEARER)

# -----------------------------
# Helpers
# -----------------------------

def _dt_from_iso(value: Optional[str]) -> Optional[datetime]:
    if value is None or value == "":
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Invalid datetime ISO string: {value}") from e


SERVICE_NAME = "pm"
HEALTH_TOOL = "pm_health"


async def _call(tool_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return await mcp_proxy_call(
        mcp=mcp,
        service=SERVICE_NAME,
        tool=tool_name,
        payload=payload,
    )

# ---------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------

@router.get("/health")
async def pm_health() -> Dict[str, Any]:
    return await mcp_proxy_health(
        mcp=mcp,
        service=SERVICE_NAME,
        tool=HEALTH_TOOL,
    )


# ---------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------

@router.post("/projects")
async def pm_project_create(name: str = Body(...), description: Optional[str] = Body(...)) -> Dict[str, Any]:
    payload = {"name": name, "description": description} if description else {"name": name}
    return await _call("pm_project_create", payload=payload)


@router.get("/projects/{project_id}")
async def pm_project_get(project_id: int) -> Dict[str, Any]:
    return await _call("pm_project_get", {"project_id": project_id})


@router.patch("/projects/{project_id}")
async def pm_project_update(
    project_id: int,
    name: Optional[str] = Body(None),
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"project_id": project_id}
    if name is not None:
        payload["name"] = name
    return await _call("pm_project_update", payload)


@router.delete("/projects/{project_id}")
async def pm_project_delete(project_id: int) -> Dict[str, Any]:
    return await _call("pm_project_delete", {"project_id": project_id})


@router.get("/projects")
async def pm_project_list(
    q: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    sort_field: str = Query("updated_at"),
    sort_dir: str = Query("desc"),
) -> Dict[str, Any]:
    return await _call(
        "pm_project_list",
        {"q": q, "limit": limit, "offset": offset, "sort_field": sort_field, "sort_dir": sort_dir},
    )


@router.get("/projects/{project_id}/full")
async def pm_project_get_full(project_id: int) -> Dict[str, Any]:
    return await _call("pm_project_get_full", {"project_id": project_id})


# ---------------------------------------------------------------------
# Epics
# ---------------------------------------------------------------------

@router.post("/planning/epics")
async def pm_epic_create(
    project_id: int = Body(...),
    name: str = Body(...),
    description: str = Body(""),
) -> Dict[str, Any]:
    return await _call(
        "pm_epic_create",
        {"project_id": project_id, "name": name, "description": description},
    )


@router.get("/planning/projects/{project_id}/epics")
async def pm_epic_list(
    project_id: int,
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    return await _call("pm_epic_list", {"project_id": project_id, "limit": limit, "offset": offset})


@router.patch("/planning/epics/{epic_id}")
async def pm_epic_update(
    epic_id: int,
    name: Optional[str] = Body(None),
    description: Optional[str] = Body(None),
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"epic_id": epic_id}
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    return await _call("pm_epic_update", payload)


@router.delete("/planning/epics/{epic_id}")
async def pm_epic_delete(epic_id: int) -> Dict[str, Any]:
    return await _call("pm_epic_delete", {"epic_id": epic_id})


# ---------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------

@router.post("/planning/features")
async def pm_feature_create(
    epic_id: int = Body(...),
    name: str = Body(...),
    description: str = Body(""),
) -> Dict[str, Any]:
    return await _call(
        "pm_feature_create",
        {"epic_id": epic_id, "name": name, "description": description},
    )


@router.get("/planning/epics/{epic_id}/features")
async def pm_feature_list(
    epic_id: int,
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    return await _call("pm_feature_list", {"epic_id": epic_id, "limit": limit, "offset": offset})


@router.patch("/planning/features/{feature_id}")
async def pm_feature_update(
    feature_id: int,
    name: Optional[str] = Body(None),
    description: Optional[str] = Body(None),
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"feature_id": feature_id}
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    return await _call("pm_feature_update", payload)


@router.delete("/planning/features/{feature_id}")
async def pm_feature_delete(feature_id: int) -> Dict[str, Any]:
    return await _call("pm_feature_delete", {"feature_id": feature_id})


# ---------------------------------------------------------------------
# WorkItems
# ---------------------------------------------------------------------

@router.post("/planning/workitems")
async def pm_workitem_create(
    feature_id: int = Body(...),
    name: str = Body(...),
    description: str = Body(""),
) -> Dict[str, Any]:
    return await _call(
        "pm_workitem_create",
        {"feature_id": feature_id, "name": name, "description": description},
    )


@router.get("/planning/features/{feature_id}/workitems")
async def pm_workitem_list(
    feature_id: int,
    limit: int = Query(1000, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    return await _call("pm_workitem_list", {"feature_id": feature_id, "limit": limit, "offset": offset})


@router.patch("/planning/workitems/{workitem_id}")
async def pm_workitem_update(
    workitem_id: int,
    name: Optional[str] = Body(None),
    description: Optional[str] = Body(None),
    progress: Optional[str] = Body(None),
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"workitem_id": workitem_id}
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    if progress is not None:
        payload["progress"] = progress
    return await _call("pm_workitem_update", payload)


@router.delete("/planning/workitems/{workitem_id}")
async def pm_workitem_delete(workitem_id: int) -> Dict[str, Any]:
    return await _call("pm_workitem_delete", {"workitem_id": workitem_id})


# ---------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------

@router.post("/planning/tasks")
async def pm_task_create(
    workitem_id: int = Body(...),
    name: str = Body(...),
    description: str = Body(""),
) -> Dict[str, Any]:
    return await _call(
        "pm_task_create",
        {"workitem_id": workitem_id, "name": name, "description": description},
    )


@router.get("/planning/workitems/{workitem_id}/tasks")
async def pm_task_list(
    workitem_id: int,
    include_done: bool = Query(True),
    limit: int = Query(2000, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    return await _call(
        "pm_task_list",
        {"workitem_id": workitem_id, "include_done": include_done, "limit": limit, "offset": offset},
    )


@router.patch("/planning/tasks/{task_id}")
async def pm_task_update(
    task_id: int,
    name: Optional[str] = Body(None),
    description: Optional[str] = Body(None),
    progress: Optional[str] = Body(None),
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"task_id": task_id}
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    if progress is not None:
        payload["progress"] = progress
    return await _call("pm_task_update", payload)


@router.delete("/planning/tasks/{task_id}")
async def pm_task_delete(task_id: int) -> Dict[str, Any]:
    return await _call("pm_task_delete", {"task_id": task_id})


# ---------------------------------------------------------------------
# Time tracking
# ---------------------------------------------------------------------

@router.post("/time/start")
async def pm_time_start(
    project_id: int = Body(...),
    registration_type: str = Body(...), # "task" | "workitem"
    item_id: int = Body(...),
    hour_code: str = Body(...),
    hour_code_type: str = Body(...),  # "zakelijk" | "niet_zakelijk"
    note: str = Body(""),
) -> Dict[str, Any]:
    return await _call(
        "pm_time_start",
        {
            "project_id": project_id,
            "registration_type": registration_type,
            "item_id": item_id,
            "hour_code": hour_code,
            "hour_code_type": hour_code_type,
            "note": note,
        },
    )


@router.post("/time/stop/{entry_id}")
async def pm_time_stop(entry_id: int, note: str = Body("", embed=True),) -> Dict[str, Any]:
    return await _call("pm_time_stop", {"entry_id": entry_id, "note": note})


# ---------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------

@router.post("/reports/hours")
async def pm_report_hours_by_task(
    project_id: int = Body(...),
    from_date: Optional[str] = Body(None),  # ISO datetime
    to_date: Optional[str] = Body(None),    # ISO datetime
    hour_code: Optional[str] = Body(None),
    hour_code_type: Optional[str] = Body(None),
) -> Dict[str, Any]:
    # validate ISO strings early (optional but consistent with example)
    _ = _dt_from_iso(from_date)
    _ = _dt_from_iso(to_date)

    payload: Dict[str, Any] = {"project_id": project_id}
    if from_date is not None:
        payload["from_date"] = from_date
    if to_date is not None:
        payload["to_date"] = to_date
    if hour_code is not None:
        payload["hour_code"] = hour_code
    if hour_code_type is not None:
        payload["hour_code_type"] = hour_code_type

    return await _call("pm_report_hours_by_task", payload)

@router.post("/reports/hours_by_day_code")
async def pm_report_hours_by_day_code(
        project_id: int = Body(...),
        from_date: Optional[str] = Body(None),
        to_date: Optional[str] = Body(None),
        hour_code: Optional[str] = Body(None),
        hour_code_type: Optional[str] = Body(None),
):
    _ = _dt_from_iso(from_date)
    _ = _dt_from_iso(to_date)
    payload = {
        "project_id": project_id,
        "from_date": from_date,
        "to_date": to_date,
        "hour_code": hour_code,
        "hour_code_type": hour_code_type,
    }

    payload = {k: v for k, v in payload.items() if v is not None}
    return await _call("pm_report_hours_by_day_code", payload)

@router.post("/reports/hours_by_day_code/all_projects")
async def pm_report_hours_by_day_code(
        project_id: int = Body(None),
        from_date: Optional[str] = Body(None),
        to_date: Optional[str] = Body(None),
):
    _ = _dt_from_iso(from_date)
    _ = _dt_from_iso(to_date)
    payload = {
        "project_id": project_id,
        "from_date": from_date,
        "to_date": to_date,
    }

    payload = {k: v for k, v in payload.items() if v is not None}
    return await _call("pm_report_hours_by_day_code_all_projects", payload)

@router.get("/reports/active-tasks")
async def pm_report_active_tasks() -> Any:
    return await _call("pm_report_active_tasks", {})

@router.get("/reports/active-workitems")
async def pm_report_active_tasks() -> Any:
    return await _call("pm_report_active_workitems", {})

@router.post("/reports/hours_by_code")
async def pm_report_hours_by_code(
    project_id: int = Body(...),
    from_date: Optional[str] = Body(None),  # ISO datetime
    to_date: Optional[str] = Body(None),    # ISO datetime
) -> Dict[str, Any]:
    _ = _dt_from_iso(from_date)
    _ = _dt_from_iso(to_date)

    payload: Dict[str, Any] = {"project_id": project_id}
    if from_date is not None:
        payload["from_date"] = from_date
    if to_date is not None:
        payload["to_date"] = to_date

    return await _call("pm_report_hours_by_code", payload)
