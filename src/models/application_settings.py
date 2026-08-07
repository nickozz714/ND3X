from sqlalchemy import Column, Integer, String, Text
from db.database import Base


class ApplicationSetting(Base):
    __tablename__ = "application_settings"

    id = Column(Integer, primary_key=True, index=True)
    org_id = Column(Integer, nullable=True, index=True)  # tenant scope (multi-tenancy phase 1)
    key = Column(String(255), unique=True, nullable=False, index=True)
    value = Column(Text, nullable=False)