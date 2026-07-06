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
        return {"id": u.id, "email": u.email, "roles": db_roles}
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
