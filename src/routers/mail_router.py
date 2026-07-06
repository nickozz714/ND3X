# app/routers/mail_router.py

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from db.database import get_db  # pas aan naar jouw dependency
from repository.mail_settings_repository import MailSettingsRepository
from schemas.mail import (
    MailSettingsCreate,
    MailSettingsResponse,
    MailSettingsUpdate,
    SendMailRequest, SendTestMailRequest,
)
from services.mail.mail_service import MailService


router = APIRouter(
    prefix="/mail",
    tags=["Mail"],
)


@router.get("/settings", response_model=list[MailSettingsResponse])
def get_mail_settings(db: Session = Depends(get_db)):
    repository = MailSettingsRepository(db)
    return repository.get_all()


@router.get("/settings/active", response_model=MailSettingsResponse)
def get_active_mail_settings(db: Session = Depends(get_db)):
    repository = MailSettingsRepository(db)
    settings = repository.get_active()

    if not settings:
        raise HTTPException(
            status_code=404,
            detail="Geen actieve mailinstellingen gevonden.",
        )

    return settings


@router.post("/settings", response_model=MailSettingsResponse)
def create_mail_settings(
    data: MailSettingsCreate,
    db: Session = Depends(get_db),
):
    repository = MailSettingsRepository(db)
    return repository.create(data)


@router.patch("/settings/{settings_id}", response_model=MailSettingsResponse)
def update_mail_settings(
    settings_id: int,
    data: MailSettingsUpdate,
    db: Session = Depends(get_db),
):
    repository = MailSettingsRepository(db)
    settings = repository.update(settings_id, data)

    if not settings:
        raise HTTPException(
            status_code=404,
            detail="Mailinstellingen niet gevonden.",
        )

    return settings


@router.delete("/settings/{settings_id}")
def delete_mail_settings(
    settings_id: int,
    db: Session = Depends(get_db),
):
    repository = MailSettingsRepository(db)
    deleted = repository.delete(settings_id)

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Mailinstellingen niet gevonden.",
        )

    return {"message": "Mailinstellingen verwijderd."}


@router.post("/send")
def send_mail(
    data: SendMailRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    service = MailService(db)

    background_tasks.add_task(service.send_mail, data)

    return {
        "message": "Mail wordt op de achtergrond verzonden.",
    }

@router.post("/settings/{settings_id}/test")
def send_test_mail(
    settings_id: int,
    data: SendTestMailRequest,
    db: Session = Depends(get_db),
):
    repository = MailSettingsRepository(db)
    settings = repository.get_by_id(settings_id)

    if not settings:
        raise HTTPException(
            status_code=404,
            detail="Mailinstellingen niet gevonden.",
        )

    service = MailService(db)
    service.send_test_mail(
        settings=settings,
        to_email=str(data.to_email),
    )

    return {
        "message": "Testmail succesvol verzonden.",
    }