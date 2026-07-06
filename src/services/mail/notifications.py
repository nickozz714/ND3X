# app/services/notifications.py

import logging

from sqlalchemy.orm import Session

from component.logging import get_logger
from services.mail.notification_mail_service import NotificationMailService


logger = logging.getLogger(__name__)
log = get_logger(__name__)


def send_system_notification(
    db: Session,
    subject: str,
    title: str,
    message: str,
    data: dict | None = None,
    action_url: str | None = None,
    recipients: list[str] | None = None,
) -> bool:
    log.infox(
        "System notification versturen gestart",
        subject=subject,
        title=title,
        message_length=len(message or ""),
        has_data=data is not None,
        data_keys=list(data.keys()) if isinstance(data, dict) else None,
        has_action_url=bool(action_url),
    )

    try:
        logger.info(f"Sending system notification.")
        log.debugx(
            "NotificationMailService aanmaken voor system notification",
            has_db_session=db is not None,
        )
        service = NotificationMailService(db)

        result = service.send_notification(
            subject=subject,
            title=title,
            message=message,
            data=data,
            action_url=action_url,
            recipients=recipients,
        )

        log.infox(
            "System notification versturen afgerond",
            subject=subject,
            title=title,
            success=result,
        )
        return result

    except Exception:
        logger.exception(
            "System notification failed, but execution will continue."
        )
        log.errorx(
            "System notification mislukt, uitvoering gaat door",
            subject=subject,
            title=title,
        )
        return False