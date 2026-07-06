# app/routers/notification_recipient_router.py

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from db.database import get_db
from repository.notification_recipient_repository import (
    NotificationRecipientRepository,
)
from schemas.notification_recipient import (
    NotificationRecipientCreate,
    NotificationRecipientResponse,
    NotificationRecipientUpdate,
)


router = APIRouter(
    prefix="/notification-recipients",
    tags=["Notification recipients"],
)


@router.get("", response_model=list[NotificationRecipientResponse])
def get_notification_recipients(db: Session = Depends(get_db)):
    repository = NotificationRecipientRepository(db)
    return repository.get_all()


@router.post("", response_model=NotificationRecipientResponse, status_code=201)
def create_notification_recipient(
    data: NotificationRecipientCreate,
    db: Session = Depends(get_db),
):
    repository = NotificationRecipientRepository(db)
    return repository.create(data)


@router.patch("/{recipient_id}", response_model=NotificationRecipientResponse)
def update_notification_recipient(
    recipient_id: int,
    data: NotificationRecipientUpdate,
    db: Session = Depends(get_db),
):
    repository = NotificationRecipientRepository(db)
    recipient = repository.update(recipient_id, data)

    if not recipient:
        raise HTTPException(
            status_code=404,
            detail="Notificatie ontvanger niet gevonden.",
        )

    return recipient


@router.delete("/{recipient_id}")
def delete_notification_recipient(
    recipient_id: int,
    db: Session = Depends(get_db),
):
    repository = NotificationRecipientRepository(db)
    deleted = repository.delete(recipient_id)

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Notificatie ontvanger niet gevonden.",
        )

    return {
        "message": "Notificatie ontvanger verwijderd.",
    }