from __future__ import annotations

from fastapi import HTTPException

ALLOWED_ROLES = {"User", "Expert", "Admin"}


def normalize_roles(roles: list[str] | None) -> list[str]:
    roles = roles or []
    canon = []
    seen = set()
    mapping = {"user": "User", "expert": "Expert", "admin": "Admin"}
    for role in roles:
        r = str(role or "").strip().lower()
        if not r:
            continue
        if r not in mapping:
            raise ValueError(f"Invalid role: {role}")
        c = mapping[r]
        if c not in seen:
            seen.add(c)
            canon.append(c)
    return canon


def user_has_role(user: dict | None, role: str) -> bool:
    if not user:
        return False
    roles = normalize_roles((user.get("roles") or []))
    requested = (role or "").strip().lower()
    if requested == "expert":
        return "Expert" in roles or "Admin" in roles
    if requested == "admin":
        return "Admin" in roles
    if requested == "user":
        return "User" in roles or "Expert" in roles or "Admin" in roles
    return False


def assert_expert_role(user: dict | None) -> None:
    if not user_has_role(user, "Expert"):
        raise HTTPException(status_code=403, detail="Expert role required for this action")


def assert_admin_role(user: dict | None) -> None:
    if not user_has_role(user, "Admin"):
        raise HTTPException(status_code=403, detail="Admin role required for this action")
