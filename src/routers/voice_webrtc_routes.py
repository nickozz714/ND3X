# routes/voice_webrtc_routes.py
from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from authentication.dependencies import require_user
from db.database import SessionLocal
from services.assistants.ask_job_callbacks import ask_job_callbacks
from services.providers.registry_service import ProviderRegistryService
from services.voice.voice_webrtc_conversation_service import VoiceWebRTCConversationService


router = APIRouter(
    prefix="/main/voice-webrtc",
    tags=["voice-webrtc"],
    dependencies=[Depends(require_user)],
)

voice_webrtc_service = VoiceWebRTCConversationService(
    # OpenAI key resolved lazily from the registry's OpenAI provider
    **ask_job_callbacks(),
)


class RealtimeSessionRequest(BaseModel):
    thread_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)


class StartTaskRequest(BaseModel):
    question: str
    thread_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    model: Optional[str] = None


class SubmitConfirmationRequest(BaseModel):
    thread_id: str
    answer: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    model: Optional[str] = None


def _user_identifier(user: Any) -> str:
    """
    Best-effort extraction.
    Keeps this router compatible with multiple require_user return shapes.
    """
    if user is None:
        return "anonymous"

    if isinstance(user, str):
        return user

    if isinstance(user, dict):
        return (
            user.get("email")
            or user.get("sub")
            or user.get("id")
            or user.get("username")
            or "anonymous"
        )

    return (
        getattr(user, "email", None)
        or getattr(user, "sub", None)
        or getattr(user, "id", None)
        or getattr(user, "username", None)
        or "anonymous"
    )


@router.post("/session")
async def create_realtime_session(
    req: RealtimeSessionRequest,
    user: Any = Depends(require_user),
) -> Dict[str, Any]:
    """
    Creates an ephemeral OpenAI Realtime client secret.

    Browser flow:
    1. POST here.
    2. Use response.value as Bearer token.
    3. Browser creates RTCPeerConnection.
    4. Browser POSTs SDP offer to https://api.openai.com/v1/realtime/calls.
    """
    thread_id = req.thread_id or req.payload.get("thread_id") or str(uuid.uuid4())
    payload = dict(req.payload or {})
    payload["thread_id"] = thread_id

    # Routing is authoritative: full-duplex voice requires a model on the realtime
    # slot. Without this, the service silently fell back to a hardcoded model and a
    # session started even though nothing was configured.
    db = SessionLocal()
    try:
        resolved = ProviderRegistryService(db).resolve_slot("realtime")
    finally:
        db.close()
    if resolved is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Voice (Realtime) is not configured. Assign a model to the "
                "Voice — Realtime slot under AI Models → Routing."
            ),
        )
    # Use the assigned model rather than the built-in default.
    payload.setdefault("realtime_model", resolved.model_id)

    return await voice_webrtc_service.create_client_secret(
        thread_id=thread_id,
        payload=payload,
        user_identifier=_user_identifier(user),
    )


@router.post("/task/start")
async def start_nd3x_task(req: StartTaskRequest) -> Dict[str, Any]:
    """
    Called by the browser when the Realtime datachannel receives a function call
    named start_nd3x_task.
    """
    payload = dict(req.payload or {})
    thread_id = req.thread_id or payload.get("thread_id") or str(uuid.uuid4())
    payload["thread_id"] = thread_id

    return await voice_webrtc_service.start_nd3x_task(
        question=req.question,
        thread_id=thread_id,
        payload=payload,
        model=req.model,
    )


@router.post("/task/confirm")
async def submit_nd3x_confirmation(req: SubmitConfirmationRequest) -> Dict[str, Any]:
    payload = dict(req.payload or {})
    payload["thread_id"] = req.thread_id

    return await voice_webrtc_service.submit_nd3x_confirmation(
        thread_id=req.thread_id,
        answer=req.answer,
        payload=payload,
        model=req.model,
    )


@router.get("/task/{thread_id}/{run_id}")
async def get_nd3x_task_status(thread_id: str, run_id: str) -> Dict[str, Any]:
    return voice_webrtc_service.get_nd3x_task_status(thread_id=thread_id, run_id=run_id)


@router.get("/task/{thread_id}/{run_id}/result")
async def get_nd3x_task_result(
    thread_id: str,
    run_id: str,
    max_chars: int = 6000,
) -> Dict[str, Any]:
    return voice_webrtc_service.get_nd3x_task_result(
        thread_id=thread_id,
        run_id=run_id,
        max_chars=max_chars,
    )
