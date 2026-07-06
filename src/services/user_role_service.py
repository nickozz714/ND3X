from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from repository.authenticate import UserRepository
from services.authz_service import normalize_roles


class UserRoleService:
    def __init__(self, db: Session):
        self.db = db
        self.repo = UserRepository()

    def list_users(self):
        users = self.repo.get_all(self.db)
        out = []
        for u in users:
            out.append({
                "id": u.id,
                "email": u.email,
                "is_active": bool(getattr(u, "is_active", True)),
                "roles": normalize_roles(getattr(u, "roles", None) or []),
            })
        return out

    def update_roles(self, user_id: int, roles: list[str]):
        normalized = normalize_roles(roles)
        user = self.repo.get_by_id(self.db, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        current = normalize_roles(getattr(user, "roles", None) or [])
        # last-admin safety
        if "Admin" in current and "Admin" not in normalized:
            if self.repo.count_admins(self.db) <= 1:
                raise HTTPException(status_code=400, detail="Cannot remove last Admin role")

        updated = self.repo.update_roles(self.db, user_id, normalized)
        return {
            "id": updated.id,
            "email": updated.email,
            "is_active": bool(getattr(updated, "is_active", True)),
            "roles": normalize_roles(getattr(updated, "roles", None) or []),
        }
