from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from authentication.dependencies import require_user
from authentication import login_rate_limit
from component.logging import get_logger
from db.database import get_db
from exception.Exceptions import InvalidTokenError
from repository.authenticate import UserRepository
from services.auth_service import AuthService

log = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/login")
def login(request: Request, form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    # OAuth2PasswordRequestForm gebruikt "username" field → wij zetten daar email in
    email = form.username
    password = form.password

    # Brute-force protection: lock out an (IP, email) pair after repeated failures.
    rl_key = f"{_client_ip(request)}:{(email or '').strip().lower()}"
    wait = login_rate_limit.retry_after(rl_key)
    if wait > 0:
        log.warningx("Login geblokkeerd door rate limit", email=email, retry_after=wait)
        raise HTTPException(
            status_code=429,
            detail="Too many failed attempts. Try again later.",
            headers={"Retry-After": str(wait)},
        )

    log.infox(
        "Login request ontvangen",
        email=email,
        has_password=bool(password),
    )

    try:
        result = AuthService(UserRepository()).login(db, email, password)
        login_rate_limit.record_success(rl_key)
        log.infox(
            "Login succesvol",
            email=email,
        )
        return result
    except ValueError:
        login_rate_limit.record_failure(rl_key)
        log.warningx(
            "Login mislukt: ongeldige credentials",
            email=email,
        )
        raise HTTPException(status_code=401, detail="Invalid credentials")

@router.get("/refresh")
def refresh(request: Request, db: Session = Depends(get_db), _ = Depends(require_user)):
    log.infox(
        "Token refresh request ontvangen",
        has_authorization_header=bool(request.headers.get("Authorization")),
    )

    try:
        token = request.headers.get("Authorization")
        log.debugx(
            "Authorization header opgehaald voor token refresh",
            has_token=bool(token),
            token_length=len(token) if token else 0,
        )

        token_without_bearer = token[7:]
        log.debugx(
            "Bearer prefix verwijderd voor token refresh",
            token_length=len(token_without_bearer),
        )

        result = AuthService(UserRepository()).refresh_access_token(db,token_without_bearer)
        log.infox("Token refresh succesvol")
        return result
    except InvalidTokenError:
        log.warningx("Token refresh mislukt: InvalidTokenError")
        raise HTTPException(status_code=401, detail="Invalid token acquired, cannot refresh")
    except ValueError:
        log.warningx("Token refresh mislukt: ValueError / ongeldig token")
        raise HTTPException(status_code=401, detail="Invalid token")
    except KeyError:
        log.warningx("Token refresh mislukt: KeyError / ontbrekende token data")
        raise HTTPException(status_code=401, detail="Invalid token")
    except Exception as e:
        log.errorx(
            "Token refresh mislukt met onverwachte fout",
            error=str(e),
        )
        raise HTTPException(status_code=400, detail=str(e))