"""A workflow assistant-operation that finishes (mode=final) but self-reports
downstream_handoff.status="failed" must FAIL the operation, not count as success."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from services.workflows.workflow_executor import WorkflowExecutor


class _Runner:
    def __init__(self, result):
        self._result = result

    async def run(self, **kwargs):
        return self._result


def _executor(result):
    ex = WorkflowExecutor.__new__(WorkflowExecutor)
    ex.prompt_variable_resolver = None
    ex.assistant_runner = _Runner(result)
    ex._raise_if_cancel_requested = lambda rid: None
    return ex


def _op():
    return SimpleNamespace(id=198, operation_ref_id=1,
                           config={"skill_names": ["declaration_document_management"], "question": "do it"})


_CTX = {"workflow_run_id": 208, "workflow_id": 2, "operation_outputs": {}, "operations": []}


def test_handoff_failed_fails_the_operation():
    ex = _executor({
        "mode": "final", "answer": "Niet voltooid.",
        "downstream_handoff": {"status": "failed", "summary": "could not read document"},
    })
    with pytest.raises(RuntimeError):
        asyncio.run(ex._execute_assistant_operation(_op(), {}, _CTX))


def test_handoff_partial_also_fails_the_operation():
    ex = _executor({
        "mode": "final", "answer": "Partly done.",
        "downstream_handoff": {"status": "partial", "summary": "some progress"},
    })
    with pytest.raises(RuntimeError):
        asyncio.run(ex._execute_assistant_operation(_op(), {}, _CTX))


def test_handoff_success_is_success():
    ex = _executor({
        "mode": "final", "answer": "Done.",
        "downstream_handoff": {"status": "success", "summary": "ok"},
    })
    out = asyncio.run(ex._execute_assistant_operation(_op(), {}, _CTX))
    assert out["mode"] == "final"
