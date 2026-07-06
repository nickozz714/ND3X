# server.py
from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio

import uvicorn
from fastapi import FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware

from component.config import settings
from component.logging import get_logger
from component.runtime_binaries import prepend_bundled_bin_to_path
from routers.setup_router import router as setup_router

log = get_logger(__name__)

# Put any bundled external binaries (ffmpeg/pandoc/poppler/…) on PATH before the
# app or its services shell out to them. No-op in dev unless ND3X_BIN_DIR is set.
prepend_bundled_bin_to_path()


def add_security_headers(
    app: FastAPI,
    *,
    allow_origins: list[str] | None = None,
    allow_credentials: bool = False,
):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins or [],
        allow_credentials=allow_credentials,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["Content-Disposition"],
    )

    @app.middleware("http")
    async def _headers(request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            "frame-ancestors 'none'; "
            "base-uri 'none'; "
            "form-action 'self'"
        )
        return resp


@asynccontextmanager
async def lifespan(app: FastAPI):
    # First-time-setup mode: no DB engine yet, so skip the whole runtime and serve
    # only the setup API until the wizard writes the bootstrap config.
    if not settings.CONFIGURED:
        log.warningx(
            "ND3X is not configured yet — serving first-time setup only. "
            "Complete the wizard to initialize the database and runtime."
        )
        yield
        return

    # Heavy/runtime imports live here so unconfigured boot never pulls them.
    from db.database import SessionLocal
    from db.init_db import init_db
    from services.openai_service import OpenAIResponsesService
    from services.assistants.ask_job_runtime import ask_job_service
    from services.assistants.ask_job_callbacks import boot_stdio_servers, stdio_process_manager
    from services.scheduling.dynamic_scheduler import DynamicScheduler
    from services.system_cognition.factory import create_system_cognition_service
    from services.system_cognition.system_curiosity_tick_service import SystemCuriosityTickService
    from services.workflows.workflow_schedule_tick_service import WorkflowScheduleTickService
    from services.workflows.workflow_worker import WorkflowWorker
    from services.providers.capability_router import log_resolved_models
    from db.integrity import log_dangling_links

    # Security guard: never run a configured deployment on the placeholder JWT
    # secret (sessions would be forgeable). The wizard generates a real one.
    if (settings.JWT_SECRET or "") in ("", "REPLACE_WITH_256BIT_SECRET"):
        log.warningx(
            "JWT_SECRET is not set to a real secret — tokens are insecure. "
            "Re-run setup or set JWT_SECRET in the environment."
        )

    await init_db()

    # Pull DB-backed configuration into the in-memory settings snapshot, then
    # rebuild loggers so LOG_* from the DB take effect.
    from services.app_settings_registry import hydrate as hydrate_settings
    from component.logging import reconfigure_logging
    _hydrate_db = SessionLocal()
    try:
        hydrate_settings(_hydrate_db)
    finally:
        _hydrate_db.close()
    reconfigure_logging()

    # Start alle geregistreerde stdio MCP servers vanuit de database
    await boot_stdio_servers()

    db = SessionLocal()
    try:
        # Log the resolved model per routing slot so a misconfigured
        # deployment is obvious at startup (no silent defaults).
        log_resolved_models(db)
        # Surface any dangling skill/tool association rows (cascade-delete
        # contract — see db/integrity.py and TODO §12).
        log_dangling_links(db)
    finally:
        db.close()

    dynamic_scheduler = DynamicScheduler(tick_seconds=30)

    workflow_schedule_tick = WorkflowScheduleTickService(
        session_factory=SessionLocal,
    )

    scheduler_openai = OpenAIResponsesService(
        # OpenAI key resolved lazily from the registry's OpenAI provider
        model=None,  # chat model comes from the routing slots (registry), not config
        embedding_model=None,  # embeddings model comes from the embeddings slot
    )
    workflow_worker = WorkflowWorker(
        session_factory=SessionLocal,
        interval_seconds=getattr(settings, "WORKFLOW_WORKER_INTERVAL_SECONDS", 10),
        batch_size=getattr(settings, "WORKFLOW_WORKER_BATCH_SIZE", 3),
    )
    system_cognition, _ = create_system_cognition_service(
        openai_service=scheduler_openai,
    )
    system_curiosity_tick = SystemCuriosityTickService(
        cognition_service=system_cognition,
        batch_size=getattr(settings, "SYSTEM_CURIOSITY_WORKER_BATCH_SIZE", 1),
    )

    from services.transfer.transfer_poller import TransferPollService
    transfer_poll = TransferPollService(session_factory=SessionLocal)

    async def runtime_job_cleanup_tick():
        return await asyncio.to_thread(ask_job_service.cleanup_old_jobs)

    dynamic_scheduler.register(
        name="workflow_schedule_tick",
        interval_seconds=getattr(settings, "WORKFLOW_SCHEDULER_INTERVAL_SECONDS", 30),
        fn=workflow_schedule_tick.tick_once,
        run_immediately=True,
    )
    dynamic_scheduler.register(
        name="system_curiosity_tick",
        interval_seconds=getattr(settings, "SYSTEM_CURIOSITY_WORKER_INTERVAL_SECONDS", 60),
        fn=system_curiosity_tick.tick_once,
        run_immediately=False,
    )
    dynamic_scheduler.register(
        name="runtime_job_cleanup",
        interval_seconds=settings.RUNTIME_JOB_CLEANUP_INTERVAL_SECONDS,
        fn=runtime_job_cleanup_tick,
        run_immediately=False,
    )
    dynamic_scheduler.register(
        name="transfer_poll",
        interval_seconds=getattr(settings, "TRANSFER_POLL_INTERVAL_SECONDS", 20),
        fn=transfer_poll.tick_once,
        run_immediately=False,
    )
    # Periodic full-vs-light model evaluation (TODO 1.5). Off by default
    # (MODEL_EVAL_INTERVAL_HOURS=0) — a run keeps a local model busy for many
    # minutes. When enabled it evaluates the local chat-slot models.
    _eval_hours = float(getattr(settings, "MODEL_EVAL_INTERVAL_HOURS", 0) or 0)
    if _eval_hours > 0:
        from services.model_eval_service import run_scheduled_eval
        dynamic_scheduler.register(
            name="model_eval",
            interval_seconds=int(_eval_hours * 3600),
            fn=run_scheduled_eval,
            run_immediately=False,
        )

    await dynamic_scheduler.start()
    workflow_worker.start()

    try:
        yield
    finally:
        await dynamic_scheduler.stop()
        # Stop alle stdio MCP child processes netjes bij shutdown
        stdio_process_manager.stop_all()


def _frontend_dist():
    """Locate the built frontend (dist) to serve from the backend, so the app is a
    single origin/process (and packageable). Order: FRONTEND_DIST_DIR env, a bundled
    `web/` next to the backend (for the packaged app), then the repo dist."""
    import os
    from pathlib import Path
    here = Path(__file__).resolve()
    candidates = []
    env = os.environ.get("FRONTEND_DIST_DIR") or getattr(settings, "FRONTEND_DIST_DIR", "")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(here.parent / "web")                                  # bundled (packaged)
    candidates.append(here.parents[2] / "lovely-landing-project" / "dist")  # repo (dev)
    for c in candidates:
        if c.is_dir() and (c / "index.html").is_file():
            return c
    log.warningx("Frontend dist not found — serving API only", checked=[str(c) for c in candidates])
    return None


def _mount_frontend(app: FastAPI) -> None:
    """Serve the SPA: hashed assets under /assets, everything else falls back to
    index.html for client-side routing. Registered AFTER the API router so /api wins."""
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse
    from fastapi import HTTPException

    dist = _frontend_dist()
    if dist is None:
        return
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")
    index = dist / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa(full_path: str):
        # API + docs are matched earlier (registered first); guard anyway.
        if full_path == "api" or full_path.startswith("api/") or full_path in ("docs", "redoc", "openapi.json"):
            raise HTTPException(status_code=404)
        candidate = dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(index))

    log.infox("Frontend served from dist", dist=str(dist))


def create_app() -> FastAPI:
    app = FastAPI(title="ND3X Intelligent Workspace", lifespan=lifespan)

    add_security_headers(
        app,
        allow_origins=["*"],
        allow_credentials=False,
    )

    api_router = APIRouter(prefix="/api", tags=["API", "ND3X Intelligent Workspace"])
    # The setup router is always mounted; it self-locks once an admin exists.
    api_router.include_router(setup_router)

    if settings.CONFIGURED:
        from routers import all_routers
        for r in all_routers:
            api_router.include_router(r)
    else:
        log.warningx("Mounting first-time-setup API only (app not configured).")

    app.include_router(api_router)
    # Serve the built frontend (single origin) — after the API router so /api wins.
    _mount_frontend(app)
    return app


app = create_app()


def main():
    uvicorn.run(
        "server:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=True,
        log_level="info",
        workers=settings.RUNTIME_WORKERS,
    )


if __name__ == "__main__":
    main()
