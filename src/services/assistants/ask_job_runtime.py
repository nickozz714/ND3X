from pathlib import Path

from component.config import settings
from services.assistants.ask_job_service import AskJobService


ask_job_service = AskJobService(
    ask_root=Path(settings.ASK_JOB_ROOT),
    voice_root=Path(settings.VOICE_JOB_ROOT),
    cleanup_interval_seconds=settings.RUNTIME_JOB_CLEANUP_INTERVAL_SECONDS,
    run_retention_hours=settings.ASK_JOB_RUN_RETENTION_HOURS,
    active_retention_hours=settings.ASK_JOB_ACTIVE_RETENTION_HOURS,
    voice_retention_hours=settings.VOICE_JOB_RETENTION_HOURS,
    voice_active_retention_hours=settings.VOICE_JOB_ACTIVE_RETENTION_HOURS,
)
