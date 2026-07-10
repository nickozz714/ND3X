# src/db/init_db.py
from __future__ import annotations


from db.bootstrap import run_bootstrap
from db.database import Base, get_engine, get_session_factory


async def init_db() -> None:
    """
    Registreer alle models op dezelfde Base en maak tabellen aan (idempotent).
    """
    # Belangrijk: imports zorgen dat SQLAlchemy de tabellen "ziet".
    from models import authenticate as _auth_models  # noqa: F401
    from models import audit as _audit_models  # noqa: F401
    from models import assistant as _assistant_models # noqa: F401
    from models import tool as _tool_models # noqa: F401
    from models import assistant_tool as _assistant_tools # noqa: F401
    from models import mcp_server as _mcp_server_models # noqa: F401
    from models import assistant_output_chunk as _assistant_output_chunk_models # noqa: F401
    from models import system_cognition as _system_cognition_models  # noqa: F401
    from models import log_entry as _log_entry_models  # noqa: F401
    from models import application_settings as _application_settings_models # noqa: F401
    from models import skill as _skill_models  # noqa: F401
    from models import skill_file as _skill_file_models  # noqa: F401
    from models import assistant_skill as _assistant_skill_models  # noqa: F401
    from models import skill_tool as _skill_tool_models  # noqa: F401
    from models import assistant_thread as _assistant_thread_models  # noqa F401
    from models import shell_script as _shell_script_models # noqa F401
    from models import token_usage as _token_usage_models  # noqa: F401

    # Text indexing models
    from models import text_document as _text_document_models  # noqa: F401

    # Provider/model registry (model-agnostic AI platform)
    from models import provider as _provider_models  # noqa: F401
    from models import fabric_data_agent as _fabric_data_agent_models  # noqa: F401
    from models import transfer as _transfer_models  # noqa: F401
    from models import meeting_profile as _meeting_profile_models  # noqa: F401
    from models import slash_command as _slash_command_models  # noqa: F401
    from models import secret as _secret_models  # noqa: F401
    from models import board as _board_models  # noqa: F401
    from models import background_task as _background_task_models  # noqa: F401
    from models import repository as _repository_models  # noqa: F401

    Base.metadata.create_all(bind=get_engine())
    db = get_session_factory()()
    try:
        await run_bootstrap(db)
    finally:
        db.close()