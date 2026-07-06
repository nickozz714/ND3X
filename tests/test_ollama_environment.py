"""Ollama environment wiring (TODO 7): the OLLAMA_HOST env (Docker sidecar) must
drive the default host everywhere, and a containerized backend must never offer
to install/start Ollama inside its own container."""
from __future__ import annotations

from services.local_models.ollama_client import _default_host
from services.local_models.ollama_setup import OllamaSetupService


def test_default_host_falls_back_to_localhost(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert _default_host() == "http://localhost:11434"


def test_default_host_reads_env_and_adds_scheme(monkeypatch):
    # docker-compose sets a bare host:port (ollama:11434)
    monkeypatch.setenv("OLLAMA_HOST", "ollama:11434")
    assert _default_host() == "http://ollama:11434"


def test_default_host_keeps_explicit_scheme(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "https://gpu-box.local:11434/")
    assert _default_host() == "https://gpu-box.local:11434"


def test_can_manage_localhost_outside_container(monkeypatch):
    monkeypatch.setattr(OllamaSetupService, "in_container", staticmethod(lambda: False))
    assert OllamaSetupService.can_manage("http://localhost:11434") is True
    assert OllamaSetupService.can_manage("http://ollama:11434") is False


def test_can_manage_always_false_in_container(monkeypatch):
    # Inside Docker "localhost" is the app container itself — never manageable.
    monkeypatch.setattr(OllamaSetupService, "in_container", staticmethod(lambda: True))
    assert OllamaSetupService.can_manage("http://localhost:11434") is False


def test_ollama_preset_uses_effective_host(monkeypatch):
    import services.local_models.ollama_client as oc
    monkeypatch.setattr(oc, "DEFAULT_HOST", "http://ollama:11434")
    from services.providers.provider_presets import get_presets
    preset = next(p for p in get_presets() if p["key"] == "ollama")
    assert preset["base_url"] == "http://ollama:11434/v1"
