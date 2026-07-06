from sqlalchemy.orm import Session

from models.application_settings import ApplicationSetting
from schemas.application_settings import ApplicationSettingCreate


class ApplicationSettingRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_key(self, key: str) -> ApplicationSetting | None:
        return (
            self.db.query(ApplicationSetting)
            .filter(ApplicationSetting.key == key)
            .first()
        )

    def get_all(self) -> list[ApplicationSetting]:
        return self.db.query(ApplicationSetting).all()

    def create(self, setting: ApplicationSettingCreate) -> ApplicationSetting:
        db_setting = ApplicationSetting(
            key=setting.key,
            value=setting.value,
        )

        self.db.add(db_setting)
        self.db.commit()
        self.db.refresh(db_setting)

        return db_setting

    def update(self, db_setting: ApplicationSetting, value: str) -> ApplicationSetting:
        db_setting.value = value

        self.db.commit()
        self.db.refresh(db_setting)

        return db_setting

    def delete(self, db_setting: ApplicationSetting) -> None:
        self.db.delete(db_setting)
        self.db.commit()