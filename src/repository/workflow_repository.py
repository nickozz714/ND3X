from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session, joinedload

from models.workflow import Workflow, WorkflowOperation
from schemas.workflow import WorkflowCreate, WorkflowUpdate


class WorkflowRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_all(self, skip: int = 0, limit: int = 100, include_disabled: bool = True):
        q = self.db.query(Workflow).filter(Workflow.deleted_at.is_(None))
        if not include_disabled:
            q = q.filter(Workflow.is_enabled.is_(True))
        return q.order_by(Workflow.name.asc()).offset(skip).limit(limit).all()

    def get_by_id(self, workflow_id: int) -> Optional[Workflow]:
        return (
            self.db.query(Workflow)
            .filter(Workflow.id == workflow_id, Workflow.deleted_at.is_(None))
            .first()
        )

    def get_by_name(self, name: str) -> Optional[Workflow]:
        return (
            self.db.query(Workflow)
            .filter(Workflow.name == name, Workflow.deleted_at.is_(None))
            .first()
        )

    def get_with_operations(self, workflow_id: int) -> Optional[Workflow]:
        return (
            self.db.query(Workflow)
            .options(joinedload(Workflow.operations))
            .filter(Workflow.id == workflow_id, Workflow.deleted_at.is_(None))
            .first()
        )

    def get_enabled_scheduled(self):
        return (
            self.db.query(Workflow)
            .filter(
                Workflow.deleted_at.is_(None),
                Workflow.is_enabled.is_(True),
                Workflow.schedule_cron.isnot(None),
            )
            .all()
        )

    def create(self, data: WorkflowCreate) -> Workflow:
        workflow = Workflow(
            name=data.name,
            description=data.description,
            input_schema=data.input_schema or {},
            schedule_cron=data.schedule_cron,
            is_enabled=data.is_enabled,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )

        self.db.add(workflow)
        self.db.flush()

        self._replace_operations_with_position_resolution(
            workflow=workflow,
            operations=data.operations or [],
        )

        self.db.commit()
        self.db.refresh(workflow)
        return workflow

    def update(self, workflow_id: int, data: WorkflowUpdate) -> Optional[Workflow]:
        workflow = self.get_with_operations(workflow_id)
        if not workflow:
            return None

        values = data.model_dump(exclude_unset=True)
        operations = values.pop("operations", None)

        for key, value in values.items():
            setattr(workflow, key, value)

        if operations is not None:
            workflow.operations.clear()
            self.db.flush()

            self._replace_operations_with_position_resolution(
                workflow=workflow,
                operations=operations,
            )

        workflow.updated_at = datetime.utcnow()

        self.db.commit()
        self.db.refresh(workflow)
        return workflow

    def delete(self, workflow_id: int) -> bool:
        workflow = self.get_by_id(workflow_id)
        if not workflow:
            return False

        workflow.deleted_at = datetime.utcnow()
        workflow.is_enabled = False
        workflow.updated_at = datetime.utcnow()

        self.db.commit()
        return True

    def _replace_operations_with_position_resolution(
        self,
        *,
        workflow: Workflow,
        operations: list[Any],
    ) -> None:
        """
        Persist workflow operations in two passes.

        Incoming references are expected to be 1-based position references:

            depends_on: [1, 2]
            on_success_follow_up: 3
            on_failure_follow_up: null

        Pass 1:
            Create all operations without relational refs so DB ids exist.

        Pass 2:
            Convert position refs to DB ids and update the saved operations.
        """

        normalized_ops = [
            self._normalize_operation_payload(op, fallback_position=index)
            for index, op in enumerate(operations, start=1)
        ]

        position_to_db_operation: Dict[int, WorkflowOperation] = {}

        # PASS 1: create operations without graph refs.
        for op in normalized_ops:
            position = int(op.get("position") or len(position_to_db_operation) + 1)

            db_op = WorkflowOperation(
                workflow_id=workflow.id,
                name=op["name"],
                operation_type=op["operation_type"],
                operation_ref_id=op["operation_ref_id"],
                config=op.get("config") or {},
                depends_on=[],
                on_success_follow_up=None,
                on_failure_follow_up=None,
                join_strategy=op.get("join_strategy") or "all",
                timeout_seconds=op.get("timeout_seconds"),
                retry_policy=op.get("retry_policy"),
                position=position,
            )

            self.db.add(db_op)
            self.db.flush()

            position_to_db_operation[position] = db_op

        # PASS 2: resolve position refs to real DB ids.
        for op in normalized_ops:
            position = int(op.get("position") or 0)
            db_op = position_to_db_operation.get(position)
            if not db_op:
                continue

            db_op.depends_on = self._resolve_position_list(
                values=op.get("depends_on") or [],
                position_to_db_operation=position_to_db_operation,
            )

            db_op.on_success_follow_up = self._resolve_position_or_none(
                value=op.get("on_success_follow_up"),
                position_to_db_operation=position_to_db_operation,
            )

            db_op.on_failure_follow_up = self._resolve_position_or_none(
                value=op.get("on_failure_follow_up"),
                position_to_db_operation=position_to_db_operation,
            )

        self.db.flush()

    def _normalize_operation_payload(self, op: Any, *, fallback_position: int) -> Dict[str, Any]:
        if hasattr(op, "model_dump"):
            data = op.model_dump()
        elif isinstance(op, dict):
            data = dict(op)
        else:
            raise TypeError(f"Unsupported workflow operation payload type: {type(op)!r}")

        data["position"] = int(data.get("position") or fallback_position)

        data.setdefault("config", {})
        data.setdefault("depends_on", [])
        data.setdefault("on_success_follow_up", None)
        data.setdefault("on_failure_follow_up", None)
        data.setdefault("join_strategy", "all")
        data.setdefault("timeout_seconds", None)
        data.setdefault("retry_policy", None)

        return data

    def _resolve_position_list(
        self,
        *,
        values: list[Any],
        position_to_db_operation: Dict[int, WorkflowOperation],
    ) -> list[int]:
        resolved: list[int] = []

        for value in values or []:
            position = int(value)
            db_op = position_to_db_operation.get(position)
            if not db_op:
                raise ValueError(f"Workflow operation reference points to unknown position: {position}")
            resolved.append(db_op.id)

        return resolved

    def _resolve_position_or_none(
        self,
        *,
        value: Any,
        position_to_db_operation: Dict[int, WorkflowOperation],
    ) -> Optional[int]:
        if value is None or value == "":
            return None

        position = int(value)
        db_op = position_to_db_operation.get(position)

        if not db_op:
            raise ValueError(f"Workflow operation follow-up points to unknown position: {position}")

        return db_op.id