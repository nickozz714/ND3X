"""WorkflowRunService.cancel_run requests cancellation (the endpoint relied on a
method that didn't exist, so cancel previously 500'd)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services.workflows.workflow_run_service import WorkflowRunService


class _FakeRunRepo:
    def __init__(self):
        self.runs = {
            7: SimpleNamespace(id=7, status="running"),
            8: SimpleNamespace(id=8, status="success"),
        }
        self.cancel_requested = []

    def get_run(self, run_id):
        return self.runs.get(run_id)

    def request_cancel_run(self, run_id):
        run = self.runs.get(run_id)
        if run and run.status not in {"success", "failed", "cancelled"}:
            run.status = "cancel_requested"
            self.cancel_requested.append(run_id)
        return run


def _service_with(repo: _FakeRunRepo) -> WorkflowRunService:
    svc = WorkflowRunService.__new__(WorkflowRunService)  # bypass DB-bound __init__
    svc.run_repository = repo
    svc.workflow_repository = None
    return svc


def test_cancel_active_run_sets_cancel_requested():
    repo = _FakeRunRepo()
    svc = _service_with(repo)
    run = svc.cancel_run(7)
    assert run.status == "cancel_requested"
    assert 7 in repo.cancel_requested


def test_cancel_terminal_run_is_noop():
    repo = _FakeRunRepo()
    svc = _service_with(repo)
    run = svc.cancel_run(8)
    assert run.status == "success"
    assert repo.cancel_requested == []


def test_cancel_unknown_run_raises_404():
    repo = _FakeRunRepo()
    svc = _service_with(repo)
    with pytest.raises(HTTPException) as exc:
        svc.cancel_run(999)
    assert exc.value.status_code == 404
