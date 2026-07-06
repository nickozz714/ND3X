from pydantic import BaseModel


class UserRoleUpdateRequest(BaseModel):
    roles: list[str]


class UserAdminResponse(BaseModel):
    id: int
    email: str
    is_active: bool
    roles: list[str]
