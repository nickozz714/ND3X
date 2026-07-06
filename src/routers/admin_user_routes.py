from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from authentication.dependencies import require_admin_user
from db.database import get_db
from schemas.user_admin import UserAdminResponse, UserRoleUpdateRequest
from services.user_role_service import UserRoleService

router = APIRouter(prefix="/admin/users", tags=["Admin Users"])


@router.get("", response_model=list[UserAdminResponse])
def list_users(db: Session = Depends(get_db), _=Depends(require_admin_user)):
    return UserRoleService(db).list_users()


@router.patch("/{user_id}/roles", response_model=UserAdminResponse)
def patch_user_roles(user_id: int, data: UserRoleUpdateRequest, db: Session = Depends(get_db), _=Depends(require_admin_user)):
    return UserRoleService(db).update_roles(user_id, data.roles)
