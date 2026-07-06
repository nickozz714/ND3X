"""Auto mode — a small/cheap model decides on the user's behalf so the agent can
run unattended.

When the orchestrator pauses (ask_user / propose_plan / confirm_action), Auto mode
asks the **auto-decider** model to answer the way the user would, inferring intent
from the original request + the conversation so far. The reply is then submitted as
the next user turn (the front end drives the loop). No LLM-heavy orchestration here —
one short call.

Model resolution (no hardcoded models): routing slot `chat.auto_decision` →
fall back to `chat.cognition` → `chat.planner`.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from component.logging import get_logger
from models.assistant_thread import AssistantThreadMessageModel

log = get_logger(__name__)

_INSTRUCTIONS = (
    "You are an autopilot acting on the user's behalf in an AI chat. The assistant has "
    "paused and needs a decision. Using the user's ORIGINAL request and the conversation "
    "so far, reply EXACTLY as the user would — a concrete instruction (e.g. 'grab that "
    "document and return it', 'keep searching for the exact one'), or 'yes'/'approve'/'no' "
    "for an approval or confirmation. Stay faithful to what the user originally wanted; "
    "prefer letting the work finish. Output ONLY the reply text, nothing else. If you "
    "genuinely cannot infer the user's intent, output exactly 'STOP: <short reason>'."
)

_KIND_HINT = {
    "ask_user": "The assistant asked a clarifying question.",
    "propose_plan": "The assistant proposed a plan and is waiting for approval (reply 'approve' to proceed, or adjust it).",
    "confirm_action": "The assistant wants to confirm a (possibly destructive) action (reply 'yes' to allow, 'no' to decline).",
}


def _resolve_decider_model(db: Session) -> Optional[str]:
    from services.providers.registry_service import ProviderRegistryService
    reg = ProviderRegistryService(db)
    for slot in ("chat.auto_decision", "chat.cognition", "chat.planner"):
        try:
            r = reg.resolve_slot(slot)
        except Exception:  # noqa: BLE001
            r = None
        if r and getattr(r, "model_id", None):
            return r.model_id
    return None


def _recent_transcript(db: Session, thread_id: str, *, max_msgs: int = 8) -> str:
    rows = (
        db.query(AssistantThreadMessageModel)
        .filter(AssistantThreadMessageModel.thread_id == thread_id)
        .order_by(AssistantThreadMessageModel.sequence.asc())
        .all()
    )
    if not rows:
        return ""
    original = rows[0]
    tail = rows[-max_msgs:]
    lines = [f"ORIGINAL REQUEST ({original.role}): {original.content}", "", "RECENT:"]
    for r in tail:
        who = "user" if r.role == "user" else "assistant"
        lines.append(f"- {who}: {(r.content or '')[:600]}")
    return "\n".join(lines)


async def decide(db: Session, *, thread_id: str, kind: str, agent_message: str) -> Dict[str, Any]:
    """Return {'reply': str, 'stop': bool, 'model': str|None}. `stop` is True when
    the decider hands control back to the human (couldn't infer / no model / error)."""
    model = _resolve_decider_model(db)
    if not model:
        return {"reply": "STOP: no auto-decider model assigned (set one under AI Models → Routing).", "stop": True, "model": None}

    transcript = _recent_transcript(db, thread_id)
    prompt = (
        f"{transcript}\n\n"
        f"ASSISTANT IS WAITING — {_KIND_HINT.get(kind, 'The assistant needs a decision.')}\n"
        f"Assistant message:\n{agent_message}\n\n"
        "Your reply (as the user):"
    )
    try:
        from services.openai_service import OpenAIResponsesService
        from services.providers.provider_factory import build_llm_router
        router = build_llm_router(OpenAIResponsesService(), db)
        resp = await router.ask_orchestration_async(
            prompt,
            role="auto_decision",
            model=model,
            instructions=_INSTRUCTIONS,
            keep_context=False,
            store=False,
            session_id=None,
            max_output_tokens=400,
        )
        text = (getattr(resp, "text", "") or "").strip()
    except Exception as exc:  # noqa: BLE001 — auto decision must never crash the turn
        log.warningx("Auto-decision mislukt", thread_id=thread_id, error=str(exc))
        return {"reply": f"STOP: auto-decider error ({exc}).", "stop": True, "model": model}

    if not text:
        return {"reply": "STOP: auto-decider returned nothing.", "stop": True, "model": model}
    return {"reply": text, "stop": text.upper().startswith("STOP"), "model": model}
