# app/services/notification_mail_service.py

import logging

from sqlalchemy.orm import Session

from component.logging import get_logger
from repository.mail_settings_repository import MailSettingsRepository
from repository.notification_recipient_repository import (
    NotificationRecipientRepository,
)
from services.mail.mail_service import MailService


logger = logging.getLogger(__name__)
log = get_logger(__name__)


class NotificationMailService:
    def __init__(self, db: Session):
        log.debugx(
            "NotificationMailService initialiseren",
            has_db_session=db is not None,
        )
        self.db = db
        self.recipient_repository = NotificationRecipientRepository(db)
        log.debugx("NotificationRecipientRepository gekoppeld aan NotificationMailService")
        self.mail_settings_repository = MailSettingsRepository(db)
        log.debugx("MailSettingsRepository gekoppeld aan NotificationMailService")
        self.mail_service = MailService(db)
        log.debugx("MailService gekoppeld aan NotificationMailService")

    def send_notification(
        self,
        subject: str,
        title: str,
        message: str,
        data: dict | None = None,
        action_url: str | None = None,
        recipients: list[str] | None = None,
    ) -> bool:
        log.infox(
            "Notification mail versturen gestart",
            subject=subject,
            title=title,
            message_length=len(message or ""),
            has_data=data is not None,
            data_keys=list(data.keys()) if isinstance(data, dict) else None,
            has_action_url=bool(action_url),
        )

        mail_settings = self.mail_settings_repository.get_active()
        log.debugx(
            "Actieve mailinstellingen opgehaald voor notification mail",
            has_mail_settings=mail_settings is not None,
            smtp_host=getattr(mail_settings, "smtp_host", None) if mail_settings else None,
            smtp_port=getattr(mail_settings, "smtp_port", None) if mail_settings else None,
            from_email=getattr(mail_settings, "from_email", None) if mail_settings else None,
        )

        if not mail_settings:
            logger.info(
                "Notification mail skipped: no active mail settings configured."
            )
            log.warningx(
                "Notification mail overgeslagen: geen actieve mailinstellingen geconfigureerd",
                subject=subject,
                title=title,
            )
            return False

        # Explicit per-notification recipients (e.g. a workflow notification op emailing a
        # specific user) take precedence; otherwise fall back to the global active list.
        if recipients:
            recipient_emails = [str(e).strip() for e in recipients if str(e or "").strip()]
            log.debugx("Expliciete notification recipients gebruikt", email_count=len(recipient_emails))
        else:
            active = self.recipient_repository.get_active()
            recipient_emails = [r.email for r in active if r.email]
            log.debugx(
                "Globale notification recipients gebruikt",
                email_count=len(recipient_emails),
            )

        if not recipient_emails:
            logger.info(
                "Notification mail skipped: no active notification recipients configured."
            )
            log.warningx(
                "Notification mail overgeslagen: geen actieve notification recipients geconfigureerd",
                subject=subject,
                title=title,
            )
            return False

        try:
            log.infox(
                "Notification template mail verzenden",
                subject=subject,
                title=title,
                recipient_count=len(recipient_emails),
                template_name="notification_mail.html",
                has_action_url=bool(action_url),
            )
            self.mail_service.send_template_mail(
                to=recipient_emails,
                subject=subject,
                template_name="notification_mail.html",
                context={
                    "subject": subject,
                    "title": title,
                    "message": message,
                    "data": data or {},
                    "action_url": action_url,
                },
                settings=mail_settings,
            )

            log.infox(
                "Notification mail succesvol verstuurd",
                subject=subject,
                title=title,
                recipient_count=len(recipient_emails),
            )
            return True

        except Exception:
            logger.exception(
                "Notification mail failed, but the workflow will continue."
            )
            log.errorx(
                "Notification mail mislukt, workflow gaat door",
                subject=subject,
                title=title,
                recipient_count=len(recipient_emails),
            )
            return False