from typing import Optional
from sqlalchemy import select
from sqlalchemy.orm import Session
from models.authenticate import User

class UserRepository:
    def get_by_email(self, db: Session, email: str) -> Optional[User]:
        return db.scalars(select(User).where(User.email == email)).first()

    def get_by_id(self, db: Session, user_id: int) -> Optional[User]:
        return db.get(User, user_id)


    def get_all(self, db: Session):
        return db.scalars(select(User)).all()

    def update_roles(self, db: Session, user_id: int, roles: list[str]) -> Optional[User]:
        user = self.get_by_id(db, user_id)
        if not user:
            return None
        user.roles = roles
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    def count_admins(self, db: Session) -> int:
        users = self.get_all(db)
        out = 0
        for u in users:
            roles = getattr(u, "roles", None) or []
            if any(str(r).lower()=="admin" for r in roles):
                out += 1
        return out
