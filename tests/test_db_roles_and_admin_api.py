from fastapi import HTTPException

from services.authz_service import normalize_roles, user_has_role, assert_admin_role, assert_expert_role
from services.user_role_service import UserRoleService


class DummyUser:
    def __init__(self, id, email, roles=None, is_active=True):
        self.id = id
        self.email = email
        self.roles = roles
        self.is_active = is_active


class DummyRepo:
    def __init__(self):
        self.users = {
            1: DummyUser(1, "a@x", ["Admin"]),
            2: DummyUser(2, "b@x", ["Expert"]),
            3: DummyUser(3, "c@x", None),
        }

    def get_all(self, db):
        return list(self.users.values())

    def get_by_id(self, db, user_id):
        return self.users.get(user_id)

    def update_roles(self, db, user_id, roles):
        self.users[user_id].roles = roles
        return self.users[user_id]

    def count_admins(self, db):
        return sum(1 for u in self.users.values() if "Admin" in (u.roles or []))


def test_role_semantics_and_normalization():
    assert user_has_role({"roles": []}, "Expert") is False
    assert user_has_role({"roles": ["Expert"]}, "Expert") is True
    assert user_has_role({"roles": ["Admin"]}, "Expert") is True
    assert user_has_role({"roles": ["Admin"]}, "Admin") is True
    assert user_has_role({"roles": ["Expert"]}, "Admin") is False
    assert normalize_roles([" expert ", "ADMIN", "user", "Admin"]) == ["Expert", "Admin", "User"]
    try:
        normalize_roles(["bad"])
        assert False
    except ValueError:
        assert True


def test_assertions_admin_expert():
    try:
        assert_expert_role({"roles": ["User"]})
        assert False
    except HTTPException as e:
        assert e.status_code == 403
    try:
        assert_admin_role({"roles": ["Expert"]})
        assert False
    except HTTPException as e:
        assert e.status_code == 403


def test_admin_role_service_update_and_last_admin_protection():
    svc = UserRoleService(db=None)
    svc.repo = DummyRepo()
    updated = svc.update_roles(2, ["User", "Expert"])
    assert updated["roles"] == ["User", "Expert"]
    try:
        svc.update_roles(1, ["User"])  # remove last admin
        assert False
    except HTTPException as e:
        assert e.status_code == 400
