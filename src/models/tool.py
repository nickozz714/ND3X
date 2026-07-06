from sqlalchemy import Column, Integer, String, Text, JSON, Boolean, DateTime, UniqueConstraint, ForeignKey
from sqlalchemy.orm import relationship
from db.database import Base
from models.assistant_tool import assistant_tool


class Tool(Base):
    __tablename__ = "tool"

    id = Column(Integer, primary_key=True, autoincrement=True)
    remote_name = Column(String, nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=False)

    # Keep existing field name for backward compatibility for now
    argument = Column(JSON, nullable=False)  # argument schema
    output_schema = Column(JSON, nullable=True)
    annotations = Column(JSON, nullable=True)
    meta = Column(JSON, nullable=True)

    type = Column(String, nullable=False)
    tool_instructions = Column(Text, nullable=False)

    is_dynamic_micro_tool = Column(Boolean, nullable=True)
    attached_microservice = Column(Text, nullable=True)

    is_enabled = Column(Boolean, nullable=False, default=True)
    availability_scope = Column(String, nullable=True)  # optional future use

    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    mcp_server_id = Column(Integer, ForeignKey('mcp_server.id'), nullable=False)

    mcp_server = relationship("MCPServer", back_populates="tools")
    assistants = relationship(
        "Assistant",
        secondary=assistant_tool,
        back_populates="tools",
    )

    __table_args__ = (
        UniqueConstraint("mcp_server_id", "remote_name", name="uq_tool_server_remote_name"),
    )