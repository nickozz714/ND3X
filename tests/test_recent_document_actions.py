"""Session memory of tool ACTIONS: documents created/edited earlier in the thread are
surfaced (from the audit trail) so the agent can resolve "the document we made"."""
from __future__ import annotations

import services.assistants.ask_job_callbacks as cb


class _FakeAudit:
    def __init__(self, events):
        self._events = events

    def get_thread_events(self, *, thread_id, limit=300, newest_first=True):
        return len(self._events), self._events


def _patch(monkeypatch, events):
    import services.audit_service as audit_mod
    monkeypatch.setattr(audit_mod, "AuditService", lambda: _FakeAudit(events))


def test_collects_recent_document_actions(monkeypatch):
    events = [
        {"type": "tool_result", "data": {"tool": "text__ingest", "status": "success",
                                          "args": {"content": "Titel: Een onverwachte reis\n..."}}},
        {"type": "tool_result", "data": {"tool": "text__update", "status": "success",
                                          "args": {"path": "stories/reis.md"}}},
        {"type": "plan", "data": {}},                                   # ignored (not tool_result)
        {"type": "tool_result", "data": {"tool": "text__search", "status": "success",
                                          "args": {"query": "x"}}},     # ignored (read-only tool)
    ]
    _patch(monkeypatch, events)
    out = cb._recent_document_actions("t1")
    tools = [d["tool"] for d in out]
    assert "text__ingest" in tools and "text__update" in tools
    assert "text__search" not in tools
    assert any("Een onverwachte reis" in d["ref"] for d in out)
    assert any(d["ref"] == "stories/reis.md" for d in out)


def test_empty_and_failure_safe(monkeypatch):
    _patch(monkeypatch, [])
    assert cb._recent_document_actions("t1") == []
    # failed actions are skipped
    _patch(monkeypatch, [{"type": "tool_result", "data": {"tool": "text__ingest",
                                                          "status": "failed", "args": {"path": "x"}}}])
    assert cb._recent_document_actions("t1") == []
