from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from db.database import get_db
from repository.authenticate import UserRepository
from services.auth_service import decode_access_token
from services.authz_service import normalize_roles, assert_admin_role
from component.logging import get_logger

users = UserRepository()
log = get_logger(__name__)

def require_user(request: Request, db: Session = Depends(get_db)):
    log.debugx(
        "Authenticatiecontrole gestart",
        path=str(request.url.path),
        method=request.method,
        has_authorization_header=bool(request.headers.get("Authorization")),
    )

    authz = request.headers.get("Authorization", "")
    if not authz.startswith("Bearer "):
        log.warningx(
            "Authenticatie mislukt: Authorization header ontbreekt of is geen Bearer token",
            path=str(request.url.path),
            method=request.method,
            has_authorization_header=bool(authz),
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    token = authz[7:]
    log.debugx(
        "Bearer token ontvangen, token wordt gedecodeerd",
        path=str(request.url.path),
        method=request.method,
        token_length=len(token),
    )

    try:
        data = decode_access_token(token)
        log.debugx(
            "Access token succesvol gedecodeerd",
            path=str(request.url.path),
            method=request.method,
            token_subject=data.get("sub"),
            roles=data.get("roles", []),
        )

        uid = int(data["sub"])
        log.debugx(
            "Gebruiker wordt opgehaald uit database",
            path=str(request.url.path),
            method=request.method,
            user_id=uid,
        )

        u = users.get_by_id(db, uid)
        if not u or not getattr(u, "is_active", True):
            log.warningx(
                "Authenticatie mislukt: gebruiker niet gevonden of niet actief",
                path=str(request.url.path),
                method=request.method,
                user_id=uid,
                user_found=bool(u),
                is_active=getattr(u, "is_active", None) if u else None,
            )
            raise HTTPException(status_code=401, detail="Invalid token")

        log.infox(
            "Gebruiker succesvol geauthenticeerd",
            path=str(request.url.path),
            method=request.method,
            user_id=u.id,
            email=u.email,
            roles=data.get("roles", []),
        )

        db_roles = normalize_roles(getattr(u, "roles", None) or [])
        # "org" = the token's org claim (None on pre-tenancy tokens; require_org
        # resolves the fallback so old sessions keep working).
        return {"id": u.id, "email": u.email, "roles": db_roles, "org": data.get("org")}
    except HTTPException:
        log.warningx(
            "Authenticatie afgebroken met HTTPException",
            path=str(request.url.path),
            method=request.method,
        )
        raise
    except Exception:
        log.errorx(
            "Authenticatie mislukt: token ongeldig of verlopen",
            path=str(request.url.path),
            method=request.method,
        )
        raise HTTPException(status_code=401, detail="Invalid or expired token")

def require_admin(user=Depends(require_user)):
    log.debugx(
        "Admincontrole gestart",
        user_id=user.get("id"),
        email=user.get("email"),
        roles=user.get("roles"),
    )

    if not any(str(r).lower()=="admin" for r in (user.get("roles") or [])):
        log.warningx(
            "Admincontrole mislukt: gebruiker heeft geen admin rol",
            user_id=user.get("id"),
            email=user.get("email"),
            roles=user.get("roles"),
        )
        raise HTTPException(status_code=403, detail="Admin only")

    log.infox(
        "Admincontrole succesvol",
        user_id=user.get("id"),
        email=user.get("email"),
        roles=user.get("roles"),
    )

    return user

def require_expert_user(user=Depends(require_user)):
    from services.authz_service import assert_expert_role
    assert_expert_role(user)
    return user

def require_admin_user(user=Depends(require_user)):
    assert_admin_role(user)
    return user


def require_org(request: Request, db: Session = Depends(get_db)):
    """Authenticated + resolved organization context (multi-tenancy phase 2).

    Org resolution is migration-safe: the token's ``org`` claim wins; a
    pre-tenancy token without the claim falls back to the user's default
    membership, and a membership-less user is self-healed into the single
    existing org (see tenancy_service). Only a claim for an org the user does
    NOT belong to — or no resolvable org at all — is refused."""
    user = require_user(request, db)
    from services.tenancy_service import OrgContext, resolve_membership

    claim = user.get("org")
    m = resolve_membership(db, user["id"], org_id=claim)
    if m is None and claim is not None:
        log.warningx(
            "Org-claim geweigerd: gebruiker is geen lid van deze organisatie",
            user_id=user["id"], org_id=claim, path=str(request.url.path),
        )
        raise HTTPException(status_code=403, detail="Not a member of this organization")
    if m is None:
        m = resolve_membership(db, user["id"])
    if m is None:
        raise HTTPException(status_code=403, detail="No organization membership")
    return OrgContext(
        user_id=user["id"], email=user["email"], roles=user["roles"],
        org_id=m.org_id, org_role=m.role,
    )


def require_project(request: Request, db: Session = Depends(get_db)):
    """Org context + the ACTIVE PROJECT from the X-ND3X-Project header (phase 6:
    project = workspace). Returns the OrgContext with a `project_id` attribute
    (None = no project selected → org-shared view). A header for a project
    outside the caller's org is refused."""
    ctx = require_org(request, db)
    project_id = (request.headers.get("X-ND3X-Project") or "").strip() or None
    from models.assistant_thread import AssistantProjectModel
    if project_id:
        p = db.get(AssistantProjectModel, project_id)
        if not p or (p.org_id is not None and p.org_id != ctx.org_id):
            raise HTTPException(status_code=403, detail="Project not in your organization")
    else:
        # No explicit project → the user's "Persoonlijk" project. There is no
        # project-less state: everyone always works inside a project.
        from models.tenancy import ProjectMember
        personal = (
            db.query(AssistantProjectModel)
            .join(ProjectMember, ProjectMember.project_id == AssistantProjectModel.id)
            .filter(AssistantProjectModel.org_id == ctx.org_id,
                    AssistantProjectModel.domain == "personal",
                    ProjectMember.user_id == ctx.user_id)
            .first()
        )
        project_id = personal.id if personal else None
    ctx.project_id = project_id
    return ctx
