from pydantic import BaseModel, EmailStr, Field


class MailSettingsCreate(BaseModel):
    name: str = "Default SMTP"

    smtp_host: str
    smtp_port: int = 587

    smtp_username: str
    smtp_password: str

    from_email: EmailStr
    from_name: str | None = None

    use_tls: bool = True
    use_ssl: bool = False
    is_active: bool = True


class MailSettingsUpdate(BaseModel):
    name: str | None = None

    smtp_host: str | None = None
    smtp_port: int | None = None

    smtp_username: str | None = None
    smtp_password: str | None = None

    from_email: EmailStr | None = None
    from_name: str | None = None

    use_tls: bool | None = None
    use_ssl: bool | None = None
    is_active: bool | None = None


class MailSettingsResponse(BaseModel):
    id: int
    name: str

    smtp_host: str
    smtp_port: int

    smtp_username: str

    from_email: EmailStr
    from_name: str | None

    use_tls: bool
    use_ssl: bool
    is_active: bool

    class Config:
        from_attributes = True


class SendMailRequest(BaseModel):
    to: list[EmailStr]
    subject: str
    title: str | None = None
    message: str

    cc: list[EmailStr] = Field(default_factory=list)
    bcc: list[EmailStr] = Field(default_factory=list)

    template_name: str = "default_mail.html"

class SendTestMailRequest(BaseModel):
    to_email: EmailStr