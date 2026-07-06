# app/repositories/notification_recipient_repository.py

from sqlalchemy.orm import Session

from models.notification_recipient import NotificationRecipient
from schemas.notification_recipient import (
    NotificationRecipientCreate,
    NotificationRecipientUpdate,
)


class NotificationRecipientRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_all(self) -> list[NotificationRecipient]:
        return (
            self.db.query(NotificationRecipient)
            .order_by(NotificationRecipient.id.desc())
            .all()
        )

    def get_active(self) -> list[NotificationRecipient]:
        return (
            self.db.query(NotificationRecipient)
            .filter(NotificationRecipient.is_active.is_(True))
            .order_by(NotificationRecipient.id.desc())
            .all()
        )

    def get_by_id(self, recipient_id: int) -> NotificationRecipient | None:
        return (
            self.db.query(NotificationRecipient)
            .filter(NotificationRecipient.id == recipient_id)
            .first()
        )

    def create(
        self,
        data: NotificationRecipientCreate,
    ) -> NotificationRecipient:
        recipient = NotificationRecipient(
            name=data.name,
            email=str(data.email),
            is_active=data.is_active,
        )

        self.db.add(recipient)
        self.db.commit()
        self.db.refresh(recipient)

        return recipient

    def update(
        self,
        recipient_id: int,
        data: NotificationRecipientUpdate,
    ) -> NotificationRecipient | None:
        recipient = self.get_by_id(recipient_id)

        if not recipient:
            return None

        update_data = data.model_dump(exclude_unset=True)

        for field, value in update_data.items():
            setattr(recipient, field, value)

        self.db.commit()
        self.db.refresh(recipient)

        return recipient

    def delete(self, recipient_id: int) -> bool:
        recipient = self.get_by_id(recipient_id)

        if not recipient:
            return False

        self.db.delete(recipient)
        self.db.commit()

        return True