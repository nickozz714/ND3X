"""Organization administration API (multi-tenancy phase 5).

Everything acts within the caller's org context (require_org). Org-admin rights
= org_role in {owner, admin}. The org switcher re-issues the JWT with the new
org claim after a membership check.
"""
from __future__ import annotations

import secrets as _secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from authentication.dependencies import require_org
from db.database import get_db
from models.authenticate import User
from models.tenancy import (
    ORG_ROLES, Organization, OrgInvite, OrgMembership, ProjectMember,
    ProjectSkill, ProjectTool, Team, TeamMembership,
)

router = APIRouter(prefix="/orgs", tags=["organizations"])


def _require_org_admin(ctx) -> None:
    if ctx.org_role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Org admin only")


def _member_dict(db: Session, m: OrgMembership) -> dict:
    u = db.get(User, m.user_id)
    return {"user_id": m.user_id, "email": getattr(u, "email", "?"),
            "role": m.role, "is_active": bool(getattr(u, "is_active", True))}


# ── me / switch ───────────────────────────────────────────────────────────────

@router.get("/me")
def my_orgs(ctx=Depends(require_org), db: Session = Depends(get_db)):
    """Current org + every org the caller can switch to (drives the switcher)."""
    rows = (
        db.query(OrgMembership, Organization)
        .join(Organization, Organization.id == OrgMembership.org_id)
        .filter(OrgMembership.user_id == ctx.user_id, Organization.is_active.is_(True))
        .order_by(Organization.id)
        .all()
    )
    from models.assistant_thread import AssistantProjectModel
    personal = (
        db.query(AssistantProjectModel)
        .join(ProjectMember, ProjectMember.project_id == AssistantProjectModel.id)
        .filter(AssistantProjectModel.org_id == ctx.org_id,
                AssistantProjectModel.domain == "personal",
                ProjectMember.user_id == ctx.user_id)
        .first()
    )
    return {
        "active_org_id": ctx.org_id,
        "org_role": ctx.org_role,
        "personal_project_id": personal.id if personal else None,
        "orgs": [{"id": o.id, "name": o.name, "slug": o.slug, "role": m.role} for m, o in rows],
    }


class SwitchIn(BaseModel):
    org_id: int


@router.post("/switch")
def switch_org(body: SwitchIn, ctx=Depends(require_org), db: Session = Depends(get_db)):
    """Re-issue the access token for another org the caller belongs to."""
    m = (db.query(OrgMembership)
         .filter(OrgMembership.user_id == ctx.user_id, OrgMembership.org_id == body.org_id)
         .first())
    if not m:
        raise HTTPException(status_code=403, detail="Not a member of that organization")
    from services.auth_service import make_access_token
    token = make_access_token(ctx.user_id, ctx.email, ctx.roles, org_id=body.org_id)
    return {"access_token": token, "token_type": "bearer", "org_id": body.org_id}


# ── org + members ─────────────────────────────────────────────────────────────

class OrgCreate(BaseModel):
    name: str
    slug: str


@router.post("")
def create_org(body: OrgCreate, ctx=Depends(require_org), db: Session = Depends(get_db)):
    """Create a new organization; the creator becomes its owner."""
    slug = body.slug.strip().lower()
    if db.query(Organization).filter(Organization.slug == slug).first():
        raise HTTPException(status_code=409, detail="Slug already in use")
    org = Organization(name=body.name.strip(), slug=slug)
    db.add(org); db.commit(); db.refresh(org)
    db.add(OrgMembership(org_id=org.id, user_id=ctx.user_id, role="owner")); db.commit()
    return {"id": org.id, "name": org.name, "slug": org.slug}


@router.get("/members")
def list_members(ctx=Depends(require_org), db: Session = Depends(get_db)):
    ms = db.query(OrgMembership).filter(OrgMembership.org_id == ctx.org_id).order_by(OrgMembership.id).all()
    return [_member_dict(db, m) for m in ms]


class MemberIn(BaseModel):
    email: str
    role: str = "member"


@router.post("/members")
def add_member(body: MemberIn, ctx=Depends(require_org), db: Session = Depends(get_db)):
    """Add an EXISTING user to this org by email; unknown emails get an invite."""
    _require_org_admin(ctx)
    if body.role not in ORG_ROLES:
        raise HTTPException(status_code=400, detail=f"Role must be one of {ORG_ROLES}")
    u = db.query(User).filter(User.email == body.email.strip().lower()).first()
    if not u:
        inv = OrgInvite(org_id=ctx.org_id, email=body.email.strip().lower(),
                        role=body.role, token=_secrets.token_urlsafe(24))
        db.add(inv); db.commit()
        return {"invited": True, "email": inv.email, "token": inv.token}
    if db.query(OrgMembership).filter(OrgMembership.org_id == ctx.org_id,
                                      OrgMembership.user_id == u.id).first():
        raise HTTPException(status_code=409, detail="Already a member")
    db.add(OrgMembership(org_id=ctx.org_id, user_id=u.id, role=body.role)); db.commit()
    return {"invited": False, "user_id": u.id, "email": u.email, "role": body.role}


class RoleIn(BaseModel):
    role: str


@router.patch("/members/{user_id}")
def set_member_role(user_id: int, body: RoleIn, ctx=Depends(require_org), db: Session = Depends(get_db)):
    _require_org_admin(ctx)
    if body.role not in ORG_ROLES:
        raise HTTPException(status_code=400, detail=f"Role must be one of {ORG_ROLES}")
    m = db.query(OrgMembership).filter(OrgMembership.org_id == ctx.org_id,
                                       OrgMembership.user_id == user_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Not a member")
    # Never demote the last owner (lockout guard).
    if m.role == "owner" and body.role != "owner":
        owners = db.query(OrgMembership).filter(OrgMembership.org_id == ctx.org_id,
                                                OrgMembership.role == "owner").count()
        if owners <= 1:
            raise HTTPException(status_code=400, detail="Cannot demote the last owner")
    m.role = body.role
    db.commit()
    return _member_dict(db, m)


@router.delete("/members/{user_id}")
def remove_member(user_id: int, ctx=Depends(require_org), db: Session = Depends(get_db)):
    _require_org_admin(ctx)
    m = db.query(OrgMembership).filter(OrgMembership.org_id == ctx.org_id,
                                       OrgMembership.user_id == user_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Not a member")
    if m.role == "owner":
        owners = db.query(OrgMembership).filter(OrgMembership.org_id == ctx.org_id,
                                                OrgMembership.role == "owner").count()
        if owners <= 1:
            raise HTTPException(status_code=400, detail="Cannot remove the last owner")
    db.delete(m); db.commit()
    return {"removed": True}


# ── teams ─────────────────────────────────────────────────────────────────────

class TeamIn(BaseModel):
    name: str
    description: Optional[str] = None


@router.get("/teams")
def list_teams(ctx=Depends(require_org), db: Session = Depends(get_db)):
    teams = db.query(Team).filter(Team.org_id == ctx.org_id).order_by(Team.name).all()
    out = []
    for t in teams:
        ms = db.query(TeamMembership).filter(TeamMembership.team_id == t.id).all()
        out.append({"id": t.id, "name": t.name, "description": t.description,
                    "members": [{"user_id": m.user_id, "role": m.role} for m in ms]})
    return out


@router.post("/teams")
def create_team(body: TeamIn, ctx=Depends(require_org), db: Session = Depends(get_db)):
    _require_org_admin(ctx)
    if db.query(Team).filter(Team.org_id == ctx.org_id, Team.name == body.name.strip()).first():
        raise HTTPException(status_code=409, detail="Team name already exists")
    t = Team(org_id=ctx.org_id, name=body.name.strip(), description=body.description)
    db.add(t); db.commit(); db.refresh(t)
    return {"id": t.id, "name": t.name}


class TeamMemberIn(BaseModel):
    user_id: int
    role: str = "member"


@router.post("/teams/{team_id}/members")
def add_team_member(team_id: int, body: TeamMemberIn, ctx=Depends(require_org), db: Session = Depends(get_db)):
    _require_org_admin(ctx)
    t = db.query(Team).filter(Team.id == team_id, Team.org_id == ctx.org_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Team not found")
    # Team members must be org members.
    if not db.query(OrgMembership).filter(OrgMembership.org_id == ctx.org_id,
                                          OrgMembership.user_id == body.user_id).first():
        raise HTTPException(status_code=400, detail="User is not a member of this organization")
    if db.query(TeamMembership).filter(TeamMembership.team_id == team_id,
                                       TeamMembership.user_id == body.user_id).first():
        raise HTTPException(status_code=409, detail="Already in team")
    db.add(TeamMembership(team_id=team_id, user_id=body.user_id, role=body.role)); db.commit()
    return {"added": True}


@router.delete("/teams/{team_id}/members/{user_id}")
def remove_team_member(team_id: int, user_id: int, ctx=Depends(require_org), db: Session = Depends(get_db)):
    _require_org_admin(ctx)
    t = db.query(Team).filter(Team.id == team_id, Team.org_id == ctx.org_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Team not found")
    m = db.query(TeamMembership).filter(TeamMembership.team_id == team_id,
                                        TeamMembership.user_id == user_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Not in team")
    db.delete(m); db.commit()
    return {"removed": True}


# ── projects ──────────────────────────────────────────────────────────────────

class ProjectIn(BaseModel):
    name: str
    description: Optional[str] = None


@router.get("/projects")
def list_projects(ctx=Depends(require_org), db: Session = Depends(get_db)):
    """This org's projects, with member/grant counts for the admin overview."""
    from models.assistant_thread import AssistantProjectModel
    rows = (
        db.query(AssistantProjectModel)
        .filter((AssistantProjectModel.org_id == ctx.org_id) | (AssistantProjectModel.org_id.is_(None)))
        .order_by(AssistantProjectModel.name)
        .all()
    )
    out = []
    for pr in rows:
        members = db.query(ProjectMember).filter(ProjectMember.project_id == pr.id).count()
        grants = db.query(ProjectSkill).filter(ProjectSkill.project_id == pr.id).count()
        out.append({
            "id": pr.id, "name": pr.name, "description": pr.description,
            "status": getattr(pr, "status", "active"),
            "is_archived": bool(getattr(pr, "is_archived", False)),
            "member_count": members, "grant_count": grants,
        })
    return out


@router.post("/projects")
def create_project(body: ProjectIn, ctx=Depends(require_org), db: Session = Depends(get_db)):
    """Create a project in this org; the creator becomes project lead."""
    import uuid as _uuid
    from datetime import datetime, timezone
    from models.assistant_thread import AssistantProjectModel
    now = datetime.now(timezone.utc).isoformat()
    pr = AssistantProjectModel(
        id=str(_uuid.uuid4()), name=body.name.strip(), description=body.description,
        org_id=ctx.org_id, created_at=now, updated_at=now,
    )
    db.add(pr); db.commit()
    db.add(ProjectMember(project_id=pr.id, user_id=ctx.user_id, role="lead")); db.commit()
    return {"id": pr.id, "name": pr.name}


# ── project members & capability grants ──────────────────────────────────────

def _project_in_org(db: Session, ctx, project_id: str):
    from models.assistant_thread import AssistantProjectModel
    p = db.get(AssistantProjectModel, project_id)
    if not p or (p.org_id is not None and p.org_id != ctx.org_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return p


@router.get("/projects/{project_id}/access")
def project_access(project_id: str, ctx=Depends(require_org), db: Session = Depends(get_db)):
    """Members + skill/tool grants of a project, for the grants editor."""
    _project_in_org(db, ctx, project_id)
    members = db.query(ProjectMember).filter(ProjectMember.project_id == project_id).all()
    skills = db.query(ProjectSkill).filter(ProjectSkill.project_id == project_id).all()
    tools = db.query(ProjectTool).filter(ProjectTool.project_id == project_id).all()
    return {
        "members": [{"user_id": m.user_id, "role": m.role} for m in members],
        "skill_ids": [s.skill_id for s in skills],
        "tool_ids": [t.tool_id for t in tools],
    }


class ProjectMemberIn(BaseModel):
    user_id: int
    role: str = "member"


@router.post("/projects/{project_id}/members")
def add_project_member(project_id: str, body: ProjectMemberIn, ctx=Depends(require_org), db: Session = Depends(get_db)):
    _require_org_admin(ctx)
    _project_in_org(db, ctx, project_id)
    if not db.query(OrgMembership).filter(OrgMembership.org_id == ctx.org_id,
                                          OrgMembership.user_id == body.user_id).first():
        raise HTTPException(status_code=400, detail="User is not a member of this organization")
    if db.query(ProjectMember).filter(ProjectMember.project_id == project_id,
                                      ProjectMember.user_id == body.user_id).first():
        raise HTTPException(status_code=409, detail="Already a project member")
    db.add(ProjectMember(project_id=project_id, user_id=body.user_id, role=body.role)); db.commit()
    return {"added": True}


@router.delete("/projects/{project_id}/members/{user_id}")
def remove_project_member(project_id: str, user_id: int, ctx=Depends(require_org), db: Session = Depends(get_db)):
    _require_org_admin(ctx)
    _project_in_org(db, ctx, project_id)
    m = db.query(ProjectMember).filter(ProjectMember.project_id == project_id,
                                       ProjectMember.user_id == user_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Not a project member")
    db.delete(m); db.commit()
    return {"removed": True}


class GrantsIn(BaseModel):
    skill_ids: Optional[list[int]] = None
    tool_ids: Optional[list[int]] = None


@router.put("/projects/{project_id}/grants")
def set_project_grants(project_id: str, body: GrantsIn, ctx=Depends(require_org), db: Session = Depends(get_db)):
    """Replace the project's capability grants. Empty list = explicit lockdown to
    nothing; omit a field to leave that grant type unchanged. No grants at all =
    unrestricted (phase-4 default-open)."""
    _require_org_admin(ctx)
    _project_in_org(db, ctx, project_id)
    if body.skill_ids is not None:
        db.query(ProjectSkill).filter(ProjectSkill.project_id == project_id).delete()
        for sid in set(body.skill_ids):
            db.add(ProjectSkill(project_id=project_id, skill_id=sid))
    if body.tool_ids is not None:
        db.query(ProjectTool).filter(ProjectTool.project_id == project_id).delete()
        for tid in set(body.tool_ids):
            db.add(ProjectTool(project_id=project_id, tool_id=tid))
    db.commit()
    return project_access(project_id, ctx, db)
