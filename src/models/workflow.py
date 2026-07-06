from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import relationship
from datetime import datetime

from db.database import Base


class Workflow(Base):
    __tablename__ = "workflow"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    input_schema = Column(JSON, nullable=False, default=dict)
    schedule_cron = Column(String(255), nullable=True, index=True)
    is_enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True)

    operations = relationship(
        "WorkflowOperation",
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="WorkflowOperation.position.asc(), WorkflowOperation.id.asc()",
    )
    runs = relationship("WorkflowRun", back_populates="workflow")


class WorkflowOperation(Base):
    __tablename__ = "workflow_operation"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workflow_id = Column(Integer, ForeignKey("workflow.id"), nullable=False, index=True)

    name = Column(String(255), nullable=False)
    operation_type = Column(String(64), nullable=False, index=True)  # assistant | sub_workflow | future extensions
    operation_ref_id = Column(Integer, nullable=False, index=True)   # assistant_id or workflow_id

    config = Column(JSON, nullable=False, default=dict)
    depends_on = Column(JSON, nullable=False, default=list)          # list[int]
    on_success_follow_up = Column(Integer, nullable=True)
    on_failure_follow_up = Column(Integer, nullable=True)
    join_strategy = Column(String(32), nullable=False, default="all")
    timeout_seconds = Column(Integer, nullable=True)
    retry_policy = Column(JSON, nullable=False, default=dict)
    position = Column(Integer, nullable=False, default=100)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    workflow = relationship("Workflow", back_populates="operations")

    __table_args__ = (
        UniqueConstraint("workflow_id", "name", name="uq_workflow_operation_name"),
        Index("ix_workflow_operation_order", "workflow_id", "position", "id"),
    )


class WorkflowRun(Base):
    __tablename__ = "workflow_run"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workflow_id = Column(Integer, ForeignKey("workflow.id"), nullable=False, index=True)
    trigger_type = Column(String(32), nullable=False, index=True)  # manual | cron
    status = Column(String(32), nullable=False, default="queued", index=True)

    input_payload = Column(JSON, nullable=False, default=dict)
    result_payload = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    parent_run_id = Column(Integer, nullable=True, index=True)
    parent_operation_run_id = Column(Integer, nullable=True, index=True)
    parent_item_index = Column(Integer, nullable=True, index=True)

    cancel_requested_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)

    workflow = relationship("Workflow", back_populates="runs")
    operation_runs = relationship(
        "WorkflowOperationRun",
        back_populates="workflow_run",
        cascade="all, delete-orphan",
        order_by="WorkflowOperationRun.id.asc()",
    )


class WorkflowOperationRun(Base):
    __tablename__ = "workflow_operation_run"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workflow_run_id = Column(Integer, ForeignKey("workflow_run.id"), nullable=False, index=True)
    workflow_operation_id = Column(Integer, ForeignKey("workflow_operation.id"), nullable=False, index=True)

    status = Column(String(32), nullable=False, default="queued", index=True)
    input_payload = Column(JSON, nullable=False, default=dict)
    output_payload = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    trace = Column(JSON, nullable=True)

    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    progress_payload = Column(JSON, nullable=True)
    last_heartbeat_at = Column(DateTime, nullable=True)

    workflow_run = relationship("WorkflowRun", back_populates="operation_runs")
    operation = relationship("WorkflowOperation")
