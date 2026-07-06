from sqlalchemy import Table, Column, Integer, ForeignKey
from db.database import Base

assistant_tool = Table(
    "assistant_tool",
    Base.metadata,
    Column(
        "assistant_id",
        Integer,
        ForeignKey("assistant.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "tool_id",
        Integer,
        ForeignKey("tool.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)