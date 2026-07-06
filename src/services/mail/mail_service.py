# app/services/mail_service.py
import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from component.logging import get_logger
from models.mail_settings import MailSettings
from repository.mail_settings_repository import MailSettingsRepository
from utils.crypto import decrypt_value

logger = logging.getLogger(__name__)
log = get_logger(__name__)

class MailService:
    def __init__(self, db: Session):
        log.debugx(
            "MailService initialiseren",
            has_db_session=db is not None,
        )
        self.db = db
        self.mail_settings_repository = MailSettingsRepository(db)
        log.debugx("MailSettingsRepository gekoppeld aan MailService")

        template_path = Path(__file__).resolve().parent.parents[1] / "templates" / "email"
        log.debugx(
            "Mail template pad bepaald",
            template_path=str(template_path),
            template_path_exists=template_path.exists(),
        )

        self.jinja_env = Environment(
            loader=FileSystemLoader(template_path),
            autoescape=select_autoescape(["html", "xml"]),
        )
        log.debugx("Jinja mail environment geïnitialiseerd")

    def send_template_mail(
        self,
        to: list[str],
        subject: str,
        template_name: str,
        context: dict,
        settings: MailSettings | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
    ) -> None:
        log.infox(
            "Template mail versturen gestart",
            subject=subject,
            template_name=template_name,
            to_count=len(to or []),
            cc_count=len(cc or []),
            bcc_count=len(bcc or []),
            context_keys=list(context.keys()) if isinstance(context, dict) else None,
            settings_provided=settings is not None,
        )

        if not to:
            log.warningx(
                "Template mail versturen overgeslagen: geen ontvangers opgegeven",
                subject=subject,
                template_name=template_name,
            )
            return

        mail_settings = settings or self.mail_settings_repository.get_active()
        log.debugx(
            "Mailinstellingen opgehaald voor template mail",
            subject=subject,
            template_name=template_name,
            settings_provided=settings is not None,
            has_mail_settings=mail_settings is not None,
            smtp_host=getattr(mail_settings, "smtp_host", None) if mail_settings else None,
            smtp_port=getattr(mail_settings, "smtp_port", None) if mail_settings else None,
            from_email=getattr(mail_settings, "from_email", None) if mail_settings else None,
            use_ssl=getattr(mail_settings, "use_ssl", None) if mail_settings else None,
            use_tls=getattr(mail_settings, "use_tls", None) if mail_settings else None,
        )

        if not mail_settings:
            log.warningx(
                "Template mail versturen mislukt: geen actieve mailinstellingen gevonden",
                subject=subject,
                template_name=template_name,
            )
            raise HTTPException(
                status_code=400,
                detail="Geen actieve mailinstellingen gevonden.",
            )

        html_body = self._render_template(template_name, context)

        if html_body is None:
            log.warningx(
                "Template mail versturen overgeslagen: template render gaf geen resultaat",
                subject=subject,
                template_name=template_name,
            )
            return

        log.debugx(
            "Template mail HTML body gerenderd",
            subject=subject,
            template_name=template_name,
            html_length=len(html_body),
        )

        plain_text = context.get("message", subject)
        log.debugx(
            "Template mail plain text bepaald",
            subject=subject,
            plain_text_length=len(str(plain_text)),
        )

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr(
            (
                mail_settings.from_name or "",
                mail_settings.from_email,
            )
        )
        message["To"] = ", ".join(to)

        if cc:
            message["Cc"] = ", ".join(cc)
            log.debugx(
                "Template mail CC ingesteld",
                subject=subject,
                cc_count=len(cc),
            )

        if bcc:
            message["Bcc"] = ", ".join(bcc)
            log.debugx(
                "Template mail BCC ingesteld",
                subject=subject,
                bcc_count=len(bcc),
            )

        message.set_content(str(plain_text))
        message.add_alternative(html_body, subtype="html")
        log.debugx(
            "Template mail bericht opgebouwd",
            subject=subject,
            from_email=mail_settings.from_email,
            to_count=len(to),
            cc_count=len(cc or []),
            bcc_count=len(bcc or []),
        )

        recipients = [
            *to,
            *(cc or []),
            *(bcc or []),
        ]
        log.infox(
            "Template mail verzenden via SMTP",
            subject=subject,
            recipient_count=len(recipients),
            smtp_host=mail_settings.smtp_host,
            smtp_port=mail_settings.smtp_port,
            use_ssl=mail_settings.use_ssl,
            use_tls=mail_settings.use_tls,
        )

        self._send_with_smtp(
            settings=mail_settings,
            message=message,
            recipients=recipients,
        )

        log.infox(
            "Template mail succesvol verstuurd",
            subject=subject,
            template_name=template_name,
            recipient_count=len(recipients),
        )

    def _render_template(self, template_name: str, context: dict) -> str | None:
        log.debugx(
            "Mailtemplate renderen gestart",
            template_name=template_name,
            context_keys=list(context.keys()) if isinstance(context, dict) else None,
        )
        try:
            template = self.jinja_env.get_template(template_name)
            log.debugx(
                "Mailtemplate gevonden",
                template_name=template_name,
            )
        except Exception:
            logger.exception(f"Mailtemplate '{template_name}' niet gevonden.")
            log.errorx(
                "Mailtemplate niet gevonden",
                template_name=template_name,
            )
            return None

        result = template.render(**context)
        log.debugx(
            "Mailtemplate renderen afgerond",
            template_name=template_name,
            result_length=len(result),
        )
        return result

    def _send_with_smtp(
        self,
        settings: MailSettings,
        message: EmailMessage,
        recipients: list[str],
    ) -> None:
        log.infox(
            "SMTP mail versturen gestart",
            smtp_host=settings.smtp_host,
            smtp_port=settings.smtp_port,
            use_ssl=settings.use_ssl,
            use_tls=settings.use_tls,
            smtp_username=settings.smtp_username,
            from_email=settings.from_email,
            recipient_count=len(recipients or []),
            subject=message.get("Subject"),
        )
        password = decrypt_value(settings.smtp_password)
        log.debugx(
            "SMTP wachtwoord succesvol ontsleuteld",
            smtp_host=settings.smtp_host,
            smtp_username=settings.smtp_username,
            has_password=bool(password),
        )

        try:
            if settings.use_ssl:
                log.debugx(
                    "SMTP SSL verbinding openen",
                    smtp_host=settings.smtp_host,
                    smtp_port=settings.smtp_port,
                    timeout=30,
                )
                with smtplib.SMTP_SSL(
                    settings.smtp_host,
                    settings.smtp_port,
                    timeout=30,
                ) as smtp:
                    log.debugx(
                        "SMTP SSL login uitvoeren",
                        smtp_host=settings.smtp_host,
                        smtp_username=settings.smtp_username,
                    )
                    smtp.login(settings.smtp_username, password)
                    log.debugx(
                        "SMTP SSL bericht verzenden",
                        smtp_host=settings.smtp_host,
                        recipient_count=len(recipients or []),
                    )
                    smtp.send_message(message, to_addrs=recipients)
            else:
                log.debugx(
                    "SMTP verbinding openen",
                    smtp_host=settings.smtp_host,
                    smtp_port=settings.smtp_port,
                    timeout=30,
                    use_tls=settings.use_tls,
                )
                with smtplib.SMTP(
                    settings.smtp_host,
                    settings.smtp_port,
                    timeout=30,
                ) as smtp:
                    if settings.use_tls:
                        log.debugx(
                            "SMTP STARTTLS uitvoeren",
                            smtp_host=settings.smtp_host,
                            smtp_port=settings.smtp_port,
                        )
                        smtp.starttls()

                    log.debugx(
                        "SMTP login uitvoeren",
                        smtp_host=settings.smtp_host,
                        smtp_username=settings.smtp_username,
                    )
                    smtp.login(settings.smtp_username, password)
                    log.debugx(
                        "SMTP bericht verzenden",
                        smtp_host=settings.smtp_host,
                        recipient_count=len(recipients or []),
                    )
                    smtp.send_message(message, to_addrs=recipients)

            log.infox(
                "SMTP mail succesvol verstuurd",
                smtp_host=settings.smtp_host,
                smtp_port=settings.smtp_port,
                recipient_count=len(recipients or []),
                subject=message.get("Subject"),
            )

        except smtplib.SMTPAuthenticationError:
            log.errorx(
                "SMTP mail versturen mislukt: authenticatie mislukt",
                smtp_host=settings.smtp_host,
                smtp_port=settings.smtp_port,
                smtp_username=settings.smtp_username,
                subject=message.get("Subject"),
            )
            raise HTTPException(
                status_code=500,
                detail="Mail versturen mislukt: authenticatie bij SMTP-server mislukt.",
            )
        except smtplib.SMTPConnectError:
            log.errorx(
                "SMTP mail versturen mislukt: verbinden met server mislukt",
                smtp_host=settings.smtp_host,
                smtp_port=settings.smtp_port,
                subject=message.get("Subject"),
            )
            raise HTTPException(
                status_code=500,
                detail="Mail versturen mislukt: verbinden met SMTP-server mislukt.",
            )
        except smtplib.SMTPException as exc:
            log.errorx(
                "SMTP mail versturen mislukt met SMTPException",
                smtp_host=settings.smtp_host,
                smtp_port=settings.smtp_port,
                subject=message.get("Subject"),
                error=str(exc),
            )
            raise HTTPException(
                status_code=500,
                detail=f"Mail versturen mislukt: {str(exc)}",
            )

    def send_test_mail(
            self,
            settings: MailSettings,
            to_email: str,
    ) -> None:
        log.infox(
            "Testmail versturen gestart",
            to_email=to_email,
            smtp_host=settings.smtp_host,
            smtp_port=settings.smtp_port,
            from_email=settings.from_email,
            use_ssl=settings.use_ssl,
            use_tls=settings.use_tls,
        )
        html_body = self._render_raw_test_template(settings)
        log.debugx(
            "Testmail HTML body opgebouwd",
            to_email=to_email,
            html_length=len(html_body),
        )

        message = EmailMessage()
        message["Subject"] = "Testmail vanuit mailinstellingen"
        message["From"] = formataddr(
            (
                settings.from_name or "",
                settings.from_email,
            )
        )
        message["To"] = to_email

        message.set_content(
            "Dit is een testmail. Als je deze ontvangt, werkt de SMTP-configuratie."
        )
        message.add_alternative(html_body, subtype="html")
        log.debugx(
            "Testmail bericht opgebouwd",
            to_email=to_email,
            from_email=settings.from_email,
            subject=message.get("Subject"),
        )

        password = decrypt_value(settings.smtp_password)
        log.debugx(
            "SMTP wachtwoord voor testmail succesvol ontsleuteld",
            smtp_host=settings.smtp_host,
            smtp_username=settings.smtp_username,
            has_password=bool(password),
        )

        try:
            if settings.use_ssl:
                log.debugx(
                    "SMTP SSL verbinding openen voor testmail",
                    smtp_host=settings.smtp_host,
                    smtp_port=settings.smtp_port,
                    timeout=30,
                )
                with smtplib.SMTP_SSL(
                        settings.smtp_host,
                        settings.smtp_port,
                        timeout=30,
                ) as smtp:
                    log.debugx(
                        "SMTP SSL login uitvoeren voor testmail",
                        smtp_host=settings.smtp_host,
                        smtp_username=settings.smtp_username,
                    )
                    smtp.login(settings.smtp_username, password)
                    log.debugx(
                        "SMTP SSL testmail verzenden",
                        smtp_host=settings.smtp_host,
                        to_email=to_email,
                    )
                    smtp.send_message(message, to_addrs=[to_email])
            else:
                log.debugx(
                    "SMTP verbinding openen voor testmail",
                    smtp_host=settings.smtp_host,
                    smtp_port=settings.smtp_port,
                    timeout=30,
                    use_tls=settings.use_tls,
                )
                with smtplib.SMTP(
                        settings.smtp_host,
                        settings.smtp_port,
                        timeout=30,
                ) as smtp:
                    if settings.use_tls:
                        log.debugx(
                            "SMTP STARTTLS uitvoeren voor testmail",
                            smtp_host=settings.smtp_host,
                            smtp_port=settings.smtp_port,
                        )
                        smtp.starttls()

                    log.debugx(
                        "SMTP login uitvoeren voor testmail",
                        smtp_host=settings.smtp_host,
                        smtp_username=settings.smtp_username,
                    )
                    smtp.login(settings.smtp_username, password)
                    log.debugx(
                        "SMTP testmail verzenden",
                        smtp_host=settings.smtp_host,
                        to_email=to_email,
                    )
                    smtp.send_message(message, to_addrs=[to_email])

            log.infox(
                "Testmail succesvol verstuurd",
                to_email=to_email,
                smtp_host=settings.smtp_host,
                smtp_port=settings.smtp_port,
            )

        except smtplib.SMTPAuthenticationError:
            log.errorx(
                "Testmail versturen mislukt: authenticatie mislukt",
                to_email=to_email,
                smtp_host=settings.smtp_host,
                smtp_port=settings.smtp_port,
                smtp_username=settings.smtp_username,
            )
            raise HTTPException(
                status_code=500,
                detail="Testmail versturen mislukt: authenticatie bij SMTP-server mislukt.",
            )
        except smtplib.SMTPConnectError:
            log.errorx(
                "Testmail versturen mislukt: verbinden met SMTP-server mislukt",
                to_email=to_email,
                smtp_host=settings.smtp_host,
                smtp_port=settings.smtp_port,
            )
            raise HTTPException(
                status_code=500,
                detail="Testmail versturen mislukt: verbinden met SMTP-server mislukt.",
            )
        except smtplib.SMTPException as exc:
            log.errorx(
                "Testmail versturen mislukt met SMTPException",
                to_email=to_email,
                smtp_host=settings.smtp_host,
                smtp_port=settings.smtp_port,
                error=str(exc),
            )
            raise HTTPException(
                status_code=500,
                detail=f"Testmail versturen mislukt: {str(exc)}",
            )

    def _render_raw_test_template(self, settings: MailSettings) -> str:
        log.debugx(
            "Raw testmail template renderen gestart",
            smtp_host=settings.smtp_host,
            smtp_port=settings.smtp_port,
            from_email=settings.from_email,
        )
        return f"""
        <!DOCTYPE html>
        <html lang="nl">
        <body style="margin:0;padding:0;background-color:#f4f6f8;font-family:Arial,sans-serif;">
            <table width="100%" cellpadding="0" cellspacing="0" style="padding:40px 0;">
                <tr>
                    <td align="center">
                        <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;">
                            <tr>
                                <td style="background:#111827;padding:32px;text-align:center;">
                                    <h1 style="color:#ffffff;margin:0;font-size:24px;">
                                        Testmail succesvol verzonden
                                    </h1>
                                </td>
                            </tr>
                            <tr>
                                <td style="padding:32px;color:#374151;font-size:16px;line-height:1.6;">
                                    <p>Deze e-mail bevestigt dat je SMTP-configuratie werkt.</p>

                                    <table width="100%" cellpadding="8" cellspacing="0" style="margin-top:20px;border-collapse:collapse;">
                                        <tr>
                                            <td><strong>SMTP host</strong></td>
                                            <td>{settings.smtp_host}</td>
                                        </tr>
                                        <tr>
                                            <td><strong>SMTP port</strong></td>
                                            <td>{settings.smtp_port}</td>
                                        </tr>
                                        <tr>
                                            <td><strong>Afzender</strong></td>
                                            <td>{settings.from_email}</td>
                                        </tr>
                                    </table>

                                    <p style="margin-top:24px;">
                                        Als je deze mail ontvangt, kan de backend e-mails versturen met deze instellingen.
                                    </p>
                                </td>
                            </tr>
                            <tr>
                                <td style="padding:24px 32px;background:#f9fafb;color:#6b7280;font-size:13px;text-align:center;">
                                    Automatisch verzonden vanuit de mail settings testfunctie.
                                </td>
                            </tr>
                        </table>
                    </td>
                </tr>
            </table>
        </body>
        </html>
        """