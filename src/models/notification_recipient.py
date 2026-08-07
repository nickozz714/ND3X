# app/models/notification_recipient.py

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from db.database import Base


class NotificationRecipient(Base):
    __tablename__ = "notification_recipients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    org_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # tenant scope (multi-tenancy phase 1)

    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)