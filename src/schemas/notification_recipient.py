# app/schemas/notification_recipient.py

from pydantic import BaseModel, EmailStr


class NotificationRecipientCreate(BaseModel):
    name: str | None = None
    email: EmailStr
    is_active: bool = True


class NotificationRecipientUpdate(BaseModel):
    name: str | None = None
    email: EmailStr | None = None
    is_active: bool | None = None


class NotificationRecipientResponse(BaseModel):
    id: int
    name: str | None
    email: EmailStr
    is_active: bool

    class Config:
        from_attributes = True