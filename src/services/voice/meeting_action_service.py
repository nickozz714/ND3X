"""Meeting-driven actions (TODO #9) — Phase 1: read-only, suggest-first.

A cheap "trigger detector" model runs once per live-updater tick over the new
transcript delta + the current structured notes state. When it spots an
actionable moment (a factual question the room can't answer, a company/product/
term worth a quick look-up), we run the allowed READ-ONLY tool (Phase 1:
``web_search``) and append a card to ``actions.jsonl`` in the run dir, which the
front end polls alongside the markdown draft.

Design notes:
  - Mirrors ``services/auto_decision_service``: one short, slot-resolved LLM
    call; **never throws** (returns [] / an error card on failure); no heavy
    orchestration here.
  - ``process_delta`` is the single detached entry point called (fire-and-forget)
    from the live updater, so a slow look-up never blocks the note lane.
  - Everything is gated by the meeting profile's ``action_policy`` — disabled by
    default, opt-in per profile.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from component.logging import get_logger
from services.voice.voice_utilities import append_jsonl, read_jsonl, utc_iso

log = get_logger("svc.meeting_action")

ACTIONS_FILE = "actions.jsonl"

# Phase 1 defaults (used when the policy leaves them unset).
_DEFAULT_ALLOWED_TOOLS = ("web_search",)
_DEFAULT_ALLOWED_ACTIONS = ("lookup", "answer")
_DEFAULT_MIN_CONFIDENCE = 0.55
_DEFAULT_MAX_PER_TICK = 2
_DEFAULT_BUDGET = 24

# Model resolution: ONLY the dedicated detector slot. If it is unassigned the
# feature is off — no fallback to other chat slots (user decision 2026-07-05).
_DETECTOR_SLOT = "meeting.action_detector"

_DETECTOR_INSTRUCTIONS = (
    "You watch a live meeting transcript and decide whether the assistant should take a small "
    "READ-ONLY action right now to help the participants. Most of the time you do NOTHING. "
    "Only act on a clear, self-contained moment: a factual question the room cannot answer, a "
    "company/product/person/term/technology worth a quick look-up, or an explicit request for "
    "information. Never act on chit-chat, opinions, action items, or anything already present in "
    "the captured notes. Respond with ONLY a JSON array (which may be empty). Each item is "
    '{"type":"lookup"|"answer","topic":"<2-5 word label>","query":"<a focused web-search query>",'
    '"confidence":<0.0-1.0>}. Emit at most 2 items, highest-value first. No prose, no code fences.'
)


# ---------------------------------------------------------------------------
# Policy resolution
# ---------------------------------------------------------------------------
def load_policy(profile_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """The effective ``action_policy`` for a meeting profile id, or None when
    actions are not enabled for this profile. DB profiles are ``mp-<id>``;
    code/builtin profiles may carry a default ``action_policy`` attribute."""
    policy: Optional[Dict[str, Any]] = None
    try:
        if profile_id and profile_id.startswith("mp-"):
            try:
                db_id = int(profile_id[3:])
            except ValueError:
                return None
            from db.database import SessionLocal
            from models.meeting_profile import MeetingProfile
            db = SessionLocal()
            try:
                p = db.get(MeetingProfile, db_id)
                policy = getattr(p, "action_policy", None) if p is not None else None
            finally:
                db.close()
        else:
            from services.voice.voice_profiles.registry import get_profile
            policy = getattr(get_profile(profile_id), "action_policy", None)
    except Exception as exc:  # noqa: BLE001 — never break the meeting on policy lookup
        log.warningx("meeting_action:load_policy:failed", profile_id=profile_id, error=str(exc))
        return None

    if not isinstance(policy, dict) or not policy.get("enabled"):
        return None
    return policy


def _resolve_detector_model(db) -> Optional[str]:
    """The model for meeting action detection, ONLY from the dedicated
    meeting.action_detector slot. Unassigned → None → detection is off (no
    fallback to other slots — actions must be explicitly enabled)."""
    from services.providers.registry_service import ProviderRegistryService
    reg = ProviderRegistryService(db)
    try:
        r = reg.resolve_slot(_DETECTOR_SLOT)
    except Exception:  # noqa: BLE001
        r = None
    return getattr(r, "model_id", None) if r else None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def _state_brief(state: Optional[Dict[str, Any]], *, limit: int = 700) -> str:
    """A compact view of what the notes already cover, so the detector doesn't
    re-look-up things already captured."""
    if not isinstance(state, dict):
        return "(no notes yet)"
    parts: List[str] = []
    views = state.get("views") if isinstance(state.get("views"), dict) else {}
    if views.get("exec"):
        parts.append(f"Summary: {views['exec']}")
    for key in ("highlights", "open_questions"):
        vals = state.get(key)
        if isinstance(vals, list) and vals:
            flat = "; ".join(str(v.get("text") if isinstance(v, dict) else v) for v in vals[:6])
            parts.append(f"{key}: {flat}")
    brief = "\n".join(parts).strip()
    return (brief[:limit] + "…") if len(brief) > limit else (brief or "(no notes yet)")


def _parse_actions(text: str) -> List[Dict[str, Any]]:
    """Robustly pull a JSON array of action dicts out of the model output."""
    if not text:
        return []
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", t).strip()
    # Grab the first [...] block if there's surrounding prose.
    if not t.startswith("["):
        m = re.search(r"\[.*\]", t, re.DOTALL)
        if m:
            t = m.group(0)
    try:
        data = json.loads(t)
    except Exception:  # noqa: BLE001
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict)]


async def detect_actions(*, delta_text: str, state: Optional[Dict[str, Any]], policy: Dict[str, Any], db) -> List[Dict[str, Any]]:
    """Run the detector over the new delta. Returns a filtered list of action
    dicts ({type, topic, query, confidence}); [] on most ticks / any failure."""
    delta_text = (delta_text or "").strip()
    if not delta_text:
        return []
    model = _resolve_detector_model(db)
    if not model:
        log.debugx("meeting_action:detect:no_model")
        return []

    allowed_actions = set(policy.get("allowed_actions") or _DEFAULT_ALLOWED_ACTIONS)
    triggers = policy.get("triggers") or []
    min_conf = float(policy.get("min_confidence") or _DEFAULT_MIN_CONFIDENCE)
    max_per_tick = int(policy.get("max_per_tick") or _DEFAULT_MAX_PER_TICK)

    prompt = (
        (f"Profile focus keywords (bias toward these): {', '.join(triggers)}\n\n" if triggers else "")
        + f"Notes captured so far (do NOT re-look-up these):\n{_state_brief(state)}\n\n"
        + f"New transcript since last check:\n{delta_text}\n\n"
        + "Return the JSON array of actions (or [] for nothing)."
    )

    try:
        from services.openai_service import OpenAIResponsesService
        from services.providers.provider_factory import build_llm_router
        router = build_llm_router(OpenAIResponsesService(), db)
        resp = await router.ask_orchestration_async(
            prompt,
            role="meeting_action_detector",
            model=model,
            instructions=_DETECTOR_INSTRUCTIONS,
            keep_context=False,
            store=False,
            session_id=None,
            max_output_tokens=500,
        )
        text = (getattr(resp, "text", "") or "").strip()
    except Exception as exc:  # noqa: BLE001 — detection must never crash the meeting
        log.warningx("meeting_action:detect:failed", error=str(exc))
        return []

    items = _parse_actions(text)
    out: List[Dict[str, Any]] = []
    for it in items:
        kind = (it.get("type") or "lookup").strip()
        if kind not in allowed_actions:
            continue
        try:
            conf = float(it.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < min_conf:
            continue
        query = (it.get("query") or it.get("topic") or "").strip()
        if not query:
            continue
        out.append({"type": kind, "topic": (it.get("topic") or query).strip(), "query": query, "confidence": conf})
        if len(out) >= max_per_tick:
            break
    log.infox("meeting_action:detect:done", raw=len(items), kept=len(out), model=model)
    return out


# ---------------------------------------------------------------------------
# Execution (Phase 1: read-only web_search)
# ---------------------------------------------------------------------------
def _shape_search_result(result: Any) -> tuple[str, List[Dict[str, str]], str]:
    """Map a web_search tool result to (body, sources, status)."""
    if isinstance(result, dict):
        if result.get("ok") is False or result.get("status") == "error":
            return (str(result.get("error") or "Look-up unavailable."), [], "error")
        answer = result.get("answer") or result.get("text") or ""
        if answer:
            return (str(answer).strip(), [], "done")
        return ("No result.", [], "done")
    return (str(result), [], "done")


async def run_action(*, action: Dict[str, Any], policy: Dict[str, Any]) -> Dict[str, Any]:
    """Execute one detected action and return its card record. Phase 1 supports
    read-only ``web_search`` only; everything is autonomy=suggest (auto-run,
    read-only)."""
    allowed_tools = set(policy.get("allowed_tools") or _DEFAULT_ALLOWED_TOOLS)
    query = (action.get("query") or action.get("topic") or "").strip()
    rec: Dict[str, Any] = {
        "id": uuid.uuid4().hex[:12],
        "ts": utc_iso(),
        "type": action.get("type") or "lookup",
        "topic": action.get("topic") or query,
        "query": query,
        "confidence": action.get("confidence"),
        "autonomy": "suggest",
        "status": "running",
        "tool": None,
        "title": (action.get("topic") or query or "Look-up").strip(),
        "body": "",
        "sources": [],
    }

    if "web_search" not in allowed_tools:
        rec["status"] = "error"
        rec["body"] = "No read-only tool is allowed for this profile."
        return rec

    try:
        from services.builtin.internal_tool_registry import internal_tool_registry
        result = await internal_tool_registry.call("web_search", {"query": query, "max_results": 5})
        rec["tool"] = "web_search"
        body, sources, status = _shape_search_result(result)
        rec["body"], rec["sources"], rec["status"] = body, sources, status
    except Exception as exc:  # noqa: BLE001
        rec["status"] = "error"
        rec["body"] = f"Look-up failed: {exc}"
        log.warningx("meeting_action:run_action:failed", error=str(exc))
    return rec


# ---------------------------------------------------------------------------
# Artifact helpers + the detached orchestration entry point
# ---------------------------------------------------------------------------
def read_actions(run_dir: Path) -> List[Dict[str, Any]]:
    return read_jsonl(run_dir / ACTIONS_FILE)


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


async def process_delta(*, run_dir: Path, thread_id: str, profile_id: Optional[str],
                        delta_text: str, state: Optional[Dict[str, Any]]) -> None:
    """Detached, fire-and-forget entry point from the live updater. Detects
    actions for this delta, dedupes against already-fired ones, respects the
    per-meeting budget, runs the allowed read-only tool, and appends cards to
    actions.jsonl. Never throws."""
    try:
        policy = load_policy(profile_id)
        if not policy:
            return

        from db.database import SessionLocal
        db = SessionLocal()
        try:
            actions = await detect_actions(delta_text=delta_text, state=state, policy=policy, db=db)
            if not actions:
                return

            existing = read_actions(run_dir)
            seen = {_norm(a.get("topic")) for a in existing} | {_norm(a.get("query")) for a in existing}
            budget = int(policy.get("action_budget") or _DEFAULT_BUDGET)
            fired = len([a for a in existing if a.get("status") in ("done", "running")])

            for action in actions:
                if fired >= budget:
                    log.infox("meeting_action:budget_reached", run_dir=str(run_dir), budget=budget)
                    break
                key = _norm(action.get("topic") or action.get("query"))
                if key in seen:
                    continue
                seen.add(key)
                rec = await run_action(action=action, policy=policy)
                append_jsonl(run_dir / ACTIONS_FILE, rec)
                fired += 1
                log.infox("meeting_action:card_appended", run_dir=str(run_dir),
                          action_id=rec["id"], type=rec["type"], status=rec["status"])
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 — detached task must never surface
        log.warningx("meeting_action:process_delta:failed", run_dir=str(run_dir), error=str(exc))
