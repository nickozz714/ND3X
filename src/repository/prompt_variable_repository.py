from __future__ import annotations

from typing import Iterable, List, Optional

from sqlalchemy.orm import Session

from models.prompt_variable import PromptVariable
from schemas.prompt_variable import PromptVariableCreate, PromptVariableUpdate


class PromptVariableRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_all(self, skip: int = 0, limit: int = 100) -> List[PromptVariable]:
        return (
            self.db.query(PromptVariable)
            .order_by(PromptVariable.id.asc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def get_by_id(self, prompt_variable_id: int) -> Optional[PromptVariable]:
        return (
            self.db.query(PromptVariable)
            .filter(PromptVariable.id == prompt_variable_id)
            .first()
        )

    def get_by_token(self, token: str) -> Optional[PromptVariable]:
        return (
            self.db.query(PromptVariable)
            .filter(PromptVariable.token == token)
            .first()
        )

    def get_enabled_by_tokens(self, tokens: Iterable[str]) -> List[PromptVariable]:
        token_list = list(tokens)

        if not token_list:
            return []

        return (
            self.db.query(PromptVariable)
            .filter(PromptVariable.token.in_(token_list))
            .filter(PromptVariable.is_enabled.is_(True))
            .all()
        )

    def create(self, data: PromptVariableCreate) -> PromptVariable:
        item = PromptVariable(
            token=data.token,
            code=data.code,
            is_enabled=data.is_enabled,
            timeout_ms=data.timeout_ms,
        )

        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)

        return item

    def update(
        self,
        prompt_variable_id: int,
        data: PromptVariableUpdate,
    ) -> Optional[PromptVariable]:
        item = self.get_by_id(prompt_variable_id)

        if not item:
            return None

        update_data = data.model_dump(exclude_unset=True)

        for key, value in update_data.items():
            setattr(item, key, value)

        self.db.commit()
        self.db.refresh(item)

        return item

    def delete(self, prompt_variable_id: int) -> bool:
        item = self.get_by_id(prompt_variable_id)

        if not item:
            return False

        self.db.delete(item)
        self.db.commit()

        return True