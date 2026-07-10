# services/assistants/ask_job_callbacks.py
from __future__ import annotations

import asyncio
import re
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from component.config import settings
from component.logging import get_logger
from db.database import SessionLocal
from models.response_models import AskResponse

log = get_logger(__name__)

from services.application_setting_service import ApplicationSettingService
from services.assistant_thread_service import AssistantThreadService
from services.assistant_output_store_service import AssistantOutputStoreService
from services.assistants.assistant_service import AssistantService
from services.assistants.orchestration.orchestrator import AssistantOrchestrator
from services.assistants.orchestration.pending import PendingStore
from services.mcp.mcp_client_factory import MCPClientFactory
from services.mcp.stdio_process_manager import StdioProcessManager
from services.mcp.builtin_mcp_client import BuiltinMCPClient
from services.shell.az_login_service import AzLoginService
from services.mcp.tool_execution_service import ToolExecutionService
from services.openai_service import OpenAIResponsesService
from services.workflows.workflow_factory import WorkflowFactory
from services.workflows.workflow_run_service import WorkflowRunService
from services.workflows.workflow_service import WorkflowService

from db.faiss_store import FaissStore
from services.text.text_storage_service import TextStorageService
from services.text.text_indexing_service import TextIndexingService
from services.text.text_search_service import TextSearchService
# ── Toevoegen aan imports bovenaan ────────────────────────────────────────────
from services.pdf.pdf_render_service import PdfRenderService
from services.pdf.template_service import TemplateService

openai = OpenAIResponsesService(
    # OpenAI key resolved lazily from the registry's OpenAI provider
    model=None,  # chat model comes from the routing slots (registry), not config
    embedding_model=None,  # embeddings model comes from the embeddings slot
)

# ── Text service singletons ───────────────────────────────────────────────────

_files_dir = str(settings.FILES_DIR)

faiss_store = FaissStore(index_path=f"{_files_dir}/faiss/index.bin")

text_storage = TextStorageService(files_root=_files_dir)

# Embeddings route through the provider registry's "embeddings" slot when one is
# assigned (e.g. a local nomic-embed via Ollama); otherwise OpenAI as before.
from services.providers.routed_embedding import RoutedEmbeddingService
_embedding_service = RoutedEmbeddingService(openai)

text_indexer = TextIndexingService(
    faiss=faiss_store,
    storage=text_storage,
    openai=_embedding_service,
)

text_searcher = TextSearchService(
    faiss=faiss_store,
    openai=_embedding_service,
)

pdf_render_service = PdfRenderService(
    templates_root=Path(_files_dir) / "templates",
    output_dir=Path(_files_dir) / "generated_pdfs",
)

template_service = TemplateService(
    templates_root=Path(_files_dir) / "templates",
)

stdio_process_manager = StdioProcessManager()

az_login_service = AzLoginService()

builtin_mcp_client = BuiltinMCPClient(
    az_login_service=az_login_service,
    db_factory=SessionLocal,
)

mcp_client_factory = MCPClientFactory(
    stdio_process_manager=stdio_process_manager,
    builtin_mcp_client=builtin_mcp_client,
)

pending_store = PendingStore()

import services.builtin.tools.text_tools  # noqa: F401, E402
import services.builtin.tools.pdf_tools  # noqa: F401, E402
import services.builtin.tools.file_tools  # noqa: F401, E402
import services.builtin.tools.agent_tools  # noqa: F401, E402
import services.builtin.tools.background_tasks  # noqa: F401, E402
import services.builtin.tools.fabric_data_agent_tool  # noqa: F401, E402
import services.builtin.tools.transfer_tools  # noqa: F401, E402
import services.builtin.tools.web_search_tool  # noqa: F401, E402
import services.builtin.tools.image_tools  # noqa: F401, E402
import services.builtin.tools.workflow_tools  # noqa: F401, E402
import services.builtin.tools.secret_tools  # noqa: F401, E402
import services.builtin.tools.board_tools  # noqa: F401, E402
import services.builtin.tools.repo_tools  # noqa: F401, E402

async def boot_stdio_servers() -> None:
    """
    Start alle stdio MCP servers vanuit de database.

    Aanroepen in de FastAPI lifespan:

        from contextlib import asynccontextmanager
        from services.assistants.ask_job_callbacks import boot_stdio_servers, stdio_process_manager

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            await boot_stdio_servers()
            yield
            stdio_process_manager.stop_all()

        app = FastAPI(lifespan=lifespan)
    """
    db = SessionLocal()
    try:
        await stdio_process_manager.boot_from_db(db)
    finally:
        db.close()


# ── Orchestrator scope ────────────────────────────────────────────────────────

@contextmanager
def orchestrator_scope():
    db = SessionLocal()
    try:
        workflow_service = WorkflowService(db=db)
        workflow_run_service = WorkflowRunService(db=db)
        workflow_factory = WorkflowFactory(
            workflow_service=workflow_service,
            workflow_run_service=workflow_run_service,
        )
        assistant_service = AssistantService(db=db)
        tool_execution_service = ToolExecutionService(db=db)
        assistant_output_store_service = AssistantOutputStoreService(db=db)
        application_setting_service = ApplicationSettingService(db=db)

        # Wrap the OpenAI service in the provider-agnostic router. With an empty
        # provider registry this is a transparent pass-through (identical behavior);
        # configured providers (Claude, local, ...) are routed by model.
        from services.providers.provider_factory import build_llm_router
        llm_service = build_llm_router(openai, db)

        orchestrator = AssistantOrchestrator(
            openai_service=llm_service,
            assistant_service=assistant_service,
            tool_execution_service=tool_execution_service,
            assistant_output_store_service=assistant_output_store_service,
            mcp_client_factory=mcp_client_factory,
            application_setting_service=application_setting_service,
            workflow_factory=workflow_factory,
            workflow_service=workflow_service,
            pending_store=pending_store,
        )
        yield orchestrator
    finally:
        db.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_serialize(obj: Any) -> str:
    import json
    try:
        return json.dumps(obj, default=str, ensure_ascii=False, indent=2)
    except Exception:
        return str(obj)


def _base_ask_response(
    *,
    mode: str,
    answer: str,
    thread_id: str,
    pending_action: Optional[Dict[str, Any]] = None,
    trace: Optional[list] = None,
) -> Dict[str, Any]:
    payload = {
        "mode": mode,
        "answer": answer,
        "thread_id": thread_id,
        "pending_action": pending_action,
        "tool_calls": [],
        "tool_results": [],
        "docs": [],
        # trace is typed list[dict]; coerce stray strings/other so a bad entry can
        # never crash the response builder (that used to surface a raw traceback).
        "trace": _coerce_trace(trace),
    }
    try:
        return AskResponse(**payload).model_dump()
    except Exception as e:
        # Never leak a Python traceback into the chat — log it, show a clean message.
        log.exceptionx("AskResponse build mislukt", error=str(e))
        return {
            "mode": "error",
            "answer": (
                "⚠️ **Something went wrong**\n\n"
                "The assistant produced a response that couldn't be displayed. "
                "Please try again."
            ),
            "thread_id": thread_id,
            "pending_action": None,
            "tool_calls": [],
            "tool_results": [],
            "docs": [],
            "trace": [{"type": "error", "message": "response_build_error"}],
        }


def _coerce_trace(trace: Optional[list]) -> list:
    """Force every trace entry to a dict so it satisfies AskResponse.trace
    (list[dict]). Strings/other values are wrapped instead of failing validation."""
    out: list = []
    for entry in (trace or []):
        if isinstance(entry, dict):
            out.append(entry)
        else:
            out.append({"type": "trace", "message": str(entry)})
    return out


def model_rejected_ask_response(thread_id: str) -> Dict[str, Any]:
    return _base_ask_response(
        mode="error",
        answer=(
            "**Error:** This request is not valid because the chosen model is not allowed "
            "or the plan is not configured."
        ),
        thread_id=thread_id,
    )


def timeout_ask_response(thread_id: str) -> Dict[str, Any]:
    return _base_ask_response(
        mode="timeout",
        answer=(
            "⏱️ **Request timed out**\n\n"
            "This request took too long to process and hit a timeout.\n\n"
            "### What you can do next\n"
            "- Retry the request\n"
            "- Continue the conversation using the same thread\n"
        ),
        thread_id=thread_id,
    )


def _looks_like_raw_error(text: str) -> bool:
    """Heuristic: a chat answer that is actually a raw error/traceback dump rather
    than a deliberate, already-friendly message."""
    t = (text or "").lstrip()
    if t.startswith(("⚠️", "⏱️")):  # already a friendly message
        return False
    low = t.lower()
    return (
        t.startswith("**Error:**")
        or "traceback (most recent call last)" in low
        or "error code:" in low
        or "'type': 'not_found_error'" in low
    )


def humanize_error_text(msg: str) -> str:
    """Turn a raw provider/runtime error string into a short, friendly chat message.
    The full technical detail is logged separately, never shown to the user."""
    low = (msg or "").lower()

    # Model not installed / unknown (e.g. Ollama 404 "model 'qwen2.5:32b' not found").
    if ("not found" in low or "not_found" in low) and "model" in low:
        m = re.search(r"model ['\"]([^'\"]+)['\"]", msg)
        name = f"**{m.group(1)}**" if m else "the selected model"
        return (
            "⚠️ **Model not available**\n\n"
            f"The model {name} isn't installed on the provider yet.\n\n"
            "**What you can do:**\n"
            "- Install it under **AI Models → Local Models** (expand the model and pick a size), or\n"
            "- Choose a different model with the model selector at the top of the chat."
        )

    # Provider unreachable (local Ollama down, network).
    if any(k in low for k in ("connection refused", "connect error", "connecterror",
                              "max retries", "failed to establish", "name or service not known",
                              "connection error")):
        return (
            "⚠️ **Can't reach the model**\n\n"
            "The model provider didn't respond. If this is a local model, make sure "
            "**Ollama is running** (AI Models → Local Models → Start), then try again."
        )

    # Auth / key problems.
    if any(k in low for k in ("api key", "unauthorized", "401", "authentication")):
        return (
            "⚠️ **The model rejected the request**\n\n"
            "The provider returned an authentication error. Check the provider's API key "
            "under **AI Models → Providers**."
        )

    # Took too long.
    if "timed out" in low or "timeout" in low:
        return (
            "⏱️ **The model took too long**\n\n"
            "The request timed out before an answer came back. Local models can be slow — "
            "try a smaller/faster model, or retry."
        )

    # Generic fallback — short, no traceback.
    detail = (msg or "").strip().splitlines()[0][:300] if (msg or "").strip() else "Unknown error."
    return (
        "⚠️ **Something went wrong**\n\n"
        "The assistant couldn't complete this request. Please try again.\n\n"
        f"_Details: {detail}_"
    )


def _humanize_error(exc: Exception) -> str:
    return humanize_error_text(str(exc))


def normalize_error_answer(result: Dict[str, Any]) -> Dict[str, Any]:
    """If an orchestrator result is a raw error dump (mode='error' with a traceback /
    provider message in `answer`), replace the answer with a friendly message. Leaves
    already-friendly messages untouched. Used for the normal-return path."""
    if not isinstance(result, dict) or result.get("mode") != "error":
        return result
    answer = result.get("answer")
    if isinstance(answer, str) and _looks_like_raw_error(answer):
        # Strip a leading "**Error:** " so the humanizer sees the bare provider text.
        bare = re.sub(r"^\*\*Error:\*\*\s*", "", answer).strip()
        result = {**result, "answer": humanize_error_text(bare)}
    return result


def error_ask_response(thread_id: str, exc: Exception) -> Dict[str, Any]:
    # Full technical detail goes to the logs only; the chat gets a clean message.
    log.warningx("Ask turn mislukt", error=str(exc), traceback=traceback.format_exc())
    return _base_ask_response(
        mode="error",
        answer=_humanize_error(exc),
        thread_id=thread_id,
        trace=[{"type": "error", "message": str(exc).splitlines()[0][:500] if str(exc) else "error"}],
    )


def _recent_document_actions(thread_id: str, *, limit: int = 8) -> list[dict]:
    """Recent successful document/file-changing tool actions in this thread, from the audit
    trail — so the agent remembers what it created/edited across turns (these are tool
    actions, not chat messages). Best-effort; never breaks turn setup."""
    DOC_TOOLS = {"text__ingest", "text__update", "text__delete", "pdf__render"}
    out: list[dict] = []
    try:
        from services.audit_service import AuditService
        _total, events = AuditService().get_thread_events(thread_id=thread_id, limit=300, newest_first=True)
        seen: set = set()
        for ev in events or []:
            if ev.get("type") != "tool_result":
                continue
            data = ev.get("data") or {}
            tool = data.get("tool")
            if tool not in DOC_TOOLS:
                continue
            if str(data.get("status") or "success").lower() not in ("success", "ok", ""):
                continue
            args = data.get("args") or {}
            ref = (
                args.get("path") or args.get("relative_path") or args.get("title")
                or args.get("file_path") or args.get("name") or data.get("local_path")
                or (str(args.get("content") or "").strip()[:80] or None)
            )
            if not ref:
                continue
            key = (tool, str(ref))
            if key in seen:
                continue
            seen.add(key)
            out.append({"tool": tool, "ref": str(ref)[:160]})
            if len(out) >= limit:
                break
    except Exception:  # noqa: BLE001 — memory enrichment must never break a turn
        return []
    return out


async def build_active_conversation_state(
    *,
    thread_id: str,
    limit: int = 8,
    max_chars_per_message: int = 1200,
) -> Dict[str, Any]:
    try:
        service = AssistantThreadService()
        result = await service.list_messages(thread_id=thread_id, limit=limit, offset=0)
        items = result.get("items") or []

        messages = []
        for item in items:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = (item.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            messages.append({
                "role": role,
                "content": content[:max_chars_per_message],
                "created_at": item.get("created_at"),
                "turn_id": item.get("turn_id"),
                "sequence": item.get("sequence"),
            })

        messages = sorted(
            messages,
            key=lambda x: (x.get("created_at") or "", x.get("sequence") or 0),
        )
        recent_messages = messages[-limit:]

        last_user_message = None
        last_assistant_message = None
        for message in reversed(recent_messages):
            if message.get("role") == "user" and last_user_message is None:
                last_user_message = message.get("content")
            if message.get("role") == "assistant" and last_assistant_message is None:
                last_assistant_message = message.get("content")
            if last_user_message and last_assistant_message:
                break

        # If the thread was compacted, inject the running summary so the model has
        # the older context even though the server-side chain was reset.
        compaction_summary = None
        try:
            from services.compaction_service import latest_compaction_summary
            _db = SessionLocal()
            try:
                compaction_summary = latest_compaction_summary(_db, thread_id)
            finally:
                _db.close()
        except Exception:  # noqa: BLE001
            compaction_summary = None

        # Tool ACTIONS (documents created/updated, files written) are not chat messages, so
        # they aren't in recent_messages — yet the user refers to them ("the document we
        # made", "haal het op"). Surface recent document/file actions from the audit trail
        # so the agent can resolve such references and fetch the item instead of asking.
        recent_documents = _recent_document_actions(thread_id)

        state = {
            "thread_id": thread_id,
            "recent_messages": recent_messages,
            "last_user_message": last_user_message,
            "last_assistant_message": last_assistant_message,
            "instruction": (
                "Use this active conversation state to resolve follow-ups, short confirmations, "
                "corrections, frustration, pronouns, and references such as 'yes', 'doe maar', "
                "'herschrijf het document', 'die reis', 'dat document', or 'stop met vragen'. "
                "If this state shows the previous assistant was handling a specific task, document, "
                "trip, tool result, or open question, continue that task instead of treating the "
                "current user message as a brand-new request."
            ),
        }
        if recent_documents:
            state["recent_documents"] = recent_documents
            state["instruction"] += (
                " recent_documents lists documents/files you created or changed earlier in THIS "
                "session. When the user refers to 'the document', 'it', 'that story', etc., resolve "
                "it to one of these and fetch/update it with text__search / text__list_files / "
                "text__get_file / text__update — do NOT ask which document unless it is truly ambiguous."
            )
        if compaction_summary:
            state["compaction_summary"] = compaction_summary
            state["instruction"] += (
                " Earlier parts of this conversation were compacted; treat the following summary as "
                "established context: " + compaction_summary
            )
        return state
    except Exception as exc:
        return {
            "thread_id": thread_id,
            "recent_messages": [],
            "last_user_message": None,
            "last_assistant_message": None,
            "error": repr(exc),
            "instruction": "Active conversation state could not be loaded.",
        }


async def run_ask_orchestrator(
    *,
    question: str,
    payload: Dict[str, Any],
    thread_id: str,
    model: str,
    progress_cb=None,
) -> Dict[str, Any]:
    payload = dict(payload or {})

    if not payload.get("_workflow_background"):
        payload["_active_conversation_state"] = await build_active_conversation_state(
            thread_id=thread_id,
            limit=8,
            max_chars_per_message=1200,
        )
        # UI-editable chat agent-loop budgets (AI Models). Applied as the loop override so
        # operators can tune how far a turn may go without a code/env change.
        try:
            from services.llm_runtime_settings import chat_agent_budgets
            with SessionLocal() as _db:
                payload.setdefault("_agent_budget_overrides", chat_agent_budgets(_db))
        except Exception as exc:  # noqa: BLE001 — budgets fall back to config defaults
            log.warningx("Chat agent budgets laden mislukt; config defaults", error=str(exc))

    # A model explicitly chosen in the chat UI overrides workbench routing for this
    # request, and a provider switch mid-session triggers a context handoff summary
    # written by the previously active model.
    from services.providers.chat_session import forced_chat_model
    from services.providers.attachment_context import native_attachment_resources
    forced_model = payload.get("forced_model")
    forced_model_token = forced_chat_model.set(forced_model)
    attachment_resource_token = native_attachment_resources.set(
        dict(payload.get("_attachment_native_resources") or {})
    )
    if forced_model and not payload.get("_workflow_background"):
        try:
            from services.providers.model_handoff import handle_model_switch
            db = SessionLocal()
            try:
                handoff_summary = await handle_model_switch(thread_id, forced_model, openai, db=db)
            finally:
                db.close()
            if handoff_summary:
                acs = dict(payload.get("_active_conversation_state") or {})
                acs["model_handoff_summary"] = handoff_summary
                payload["_active_conversation_state"] = acs
        except Exception as exc:  # noqa: BLE001 — handoff must never break the turn
            log.warningx("Model handoff verwerken mislukt", thread_id=thread_id, error=str(exc))

    from services.providers.capability_router import CapabilityNotConfigured
    from services.providers.usage_accumulator import reset as _usage_reset, drain as _usage_drain
    _usage_reset()  # collect this turn's actual token usage across all stages/providers

    # Local chat models get a bigger overall budget (RUNTIME_TIMEOUT_LOCAL): their
    # planner steps are prefill-bound and a multi-step tool turn does not fit the
    # cloud default. Resolved per turn from the forced model or the planner slot.
    runtime_timeout = settings.RUNTIME_TIMEOUT
    try:
        from services.providers.registry_service import ProviderRegistryService
        with SessionLocal() as _db:
            _reg = ProviderRegistryService(_db)
            _turn_model = (payload.get("forced_model") or model or "").strip()
            if _turn_model:
                _local = _reg.model_is_local(_turn_model)
            else:
                _rm = _reg.resolve_slot("chat.planner")
                _local = bool(_rm and (_rm.provider_type == "ollama" or _reg.model_is_local(_rm.model_id)))
        if _local:
            runtime_timeout = max(
                runtime_timeout, int(getattr(settings, "RUNTIME_TIMEOUT_LOCAL", 900) or 900)
            )
        # Goal mode also gets the long budget: its whole point is to keep
        # working within one turn instead of giving up at the cloud default.
        if payload.get("_goal_mode"):
            runtime_timeout = max(
                runtime_timeout, int(getattr(settings, "RUNTIME_TIMEOUT_LOCAL", 900) or 900)
            )
    except Exception as exc:  # noqa: BLE001 — budget resolution must never break the turn
        log.warningx("Lokale-model timeout bepalen mislukt; cloud default", error=str(exc))

    try:
        try:
            with orchestrator_scope() as orchestrator:
                out = await asyncio.wait_for(
                    orchestrator.run(
                        question=question,
                        payload=payload,
                        thread_id=thread_id,
                        model=model,
                        progress_cb=progress_cb,
                    ),
                    timeout=runtime_timeout,
                )
        except CapabilityNotConfigured as exc:
            # A required capability (chat/embeddings) has no model assigned — stop
            # with a clear, actionable message instead of a generic failure.
            log.warningx("Vereiste capability niet geconfigureerd", thread_id=thread_id, error=str(exc))
            return {"mode": "error", "answer": f"**Not configured:** {exc}", "thread_id": thread_id}
    finally:
        native_attachment_resources.reset(attachment_resource_token)
        forced_chat_model.reset(forced_model_token)

    # Persist actual token usage for this turn (router + planner + final + cognition,
    # across providers) to the ledger for the usage dashboard / context budget.
    try:
        events = _usage_drain()
        if events:
            db_u = SessionLocal()
            try:
                from services.usage_service import UsageService
                svc = UsageService(db_u)
                turn_id = payload.get("turn_id") or payload.get("_turn_id")
                for e in events:
                    svc.record(
                        thread_id=thread_id,
                        input_tokens=e.get("input_tokens"),
                        output_tokens=e.get("output_tokens"),
                        turn_id=turn_id,
                        role=e.get("role"),
                        provider_type=e.get("provider_type"),
                        model=e.get("model"),
                        estimated=False,
                    )
                # Auto-compaction: if the thread now nears the model's context
                # window, summarise + reset the chain so the next turn starts smaller.
                try:
                    from services.providers.registry_service import ProviderRegistryService
                    reg = ProviderRegistryService(db_u)
                    window = None
                    # Resolve the active chat model's context window from the
                    # canonical chat slots (the legacy router/final_answer slots
                    # were removed — single-agent mode).
                    resolved_model = None
                    for slot in ("chat.planner", "chat.cognition"):
                        r = reg.resolve_slot(slot)
                        if r and r.model_id:
                            resolved_model = r.model_id
                            break
                    if resolved_model:
                        for m in reg.list_models(capability="chat"):
                            if m.model_id == resolved_model and m.context_window:
                                window = int(m.context_window)
                                break
                    if window and svc.thread_usage(thread_id, context_window=window).get("near_limit"):
                        from services.compaction_service import CompactionService
                        await CompactionService(db_u).compact(thread_id, openai)
                except Exception as exc:  # noqa: BLE001 — compaction must never break a turn
                    log.warningx("Auto-compaction mislukt", thread_id=thread_id, error=str(exc))
            finally:
                db_u.close()
    except Exception as exc:  # noqa: BLE001 — usage accounting must never break a turn
        log.warningx("Token usage opslaan mislukt", thread_id=thread_id, error=str(exc))

    out["thread_id"] = thread_id
    return out


async def ensure_thread_and_store_user_message(
    *,
    thread_id: str,
    question: str,
    payload: Dict[str, Any],
    turn_id: Optional[int] = None,
) -> None:
    display_question = payload.get("_display_question") or question
    attachment_names = [
        item.get("name") for item in (payload.get("attachments") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    if attachment_names:
        display_question = f"{display_question}\n\nAttachments: {', '.join(attachment_names)}"
    project_id = (
        payload.get("project_id")
        or payload.get("_project_id")
        or payload.get("assistant_project_id")
    )
    thread_title = (
        payload.get("thread_title")
        or payload.get("_thread_title")
        or (display_question or "")[:120]
    )
    service = AssistantThreadService()
    await service.ensure_thread(
        thread_id=thread_id,
        project_id=project_id,
        title=thread_title,
        metadata_={
            "source": "ask_job_callbacks",
            "model": payload.get("model"),
        },
    )
    await service.add_user_message(
        thread_id=thread_id,
        content=display_question or "",
        turn_id=turn_id,
    )


def steps_from_trace(trace: Optional[list]) -> list:
    """Derive the persistent narration step thread (the agent's running commentary)
    from a turn's trace events — narration + tool calls/results, in order."""
    steps: list = []
    for ev in (trace or []):
        if not isinstance(ev, dict):
            continue
        t = ev.get("type")
        if t == "agent_narration":
            txt = (ev.get("say") or ev.get("summary") or "").strip()
            if txt:
                steps.append({"kind": "say", "text": txt})
        elif t == "tool_call":
            steps.append({"kind": "tool", "text": (ev.get("summary") or f"Using {ev.get('tool') or 'a tool'}").strip()})
        elif t == "tool_result":
            steps.append({"kind": "result", "text": (ev.get("summary") or f"{ev.get('tool') or 'Tool'} finished").strip()})
    return steps


async def store_assistant_output_message(
    *,
    thread_id: str,
    answer: str,
    turn_id: Optional[int] = None,
    steps: Optional[list] = None,
) -> None:
    if not (answer or "").strip():
        return
    service = AssistantThreadService()
    await service.add_assistant_message(
        thread_id=thread_id,
        content=answer,
        turn_id=turn_id,
        steps=steps or None,
    )


def ask_job_callbacks() -> Dict[str, Any]:
    return {
        "run_ask_cb": run_ask_orchestrator,
        "store_user_message_cb": ensure_thread_and_store_user_message,
        "store_assistant_message_cb": store_assistant_output_message,
        "timeout_response_cb": timeout_ask_response,
        "error_response_cb": error_ask_response,
    }
