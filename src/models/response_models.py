from __future__ import annotations

from typing import Any, Dict, Optional, List
from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str
    payload: Dict[str, Any] = {}
    conversation_id: Optional[str] = None
    thread_id: Optional[str] = None
    model: Optional[str] = None


class AskDoc(BaseModel):
    meta: str
    content: str
    # Optional: a compact list the UI can show as "results"
    items: Optional[list[dict]] = None

class AskResponse(BaseModel):
    # REQUIRED core
    mode: str
    answer: str

    # Thread support (UI can ignore if you don't use it yet)
    thread_id: Optional[str] = None
    pending_action: Optional[Dict[str, Any]] = None
    # ✅ NEW multi-tool / right-panel payloads
    tool_calls: List[Dict[str, Any]] = Field(default_factory=list)
    tool_results: List[Any] = Field(default_factory=list)
    docs: List[Dict[str, Any]] = Field(default_factory=list)

    # Debugging / inspection
    trace: List[Dict[str, Any]] = Field(default_factory=list)

    # ✅ Backward-compat legacy fields (keep so older UI doesn't crash)
    tool: Optional[str] = None
    tool_args: Optional[Dict[str, Any]] = None
    tool_result: Optional[Any] = None
    doc: Optional[Dict[str, Any]] = None
    kind: Optional[str] = None

class TranscriptResponse(BaseModel):
    mode: str
    text: str

class VoiceResponse(BaseModel):
    mode: str                # "voice"
    thread_id: str
    transcript: str
    answer: str              # markdown rendered from JSON
    data: Dict[str, Any]     # the JSON from VoiceAssistant

class RetrievedRepoFile(BaseModel):
    path: str
    content: str = ""
    tool: Optional[str] = None          # "repo_file_read" | "repo_file_preview"
    ev_id: Optional[str] = None
    blob_sha: Optional[str] = None
    truncated: bool = False


class CodeSessionResponse(BaseModel):
    mode: str
    answer: str

    # interactive gates
    pending_action: Optional[bool] = None
    pending: Optional[Dict[str, Any]] = None  # <-- your CSO uses "pending" for file approval state

    # NEW: files retrieved during the code session (for right panel + canvas)
    files: List[RetrievedRepoFile] = Field(default_factory=list)

    # transparency / debugging
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    trace: List[Dict[str, Any]] = Field(default_factory=list)

    # optional metadata
    applied: Optional[List[str]] = None
    skipped: Optional[List[str]] = None
    result: Optional[Any] = None
    evidence_used: Optional[List[str]] = None

