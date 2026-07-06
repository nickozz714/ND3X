"""The `fail` operation stops the run as failed with its (templated) message."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from services.workflows.workflow_executor import WorkflowExecutor


def _ex():
    return WorkflowExecutor(workflow_repository=None, run_repository=None, assistant_runner=None)


def _ctx():
    return {
        "workflow_run_id": 1,
        "workflow_id": 1,
        "operation_outputs": {},
        "operation_statuses": {},
        "input": {},
        "workflow_variables": {},
    }


def test_fail_operation_raises_with_message():
    op = SimpleNamespace(id=9, config={"message": "stop right here"})
    with pytest.raises(RuntimeError, match="stop right here"):
        asyncio.run(_ex()._execute_fail_operation(op, {}, _ctx()))


def test_fail_operation_has_default_message():
    op = SimpleNamespace(id=9, config={})
    with pytest.raises(RuntimeError):
        asyncio.run(_ex()._execute_fail_operation(op, {}, _ctx()))


def test_fail_operation_includes_error_code():
    op = SimpleNamespace(id=9, config={"message": "missing data", "error_code": "EXPENSES_MISSING"})
    with pytest.raises(RuntimeError, match=r"\[EXPENSES_MISSING\] missing data"):
        asyncio.run(_ex()._execute_fail_operation(op, {}, _ctx()))


def test_fail_operation_includes_previous_errors():
    op = SimpleNamespace(id=9, config={"message": "stop", "include_previous_errors": True})
    ctx = _ctx()
    ctx["operations"] = [SimpleNamespace(id=3, name="Grab Expenses")]
    ctx["operation_statuses"] = {3: "failed"}
    ctx["operation_outputs"] = {3: {"error": "text__search blew up"}}
    with pytest.raises(RuntimeError, match=r"Grab Expenses: text__search blew up"):
        asyncio.run(_ex()._execute_fail_operation(op, {}, ctx))


def test_fail_operation_no_previous_errors_section_when_none():
    op = SimpleNamespace(id=9, config={"message": "stop", "include_previous_errors": True})
    try:
        asyncio.run(_ex()._execute_fail_operation(op, {}, _ctx()))
        assert False
    except RuntimeError as e:
        assert "previous activities" not in str(e)
