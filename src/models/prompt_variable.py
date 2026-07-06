from datetime import datetime

from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime

from db.database import Base


class PromptVariable(Base):
    __tablename__ = "prompt_variable"

    id = Column(Integer, primary_key=True, autoincrement=True)
    token = Column(String(100), nullable=False, unique=True, index=True)  # CurrentTime
    code = Column(Text, nullable=False)
    is_enabled = Column(Boolean, nullable=False, default=True)
    timeout_ms = Column(Integer, nullable=False, default=1000)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)