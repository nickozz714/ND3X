from __future__ import annotations

from sqlalchemy.orm import Session

from models.skill_file import SkillFile


class SkillFileRepository:
    def __init__(self, db: Session):
        self.db = db

    def list_for_skill(self, skill_id: int) -> list[SkillFile]:
        return (
            self.db.query(SkillFile)
            .filter(SkillFile.skill_id == skill_id)
            .order_by(SkillFile.relative_path.asc())
            .all()
        )

    def get_by_id(self, file_id: int) -> SkillFile | None:
        return self.db.query(SkillFile).filter(SkillFile.id == file_id).first()

    def get_for_skill_by_id(self, skill_id: int, file_id: int) -> SkillFile | None:
        return (
            self.db.query(SkillFile)
            .filter(SkillFile.skill_id == skill_id, SkillFile.id == file_id)
            .first()
        )

    def get_by_path(self, skill_id: int, relative_path: str) -> SkillFile | None:
        return (
            self.db.query(SkillFile)
            .filter(SkillFile.skill_id == skill_id, SkillFile.relative_path == relative_path)
            .first()
        )

    def create(self, **values) -> SkillFile:
        item = SkillFile(**values)
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def update(self, item: SkillFile, **values) -> SkillFile:
        for key, value in values.items():
            setattr(item, key, value)
        self.db.commit()
        self.db.refresh(item)
        return item

    def delete(self, item: SkillFile) -> None:
        self.db.delete(item)
        self.db.commit()
