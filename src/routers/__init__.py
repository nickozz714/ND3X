from .authenticate_router import router as authenticate_router
from .main_routes import router as main_routes
from .ui_routes import router as ui_routes
from .ui_lifestyle import router as ui_lifestyle
from .project_management_routes import router as project_management_routes
from .tool_routes import router as tool_routes
from .assistant_routes import router as assistant_routes
from .keyvault_router import router as keyvault_routes
from .mcp_server_routes import router as mcp_server_routes
from .workflow_router import router as workflow_routes
from .workflow_router import run_router as run_workflow_routes
from .mail_router import router as mail_routes
from .notification_recipient_router import router as notification_recipient_routes
from .prompt_variable_router import router as prompt_variable_routes
from .systen_cognition import router as systen_cognition_routes
from .logs import router as logs_routes
from .application_settings import router as application_settings_routes
from .skills import router as skills_routes
from .assistant_thread_routes import router as assistant_thread_routes
from .voice_webrtc_routes import router as voice_webrtc_routes
from .builtin import router as builtin_routes
from .pdf_routes import router as pdf_routes
from .admin_user_routes import router as admin_user_routes
from .providers_router import router as providers_routes
from .local_models_router import router as local_models_routes
from .usage_router import router as usage_routes
from .fabric_data_agent_routes import router as fabric_data_agent_routes
from .transfer_routes import router as transfer_routes
from .meeting_profile_routes import router as meeting_profile_routes
from .slash_command_router import router as slash_command_routes
from .image_routes import router as image_routes
from .import_export_router import router as import_export_routes
from .secrets import router as secrets_routes
from .board_router import router as board_routes

all_routers = [
    authenticate_router,
    main_routes,
    ui_routes,
    project_management_routes,
    ui_lifestyle,
    tool_routes,
    assistant_routes,
    keyvault_routes,
    mcp_server_routes,
    workflow_routes,
    run_workflow_routes,
    notification_recipient_routes,
    mail_routes,
    prompt_variable_routes,
    systen_cognition_routes,
    logs_routes,
    application_settings_routes,
    skills_routes,
    assistant_thread_routes,
    voice_webrtc_routes,
    builtin_routes,
    pdf_routes,
    admin_user_routes,
    providers_routes,
    local_models_routes,
    usage_routes,
    fabric_data_agent_routes,
    transfer_routes,
    meeting_profile_routes,
    slash_command_routes,
    image_routes,
    import_export_routes,
    secrets_routes,
    board_routes,
]