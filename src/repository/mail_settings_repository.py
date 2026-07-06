# app/repositories/mail_settings_repository.py

from sqlalchemy.orm import Session

from models.mail_settings import MailSettings
from schemas.mail import MailSettingsCreate, MailSettingsUpdate
from utils.crypto import encrypt_value


class MailSettingsRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, settings_id: int) -> MailSettings | None:
        return (
            self.db.query(MailSettings)
            .filter(MailSettings.id == settings_id)
            .first()
        )

    def get_active(self) -> MailSettings | None:
        return (
            self.db.query(MailSettings)
            .filter(MailSettings.is_active.is_(True))
            .first()
        )

    def get_all(self) -> list[MailSettings]:
        return self.db.query(MailSettings).all()

    def create(self, data: MailSettingsCreate) -> MailSettings:
        mail_settings = MailSettings(
            name=data.name,
            smtp_host=data.smtp_host,
            smtp_port=data.smtp_port,
            smtp_username=data.smtp_username,
            smtp_password=encrypt_value(data.smtp_password),
            from_email=str(data.from_email),
            from_name=data.from_name,
            use_tls=data.use_tls,
            use_ssl=data.use_ssl,
            is_active=data.is_active,
        )

        self.db.add(mail_settings)
        self.db.commit()
        self.db.refresh(mail_settings)

        return mail_settings

    def update(
        self,
        settings_id: int,
        data: MailSettingsUpdate,
    ) -> MailSettings | None:
        mail_settings = self.get_by_id(settings_id)

        if not mail_settings:
            return None

        update_data = data.model_dump(exclude_unset=True)

        if "smtp_password" in update_data:
            update_data["smtp_password"] = encrypt_value(update_data["smtp_password"])

        for field, value in update_data.items():
            setattr(mail_settings, field, value)

        self.db.commit()
        self.db.refresh(mail_settings)

        return mail_settings

    def delete(self, settings_id: int) -> bool:
        mail_settings = self.get_by_id(settings_id)

        if not mail_settings:
            return False

        self.db.delete(mail_settings)
        self.db.commit()

        return True