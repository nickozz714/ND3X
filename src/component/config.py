from __future__ import annotations

import os
import urllib.parse
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel

from component import runtime_paths

load_dotenv()

# First-time-setup overlay: when ND3X_HOME/bootstrap.json exists the app is
# "configured" and DB / filesystem roots / secrets come from there (chosen in the
# setup wizard). Env vars still override every value (Docker/headless). Without a
# bootstrap file AND without an explicit DB in the environment, the app starts
# unconfigured and serves only the setup flow (no DB engine, no schedulers).
_bootstrap = runtime_paths.load_bootstrap()
_secrets_store = runtime_paths.load_secrets()
_db_cfg = (_bootstrap or {}).get("database", {}) or {}
_roots = runtime_paths.resolve_roots(_bootstrap["base_dir"]) if _bootstrap else {}


def _env_or(name: str, fallback: str) -> str:
    """Env value if set and non-empty, else the bootstrap/derived fallback."""
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return fallback
    return val


def _explicit_env_db() -> bool:
    """True when an operator has pointed us at a DB via env (the headless/Docker
    path), so we should run the full stack instead of the setup wizard."""
    if any((os.getenv(k) or "").strip() for k in
           ("MYSQL_URL", "SQLITE_URL", "SQLITE_PATH", "DB_HOST", "DB_NAME", "DB_DIALECT")):
        return True
    return (os.getenv("ND3X_CONFIGURED") or "").strip().lower() in ("1", "true", "yes", "on")


# "Configured" = storage is known (bootstrap file or explicit env DB). Gates the
# server lifespan. Whether an *admin user* exists is a separate runtime check
# (the setup router queries the DB), so the wizard can still own admin creation.
_CONFIGURED = (_bootstrap is not None) or _explicit_env_db()


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    # An empty value (e.g. `EVALUATION_HOPS=` in a .env) means "unset" — fall
    # back to the default instead of crashing on int("").
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return int(val)


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return float(val)


def _env_optional_int(name: str, default: Optional[int] = None) -> Optional[int]:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return int(val)


def _env_csv(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _required(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


class Settings(BaseModel):
    # ---- First-time setup ----
    # True when ND3X_HOME/bootstrap.json exists. When False the app boots in
    # setup-only mode (no DB engine, no schedulers) until the wizard completes.
    CONFIGURED: bool = False
    # The chosen BASE directory holding db/, logs/, files/, ask/, voice/.
    BASE_DIR: str = ""

    # ---- AI providers ----
    # OPEN_AI_API_KEY / LLM_MODEL / EMBEDDING_MODEL removed — the OpenAI key comes
    # from the registry's OpenAI provider and models come from the routing slots.
    EVALUATION_HOPS: int = 5
    # Per-request timeout (seconds) for local/compatible model endpoints.
    LOCAL_MODEL_TIMEOUT: int = 180
    # Context window (tokens) requested per Ollama chat call: the model's
    # configured context_window clamped to this cap. Without it Ollama runs at
    # its 4096 default and silently truncates longer prompts.
    OLLAMA_NUM_CTX: int = 16384
    # Planner steps slower than this (seconds) are WARN-flagged in the audit and
    # counted as slow calls in the per-model metrics rollup.
    MODEL_SLOW_STEP_WARN_S: int = 90
    # Periodic full-vs-light model evaluation interval (hours). 0 = off (default):
    # an eval run keeps a local model busy for many minutes.
    MODEL_EVAL_INTERVAL_HOURS: int = 0
    # Goal mode (/goal): multiplier applied to the chat agent-loop budgets
    # (iterations/tool-calls/wall-clock) for goal turns. Raises, never removes.
    GOAL_MODE_BUDGET_MULTIPLIER: float = 3.0
    # Live local-model discovery. URL of the Ollama library to scan for pullable
    # models (set to "" to disable and use only the curated catalog + installed).
    OLLAMA_LIBRARY_URL: str = "https://ollama.com/library"
    OLLAMA_LIBRARY_TTL: int = 3600

    # ---- MCP ----
    MCP_URL: str = ""
    MCP_BEARER: str = ""

    # ---- Server ----
    HOST: str = "0.0.0.0"
    PORT: int = 8088
    DEBUG: bool = True

    # ---- Runtime ----
    RUNTIME_TIMEOUT: int = 300
    # Overall run budget when the turn's chat model is a LOCAL model — local
    # planner hops are much slower (prompt prefill), so multi-hop tool turns
    # need more headroom than the cloud default.
    RUNTIME_TIMEOUT_LOCAL: int = 900
    RUNTIME_WORKERS: int = 5

    # ---- Runtime disk job cleanup ----
    ASK_JOB_ROOT: str = "ask"
    VOICE_JOB_ROOT: str = "voice"
    RUNTIME_JOB_CLEANUP_INTERVAL_SECONDS: int = 60 * 60
    ASK_JOB_RUN_RETENTION_HOURS: int = 24
    ASK_JOB_ACTIVE_RETENTION_HOURS: int = 6
    VOICE_JOB_RETENTION_HOURS: int = 24
    VOICE_JOB_ACTIVE_RETENTION_HOURS: int = 6

    # ---- Mail ----
    MAIL_SECRET_KEY: str = ""

    # ---- System cognition / workers ----
    # SYSTEM_COGNITION_MODEL removed — uses the chat.cognition slot.
    SYSTEM_COGNITION_MAX_HOPS: int = 4
    SYSTEM_COGNITION_MAX_JOBS_PER_TURN: int = 2
    SYSTEM_CURIOSITY_WORKER_BATCH_SIZE: int = 1
    SYSTEM_CURIOSITY_WORKER_INTERVAL_SECONDS: int = 1
    WORKFLOW_SCHEDULER_INTERVAL_SECONDS: int = 30
    BOOTSTRAP_DATA: bool = False

    # ---- Limits / Safety ----
    MAX_TOOL_STEPS: int = 3
    # Max independent (non-guarded, dependency-free) tool calls executed
    # concurrently within a single agent turn. 1 disables parallelism.
    MAX_PARALLEL_TOOL_CALLS: int = 5
    # Subagent dispatch (agent__dispatch internal tool).
    # Max nesting depth of subagents dispatching further subagents.
    SUBAGENT_MAX_DEPTH: int = 3
    # Optional assistant name forced for ad-hoc (unnamed) subagent dispatch.
    # Empty string => let the normal router pick an assistant.
    SUBAGENT_DEFAULT_ASSISTANT: str = ""
    # Hard cap on characters of a subagent answer folded into the summary.
    SUBAGENT_SUMMARY_MAX_CHARS: int = 4000
    # Max simultaneously-running background tasks (task__create).
    BACKGROUND_TASK_MAX_ACTIVE: int = 16
    # Self-check (verification) hop before completing a final answer.
    AGENT_VERIFICATION_ENABLED: bool = True
    # Max re-attempts triggered by a failed self-check (bounded further by the
    # agent loop iteration/wall-clock budgets).
    AGENT_MAX_VERIFICATION_HOPS: int = 1
    # Single-agent orchestration: one agent selects skills (by description) instead of
    # the router choosing among assistants. This is the default; the legacy router path
    # has been retired.
    SINGLE_AGENT_MODE: bool = True
    # 12 (was 8): the merged loop spends a hop on skill selection and multi-tool turns
    # (search → preview → inspect → read → act) need headroom. The selection hop is also
    # refunded in the loop so it doesn't eat this budget.
    CHAT_AGENT_MAX_ITERATIONS_PER_STEP: int = 12
    CHAT_AGENT_MAX_TOOL_CALLS_PER_STEP: int = 16
    CHAT_AGENT_MAX_SAME_ERROR_REPEATS: int = 2
    CHAT_AGENT_MAX_WALL_CLOCK_SECONDS: int = 300
    WORKFLOW_AGENT_MAX_ITERATIONS_PER_OPERATION: int = 12
    WORKFLOW_AGENT_MAX_TOOL_CALLS_PER_OPERATION: int = 20
    WORKFLOW_AGENT_MAX_SAME_ERROR_REPEATS: int = 2
    WORKFLOW_AGENT_MAX_WALL_CLOCK_SECONDS: int = 600
    # Absolute ceiling that also bounds a per-operation "no limit" (wall-clock 0)
    # or an override set higher — so a wandering agent can never run forever.
    # 0 disables the ceiling (truly unbounded). Default 30 min.
    WORKFLOW_AGENT_MAX_WALL_CLOCK_HARD_SECONDS: int = 1800
    # A `running` workflow run whose last activity is older than this is treated
    # as orphaned (its in-process executor died, e.g. a restart) and failed at
    # startup. Must comfortably exceed the per-operation wall-clock budget so a
    # legitimately long, still-live run is never reaped.
    WORKFLOW_ORPHAN_THRESHOLD_MINUTES: int = 30
    MAX_SEARCH_K: int = 5
    MAX_FILE_CHARS: int = 80_000

    TOOL_RESULT_MAX_INLINE_CHARS: int = 30_000
    TOOL_RESULT_MAX_PREVIEW_CHARS: int = 8_000
    TOOL_RESULT_MAX_INLINE_BYTES: int = 128_000
    TOOL_ARTIFACTS_ENABLED: bool = True

    # ---- Database ----
    DB_DIALECT: str = "sqlite"
    SQLITE_PATH: str = "./db/nd3x.dev.db"
    SQLITE_URL: str = ""
    DB_USER: str = ""
    DB_PASS: str = ""
    DB_HOST: str = ""
    DB_NAME: str = ""
    DB_PORT: int = 3306
    MYSQL_URL: str = ""
    DB_POOL_RECYCLE: int = 300
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT: int = 30

    # ---- Logging ----
    ENV: str = "dev"
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "text"
    LOG_COLOR: bool | None = None
    LOG_FILE: str = "logs/app.log"
    LOG_ROTATE_MB: int = 10
    LOG_BACKUP_COUNT: int = 5
    LOG_SAMPLING_RATE: float = 1.0
    LOG_THROTTLE_SECS: float = 0.0
    LOG_CONTEXT: str = ""
    LOG_DB_ENABLED: bool = False
    LOG_DB_LEVEL: str = "WARNING"
    LOG_DB_SAMPLE_RATE: float = 1.0
    LOG_DB_MAX_FIELD_CHARS: int = 12_000
    LOG_DB_EXCLUDE_LOGGERS: list[str] = []

    # ---- Auth / JWT ----
    JWT_SECRET: str = "REPLACE_WITH_256BIT_SECRET"
    JWT_ALG: str = "HS256"
    ACCESS_TOKEN_MIN: int = 30

    # ---- External tools ----
    EXA_API_KEY: str = ""
    EXA_TIMEOUT_S: float = 30.0
    # Curiosity/cognition web research backend: "auto" (keyless DuckDuckGo unless
    # an Exa key is set), "duckduckgo" (keyless), "exa", or "none".
    RESEARCH_PROVIDER: str = "auto"
    # Light mode for local/small models: when the planner reply can't be parsed
    # as JSON even after a retry, salvage the model's raw text as the answer
    # instead of failing the turn. Default on — small local models often emit prose.
    LOCAL_MODEL_LIGHT_MODE: bool = True

    # Online model catalog (enriches cloud model discovery with names, context
    # windows and indicative prices). Public, no-auth; cached for TTL seconds.
    MODEL_CATALOG_URL: str = "https://models.dev/api.json"
    MODEL_CATALOG_TTL: int = 86400

    # ---- Settings encryption ----
    SETTINGS_ENCRYPTION_KEY: str = ""

    # ---- Memory retrieval / injection ----
    # MEMORY_RETRIEVAL_DECISION_MODEL removed — uses the chat.memory_decision slot.
    ROUTER_MEMORY_INJECTION_ENABLED: bool = True
    PLANNER_MEMORY_INJECTION_ENABLED: bool = True

    # ---- System cognition embeddings ----
    # SYSTEM_COGNITION_EMBEDDING_MODEL removed — uses the embeddings slot.
    SYSTEM_COGNITION_EMBEDDING_DIMENSIONS: int | None = None
    SYSTEM_COGNITION_EMBEDDING_BATCH_SIZE: int = 64

    # Only enable manually/once. The bootstrap is idempotent, but embedding all
    # existing records can cost tokens and time.
    BOOTSTRAP_SYSTEM_COGNITION_EMBEDDINGS: bool = False
    BOOTSTRAP_SYSTEM_COGNITION_EMBEDDING_MAX_BATCHES: int = 100

    FILES_DIR: str = "./Files"


_sqlite_path = _env_or("SQLITE_PATH", _db_cfg.get("sqlite_path") or _roots.get("db_path") or "./db/nd3x.dev.db")
_db_user = _env_or("DB_USER", _db_cfg.get("user", ""))
_db_pass = _env_or("DB_PASS", _db_cfg.get("pass", ""))
_db_host = _env_or("DB_HOST", _db_cfg.get("host", ""))
_db_port = _env_int("DB_PORT", int(_db_cfg.get("port") or 3306))
_db_name = _env_or("DB_NAME", _db_cfg.get("name", ""))


settings = Settings(
    CONFIGURED=_CONFIGURED,
    BASE_DIR=_roots.get("base_dir", ""),
    EVALUATION_HOPS=_env_int("EVALUATION_HOPS", 5),
    LOCAL_MODEL_TIMEOUT=_env_int("LOCAL_MODEL_TIMEOUT", 180),
    OLLAMA_NUM_CTX=_env_int("OLLAMA_NUM_CTX", 16384),
    MODEL_SLOW_STEP_WARN_S=_env_int("MODEL_SLOW_STEP_WARN_S", 90),
    MODEL_EVAL_INTERVAL_HOURS=_env_int("MODEL_EVAL_INTERVAL_HOURS", 0),
    GOAL_MODE_BUDGET_MULTIPLIER=float(os.getenv("GOAL_MODE_BUDGET_MULTIPLIER", "3.0") or 3.0),
    OLLAMA_LIBRARY_URL=os.getenv("OLLAMA_LIBRARY_URL", "https://ollama.com/library"),
    OLLAMA_LIBRARY_TTL=_env_int("OLLAMA_LIBRARY_TTL", 3600),

    MCP_URL=os.getenv("MCP_URL", ""),
    MCP_BEARER=os.getenv("MCP_BEARER", ""),

    HOST=os.getenv("HOST", "0.0.0.0"),
    PORT=_env_int("PORT", 8088),
    DEBUG=_env_bool("DEBUG", True),

    RUNTIME_TIMEOUT=_env_int("RUNTIME_TIMEOUT", 300),
    RUNTIME_TIMEOUT_LOCAL=_env_int("RUNTIME_TIMEOUT_LOCAL", 900),
    RUNTIME_WORKERS=_env_int("RUNTIME_WORKERS", 5),

    ASK_JOB_ROOT=_env_or("ASK_JOB_ROOT", _roots.get("ask_root", "ask")),
    VOICE_JOB_ROOT=_env_or("VOICE_JOB_ROOT", _roots.get("voice_root", "voice")),
    RUNTIME_JOB_CLEANUP_INTERVAL_SECONDS=_env_int(
        "RUNTIME_JOB_CLEANUP_INTERVAL_SECONDS",
        _env_int("ASK_JOB_CLEANUP_INTERVAL_SECONDS", 60 * 60),
    ),
    ASK_JOB_RUN_RETENTION_HOURS=_env_int("ASK_JOB_RUN_RETENTION_HOURS", 24),
    ASK_JOB_ACTIVE_RETENTION_HOURS=_env_int("ASK_JOB_ACTIVE_RETENTION_HOURS", 6),
    VOICE_JOB_RETENTION_HOURS=_env_int("VOICE_JOB_RETENTION_HOURS", 24),
    VOICE_JOB_ACTIVE_RETENTION_HOURS=_env_int("VOICE_JOB_ACTIVE_RETENTION_HOURS", 6),

    MAIL_SECRET_KEY=_env_or("MAIL_SECRET_KEY", _secrets_store.get("MAIL_SECRET_KEY", "")),

    SYSTEM_COGNITION_MAX_HOPS=_env_int("SYSTEM_COGNITION_MAX_HOPS", 4),
    SYSTEM_COGNITION_MAX_JOBS_PER_TURN=_env_int("SYSTEM_COGNITION_MAX_JOBS_PER_TURN", 2),
    SYSTEM_CURIOSITY_WORKER_BATCH_SIZE=_env_int("SYSTEM_CURIOSITY_WORKER_BATCH_SIZE", 1),
    SYSTEM_CURIOSITY_WORKER_INTERVAL_SECONDS=_env_int("SYSTEM_CURIOSITY_WORKER_INTERVAL_SECONDS", 1),
    WORKFLOW_SCHEDULER_INTERVAL_SECONDS=_env_int("WORKFLOW_SCHEDULER_INTERVAL_SECONDS", 30),
    BOOTSTRAP_DATA=_env_bool("BOOTSTRAP_DATA", False),

    MAX_TOOL_STEPS=_env_int("MAX_TOOL_STEPS", 3),
    SINGLE_AGENT_MODE=_env_bool("SINGLE_AGENT_MODE", True),
    CHAT_AGENT_MAX_ITERATIONS_PER_STEP=_env_int("CHAT_AGENT_MAX_ITERATIONS_PER_STEP", 12),
    CHAT_AGENT_MAX_TOOL_CALLS_PER_STEP=_env_int("CHAT_AGENT_MAX_TOOL_CALLS_PER_STEP", 16),
    CHAT_AGENT_MAX_SAME_ERROR_REPEATS=_env_int("CHAT_AGENT_MAX_SAME_ERROR_REPEATS", 2),
    CHAT_AGENT_MAX_WALL_CLOCK_SECONDS=_env_int("CHAT_AGENT_MAX_WALL_CLOCK_SECONDS", 300),
    WORKFLOW_AGENT_MAX_ITERATIONS_PER_OPERATION=_env_int("WORKFLOW_AGENT_MAX_ITERATIONS_PER_OPERATION", 12),
    WORKFLOW_AGENT_MAX_TOOL_CALLS_PER_OPERATION=_env_int("WORKFLOW_AGENT_MAX_TOOL_CALLS_PER_OPERATION", 20),
    WORKFLOW_AGENT_MAX_SAME_ERROR_REPEATS=_env_int("WORKFLOW_AGENT_MAX_SAME_ERROR_REPEATS", 2),
    WORKFLOW_AGENT_MAX_WALL_CLOCK_SECONDS=_env_int("WORKFLOW_AGENT_MAX_WALL_CLOCK_SECONDS", 600),
    WORKFLOW_AGENT_MAX_WALL_CLOCK_HARD_SECONDS=_env_int("WORKFLOW_AGENT_MAX_WALL_CLOCK_HARD_SECONDS", 1800),
    WORKFLOW_ORPHAN_THRESHOLD_MINUTES=_env_int("WORKFLOW_ORPHAN_THRESHOLD_MINUTES", 30),
    MAX_SEARCH_K=_env_int("MAX_SEARCH_K", 5),
    MAX_FILE_CHARS=_env_int("MAX_FILE_CHARS", 80_000),
    TOOL_RESULT_MAX_INLINE_CHARS=_env_int("TOOL_RESULT_MAX_INLINE_CHARS", 30_000),
    TOOL_RESULT_MAX_PREVIEW_CHARS=_env_int("TOOL_RESULT_MAX_PREVIEW_CHARS", 8_000),
    TOOL_RESULT_MAX_INLINE_BYTES=_env_int("TOOL_RESULT_MAX_INLINE_BYTES", 128_000),
    TOOL_ARTIFACTS_ENABLED=_env_bool("TOOL_ARTIFACTS_ENABLED", True),

    DB_DIALECT=(os.getenv("DB_DIALECT") or _db_cfg.get("dialect") or "sqlite").strip().lower(),
    SQLITE_PATH=_sqlite_path,
    SQLITE_URL=os.getenv("SQLITE_URL", f"sqlite:///{_sqlite_path}"),
    DB_USER=_db_user,
    DB_PASS=_db_pass,
    DB_HOST=_db_host,
    DB_NAME=_db_name,
    DB_PORT=_db_port,
    MYSQL_URL=os.getenv(
        "MYSQL_URL",
        _db_cfg.get("mysql_url")
        or f"mysql+pymysql://{_db_user}:{urllib.parse.quote_plus(_db_pass or '')}@{_db_host}:{_db_port}/{_db_name}?charset=utf8mb4",
    ),
    DB_POOL_RECYCLE=_env_int("DB_POOL_RECYCLE", 300),
    DB_POOL_SIZE=_env_int("DB_POOL_SIZE", 5),
    DB_MAX_OVERFLOW=_env_int("DB_MAX_OVERFLOW", 10),
    DB_POOL_TIMEOUT=_env_int("DB_POOL_TIMEOUT", 30),

    ENV=os.getenv("ENV", "dev"),
    LOG_LEVEL=os.getenv("LOG_LEVEL", "INFO"),
    LOG_FORMAT=os.getenv("LOG_FORMAT", "text"),
    LOG_COLOR=None if os.getenv("LOG_COLOR") is None else _env_bool("LOG_COLOR", True),
    LOG_FILE=_env_or("LOG_FILE", _roots.get("log_file", "logs/app.log")),
    LOG_ROTATE_MB=_env_int("LOG_ROTATE_MB", 10),
    LOG_BACKUP_COUNT=_env_int("LOG_BACKUP_COUNT", 5),
    LOG_SAMPLING_RATE=_env_float("LOG_SAMPLING_RATE", 1.0),
    LOG_THROTTLE_SECS=_env_float("LOG_THROTTLE_SECS", 0.0),
    LOG_CONTEXT=os.getenv("LOG_CONTEXT", ""),
    LOG_DB_ENABLED=_env_bool("LOG_DB_ENABLED", False),
    LOG_DB_LEVEL=os.getenv("LOG_DB_LEVEL", "WARNING"),
    LOG_DB_SAMPLE_RATE=_env_float("LOG_DB_SAMPLE_RATE", 1.0),
    LOG_DB_MAX_FIELD_CHARS=_env_int("LOG_DB_MAX_FIELD_CHARS", 12_000),
    LOG_DB_EXCLUDE_LOGGERS=_env_csv("LOG_DB_EXCLUDE_LOGGERS"),

    JWT_SECRET=_env_or("JWT_SECRET", _secrets_store.get("JWT_SECRET", "REPLACE_WITH_256BIT_SECRET")),
    JWT_ALG=os.getenv("JWT_ALG", "HS256"),
    ACCESS_TOKEN_MIN=_env_int("ACCESS_TOKEN_MIN", 30),

    EXA_API_KEY=os.getenv("EXA_API_KEY", ""),
    EXA_TIMEOUT_S=_env_float("EXA_TIMEOUT_S", 30.0),
    RESEARCH_PROVIDER=os.getenv("RESEARCH_PROVIDER", "auto"),
    LOCAL_MODEL_LIGHT_MODE=_env_bool("LOCAL_MODEL_LIGHT_MODE", True),
    MODEL_CATALOG_URL=os.getenv("MODEL_CATALOG_URL", "https://models.dev/api.json"),
    MODEL_CATALOG_TTL=_env_int("MODEL_CATALOG_TTL", 86400),

    SETTINGS_ENCRYPTION_KEY=_env_or("SETTINGS_ENCRYPTION_KEY", _secrets_store.get("SETTINGS_ENCRYPTION_KEY", "")),

    ROUTER_MEMORY_INJECTION_ENABLED=_env_bool("ROUTER_MEMORY_INJECTION_ENABLED", True),
    PLANNER_MEMORY_INJECTION_ENABLED=_env_bool("PLANNER_MEMORY_INJECTION_ENABLED", True),

    SYSTEM_COGNITION_EMBEDDING_DIMENSIONS=_env_optional_int(
        "SYSTEM_COGNITION_EMBEDDING_DIMENSIONS",
        None,
    ),
    SYSTEM_COGNITION_EMBEDDING_BATCH_SIZE=_env_int("SYSTEM_COGNITION_EMBEDDING_BATCH_SIZE", 64),
    BOOTSTRAP_SYSTEM_COGNITION_EMBEDDINGS=_env_bool("BOOTSTRAP_SYSTEM_COGNITION_EMBEDDINGS", False),
    BOOTSTRAP_SYSTEM_COGNITION_EMBEDDING_MAX_BATCHES=_env_int(
        "BOOTSTRAP_SYSTEM_COGNITION_EMBEDDING_MAX_BATCHES",
        100,
    ),
    FILES_DIR=_env_or("FILES_DIR", _roots.get("files_dir", "./files")),
)
