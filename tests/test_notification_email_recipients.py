"""Notification op can email explicit recipients (channel='email' + config.recipients)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import services.workflows.workflow_executor as wem
from services.workflows.workflow_executor import WorkflowExecutor


def _ex():
    return WorkflowExecutor(
        workflow_repository=SimpleNamespace(db=None),
        run_repository=SimpleNamespace(db=None),
        assistant_runner=None,
    )


def _ctx():
    return {
        "workflow_id": 1,
        "workflow_run_id": 2,
        "operation_outputs": {},
        "operation_statuses": {},
        "input": {},
        "workflow_variables": {},
    }


def test_email_channel_passes_explicit_recipients(monkeypatch):
    captured = {}

    def fake_send(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(wem, "send_system_notification", fake_send)
    op = SimpleNamespace(id=5, config={
        "channel": "email", "subject": "S", "message": "M",
        "recipients": ["alice@x.com", " bob@x.com "],
    })
    out = asyncio.run(_ex()._execute_notification_operation(op, {}, _ctx()))
    assert out["sent"] is True
    assert captured.get("recipients") == ["alice@x.com", "bob@x.com"]


def test_trace_channel_does_not_send(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(wem, "send_system_notification", lambda **k: called.__setitem__("n", called["n"] + 1) or True)
    op = SimpleNamespace(id=6, config={"channel": "trace", "subject": "S", "message": "M"})
    out = asyncio.run(_ex()._execute_notification_operation(op, {}, _ctx()))
    assert out["sent"] is False
    assert called["n"] == 0
