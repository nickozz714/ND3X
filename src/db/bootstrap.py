from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from component.config import settings
from component.logging import get_logger

log = get_logger(__name__)

# The first admin user is created by the first-time setup wizard
# (routers/setup_router.py), not from environment variables — the old
# ND3X_BOOTSTRAP_* / BOOTSTRAP_ADMIN_EMAIL / BOOTSTRAP_EXPERT_EMAIL path was removed.


async def ensure_user_roles_column(db: Session) -> None:
    inspector = inspect(db.bind)
    cols = {c["name"] for c in inspector.get_columns("users")}
    if "roles" not in cols:
        db.execute(text("ALTER TABLE users ADD COLUMN roles JSON"))
        db.commit()


async def ensure_message_important_column(db: Session) -> None:
    inspector = inspect(db.bind)
    try:
        cols = {c["name"] for c in inspector.get_columns("assistant_thread_messages")}
    except Exception:
        return
    if "important" not in cols:
        db.execute(text(
            "ALTER TABLE assistant_thread_messages "
            "ADD COLUMN important BOOLEAN NOT NULL DEFAULT 0"
        ))
        db.commit()


async def ensure_builtin_tools_synced(db: Session) -> None:
    """Sync the always-on **Builtin** MCP tool set from the code registry on every
    boot. These tools (system__shell_exec, file ops, pdf, text store, …) are
    available to the agent on every turn WITHOUT selecting a skill. If the Builtin
    set is ever empty, the guard silently forces every tool through skills and
    builtin calls hard-fail ("not allowed to call tool_id=… with selected skills").
    Best-effort: a sync hiccup must never block boot."""
    try:
        from models.mcp_server import MCPServer
        from services.mcp.mcp_server_sync_service import MCPServerSyncService
        server = db.query(MCPServer).filter(MCPServer.name == "Builtin").first()
        if server is None or not getattr(server, "is_enabled", False):
            log.warningx("Builtin MCP server ontbreekt of is uitgeschakeld — geen always-on tools")
            return
        await MCPServerSyncService(db).sync_server_tools(server.id)
        log.infox("Builtin tools gesynchroniseerd bij boot", server_id=server.id)
    except Exception as exc:  # noqa: BLE001 — never block boot on tool sync
        log.warningx("Builtin tools sync bij boot mislukt", error=str(exc))


_ROUTE_BUILDING_INSTRUCTIONS = """Build file-transfer integrations ("routes") with the transfer_* tools.

Flow:
1. transfer_list_connectors — see protocols (file, sftp, s3, azure-storage-blob, azure-files, sharepoint, …) and the credential type each needs.
2. transfer_list_inventory — existing hosts/credentials/routes/parameters; reference them by id.
3. Create prerequisites if missing: transfer_create_host, then transfer_create_credential (credential_type must match the connector; provide only the relevant secret fields).
4. transfer_test_endpoint to verify connectivity BEFORE building.
5. transfer_create_route with endpoints: at least one FROM (source) and one TO (destination); each {direction, protocol, host_id, credential_id?, path, parameter?}. `parameter` is a JSON object string of connector options.
6. transfer_run_route to execute now, or tell the user to Activate the route for scheduled polling.

Rules: ask the user (ask_user) for missing host/path/credential details instead of guessing. Never put secrets in a path/URI — reference credentials by id. A directory source transfers every file in it."""


_WORKFLOW_BUILDING_INSTRUCTIONS = """Build and inspect ND3X workflows with the workflow__* tools. You are ALLOWED and EXPECTED to create workflows when the user asks for automation, scheduled work, or notifications — do not claim you can't.

Flow:
1. workflow__list — see what already exists before creating something new.
2. workflow__generate with a thorough plain-language description — it designs a linear draft and CREATES it DISABLED. Include in the description: the desired name, each step, any email recipients, and schedule wishes.
3. workflow__describe on the result — present the steps to the user for review.
4. The user reviews and ENABLES it in the Workflows builder (you cannot enable it; that is by design). Schedules/triggers are also configured there.
5. workflow__run to start an ENABLED workflow now.

Operations a workflow can contain (the generator emits the starred ones; the rest can be added in the builder):
- *assistant — an agent step: { question } (reasoning/writing/tool work)
- *tool — one direct tool call: { tool_name, arguments }
- *notification — notify the user: { channel: 'ui'|'email'|'trace', subject, message, severity, recipients?: [emails] }. Email REQUIRES mail settings (Instellingen → Mail) to be configured; without them advise 'ui'.
- *http_request — { method, url, headers }
- *set_variable / new_thread — state control between steps
- condition, for_each, sub_workflow, merge, artifact, fail — branching/looping/composition (builder-only)
Steps can reference earlier output via {{variables}} in their config.

Rules: prefer ONE workflow__generate call with a complete description over many small edits. Never promise an email notification when mail settings are not configured — say so and offer channel 'ui' instead. After generating, always show the draft (workflow__describe) and tell the user to review + enable it in the builder."""


async def ensure_workflow_building_skill(db: Session) -> None:
    """Seed the 'workflow_building' domain skill and link the workflow__* builtin
    tools to it, so the CLI agent knows it can BUILD workflows (generate/describe/
    list/run) and gets the how-to only when the skill is selected (the gateway
    skill-scopes linked tools). Idempotent + best-effort — never blocks boot."""
    try:
        from models.skill import Skill
        from models.skill_tool import SkillTool
        from models.tool import Tool
        from models.mcp_server import MCPServer

        skill = db.query(Skill).filter(Skill.name == "workflow_building").first()
        if skill is None:
            skill = Skill(
                name="workflow_building",
                display_name="Workflow building",
                description=("Create, inspect and run ND3X workflows (automations): "
                             "multi-step flows with agent steps, tool calls, ui/email "
                             "notifications, http requests and schedules. Use for any "
                             "'automate this', 'every day/week', 'notify/mail me when' request."),
                instructions=_WORKFLOW_BUILDING_INSTRUCTIONS,
                is_enabled=True, priority=100, source="builtin",
            )
            db.add(skill)
            db.flush()
        else:
            skill.instructions = _WORKFLOW_BUILDING_INSTRUCTIONS  # keep current per release

        server = db.query(MCPServer).filter(MCPServer.name == "Builtin").first()
        if server is not None:
            tools = db.query(Tool).filter(Tool.mcp_server_id == server.id,
                                          Tool.name.like("workflow\\_\\_%", escape="\\")).all()
            existing = {st.tool_id for st in db.query(SkillTool).filter(SkillTool.skill_id == skill.id).all()}
            for t in tools:
                if t.id not in existing:
                    db.add(SkillTool(skill_id=skill.id, tool_id=t.id, is_enabled=True))
        db.commit()
        log.infox("Workflow-building skill geseed", skill_id=skill.id)
    except Exception as exc:  # noqa: BLE001 — never block boot
        log.warningx("Workflow-building skill seed mislukt", error=str(exc))


async def ensure_route_building_skill(db: Session) -> None:
    """Seed the 'transfer_route_building' domain skill and link the transfer_* builtin
    tools to it. Idempotent + best-effort (must never block boot). Runs after the
    builtin sync so the transfer tools exist as DB tools."""
    try:
        from models.skill import Skill
        from models.skill_tool import SkillTool
        from models.tool import Tool
        from models.mcp_server import MCPServer

        skill = db.query(Skill).filter(Skill.name == "transfer_route_building").first()
        if skill is None:
            skill = Skill(
                name="transfer_route_building",
                display_name="Transfer route building",
                description="Build and run file-transfer integrations (routes) across connectors (file/sftp/s3/azure/sharepoint): create hosts/credentials, compose FROM→TO endpoints, test and run.",
                instructions=_ROUTE_BUILDING_INSTRUCTIONS,
                is_enabled=True, priority=100, source="builtin",
            )
            db.add(skill)
            db.flush()
        else:
            skill.instructions = _ROUTE_BUILDING_INSTRUCTIONS  # keep instructions current

        server = db.query(MCPServer).filter(MCPServer.name == "Builtin").first()
        if server is not None:
            tools = db.query(Tool).filter(Tool.mcp_server_id == server.id, Tool.name.like("transfer\\_%", escape="\\")).all()
            existing = {st.tool_id for st in db.query(SkillTool).filter(SkillTool.skill_id == skill.id).all()}
            for t in tools:
                if t.id not in existing:
                    db.add(SkillTool(skill_id=skill.id, tool_id=t.id, is_enabled=True))
        db.commit()
        log.infox("Route-building skill geseed", skill_id=skill.id)
    except Exception as exc:  # noqa: BLE001 — never block boot
        log.warningx("Route-building skill seed mislukt", error=str(exc))


async def ensure_message_steps_column(db: Session) -> None:
    """JSON column holding the agent's running commentary (narration + tool steps)
    for an assistant message, so the step thread survives a reload. Idempotent."""
    inspector = inspect(db.bind)
    try:
        cols = {c["name"] for c in inspector.get_columns("assistant_thread_messages")}
    except Exception:
        return
    if "steps" not in cols:
        db.execute(text("ALTER TABLE assistant_thread_messages ADD COLUMN steps JSON"))
        db.commit()


async def ensure_provider_admin_key_column(db: Session) -> None:
    """Encrypted per-provider Admin/usage key (for the provider's billing/usage
    API — e.g. OpenAI/Anthropic org usage & cost). Idempotent; mirrors the
    api_key_encrypted column. create_all() does not alter existing tables."""
    inspector = inspect(db.bind)
    try:
        cols = {c["name"] for c in inspector.get_columns("providers")}
    except Exception:
        return
    if "admin_api_key_encrypted" not in cols:
        db.execute(text("ALTER TABLE providers ADD COLUMN admin_api_key_encrypted TEXT"))
        db.commit()


async def ensure_provider_model_web_search_column(db: Session) -> None:
    """Per-model override for native web search support (None → curated default).
    Idempotent; create_all() does not alter existing tables."""
    inspector = inspect(db.bind)
    try:
        cols = {c["name"] for c in inspector.get_columns("provider_models")}
    except Exception:
        return
    if "supports_web_search" not in cols:
        db.execute(text("ALTER TABLE provider_models ADD COLUMN supports_web_search BOOLEAN"))
        db.commit()


async def ensure_provider_model_extra_guidance_column(db: Session) -> None:
    """Per-model flag: append the 'extra guidance' instruction block for
    less-capable models. Idempotent; create_all() does not alter existing tables."""
    inspector = inspect(db.bind)
    try:
        cols = {c["name"] for c in inspector.get_columns("provider_models")}
    except Exception:
        return
    if "needs_extra_guidance" not in cols:
        db.execute(text("ALTER TABLE provider_models ADD COLUMN needs_extra_guidance BOOLEAN"))
        db.commit()


async def ensure_legacy_routing_slots_removed(db: Session) -> None:
    """Drop capability-assignment rows for slots that no longer exist
    (chat.router / chat.final_answer — removed with the single-agent merge;
    the planner writes the answers). A stale row only misleads operators into
    configuring a model that is never used. chat.memory_decision stays: it is
    a real optional slot (assigned → that model decides memory retrieval;
    empty → the decision step is off). Idempotent."""
    try:
        db.execute(text(
            "DELETE FROM capability_assignments "
            "WHERE slot IN ('chat.router', 'chat.final_answer', 'meeting.profile_generator')"
        ))
        db.commit()
    except Exception:
        db.rollback()


async def ensure_provider_model_prompt_mode_column(db: Session) -> None:
    """Per-model planner prompt mode: 'full' | 'light' | NULL → auto (light when
    local). Idempotent; create_all() does not alter existing tables."""
    inspector = inspect(db.bind)
    try:
        cols = {c["name"] for c in inspector.get_columns("provider_models")}
    except Exception:
        return
    if "prompt_mode" not in cols:
        db.execute(text("ALTER TABLE provider_models ADD COLUMN prompt_mode VARCHAR(16)"))
        db.commit()


async def ensure_provider_model_num_parallel_column(db: Session) -> None:
    """Per-model concurrent-turn threshold for the local-model queue indicator
    (match OLLAMA_NUM_PARALLEL). Idempotent."""
    inspector = inspect(db.bind)
    try:
        cols = {c["name"] for c in inspector.get_columns("provider_models")}
    except Exception:
        return
    if "num_parallel" not in cols:
        db.execute(text("ALTER TABLE provider_models ADD COLUMN num_parallel INTEGER"))
        db.commit()


async def ensure_provider_model_vision_column(db: Session) -> None:
    """Per-model override for vision/image input (None → curated default).
    Idempotent; create_all() does not alter existing tables."""
    inspector = inspect(db.bind)
    try:
        cols = {c["name"] for c in inspector.get_columns("provider_models")}
    except Exception:
        return
    if "supports_vision" not in cols:
        db.execute(text("ALTER TABLE provider_models ADD COLUMN supports_vision BOOLEAN"))
        db.commit()


async def ensure_meeting_profile_action_policy_column(db: Session) -> None:
    """JSON column holding the meeting-driven-actions (#9) policy on a meeting
    profile (enabled/allowed_actions/allowed_tools/autonomy/triggers/budget).
    Idempotent; create_all() does not alter existing tables."""
    inspector = inspect(db.bind)
    try:
        cols = {c["name"] for c in inspector.get_columns("meeting_profiles")}
    except Exception:
        return
    if "action_policy" not in cols:
        db.execute(text("ALTER TABLE meeting_profiles ADD COLUMN action_policy JSON"))
        db.commit()


async def ensure_transfer_schedule_columns(db: Session) -> None:
    """Per-route cron schedule + last-run timestamp on transfer_records. Idempotent."""
    inspector = inspect(db.bind)
    try:
        cols = {c["name"] for c in inspector.get_columns("transfer_records")}
    except Exception:
        return
    if "schedule_cron" not in cols:
        db.execute(text("ALTER TABLE transfer_records ADD COLUMN schedule_cron VARCHAR(120)"))
    if "last_run_at" not in cols:
        db.execute(text("ALTER TABLE transfer_records ADD COLUMN last_run_at DATETIME"))
    db.commit()


async def ensure_system_cognition_embedding_columns(db: Session) -> None:
    """
    SQLAlchemy create_all() does not alter existing tables.
    This makes local/dev DB upgrades idempotent without needing Alembic yet.
    """
    inspector = inspect(db.bind)

    table_columns = {
        "system_memories": {
            "embedding": "JSON",
            "embedding_model": "VARCHAR(120)",
            "embedding_hash": "VARCHAR(64)",
            "embedding_updated_at": "VARCHAR(64)",
        },
        "system_beliefs": {
            "embedding": "JSON",
            "embedding_model": "VARCHAR(120)",
            "embedding_hash": "VARCHAR(64)",
            "embedding_updated_at": "VARCHAR(64)",
        },
    }

    for table_name, columns in table_columns.items():
        existing_columns = {column["name"] for column in inspector.get_columns(table_name)}

        for column_name, column_type in columns.items():
            if column_name in existing_columns:
                continue

            db.execute(
                text(
                    f"ALTER TABLE {table_name} "
                    f"ADD COLUMN {column_name} {column_type}"
                )
            )

    db.commit()


def _has_openai_key() -> bool:
    from services.providers.openai_key import registry_openai_api_key
    return bool((registry_openai_api_key() or "").strip())


async def bootstrap_system_cognition_embeddings(db: Session) -> None:
    """
    One-shot/idempotent embedding backfill.

    Enable manually with:
      BOOTSTRAP_SYSTEM_COGNITION_EMBEDDINGS=true

    Existing rows with embeddings are skipped.
    """
    if not bool(getattr(settings, "BOOTSTRAP_SYSTEM_COGNITION_EMBEDDINGS", False)):
        return

    if not _has_openai_key():
        raise RuntimeError(
            "BOOTSTRAP_SYSTEM_COGNITION_EMBEDDINGS=true but no OpenAI provider with "
            "an API key is registered."
        )

    from services.openai_service import OpenAIResponsesService
    from repository.system_cognition.memory_repository import MemoryRepository
    from repository.system_cognition.belief_repository import BeliefRepository
    from services.system_cognition.system_embedding_service import SystemEmbeddingService

    openai = OpenAIResponsesService()  # OpenAI key resolved lazily from the registry
    embedding_service = SystemEmbeddingService(openai_service=openai)
    memory_repo = MemoryRepository()
    belief_repo = BeliefRepository()

    batch_size = int(getattr(settings, "SYSTEM_COGNITION_EMBEDDING_BATCH_SIZE", 64))
    max_batches = int(getattr(settings, "BOOTSTRAP_SYSTEM_COGNITION_EMBEDDING_MAX_BATCHES", 100))

    for _ in range(max_batches):
        memories = await memory_repo.records_missing_embeddings(limit=batch_size)
        if not memories:
            break

        texts = [embedding_service.memory_text(memory) for memory in memories]
        embedded_items = embedding_service.embed_batch(texts)

        for memory, embedded in zip(memories, embedded_items):
            await memory_repo.update_embedding(
                memory_id=memory["id"],
                embedding=embedded["embedding"],
                embedding_model=embedded["embedding_model"],
                embedding_hash=embedded["embedding_hash"],
                embedding_updated_at=embedded["embedding_updated_at"],
            )

    for _ in range(max_batches):
        beliefs = await belief_repo.records_missing_embeddings(limit=batch_size)
        if not beliefs:
            break

        texts = [embedding_service.belief_text(belief) for belief in beliefs]
        embedded_items = embedding_service.embed_batch(texts)

        for belief, embedded in zip(beliefs, embedded_items):
            await belief_repo.update_embedding(
                belief_id=belief["id"],
                embedding=embedded["embedding"],
                embedding_model=embedded["embedding_model"],
                embedding_hash=embedded["embedding_hash"],
                embedding_updated_at=embedded["embedding_updated_at"],
            )


# ── Fresh-install bootstrap of defaults (idempotent, only-if-missing) ─────────
# A clean DB (a new deploy via the setup wizard) otherwise has no Builtin tool
# server, no system-skill contracts and no agent — so the app is unusable. These
# create the minimum needed WITHOUT reintroducing catalog seeding: each only adds
# what is missing and never edits or overwrites existing rows.

async def ensure_builtin_mcp_server(db: Session) -> None:
    """Ensure the always-on 'Builtin' MCP server row exists (server_type=builtin).
    ensure_builtin_tools_synced needs it to populate the builtin tool set; on a fresh
    DB it's absent, leaving the agent with zero always-on tools. Idempotent."""
    try:
        from datetime import datetime, timezone
        from models.mcp_server import MCPServer
        srv = db.query(MCPServer).filter(MCPServer.name == "Builtin").first()
        if srv is None:
            now = datetime.now(timezone.utc)
            srv = MCPServer(
                name="Builtin",
                slug="builtin",
                description="Always-on built-in tools (shell, files, pdf, text store, …).",
                server_type="builtin",
                is_enabled=True,
                created_at=now,
                updated_at=now,
            )
            db.add(srv)
            db.commit()
            log.infox("Builtin MCP server aangemaakt (fresh install)", server_id=srv.id)
        elif not srv.is_enabled:
            srv.is_enabled = True
            db.commit()
            log.infox("Builtin MCP server opnieuw ingeschakeld")
    except Exception as exc:  # noqa: BLE001 — never block boot
        log.warningx("Builtin MCP server ensure mislukt", error=str(exc))


async def ensure_system_skills(db: Session) -> None:
    """Create the code-authoritative system/runtime skill rows if missing. Their
    instructions come from code (system_specs/skills + skill_override), so the rows
    only carry name/flags; the loader overrides the content. Without them
    get_system_skills() is empty and the orchestrator contracts never load on a fresh
    DB. Idempotent: only adds missing names, never edits an existing row."""
    try:
        from models.skill import Skill
        from services.assistants.runtime.system_skills import SYSTEM_SKILL_NAMES, _DESCRIPTIONS
        existing = {row[0] for row in db.query(Skill.name).all()}
        created = 0
        for name in sorted(SYSTEM_SKILL_NAMES):
            if name in existing:
                continue
            is_runtime = name.startswith("runtime_")
            db.add(Skill(
                name=name,
                display_name=name.replace("orchestrator_", "").replace("_", " ").strip().capitalize(),
                description=_DESCRIPTIONS.get(name, ""),
                instructions="",  # code-authoritative; the loader overrides this
                is_system=not is_runtime,
                is_runtime=is_runtime,
                is_enabled=True,
                priority=10,
                source="builtin",
            ))
            created += 1
        if created:
            db.commit()
            log.infox("System/runtime skills aangemaakt (fresh install)", count=created)
    except Exception as exc:  # noqa: BLE001 — never block boot
        log.warningx("System skills ensure mislukt", error=str(exc))


_DEFAULT_AGENT_INSTRUCTION = (
    "You are ND3X, the assistant for this workspace. Fulfil the user's request end to "
    "end using the skills and tools you are given. Be accurate and concise; never claim "
    "a create/update/delete/save/export succeeded unless the tool call actually did."
)


async def ensure_default_assistant(db: Session) -> None:
    """Create a default planner agent on a fresh install so the workspace is usable out
    of the box. Idempotent: only when there is NO assistant at all — never clobbers a
    user's own agent(s)."""
    try:
        from datetime import datetime, timezone
        from models.assistant import Assistant
        from services.assistants.runtime.system_assistants import schema_for_type
        if db.query(Assistant).first() is not None:
            return
        now = datetime.now(timezone.utc)
        agent = Assistant(
            name="ND3X",
            description="Your ND3X assistant.",
            instruction=_DEFAULT_AGENT_INSTRUCTION,
            schema=schema_for_type("planner") or {},
            assistant_type="planner",
            routing_tags=[],
            model=None,
            priority=100,
            is_router_selectable=True,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(agent)
        db.commit()
        log.infox("Default agent aangemaakt (fresh install)", assistant_id=agent.id)
    except Exception as exc:  # noqa: BLE001 — never block boot
        log.warningx("Default agent ensure mislukt", error=str(exc))


async def run_bootstrap(db: Session) -> None:
    # Generic add-only schema reconciliation: bring an older database up to the
    # current models (missing columns on existing tables) before anything reads
    # them. create_all() already handled brand-new tables. The hand-written
    # ensure_*_column helpers below are now redundant for add-only changes but
    # kept (idempotent) for clarity/history.
    from db.schema_reconciler import reconcile_schema
    reconcile_schema(db)
    await ensure_user_roles_column(db)
    await ensure_message_important_column(db)
    await ensure_message_steps_column(db)
    await ensure_provider_admin_key_column(db)
    await ensure_provider_model_web_search_column(db)
    await ensure_provider_model_extra_guidance_column(db)
    await ensure_provider_model_prompt_mode_column(db)
    await ensure_provider_model_vision_column(db)
    await ensure_provider_model_num_parallel_column(db)
    await ensure_legacy_routing_slots_removed(db)
    await ensure_meeting_profile_action_policy_column(db)
    await ensure_transfer_schedule_columns(db)
    await ensure_system_cognition_embedding_columns(db)
    await bootstrap_system_cognition_embeddings(db)
    # LLM runtime behaviour toggles (prompt caching, OpenAI server-side session) so they
    # exist with the intended defaults and show up in the AI Models UI.
    from services.llm_runtime_settings import ensure_seeded as ensure_llm_runtime_settings
    ensure_llm_runtime_settings(db)
    # Seed the full DB-backed configuration registry so every setting shows up in
    # the settings UI with its current default.
    from services.app_settings_registry import seed_all as seed_app_settings
    seed_app_settings(db)
    # Fresh-install: make sure the Builtin MCP server exists so its tools can sync,
    # then keep the always-on builtin tool set populated (shell, files, pdf, …).
    await ensure_builtin_mcp_server(db)
    await ensure_builtin_tools_synced(db)
    # Seed the route-building skill (links the transfer_* tools) after the sync.
    await ensure_route_building_skill(db)
    # Seed the workflow-building skill (links the workflow__* tools) likewise.
    await ensure_workflow_building_skill(db)
    # Fresh-install defaults: the system-skill contracts + a default agent, so a clean
    # DB is usable out of the box. Only-if-missing — never clobbers a curated DB.
    await ensure_system_skills(db)
    await ensure_default_assistant(db)
    # Register any runtime-defined (tier-2) transfer connector types into the live registry.
    try:
        from services.transfer.transfer_service import TransferService
        n = TransferService(db).load_connector_defs()
        if n:
            log.infox("Custom transfer connectors geladen", count=n)
    except Exception as exc:  # noqa: BLE001 — never block boot
        log.warningx("Custom transfer connectors laden mislukt", error=str(exc))

    # Surface workflow operations whose pinned model override no longer resolves
    # to a registered chat model (e.g. after a model-id rename). Report-only.
    try:
        from services.workflows.workflow_model_audit import log_stale_model_overrides
        log_stale_model_overrides(db)
    except Exception as exc:  # noqa: BLE001 — never block boot
        log.warningx("workflow_model_audit_failed", error=str(exc))

    # Fail workflow runs orphaned by a previous process (restart mid-run). Runs
    # before the WorkflowWorker starts, so nothing live is touched.
    try:
        from services.workflows.workflow_run_recovery import recover_orphaned_runs
        recover_orphaned_runs(db)
    except Exception as exc:  # noqa: BLE001 — never block boot
        log.warningx("workflow_run_recovery_failed", error=str(exc))

    # Reload the persisted background-task list into the in-memory registry;
    # tasks still marked "running" were interrupted by this restart and become
    # error (unacknowledged, so the owner thread is notified next turn).
    try:
        from services.builtin.tools.background_tasks import restore_persisted_tasks
        restore_persisted_tasks(db)
    except Exception as exc:  # noqa: BLE001 — never block boot
        log.warningx("background_task_restore_failed", error=str(exc))
