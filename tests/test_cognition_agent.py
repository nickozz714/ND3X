"""Fase 3 — agent-blackbox cognition: the runner parses the envelope, and
post_turn's agent branch persists memories/beliefs/curiosity via the repos."""
from __future__ import annotations

import asyncio

from services.providers.base import ChatResult
from services.system_cognition.cognition_agent_runner import CognitionAgentRunner


class _FakeProvider:
    def __init__(self, text: str):
        self._text = text

    async def chat(self, prompt, **kwargs):
        return ChatResult(text=self._text, provider="claude_code", model="opus")


# ── Runner: envelope extraction ──────────────────────────────────────────────

def test_extract_parses_envelope(monkeypatch):
    runner = CognitionAgentRunner(db=None)
    text = ('Sure, here is the result. {"decision":"kept 1 preference",'
            '"memories":[{"content":"User prefers dark mode","type":"user_preference",'
            '"scope":"global","importance":0.9}],"beliefs":[],"curiosity":[]}')
    monkeypatch.setattr(CognitionAgentRunner, "_build_provider", lambda self, model: _FakeProvider(text))
    out = asyncio.run(runner.extract(question="q", answer="a"))
    assert out["decision"] == "kept 1 preference"
    assert out["memories"][0]["content"].startswith("User prefers")
    assert out["beliefs"] == [] and out["curiosity"] == []


def test_extract_tolerates_non_json(monkeypatch):
    runner = CognitionAgentRunner(db=None)
    monkeypatch.setattr(CognitionAgentRunner, "_build_provider", lambda self, model: _FakeProvider("nothing to remember"))
    out = asyncio.run(runner.extract(question="q", answer="a"))
    assert out == {"decision": "no-parse", "memories": [], "beliefs": [], "curiosity": []}


# ── Service: post_turn agent branch persists ─────────────────────────────────

class _Repo:
    def __init__(self):
        self.saved = []

    async def upsert(self, rec):
        self.saved.append(rec); return rec

    async def enqueue(self, job):
        self.saved.append(job); return job


def _service(mem, bel, cur):
    from services.system_cognition.system_cognition_service import SystemCognitionService
    return SystemCognitionService(
        openai_service=None, memory_repo=mem, belief_repo=bel, curiosity_repo=cur,
        system_runner=None)


def test_post_turn_via_agent_persists(monkeypatch):
    mem, bel, cur = _Repo(), _Repo(), _Repo()
    svc = _service(mem, bel, cur)

    async def fake_extract(self, *, question, answer, model=None):
        return {"decision": "x",
                "memories": [{"content": "c", "type": "note", "scope": "thread", "importance": 0.7}],
                "beliefs": [{"topic": "t", "summary": "s", "content": "", "domain": "d", "confidence": 0.6}],
                "curiosity": [{"topic": "ct", "reason": "r"}]}

    monkeypatch.setattr(
        "services.system_cognition.cognition_agent_runner.CognitionAgentRunner.extract", fake_extract)

    res = asyncio.run(svc._post_turn_via_agent(
        question="q", answer="a", thread_id="th", project_id=None, turn_id=1, t0=0.0))

    assert res["ok"] is True and res["mode"] == "agent"
    assert len(mem.saved) == 1 and mem.saved[0].content == "c" and mem.saved[0].scope == "thread"
    assert len(bel.saved) == 1 and bel.saved[0].topic == "t"
    assert len(cur.saved) == 1 and cur.saved[0].topic == "ct"
    assert res["memory"]["saved_ids"] == [mem.saved[0].id]
    assert res["curiosity"]["queued_ids"] == [cur.saved[0].id]


def test_post_turn_via_agent_skips_empty(monkeypatch):
    mem, bel, cur = _Repo(), _Repo(), _Repo()
    svc = _service(mem, bel, cur)

    async def fake_extract(self, *, question, answer, model=None):
        return {"decision": "nothing durable", "memories": [], "beliefs": [], "curiosity": []}

    monkeypatch.setattr(
        "services.system_cognition.cognition_agent_runner.CognitionAgentRunner.extract", fake_extract)
    res = asyncio.run(svc._post_turn_via_agent(
        question="q", answer="a", thread_id="th", project_id=None, turn_id=1, t0=0.0))
    assert res["ok"] is True and not mem.saved and not bel.saved and not cur.saved
