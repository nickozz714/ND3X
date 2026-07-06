"""DB-backed application configuration.

Per the product direction, *all* settings live in the database except the few
that are physically required before the DB is usable (the connection string, the
base directory, and the encryption/JWT secrets — those stay in
``<base>/.nd3x/{bootstrap,secrets}.json``).

Mechanism (no call-site changes): the app keeps reading the in-memory
``settings`` snapshot (``settings.X``) everywhere. At startup we **hydrate** that
snapshot from the DB, overwriting each registered attribute. Precedence:

    code default  →  DB value  →  environment override (env always wins)

Defaults are taken from the already-built ``settings`` object, so they are never
duplicated here — the registry only declares *which* keys are DB-backed, their
type, and how to group/label them for the settings UI.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from component.config import settings
from component.logging import get_logger
from repository.application_setting_repository import ApplicationSettingRepository
from schemas.application_settings import ApplicationSettingCreate

log = get_logger(__name__)


@dataclass(frozen=True)
class Spec:
    key: str
    type: str  # "bool" | "int" | "float" | "str" | "csv" | "path"
    group: str
    label: str
    help: str = ""
    advanced: bool = False
    secret: bool = False
    # path settings are stored relative to BASE_DIR and resolved under it.
    base_relative: bool = False
    # When set, the setting is a fixed-choice enum (rendered as a dropdown).
    options: tuple[str, ...] = ()


# Every DB-backed setting. Keys match the `settings.X` attribute names so hydrate
# can setattr them directly. Defaults come from `settings`, not from here.
SPEC: tuple[Spec, ...] = (
    # ── Limits / safety ──────────────────────────────────────────────────────
    Spec("MAX_TOOL_STEPS", "int", "limits", "Max tool steps",
         help="How many tool→observe cycles the agent may run before it must answer. Higher = can do more per turn but slower/costlier."),
    Spec("EVALUATION_HOPS", "int", "limits", "Max evaluation hops",
         help="Max plan→tool→evaluate→tool re-evaluation rounds within a turn. Caps deep multi-step reasoning."),
    Spec("MAX_SEARCH_K", "int", "limits", "Default search results",
         help="How many results a search/retrieval tool returns by default (the fan-out)."),
    Spec("MAX_FILE_CHARS", "int", "limits", "Max characters read from a file",
         help="Upper bound on text pulled from one file into context, to avoid blowing the token budget."),
    Spec("MAX_PARALLEL_TOOL_CALLS", "int", "limits", "Max parallel tool calls",
         help="How many independent tool calls run at once in a single turn. 1 = run them one by one."),
    Spec("SUBAGENT_MAX_DEPTH", "int", "limits", "Max subagent nesting depth", advanced=True,
         help="How deep subagents may dispatch further subagents. Prevents runaway recursion."),
    Spec("SUBAGENT_SUMMARY_MAX_CHARS", "int", "limits", "Max subagent summary chars", advanced=True,
         help="Hard cap on how much of a subagent's answer is folded back into the parent's context."),
    Spec("SUBAGENT_DEFAULT_ASSISTANT", "str", "limits", "Default subagent assistant", advanced=True,
         help="Assistant name forced for ad-hoc (unnamed) subagent dispatch. Blank = let the router choose."),
    Spec("BACKGROUND_TASK_MAX_ACTIVE", "int", "limits", "Max active background tasks", advanced=True,
         help="How many background tasks (task__create) may run at the same time."),
    Spec("TOOL_RESULT_MAX_INLINE_CHARS", "int", "limits", "Tool result inline chars", advanced=True,
         help="Above this, a tool's text result is stored as an artifact and referenced instead of pasted inline."),
    Spec("TOOL_RESULT_MAX_PREVIEW_CHARS", "int", "limits", "Tool result preview chars", advanced=True,
         help="How much of a large tool result is shown as a preview."),
    Spec("TOOL_RESULT_MAX_INLINE_BYTES", "int", "limits", "Tool result inline bytes", advanced=True,
         help="Binary results larger than this are stored as artifacts, never inlined."),
    Spec("TOOL_ARTIFACTS_ENABLED", "bool", "limits", "Store large tool results as artifacts",
         help="On: oversized tool outputs are saved to disk and referenced. Off: everything goes inline (can bloat context)."),
    # ── Agent loop budgets ───────────────────────────────────────────────────
    Spec("SINGLE_AGENT_MODE", "bool", "agent", "Single-agent mode", advanced=True,
         help="On (default): one agent loop selects skills, plans, uses tools and answers. Off enables the legacy multi-assistant router."),
    # NB: the CHAT agent budgets live in AI Models → "Agent Budgets" (the
    # llm_runtime chat_agent_max_* keys, which override the code defaults per turn).
    # They are intentionally NOT duplicated here. Workflow budgets have no panel,
    # so they stay below.
    Spec("WORKFLOW_AGENT_MAX_ITERATIONS_PER_OPERATION", "int", "agent", "Workflow: max iterations per operation",
         help="Reasoning-loop steps an assistant operation in a workflow may take before it must finish."),
    Spec("WORKFLOW_AGENT_MAX_TOOL_CALLS_PER_OPERATION", "int", "agent", "Workflow: max tool calls per operation",
         help="Total tool calls one workflow operation may make."),
    Spec("WORKFLOW_AGENT_MAX_SAME_ERROR_REPEATS", "int", "agent", "Workflow: max same-error repeats",
         help="Abort an operation after the same error repeats this many times (stops loops)."),
    Spec("WORKFLOW_AGENT_MAX_WALL_CLOCK_SECONDS", "int", "agent", "Workflow: max wall-clock (s)",
         help="Time budget for one workflow operation. 0 = no time limit."),
    Spec("LOCAL_MODEL_LIGHT_MODE", "bool", "agent", "Light mode for local models",
         help="When a small/local model's reply can't be parsed as JSON, use its raw text as the answer instead of failing the turn."),
    Spec("AGENT_VERIFICATION_ENABLED", "bool", "agent", "Self-check before final answer",
         help="On: the agent does a quick self-review of its answer before sending it (catches mistakes, costs an extra step)."),
    Spec("AGENT_MAX_VERIFICATION_HOPS", "int", "agent", "Max verification hops", advanced=True,
         help="How many re-attempts a failed self-check may trigger."),
    # ── Runtime / scheduler ──────────────────────────────────────────────────
    Spec("RUNTIME_TIMEOUT", "int", "runtime", "Runtime timeout (s)", advanced=True,
         help="Overall time budget for a background ask/job run."),
    Spec("RUNTIME_WORKERS", "int", "runtime", "Runtime workers", advanced=True,
         help="Uvicorn worker processes (applied at next launch)."),
    Spec("LOCAL_MODEL_TIMEOUT", "int", "runtime", "Local model request timeout (s)",
         help="How long to wait on a local/compatible model endpoint before giving up. Raise it for slow local models."),
    Spec("RUNTIME_TIMEOUT_LOCAL", "int", "runtime", "Runtime timeout for local models (s)",
         help="Overall time budget for an ask/job run when the chat model is a local model. "
              "Local planner steps are slow, so multi-step tool turns need more time than the cloud default."),
    Spec("OLLAMA_NUM_CTX", "int", "runtime", "Ollama context window (tokens)",
         help="Context window requested per Ollama chat call (the model's own context window is used when smaller). "
              "Too small and the prompt gets truncated — the model then can't see its instructions or tools. "
              "Bigger windows cost more RAM while a model is loaded."),
    Spec("MODEL_SLOW_STEP_WARN_S", "int", "runtime", "Slow model step threshold (s)", advanced=True,
         help="Planner steps slower than this are flagged as slow in the audit and counted in the "
              "per-model metrics (AI Models → model metrics)."),
    Spec("MODEL_EVAL_INTERVAL_HOURS", "int", "runtime", "Periodic model eval (hours, 0=off)", advanced=True,
         help="Run the full-vs-light model evaluation on the local chat-slot models every N hours. "
              "0 disables it (an eval run keeps a local model busy for many minutes). "
              "Applied at next launch."),
    Spec("RUNTIME_JOB_CLEANUP_INTERVAL_SECONDS", "int", "runtime", "Job cleanup interval (s)", advanced=True,
         help="How often old ask/voice job files on disk are swept."),
    Spec("ASK_JOB_RUN_RETENTION_HOURS", "int", "runtime", "Ask job run retention (h)", advanced=True,
         help="How long finished ask-run files are kept before cleanup."),
    Spec("ASK_JOB_ACTIVE_RETENTION_HOURS", "int", "runtime", "Ask job active retention (h)", advanced=True,
         help="How long an active ask job may linger before it's considered stale."),
    Spec("VOICE_JOB_RETENTION_HOURS", "int", "runtime", "Voice job retention (h)", advanced=True,
         help="How long finished voice-job files are kept."),
    Spec("VOICE_JOB_ACTIVE_RETENTION_HOURS", "int", "runtime", "Voice job active retention (h)", advanced=True,
         help="How long an active voice job may linger before it's considered stale."),
    Spec("WORKFLOW_SCHEDULER_INTERVAL_SECONDS", "int", "runtime", "Workflow scheduler interval (s)", advanced=True,
         help="How often scheduled workflows are checked for due runs."),
    Spec("SYSTEM_CURIOSITY_WORKER_INTERVAL_SECONDS", "int", "runtime", "Curiosity worker interval (s)", advanced=True,
         help="How often the background curiosity worker wakes to process queued cognition jobs."),
    Spec("SYSTEM_CURIOSITY_WORKER_BATCH_SIZE", "int", "runtime", "Curiosity worker batch size", advanced=True,
         help="How many curiosity jobs the worker processes per tick."),
    Spec("SYSTEM_COGNITION_MAX_HOPS", "int", "runtime", "System cognition max hops", advanced=True,
         help="Reasoning steps the background cognition pipeline may take per job."),
    Spec("SYSTEM_COGNITION_MAX_JOBS_PER_TURN", "int", "runtime", "System cognition jobs per turn", advanced=True,
         help="How many cognition jobs a single chat turn may spawn (memory/belief/curiosity)."),
    # ── File roots (sub-paths under the base dir) ────────────────────────────
    Spec("FILES_DIR", "path", "paths", "Files directory", base_relative=True,
         help="Where skill files, generated docs and the attachment vector store live (relative to the base directory)."),
    Spec("ASK_JOB_ROOT", "path", "paths", "Ask job root", base_relative=True,
         help="Where chat/ask run state + attachments are written (relative to the base directory)."),
    Spec("VOICE_JOB_ROOT", "path", "paths", "Voice job root", base_relative=True,
         help="Where voice transcription/diarization job state is written (relative to the base directory)."),
    Spec("LOG_FILE", "path", "paths", "Log file", base_relative=True,
         help="Application log file, relative to the base directory — include the file name, e.g. logs/app.log."),
    # ── Logging ──────────────────────────────────────────────────────────────
    Spec("LOG_LEVEL", "str", "logging", "Log level", options=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
         help="Minimum severity written to the log. DEBUG is very verbose; INFO is the normal default."),
    Spec("LOG_FORMAT", "str", "logging", "Log format", options=("text", "json"),
         help="text = human-readable lines; json = structured logs for log aggregators."),
    Spec("LOG_ROTATE_MB", "int", "logging", "Rotate at (MB)", advanced=True,
         help="Start a new log file once the current one passes this size."),
    Spec("LOG_BACKUP_COUNT", "int", "logging", "Rotated backups kept", advanced=True,
         help="How many old rotated log files to keep."),
    Spec("LOG_SAMPLING_RATE", "float", "logging", "Sampling rate", advanced=True,
         help="Fraction of log records actually written (1.0 = all, 0.1 = 10%). Lowers log volume under load."),
    Spec("LOG_THROTTLE_SECS", "float", "logging", "Throttle (s)", advanced=True,
         help="Minimum seconds between identical throttled log lines. 0 = no throttling."),
    Spec("LOG_DB_ENABLED", "bool", "logging", "Also log to the database", advanced=True,
         help="On: write log records to the DB too (queryable in the UI). Adds DB load."),
    Spec("LOG_DB_LEVEL", "str", "logging", "DB log level", advanced=True, options=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
         help="Minimum severity stored in the DB log (usually higher than the file log)."),
    Spec("LOG_DB_SAMPLE_RATE", "float", "logging", "DB log sample rate", advanced=True,
         help="Fraction of eligible records written to the DB log."),
    Spec("LOG_DB_MAX_FIELD_CHARS", "int", "logging", "DB log max field chars", advanced=True,
         help="Truncate long fields to this length before storing in the DB log."),
    Spec("LOG_DB_EXCLUDE_LOGGERS", "csv", "logging", "DB log excluded loggers", advanced=True,
         help="Comma-separated logger names to never write to the DB log."),
    # (MCP_URL / MCP_BEARER are intentionally NOT here — configure MCP via env.)
    # ── Ollama / local models ────────────────────────────────────────────────
    Spec("OLLAMA_LIBRARY_URL", "str", "ollama", "Ollama library URL", advanced=True,
         help="Page scanned to discover pullable Ollama models. Blank = use only the curated catalog + installed models."),
    Spec("OLLAMA_LIBRARY_TTL", "int", "ollama", "Ollama library cache TTL (s)", advanced=True,
         help="How long the discovered Ollama model list is cached."),
    # ── Model catalog (cloud model metadata) ─────────────────────────────────
    Spec("MODEL_CATALOG_URL", "str", "models", "Model catalog URL",
         help="Public, no-auth catalog used to enrich cloud models with display names, context windows and indicative prices."),
    Spec("MODEL_CATALOG_TTL", "int", "models", "Model catalog cache TTL (s)", advanced=True,
         help="How long the fetched model catalog is cached before refetching."),
    # ── Memory / cognition ───────────────────────────────────────────────────
    Spec("ROUTER_MEMORY_INJECTION_ENABLED", "bool", "memory", "Inject memories at routing",
         help="On: relevant saved memories are added to context when deciding how to handle a message."),
    Spec("PLANNER_MEMORY_INJECTION_ENABLED", "bool", "memory", "Inject memories at planning",
         help="On: relevant saved memories are added to the agent's planning context."),
    Spec("SYSTEM_COGNITION_EMBEDDING_BATCH_SIZE", "int", "memory", "Cognition embedding batch", advanced=True,
         help="How many memory/belief texts are embedded per batch in the background."),
    # ── Cognition / curiosity research ───────────────────────────────────────
    Spec("RESEARCH_PROVIDER", "str", "cognition", "Research provider",
         options=("auto", "duckduckgo", "exa", "none"),
         help="Web-research backend for the curiosity/cognition system. auto = keyless DuckDuckGo unless an Exa key is set · duckduckgo = always keyless · exa = needs an Exa API key (richer) · none = disable research."),
    Spec("EXA_API_KEY", "str", "cognition", "Exa API key (optional)", secret=True,
         help="Only needed when Research provider is 'exa' (or 'auto' and you want Exa). Get one at exa.ai."),
    Spec("EXA_TIMEOUT_S", "float", "cognition", "Research timeout (s)", advanced=True,
         help="How long to wait on a web-research request before giving up."),
    # ── Auth / server ────────────────────────────────────────────────────────
    Spec("ACCESS_TOKEN_MIN", "int", "auth", "Access-token lifetime (min)",
         help="How long a login session stays valid before the token must refresh."),
    Spec("HOST", "str", "server", "Bind host", advanced=True,
         help="Network interface the server binds to. Applied at next launch; a launcher passing --host overrides this."),
    Spec("PORT", "int", "server", "Bind port", advanced=True,
         help="Port the server listens on. Applied at next launch; a launcher passing --port overrides this."),
    Spec("DEBUG", "bool", "server", "Debug mode",
         help="Extra diagnostics/verbosity. Turn off in production."),
)

_BY_KEY = {s.key: s for s in SPEC}

# Keys that used to be seeded but are now owned elsewhere (or retired). Pruned on
# boot so the settings UI doesn't show stale duplicates.
OBSOLETE_KEYS = (
    "CHAT_AGENT_MAX_ITERATIONS_PER_STEP",
    "CHAT_AGENT_MAX_TOOL_CALLS_PER_STEP",
    "CHAT_AGENT_MAX_SAME_ERROR_REPEATS",
    "CHAT_AGENT_MAX_WALL_CLOCK_SECONDS",
)


def managed_keys() -> list[str]:
    """All keys owned by a dedicated editor (this registry + the AI Models
    llm_runtime panels), so the generic key/value list can hide them."""
    keys = [s.key for s in SPEC]
    try:
        from services import llm_runtime_settings as lrs
        keys += list(lrs.DEFAULTS.keys()) + list(lrs.NUMERIC_DEFAULTS.keys())
    except Exception:  # noqa: BLE001
        pass
    return sorted(set(keys))


def prune_obsolete(db: Session) -> int:
    """Delete stale/duplicate setting rows. Returns the count removed."""
    from models.application_settings import ApplicationSetting
    removed = (
        db.query(ApplicationSetting)
        .filter(ApplicationSetting.key.in_(OBSOLETE_KEYS))
        .delete(synchronize_session=False)
    )
    if removed:
        db.commit()
    return removed


# ── base-relative path helpers ───────────────────────────────────────────────
def _base() -> str:
    return getattr(settings, "BASE_DIR", "") or ""


def rel_to_base(value: Any) -> str:
    """Make a path relative to BASE_DIR (a sub-path). Absolute paths already under
    base are relativized; values outside base or with no base set are returned as
    a cleaned relative string."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    base = _base()
    p = Path(raw)
    if base:
        base_p = Path(base)
        try:
            if p.is_absolute():
                return str(p.relative_to(base_p))
        except ValueError:
            pass  # absolute but outside base — fall through
    while raw.startswith("./"):
        raw = raw[2:]
    return raw.lstrip("/")


def abs_under_base(value: Any) -> str:
    """Resolve a base-relative path to an absolute path under BASE_DIR."""
    raw = str(value or "").strip()
    base = _base()
    if not raw:
        return base
    p = Path(raw)
    if p.is_absolute() or not base:
        return raw
    return str(Path(base) / raw)


# ── (de)serialization ────────────────────────────────────────────────────────
def _to_str(spec: Spec, value: Any) -> str:
    if spec.type == "bool":
        return "True" if bool(value) else "False"
    if spec.type == "csv":
        if isinstance(value, (list, tuple)):
            return ",".join(str(v) for v in value)
        return str(value or "")
    if spec.type == "path":
        return rel_to_base(value)
    return "" if value is None else str(value)


def _parse(spec: Spec, raw: str) -> Any:
    if spec.type == "bool":
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if spec.type == "int":
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return getattr(settings, spec.key)
    if spec.type == "float":
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            return getattr(settings, spec.key)
    if spec.type == "csv":
        return [item.strip() for item in str(raw).split(",") if item.strip()]
    return raw


def _default_str(spec: Spec) -> str:
    return _to_str(spec, getattr(settings, spec.key, None))


# ── public API ───────────────────────────────────────────────────────────────
def seed_all(db: Session) -> None:
    """Create any missing rows using the current `settings` value as the default,
    so the whole config shows up in the settings UI. Idempotent."""
    repo = ApplicationSettingRepository(db)
    for spec in SPEC:
        if repo.get_by_key(spec.key) is None:
            repo.create(ApplicationSettingCreate(key=spec.key, value=_default_str(spec)))
    prune_obsolete(db)


def hydrate(db: Session) -> int:
    """Overwrite the in-memory `settings` snapshot from the DB. An environment
    variable of the same name always wins (ops escape hatch). Returns the count
    of attributes set from the DB."""
    repo = ApplicationSettingRepository(db)
    applied = 0
    for spec in SPEC:
        env = os.getenv(spec.key)
        if env is not None and env.strip() != "":
            continue  # env override wins; leave the import-time value in place
        row = repo.get_by_key(spec.key)
        if row is None:
            continue
        try:
            parsed = _parse(spec, row.value)
            if spec.type == "path":
                parsed = abs_under_base(parsed)  # store absolute under base in the live config
            setattr(settings, spec.key, parsed)
            applied += 1
        except Exception as exc:  # noqa: BLE001 — one bad row must not break boot
            log.warningx("settings hydrate skipped", key=spec.key, error=str(exc))
    return applied


def groups(db: Session) -> list[dict[str, Any]]:
    """Grouped specs + current values for the settings UI (secrets masked)."""
    repo = ApplicationSettingRepository(db)
    by_group: dict[str, list[dict[str, Any]]] = {}
    for spec in SPEC:
        row = repo.get_by_key(spec.key)
        value = row.value if row is not None else _default_str(spec)
        by_group.setdefault(spec.group, []).append({
            "key": spec.key,
            "type": spec.type,
            "label": spec.label,
            "help": spec.help,
            "advanced": spec.advanced,
            "secret": spec.secret,
            "options": list(spec.options),
            "value": "" if spec.secret else value,
            "has_value": bool(value) if spec.secret else None,
        })
    return [{"group": g, "settings": items} for g, items in by_group.items()]


def apply_updates(db: Session, updates: dict[str, str]) -> int:
    """Upsert the given key/value settings (registry keys only), then re-hydrate.
    Returns the number of keys written."""
    repo = ApplicationSettingRepository(db)
    written = 0
    for key, value in updates.items():
        spec = _BY_KEY.get(key)
        if spec is None:
            continue
        # A blank secret means "leave unchanged".
        if spec.secret and (value is None or str(value) == ""):
            continue
        normalized = _to_str(spec, _parse(spec, str(value)))
        existing = repo.get_by_key(key)
        if existing is None:
            repo.create(ApplicationSettingCreate(key=key, value=normalized))
        else:
            existing.value = normalized
            db.add(existing)
        written += 1
    db.commit()
    hydrate(db)
    return written
