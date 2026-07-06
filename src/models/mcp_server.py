from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, ForeignKey, JSON
from sqlalchemy.orm import relationship

from db.database import Base


class MCPServer(Base):
    __tablename__ = "mcp_server"

    id          = Column(Integer, primary_key=True, autoincrement=True)

    # Human/admin identity
    name        = Column(String, nullable=False, unique=True)
    slug        = Column(String, nullable=False, unique=True)
    description = Column(Text, nullable=True)

    # Connection
    # server_type: http | sse | stdio | builtin
    server_type = Column(String, nullable=False, default="http")
    base_url    = Column(Text, nullable=True)   # nullable: niet nodig voor stdio/builtin

    # Stdio specifiek (alleen relevant als server_type == "stdio")
    stdio_command         = Column(Text, nullable=True)  # bijv. "fabric-mcp"
    stdio_install_command = Column(Text, nullable=True)  # bijv. "pipx install fabric-mcp"

    is_enabled  = Column(Boolean, nullable=False, default=True)

    # Sync/runtime metadata
    last_synced_at   = Column(DateTime, nullable=True)
    last_sync_status = Column(String, nullable=True)   # success | failed
    last_sync_error  = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)

    tools        = relationship("Tool", back_populates="mcp_server")
    auth_configs = relationship("MCPServerAuth", back_populates="mcp_server")


class MCPServerAuth(Base):
    __tablename__ = "mcp_server_auth"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    mcp_server_id = Column(Integer, ForeignKey("mcp_server.id"), nullable=False)

    auth_type = Column(String, nullable=False)   # none | bearer | basic | api_key | oauth_* | ssh_key
    config    = Column(JSON, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)

    mcp_server = relationship("MCPServer", back_populates="auth_configs")
