from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from repository.application_setting_repository import ApplicationSettingRepository
from schemas.application_settings import ApplicationSettingCreate

def _env_bool(val: str, default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")

class ApplicationSettingService:
    def __init__(self, db: Session):
        self.repository = ApplicationSettingRepository(db)

    def get_from_code(self, key: str, is_bool: bool = False, is_numeric: bool = False) -> str | bool:
        """
        Gebruik deze methode vanuit de code.

        Als de setting niet bestaat, wordt deze aangemaakt
        met een mock waarde.
        """
        setting = self.repository.get_by_key(key)

        if setting:
            if is_bool:
                return _env_bool(setting.value)
            return setting.value

        setting = self.repository.create(
            ApplicationSettingCreate(
                key=key,
                value="True" if is_bool else "1" if is_numeric else "Mock",
            )
        )

        return setting.value

    def get_by_key(self, key: str):
        """
        Gebruik deze methode vanuit de router.

        Geeft alleen een waarde terug als die bestaat.
        """
        setting = self.repository.get_by_key(key)

        if not setting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Application setting not found",
            )

        return setting

    def get_all(self):
        return self.repository.get_all()

    def create(self, data: ApplicationSettingCreate):
        existing_setting = self.repository.get_by_key(data.key)

        if existing_setting:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Application setting already exists",
            )

        return self.repository.create(data)

    def update(self, key: str, value: str):
        setting = self.repository.get_by_key(key)

        if not setting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Application setting not found",
            )

        return self.repository.update(setting, value)

    def delete(self, key: str) -> None:
        setting = self.repository.get_by_key(key)

        if not setting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Application setting not found",
            )

        self.repository.delete(setting)