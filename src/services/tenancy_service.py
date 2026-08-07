"""Tenancy resolution: which organization does this request act in?

Phase 2 of docs/MULTI-TENANCY.md. Designed to be MIGRATION-SAFE — nobody gets
locked out while old sessions/tokens are still around:

- Tokens issued before multi-tenancy carry no ``org`` claim → we fall back to the
  user's first membership (the Default Organization after the phase-1 backfill).
- A user without any membership (created in the window between backfill runs) is
  SELF-HEALED into the single existing org as a member, rather than rejected.
  Only when multiple orgs exist and the user belongs to none do we refuse.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from component.logging import get_logger
from models.tenancy import Organization, OrgMembership

log = get_logger(__name__)


@dataclass
class OrgContext:
    user_id: int
    email: str
    roles: list
    org_id: int
    org_role: str  # owner | admin | member


def resolve_membership(db: Session, user_id: int, org_id: int | None = None) -> OrgMembership | None:
    """The membership this user acts under: the requested org's if given, else the
    first (default) membership. Self-heals a missing membership when exactly one
    org exists — the no-lockout guarantee during migration."""
    q = db.query(OrgMembership).filter(OrgMembership.user_id == user_id)
    if org_id is not None:
        m = q.filter(OrgMembership.org_id == org_id).first()
        if m:
            return m
        # Requested an org they don't belong to → no fallback (that's the boundary).
        return None
    m = q.order_by(OrgMembership.id).first()
    if m:
        return m
    # No membership at all: self-heal into the only org, if there is only one.
    orgs = db.query(Organization).order_by(Organization.id).limit(2).all()
    if len(orgs) == 1:
        m = OrgMembership(org_id=orgs[0].id, user_id=user_id, role="member")
        db.add(m)
        db.commit()
        db.refresh(m)
        log.infox("Membership self-healed naar enige org", user_id=user_id, org_id=orgs[0].id)
        return m
    return None


def default_org_id(db: Session, user_id: int) -> int | None:
    """Org id to stamp into a fresh access token (None → claim omitted)."""
    m = resolve_membership(db, user_id)
    return m.org_id if m else None


def project_skill_grants(db: Session, project_id: str | None) -> list[str] | None:
    """Skill names granted to a project (phase 4). Returns None when the project
    has no grants configured — meaning NO restriction (default-open, so existing
    projects keep working until someone deliberately narrows them)."""
    if not project_id:
        return None
    from models.skill import Skill
    from models.tenancy import ProjectSkill

    rows = (
        db.query(Skill.name)
        .join(ProjectSkill, ProjectSkill.skill_id == Skill.id)
        .filter(ProjectSkill.project_id == project_id)
        .all()
    )
    names = [r[0] for r in rows]
    if not names:
        return None
    # System/runtime skills are the platform's contracts — they are always
    # available and can never be excluded by a project's grant set.
    sys_rows = (
        db.query(Skill.name)
        .filter((Skill.is_system.is_(True)) | (Skill.is_runtime.is_(True)))
        .all()
    )
    return sorted(set(names) | {r[0] for r in sys_rows})
