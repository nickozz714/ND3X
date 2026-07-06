"""
routers/local_models_router.py

Admin endpoints for the local-model manager (hardware probe, ranked
recommendations, Ollama install/list/deploy/remove). Deploy runs in the
background; progress is reflected by the provider-model `deploy_state`.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.orm import Session

from authentication.dependencies import require_user
from component.logging import get_logger
from db.database import SessionLocal, get_db
from services.authz_service import assert_expert_role
from services.local_models.local_model_service import LocalModelService
from services.local_models.ollama_client import DEFAULT_HOST, OllamaClient
from services.local_models.ollama_setup import OllamaSetupService


async def _wait_reachable(svc: "LocalModelService", host: str, attempts: int = 12) -> bool:
    for _ in range(attempts):
        if (await svc.reachability(host))["available"]:
            return True
        await asyncio.sleep(1)
    return False

log = get_logger(__name__)

router = APIRouter(prefix="/admin/local-models", tags=["Local Models"])


@router.get("/environment")
def environment(user=Depends(require_user)) -> Dict[str, Any]:
    """Effective Ollama environment for the FE: the default host (OLLAMA_HOST
    env — e.g. the Docker sidecar — else localhost), whether this backend can
    install/start Ollama itself (never inside a container), and whether it runs
    containerized (FE then shows sidecar guidance instead of Install/Start)."""
    from services.local_models.ollama_setup import OllamaSetupService
    return {
        "default_host": DEFAULT_HOST,
        "can_manage": OllamaSetupService.can_manage(DEFAULT_HOST),
        "containerized": OllamaSetupService.in_container(),
    }


@router.get("/hardware")
def hardware(db: Session = Depends(get_db), user=Depends(require_user)) -> Dict[str, Any]:
    return LocalModelService(db).hardware()


@router.get("/recommendations")
async def recommendations(
    capability: Optional[str] = Query(None),
    host: str = Query(DEFAULT_HOST),
    refresh: bool = Query(False),
    db: Session = Depends(get_db),
    user=Depends(require_user),
) -> List[Dict[str, Any]]:
    svc = LocalModelService(db)
    # Merge models already pulled on the target host so they always appear,
    # sized + fit-checked, alongside catalog + live library discovery.
    installed_names: List[str] = []
    try:
        oc = OllamaClient(host)
        if await oc.is_available():
            installed_names = [m.get("name") for m in await svc.installed(host, client=oc) if m.get("name")]
    except Exception:  # noqa: BLE001 — discovery is best-effort
        installed_names = []
    return svc.recommendations(capability=capability, installed_names=installed_names, force_library=refresh)


@router.get("/library")
def library(db: Session = Depends(get_db), user=Depends(require_user)) -> Dict[str, Any]:
    """Discovery source status (enabled, URL, cached model count)."""
    return LocalModelService(db).library_status()


@router.post("/library/refresh")
def library_refresh(db: Session = Depends(get_db), user=Depends(require_user)) -> Dict[str, Any]:
    assert_expert_role(user)
    return LocalModelService(db).refresh_library()


@router.get("/variants")
def variants(
    name: str = Query(..., description="Base model name, e.g. 'qwen2.5'"),
    capability: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user=Depends(require_user),
) -> List[Dict[str, Any]]:
    """Pullable size variants of a base model (0.5b/7b/14b/…), each sized + fit-checked."""
    return LocalModelService(db).model_variants(name, capability=capability)


@router.get("/estimate")
def estimate(
    model: str = Query(...),
    capability: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user=Depends(require_user),
) -> Dict[str, Any]:
    """Live footprint + fit verdict for an arbitrary model name (no catalog limit)."""
    return LocalModelService(db).estimate(model, capability=capability)


@router.get("/installed")
async def installed(
    host: str = Query(DEFAULT_HOST),
    db: Session = Depends(get_db),
    user=Depends(require_user),
) -> Dict[str, Any]:
    oc = OllamaClient(host)
    available = await oc.is_available()
    models = await LocalModelService(db).installed(host, client=oc) if available else []
    return {"host": host, "available": available, "models": models}


async def _deploy_background(model: str, host: str, capability: str) -> None:
    db = SessionLocal()
    try:
        await LocalModelService(db).deploy(model, host=host, capability=capability)
    finally:
        db.close()


@router.post("/deploy")
async def deploy(
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    assert_expert_role(user)
    from services.local_models.deploy_status import set_status
    model = str(payload.get("model") or "").strip()
    host = str(payload.get("host") or DEFAULT_HOST).strip()
    capability = str(payload.get("capability") or "chat").strip()
    if not model:
        return {"status": "error", "message": "model is required"}

    svc = LocalModelService(db)
    setup = OllamaSetupService()
    # Pre-flight: if Ollama is down but installed locally, auto-start it; otherwise
    # return an immediate, clear error (with flags so the UI can offer Install/Start).
    reach = await svc.reachability(host)
    if not reach["available"]:
        det = setup.detect()
        if setup.can_manage(host) and det["installed"]:
            log.infox("Ollama niet bereikbaar maar geïnstalleerd — auto-start", host=host)
            setup.start()
            if await _wait_reachable(svc, host, 8):
                reach = {"available": True, "message": None}
        if not reach["available"]:
            set_status(host, model, "error", reach["message"])
            return {
                "status": "error", "available": False, "message": reach["message"],
                "model": model, "host": host,
                "ollama_installed": det["installed"], "can_manage": setup.can_manage(host),
            }

    provider = svc.ensure_ollama_provider(host)
    svc._register_model(provider.id, model, capability, "deploying")
    set_status(host, model, "pulling", f"Pulling {model} via Ollama…")
    asyncio.create_task(_deploy_background(model, host, capability))
    return {"status": "deploying", "model": model, "host": host, "provider_id": provider.id}


@router.get("/deploy-status")
async def deploy_status(
    model: str = Query(...),
    host: str = Query(DEFAULT_HOST),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    from services.local_models.deploy_status import get_status
    st = get_status(host, model)
    return st or {"host": host, "model": model, "state": "unknown", "message": None}


@router.get("/ollama-status")
async def ollama_status(
    host: str = Query(DEFAULT_HOST),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    svc = LocalModelService(db)
    setup = OllamaSetupService()
    det = setup.detect()
    reach = await svc.reachability(host)
    return {
        **det,
        "host": host,
        "running": reach["available"],
        "reachable": reach["available"],
        "can_manage": setup.can_manage(host),
        "message": reach["message"],
    }


@router.post("/ollama-start")
async def ollama_start(
    host: str = Query(DEFAULT_HOST),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    assert_expert_role(user)
    setup = OllamaSetupService()
    if not setup.can_manage(host):
        return {"ok": False, "available": False, "message": f"Cannot manage Ollama on a non-local host ({host})."}
    res = setup.start()
    available = await _wait_reachable(LocalModelService(db), host, 12) if res.get("ok") else False
    return {**res, "available": available}


@router.post("/ollama-install")
async def ollama_install(
    host: str = Query(DEFAULT_HOST),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    assert_expert_role(user)
    setup = OllamaSetupService()
    if not setup.can_manage(host):
        return {"ok": False, "available": False, "message": f"Cannot install Ollama for a non-local host ({host})."}
    # Install can take minutes — run off the event loop.
    res = await asyncio.to_thread(setup.install)
    available = False
    if res.get("ok"):
        setup.start()
        available = await _wait_reachable(LocalModelService(db), host, 12)
    return {**res, "available": available}


@router.delete("/remove")
async def remove(
    model: str = Query(...),
    host: str = Query(DEFAULT_HOST),
    capability: str = Query("chat"),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    assert_expert_role(user)
    return await LocalModelService(db).remove(model, host=host, capability=capability)
