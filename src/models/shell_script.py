from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, JSON
from db.database import Base


class ShellScript(Base):
    __tablename__ = "shell_script"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    name        = Column(String, nullable=False)
    slug        = Column(String, nullable=False, unique=True)
    description = Column(Text, nullable=False, default="")
    script      = Column(Text, nullable=False)
    parameters  = Column(JSON, nullable=False, default=list)   # ["TITLE", "SPACE_KEY", ...]
    env         = Column(JSON, nullable=False, default=dict)   # {"FOO": "bar"}
    is_enabled  = Column(Boolean, nullable=False, default=True)
    created_at  = Column(DateTime, nullable=False)
    updated_at  = Column(DateTime, nullable=False)
