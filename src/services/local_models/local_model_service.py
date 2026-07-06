"""
services/local_models/local_model_service.py

Orchestrates local-model management: hardware probe, ranked recommendations,
Ollama install/list/delete, and registration of deployed models into the provider
registry so they become selectable in the workbench.

Deploy targets (host vs container) are expressed purely as the Ollama `host`:
- host daemon:           http://localhost:11434
- backend-in-container:  http://host.docker.internal:11434  (model on host)
- sidecar/in-container:  http://ollama:11434                 (model in container)
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from component.logging import get_logger
from models.provider import Provider, ProviderModel
from services.local_models.deploy_status import set_status
from services.local_models.hardware import detect_hardware
from services.local_models.ollama_client import DEFAULT_HOST, OllamaClient, OllamaError
from services.local_models.ollama_library import fetch_library_names, fetch_model_variants, library_status
from services.local_models.recommendations import build_recommendation, recommend
from services.providers.registry_service import ProviderRegistryService

log = get_logger(__name__)


def _ollama_base_url(host: str) -> str:
    # Ollama exposes an OpenAI-compatible API under /v1.
    return f"{(host or DEFAULT_HOST).rstrip('/')}/v1"


def _friendly_pull_error(model: str, raw: str) -> str:
    """Map Ollama's terse pull errors to a clear, actionable message."""
    low = raw.lower()
    if "manifest" in low and ("file does not exist" in low or "not found" in low or "404" in low):
        return (
            f"'{model}' can't be downloaded. It either doesn't exist under that exact name, "
            f"or it's a cloud-only model (some models, e.g. glm-4.7, only run on Ollama's "
            f"cloud and can't be pulled locally). Check the name/size on ollama.com/library, "
            f"or pick a downloadable model."
        )
    if "no space" in low or "disk" in low:
        return f"Not enough disk space to download '{model}'. Free up space and try again."
    if any(k in low for k in ("connection", "refused", "timeout", "timed out")):
        return f"Couldn't reach Ollama while pulling '{model}'. Make sure Ollama is running, then retry."
    return raw


class LocalModelService:
    def __init__(self, db: Session):
        self.db = db
        self.reg = ProviderRegistryService(db)

    # ── Read-only ────────────────────────────────────────────────────────────
    def hardware(self) -> Dict[str, Any]:
        return detect_hardware().to_dict()

    def recommendations(
        self,
        *,
        capability: Optional[str] = None,
        installed_names: Optional[List[str]] = None,
        force_library: bool = False,
    ) -> List[Dict[str, Any]]:
        # Fully dynamic: curated catalog + live library discovery + whatever is
        # already installed locally, each sized + fit-checked for this machine.
        extra: List[str] = list(installed_names or [])
        try:
            extra.extend(fetch_library_names(force=force_library))
        except Exception:  # noqa: BLE001 — discovery is best-effort
            pass
        recs = recommend(detect_hardware(), capability=capability, extra_names=extra)
        return [r.__dict__ for r in recs]

    def library_status(self) -> Dict[str, Any]:
        return library_status()

    def refresh_library(self) -> Dict[str, Any]:
        """Force a re-fetch of the discovery source and return the new status."""
        fetch_library_names(force=True)
        return library_status()

    def estimate(self, model: str, *, capability: Optional[str] = None) -> Dict[str, Any]:
        """Live footprint + fit verdict for an arbitrary model name."""
        rec = build_recommendation(detect_hardware(), ollama_name=(model or "").strip(),
                                   capability=capability)
        return rec.__dict__

    def model_variants(self, name: str, *, capability: Optional[str] = None) -> List[Dict[str, Any]]:
        """The pullable size variants of a base model (e.g. qwen2.5 → 0.5b/7b/14b/…),
        each sized + fit-checked for this machine so the UI can show specs per size."""
        base = (name or "").strip().split(":")[0]
        if not base:
            return []
        hw = detect_hardware()
        tags = fetch_model_variants(base)
        out: List[Dict[str, Any]] = []
        for tag in tags:
            full = base if tag == "latest" else f"{base}:{tag}"
            rec = build_recommendation(hw, ollama_name=full, capability=capability)
            d = rec.__dict__
            d["tag"] = tag
            out.append(d)
        return out

    async def installed(self, host: str = DEFAULT_HOST, *, client: Optional[OllamaClient] = None) -> List[Dict[str, Any]]:
        oc = client or OllamaClient(host)
        return await oc.list_models()

    # ── Provider plumbing ────────────────────────────────────────────────────
    def ensure_ollama_provider(self, host: str = DEFAULT_HOST) -> Provider:
        base_url = _ollama_base_url(host)
        existing = (
            self.db.query(Provider)
            .filter(Provider.provider_type == "ollama", Provider.base_url == base_url)
            .first()
        )
        if existing:
            return existing
        p = Provider(
            name=f"Ollama ({host})",
            provider_type="ollama",
            base_url=base_url,
            enabled=True,
            is_local=True,
        )
        self.db.add(p)
        self.db.commit()
        self.db.refresh(p)
        return p

    def _register_model(self, provider_id: int, model_id: str, capability: str, deploy_state: str) -> ProviderModel:
        existing = (
            self.db.query(ProviderModel)
            .filter(
                ProviderModel.provider_id == provider_id,
                ProviderModel.model_id == model_id,
                ProviderModel.capability == capability,
            )
            .first()
        )
        if existing:
            existing.deploy_state = deploy_state
            self.db.commit()
            self.db.refresh(existing)
            return existing
        m = ProviderModel(
            provider_id=provider_id,
            model_id=model_id,
            capability=capability,
            display_name=model_id,
            is_local=True,
            enabled=True,
            deploy_state=deploy_state,
        )
        self.db.add(m)
        self.db.commit()
        self.db.refresh(m)
        return m

    # ── Deploy / remove ──────────────────────────────────────────────────────
    async def reachability(self, host: str = DEFAULT_HOST, *, client: Optional[OllamaClient] = None) -> Dict[str, Any]:
        """Check the Ollama daemon and return {available, message}."""
        oc = client or OllamaClient(host)
        try:
            await oc.version()
            return {"available": True, "message": None}
        except OllamaError as exc:
            return {"available": False, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "message": f"Ollama at {host} returned an error: {exc}"}

    async def deploy(
        self,
        model: str,
        *,
        host: str = DEFAULT_HOST,
        capability: str = "chat",
        client: Optional[OllamaClient] = None,
    ) -> Dict[str, Any]:
        """Pull a model via Ollama and register it. Self-contained and defensive:
        pre-flight reachability → pull (clear Ollama error) → verify the model is
        actually present. Reports progress/failure via deploy_status + deploy_state.
        Runs in a background task."""
        model = (model or "").strip()
        if not model:
            set_status(host, model, "error", "No model specified.")
            return {"status": "error", "message": "No model specified.", "model": model}

        oc = client or OllamaClient(host)

        # 1) reachability
        reach = await self.reachability(host, client=oc)
        if not reach["available"]:
            set_status(host, model, "error", reach["message"])
            return {"status": "error", "message": reach["message"], "model": model, "available": False}

        provider = self.ensure_ollama_provider(host)
        pm = self._register_model(provider.id, model, capability, "deploying")
        set_status(host, model, "pulling", f"Pulling {model} via Ollama…")

        # Stream pull progress into deploy_status, throttled so we don't thrash the
        # in-memory store on every NDJSON line.
        from services.local_models.deploy_status import set_progress
        _emit = {"pct": -1.0, "ts": 0.0}

        def _on_progress(status: str, percent, completed: int, total: int) -> None:
            now = time.monotonic()
            pct = float(percent) if isinstance(percent, (int, float)) else None
            moved = pct is None or abs((pct or 0.0) - _emit["pct"]) >= 0.01
            if moved or (now - _emit["ts"]) > 0.5:
                set_progress(host, model, percent=pct, status=status, completed=completed, total=total)
                _emit["pct"] = pct if pct is not None else _emit["pct"]
                _emit["ts"] = now

        # 2) pull (surfaces Ollama's own message: model-not-found, disk, etc.)
        try:
            await oc.pull(model, on_progress=_on_progress)
        except OllamaError as exc:
            friendly = _friendly_pull_error(model, str(exc))
            self._register_model(provider.id, model, capability, "error")
            set_status(host, model, "error", friendly)
            log.warningx("Local model pull mislukt", model=model, host=host, error=str(exc))
            return {"status": "error", "message": friendly, "model": model}
        except Exception as exc:  # noqa: BLE001 — unexpected
            self._register_model(provider.id, model, capability, "error")
            msg = f"Unexpected error pulling '{model}': {type(exc).__name__}: {exc}"
            set_status(host, model, "error", msg)
            log.exceptionx("Local model deploy onverwachte fout", model=model, host=host, exception=exc)
            return {"status": "error", "message": msg, "model": model}

        # 3) verify the model is actually present
        try:
            present = await oc.has_model(model)
        except Exception:  # noqa: BLE001 — verification hiccup shouldn't fail a good pull
            present = True
        if not present:
            msg = f"Pull reported success but '{model}' is not listed by Ollama. Try deploying again."
            self._register_model(provider.id, model, capability, "error")
            set_status(host, model, "error", msg)
            return {"status": "error", "message": msg, "model": model}

        self._register_model(provider.id, model, capability, "ready")
        set_status(host, model, "ready", "Ready")
        log.infox("Local model deployed", model=model, host=host, provider_id=provider.id)
        return {"status": "ready", "provider_id": provider.id, "provider_model_id": pm.id, "model": model}

    async def remove(self, model: str, *, host: str = DEFAULT_HOST, capability: str = "chat", client: Optional[OllamaClient] = None) -> Dict[str, Any]:
        oc = client or OllamaClient(host)
        try:
            await oc.delete(model)
        except Exception as exc:  # noqa: BLE001
            log.warningx("Ollama delete mislukt (verwijder registratie toch)", model=model, error=str(exc))
        provider = self.ensure_ollama_provider(host)
        m = (
            self.db.query(ProviderModel)
            .filter(
                ProviderModel.provider_id == provider.id,
                ProviderModel.model_id == model,
                ProviderModel.capability == capability,
            )
            .first()
        )
        if m:
            self.db.delete(m)
            self.db.commit()
        return {"status": "removed", "model": model}
