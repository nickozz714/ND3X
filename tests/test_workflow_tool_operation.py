"""Workflow 'tool' operation: run a builtin tool directly as a step (no agent)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import services.builtin.internal_tool_registry as reg_mod
from services.workflows.workflow_executor import WorkflowExecutor


class _FakeRegistry:
    def __init__(self, result, known=("text__ingest",)):
        self._result = result
        self._known = set(known)
        self.calls = []

    def has_tool(self, name):
        return name in self._known

    async def call(self, name, args):
        self.calls.append((name, args))
        return self._result


def _executor():
    return WorkflowExecutor.__new__(WorkflowExecutor)


def _op(config):
    return SimpleNamespace(id=5, operation_type="tool", depends_on=[], config=config)


_CTX = {"workflow_run_id": 1, "operation_outputs": {}}


def test_runs_builtin_tool_and_returns_result(monkeypatch):
    fake = _FakeRegistry({"status": "success", "doc_id": 4})
    monkeypatch.setattr(reg_mod, "internal_tool_registry", fake)
    ex = _executor()
    out = asyncio.run(ex._execute_tool_operation(
        _op({"tool_name": "text__ingest", "args": {"content": "hello"}}), {}, _CTX))
    assert out["status"] == "success" and out["tool"] == "text__ingest"
    assert out["result"]["doc_id"] == 4
    assert fake.calls == [("text__ingest", {"content": "hello"})]


def test_unknown_tool_raises(monkeypatch):
    monkeypatch.setattr(reg_mod, "internal_tool_registry", _FakeRegistry({}, known=()))
    ex = _executor()
    with pytest.raises(ValueError):
        asyncio.run(ex._execute_tool_operation(_op({"tool_name": "nope", "args": {}}), {}, _CTX))


def test_failed_tool_result_fails_the_operation(monkeypatch):
    monkeypatch.setattr(reg_mod, "internal_tool_registry", _FakeRegistry({"status": "error", "message": "boom"}))
    ex = _executor()
    with pytest.raises(RuntimeError):
        asyncio.run(ex._execute_tool_operation(
            _op({"tool_name": "text__ingest", "args": {}}), {}, _CTX))


def test_missing_tool_name_raises(monkeypatch):
    monkeypatch.setattr(reg_mod, "internal_tool_registry", _FakeRegistry({}))
    ex = _executor()
    with pytest.raises(ValueError):
        asyncio.run(ex._execute_tool_operation(_op({"args": {}}), {}, _CTX))
