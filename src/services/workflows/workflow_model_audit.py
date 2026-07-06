"""
services/workflows/workflow_model_audit.py

Detect workflow assistant operations whose pinned model override points at a
model that is no longer a registered+enabled chat model — e.g. after a model-id
rename (gpt-5-mini → gpt-5.4-mini). Such an override is silently unusable: at run
time the step falls back to the routing slot or fails, which is exactly the
"local model was used despite an override" surprise.

Non-destructive: this only reports. We never auto-rewrite, because the old→new
name mapping cannot be inferred reliably.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from component.logging import get_logger

log = get_logger(__name__)


def find_stale_model_overrides(db: Session) -> list[dict[str, Any]]:
    from models.workflow import Workflow, WorkflowOperation
    from services.providers.registry_service import ProviderRegistryService

    try:
        enabled_ids = {
            m.model_id
            for m in ProviderRegistryService(db).list_models(capability="chat")
            if getattr(m, "enabled", False)
        }
    except Exception as exc:  # noqa: BLE001 — a registry hiccup must not crash callers
        log.warningx("workflow_model_audit:registry_failed", error=str(exc))
        return []

    rows = (
        db.query(WorkflowOperation, Workflow.name)
        .join(Workflow, Workflow.id == WorkflowOperation.workflow_id)
        .filter(
            Workflow.deleted_at.is_(None),
            WorkflowOperation.operation_type == "assistant",
        )
        .all()
    )

    issues: list[dict[str, Any]] = []
    for op, wf_name in rows:
        pinned = ((op.config or {}).get("model") or "").strip()
        if not pinned or pinned in enabled_ids:
            continue
        issues.append({
            "workflow_id": op.workflow_id,
            "workflow_name": wf_name,
            "operation_id": op.id,
            "operation_name": op.name,
            "pinned_model": pinned,
            "reason": "pinned model is not a registered/enabled chat model (renamed or removed)",
        })
    return issues


def log_stale_model_overrides(db: Session) -> int:
    """Startup hook: WARN once with the stale overrides found. Returns the count."""
    issues = find_stale_model_overrides(db)
    if issues:
        log.warningx(
            "workflow_model_audit:stale_overrides",
            count=len(issues),
            examples=[f"wf#{i['workflow_id']}/op#{i['operation_id']}→{i['pinned_model']}" for i in issues[:10]],
        )
    return len(issues)
