"""Image generation (TODO 8) — provider adapters (mocked HTTP), slot
resolution, and the image__generate no-slot error."""
from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.provider as pv
from db.database import Base
from schemas.provider import ProviderCreate, ProviderModelCreate
from services.providers.image_generation import (
    GeminiImageGenerationProvider,
    OpenAIImageGenerationProvider,
    resolve_image_generation,
)
from services.providers.registry_service import ProviderRegistryService

_PNG = b"\x89PNG fake"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        pv.Provider.__table__, pv.ProviderModel.__table__, pv.CapabilityAssignment.__table__,
    ])
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _patch_httpx(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=transport, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def test_openai_adapter_b64(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/images/generations")
        body = json.loads(request.content.decode())
        assert body["model"] == "gpt-image-1" and body["prompt"] == "een kat"
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(_PNG).decode()}]})

    _patch_httpx(monkeypatch, handler)
    p = OpenAIImageGenerationProvider(api_key="k", default_model="gpt-image-1")
    out = asyncio.run(p.generate("een kat"))
    assert out == _PNG


def test_openai_adapter_error_surfaces_message(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "billing hard limit reached"}})

    _patch_httpx(monkeypatch, handler)
    p = OpenAIImageGenerationProvider(api_key="k", default_model="gpt-image-1")
    with pytest.raises(RuntimeError, match="billing hard limit"):
        asyncio.run(p.generate("x"))


def test_gemini_adapter_inline_data(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert ":generateContent" in str(request.url)
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [
                {"text": "here you go"},
                {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(_PNG).decode()}},
            ]}}],
        })

    _patch_httpx(monkeypatch, handler)
    p = GeminiImageGenerationProvider(api_key="k", default_model="gemini-2.5-flash-image")
    assert asyncio.run(p.generate("een kat")) == _PNG


def test_resolve_image_generation_slot(db):
    reg = ProviderRegistryService(db)
    assert resolve_image_generation(db) is None  # unassigned → off

    p = reg.create_provider(ProviderCreate(name="OpenAI", provider_type="openai", api_key="k"))
    m = reg.create_model(ProviderModelCreate(provider_id=p.id, model_id="gpt-image-1", capability="image_generation"))
    reg.set_assignment("image_generation", m.id)

    resolved = resolve_image_generation(db)
    assert resolved is not None
    provider, model_id = resolved
    assert isinstance(provider, OpenAIImageGenerationProvider)
    assert model_id == "gpt-image-1"


def test_resolve_rejects_non_generating_provider(db):
    reg = ProviderRegistryService(db)
    p = reg.create_provider(ProviderCreate(name="Ollama", provider_type="ollama",
                                           base_url="http://localhost:11434/v1", is_local=True))
    m = reg.create_model(ProviderModelCreate(provider_id=p.id, model_id="llava:13b", capability="image_generation"))
    reg.set_assignment("image_generation", m.id)
    assert resolve_image_generation(db) is None  # ollama can't generate


def test_image_generate_tool_without_slot_gives_clear_error(monkeypatch):
    from services.builtin.tools import image_tools

    monkeypatch.setattr(
        "services.providers.image_generation.resolve_image_generation", lambda db: None
    )
    out = asyncio.run(image_tools.image_generate({"prompt": "een kat"}))
    assert out["status"] == "error"
    assert "image_generation slot" in out["error"]
