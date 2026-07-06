from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class WorkflowOperationBase(BaseModel):
    name: str
    operation_type: str = Field(..., description="assistant | sub_workflow | future operation types")
    operation_ref_id: int
    config: Dict[str, Any] = Field(default_factory=dict)
    depends_on: List[int] = Field(default_factory=list)
    on_success_follow_up: Optional[int] = None
    on_failure_follow_up: Optional[int] = None
    join_strategy: str = "all"
    timeout_seconds: Optional[int] = None
    retry_policy: Dict[str, Any] = Field(default_factory=dict)
    position: int = 100


class WorkflowOperationCreate(WorkflowOperationBase):
    pass


class WorkflowOperationUpdate(BaseModel):
    name: Optional[str] = None
    operation_type: Optional[str] = None
    operation_ref_id: Optional[int] = None
    config: Optional[Dict[str, Any]] = None
    depends_on: Optional[List[int]] = None
    on_success_follow_up: Optional[int] = None
    on_failure_follow_up: Optional[int] = None
    join_strategy: Optional[str] = None
    timeout_seconds: Optional[int] = None
    retry_policy: Optional[Dict[str, Any]] = None
    position: Optional[int] = None


class WorkflowBase(BaseModel):
    name: str
    description: Optional[str] = None
    input_schema: Dict[str, Any] = Field(default_factory=dict)
    schedule_cron: Optional[str] = None
    is_enabled: bool = True


class WorkflowCreate(WorkflowBase):
    operations: List[WorkflowOperationCreate] = Field(default_factory=list)


class WorkflowUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    input_schema: Optional[Dict[str, Any]] = None
    schedule_cron: Optional[str] = None
    is_enabled: Optional[bool] = None
    operations: Optional[List[WorkflowOperationCreate]] = None


class WorkflowOperationRead(WorkflowOperationBase):
    id: int
    workflow_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class WorkflowRead(WorkflowBase):
    id: int
    created_at: datetime
    updated_at: datetime
    deleted_at: Optional[datetime] = None
    operations: List[WorkflowOperationRead] = Field(default_factory=list)

    class Config:
        from_attributes = True


class WorkflowTriggerRequest(BaseModel):
    input_payload: Dict[str, Any] = Field(default_factory=dict)


class WorkflowResumeRequest(BaseModel):
    type: str
    answer: Optional[str] = None
    approved: Optional[bool] = None
    reason: Optional[str] = None


class WorkflowRunRead(BaseModel):
    id: int
    workflow_id: int
    trigger_type: str
    status: str
    input_payload: Dict[str, Any]
    result_payload: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class WorkflowOperationRunRead(BaseModel):
    id: int
    workflow_run_id: int
    workflow_operation_id: int
    status: str
    input_payload: Dict[str, Any]
    output_payload: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    trace: Optional[Any] = None
    progress_payload: dict | None = None
    last_heartbeat_at: datetime | None = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    class Config:
        from_attributes = True
