"""Unit tests for the local-model manager (Phase 2): hardware detection,
recommendation ranking, and the Ollama client (httpx MockTransport)."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.provider as pv
from services.local_models.hardware import HardwareInfo, GPUInfo, detect_hardware
from services.local_models.recommendations import recommend, estimate_footprint_gb
from services.local_models.ollama_client import OllamaClient, OllamaError, OllamaUnreachableError
from services.local_models.deploy_status import set_status, get_status
from services.local_models.local_model_service import LocalModelService


# ── Hardware detection (injected platform + subprocess runner) ────────────────
def test_detect_apple_silicon_unified_memory():
    def fake_run(cmd):
        if cmd[:2] == ["sysctl", "-n"]:
            return str(24 * 1024 ** 3)  # 24 GB
        return None  # no nvidia-smi

    hw = detect_hardware(platform_system="Darwin", platform_machine="arm64", run=fake_run)
    assert hw.os == "Darwin"
    assert hw.ram_gb == 24.0
    assert hw.unified_memory is True
    assert hw.gpus and hw.gpus[0].vendor == "apple"
    # unified -> ~70% of RAM usable
    assert hw.usable_model_memory_gb == round(24.0 * 0.70, 1)


def test_detect_nvidia_uses_vram_budget():
    def fake_run(cmd):
        if cmd and cmd[0] == "nvidia-smi":
            return "NVIDIA RTX 4090, 24576\n"  # 24 GiB in MiB
        if cmd[:2] == ["sysctl", "-n"]:
            return None
        return None

    hw = detect_hardware(platform_system="Linux", run=fake_run)
    assert any(g.vendor == "nvidia" for g in hw.gpus)
    assert hw.usable_model_memory_gb == 24.0  # discrete VRAM, not RAM-based


# ── Recommendation engine ─────────────────────────────────────────────────────
def _hw(usable_gb: float) -> HardwareInfo:
    return HardwareInfo(
        os="Darwin", arch="arm64", cpu_cores=8, ram_gb=usable_gb / 0.7,
        gpus=[GPUInfo("Apple", usable_gb, "apple")], unified_memory=True,
        disk_free_gb=200.0, usable_model_memory_gb=usable_gb,
    )


def test_footprint_estimate():
    assert estimate_footprint_gb(7) == round(7 * 0.6 + 1.5, 1)  # ~5.7


def test_recommendations_rank_and_fit_on_24gb():
    recs = recommend(_hw(16.8))  # ~24GB machine budget
    chat = [r for r in recs if r.capability == "chat"]
    # all chat models that fit come first, largest first
    fitting = [r for r in chat if r.fits]
    assert fitting[0].params_b >= fitting[-1].params_b
    assert fitting[0].verdict == "best"
    # 14B (~9.9GB) fits on a 16.8GB budget
    assert any(r.ollama_name == "qwen2.5:14b" and r.fits for r in recs)
    # embeddings appear and are tagged
    assert any(r.capability == "embeddings" and r.verdict == "embeddings" for r in recs)


def test_recommendations_flag_too_large_on_small_machine():
    recs = recommend(_hw(4.0))  # ~6GB machine
    big = next(r for r in recs if r.ollama_name == "qwen2.5:14b")
    assert big.fits is False and big.verdict == "won't fit" and big.warning
    small = next(r for r in recs if r.ollama_name == "qwen2.5:3b")
    assert small.fits is True


def test_recommendations_capability_filter():
    recs = recommend(_hw(16.8), capability="embeddings")
    assert recs and all(r.capability == "embeddings" for r in recs)


# ── Ollama client (httpx MockTransport) ───────────────────────────────────────
def _client_with(handler):
    transport = httpx.MockTransport(handler)
    return OllamaClient("http://localhost:11434", client=httpx.AsyncClient(transport=transport))


def test_ollama_list_version_delete_pull():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/version":
            return httpx.Response(200, json={"version": "0.5.0"})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5:7b"}]})
        if path == "/api/delete":
            return httpx.Response(200, json={})
        if path == "/api/pull":
            return httpx.Response(200, json={"status": "success"})
        return httpx.Response(404)

    oc = _client_with(handler)

    async def run():
        assert await oc.is_available() is True
        models = await oc.list_models()
        assert models[0]["name"] == "qwen2.5:7b"
        assert await oc.delete("qwen2.5:7b") is True
        assert (await oc.pull("qwen2.5:7b"))["status"] == "success"

    asyncio.run(run())


def test_ollama_pull_streams_progress():
    """With on_progress, pull streams Ollama's NDJSON and reports an overall percent
    aggregated across layers."""
    ndjson = (
        b'{"status":"pulling manifest"}\n'
        b'{"status":"downloading","digest":"sha256:a","total":1000,"completed":250}\n'
        b'{"status":"downloading","digest":"sha256:a","total":1000,"completed":1000}\n'
        b'{"status":"success"}\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/pull":
            return httpx.Response(200, content=ndjson)
        return httpx.Response(404)

    oc = _client_with(handler)
    seen = []

    async def run():
        await oc.pull("qwen2.5:7b", on_progress=lambda status, pct, c, t: seen.append((status, pct)))

    asyncio.run(run())
    # First downloading line → 25%, second → 100%.
    pcts = [p for _, p in seen if p is not None]
    assert pcts and pcts[0] == 0.25 and pcts[-1] == 1.0
    assert ("success", None) in seen or any(s == "success" for s, _ in seen)


def test_ollama_pull_stream_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/pull":
            return httpx.Response(200, content=b'{"error":"model not found"}\n')
        return httpx.Response(404)

    oc = _client_with(handler)
    with pytest.raises(OllamaError):
        asyncio.run(oc.pull("nope", on_progress=lambda *a: None))


def test_ollama_unavailable_and_pull_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(500)
        if request.url.path == "/api/pull":
            return httpx.Response(404, text="model not found")
        return httpx.Response(404)

    oc = _client_with(handler)

    async def run():
        assert await oc.is_available() is False
        with pytest.raises(OllamaError):
            await oc.pull("nope")

    asyncio.run(run())


# ── Ollama error handling (Phase: deploy robustness) ──────────────────────────
def test_ollama_unreachable_friendly_messages():
    def handler(_req):
        raise httpx.ConnectError("connection refused")
    oc = _client_with(handler)

    async def run():
        assert await oc.is_available() is False
        with pytest.raises(OllamaUnreachableError) as e1:
            await oc.version()
        assert "Cannot reach Ollama" in str(e1.value)
        with pytest.raises(OllamaUnreachableError):
            await oc.pull("qwen2.5:7b")
    asyncio.run(run())


def test_ollama_pull_surfaces_body_error_and_404():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/pull":
            # Ollama can 200 with an error body, or 404 a missing model.
            if b"missing" in req.content:
                return httpx.Response(404, json={"error": "model 'missing' not found"})
            return httpx.Response(200, json={"error": "no space left on device"})
        return httpx.Response(404)
    oc = _client_with(handler)

    async def run():
        with pytest.raises(OllamaError) as e1:
            await oc.pull("qwen2.5:7b")
        assert "no space left" in str(e1.value)
        with pytest.raises(OllamaError) as e2:
            await oc.pull("missing")
        assert "not found" in str(e2.value)
    asyncio.run(run())


def test_ollama_has_model():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5:7b"}]})
        return httpx.Response(404)
    oc = _client_with(handler)

    async def run():
        assert await oc.has_model("qwen2.5:7b") is True
        assert await oc.has_model("qwen2.5") is False     # ":latest" not present
        assert await oc.has_model("llama3.1:8b") is False
    asyncio.run(run())


def test_deploy_status_registry():
    set_status("http://h", "m", "pulling", "Pulling…")
    s = get_status("http://h", "m")
    assert s["state"] == "pulling" and s["message"] == "Pulling…"
    assert get_status("http://h", "other") is None


# ── LocalModelService.deploy: every failure path (DB-backed) ──────────────────
@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    for m in (pv.Provider, pv.ProviderModel, pv.CapabilityAssignment):
        m.__table__.create(bind=engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


class FakeOllama:
    def __init__(self, *, available=True, pull_error=None, present=True):
        self._available = available
        self._pull_error = pull_error
        self._present = present

    async def version(self):
        if not self._available:
            raise OllamaUnreachableError("Cannot reach Ollama at host — connection refused.")
        return {"version": "x"}

    async def pull(self, model, on_progress=None):
        if self._pull_error:
            raise OllamaError(self._pull_error)
        if on_progress:
            on_progress("downloading", 0.5, 500, 1000)
            on_progress("success", 1.0, 1000, 1000)
        return {"status": "success"}

    async def has_model(self, model):
        return self._present


def _model_state(db, model):
    m = db.query(pv.ProviderModel).filter(pv.ProviderModel.model_id == model).first()
    return m.deploy_state if m else None


def test_deploy_unreachable_returns_clear_error(db):
    r = asyncio.run(LocalModelService(db).deploy("qwen2.5:7b", host="http://h", client=FakeOllama(available=False)))
    assert r["status"] == "error" and r["available"] is False
    assert "Cannot reach Ollama" in r["message"]
    assert get_status("http://h", "qwen2.5:7b")["state"] == "error"


def test_deploy_pull_error_marks_error(db):
    r = asyncio.run(LocalModelService(db).deploy(
        "nope:1b", host="http://h2", client=FakeOllama(pull_error="Ollama could not pull 'nope:1b': model not found")))
    assert r["status"] == "error" and "not found" in r["message"]
    assert _model_state(db, "nope:1b") == "error"


def test_deploy_success_marks_ready(db):
    r = asyncio.run(LocalModelService(db).deploy("qwen2.5:7b", host="http://h3", client=FakeOllama()))
    assert r["status"] == "ready"
    assert _model_state(db, "qwen2.5:7b") == "ready"
    assert get_status("http://h3", "qwen2.5:7b")["state"] == "ready"


def test_deploy_success_but_model_missing_is_error(db):
    r = asyncio.run(LocalModelService(db).deploy("qwen2.5:7b", host="http://h4", client=FakeOllama(present=False)))
    assert r["status"] == "error" and "not listed" in r["message"]
    assert _model_state(db, "qwen2.5:7b") == "error"


def test_reachability(db):
    svc = LocalModelService(db)
    assert asyncio.run(svc.reachability(host="http://h", client=FakeOllama(available=True)))["available"] is True
    bad = asyncio.run(svc.reachability(host="http://h", client=FakeOllama(available=False)))
    assert bad["available"] is False and "Cannot reach" in bad["message"]
