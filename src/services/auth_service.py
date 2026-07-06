import ipaddress
from datetime import datetime, timedelta, timezone
from typing import List

import jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from component.config import settings
from component.logging import get_logger
from exception.Exceptions import InvalidTokenError
from repository.authenticate import UserRepository
from services.authz_service import normalize_roles


log = get_logger(__name__)


JWT_SECRET = (settings.JWT_SECRET or "REPLACE_WITH_256BIT_SECRET")  # from secrets file
JWT_ALG = (settings.JWT_ALG or "HS256")


def _access_min() -> int:
    # Read at call time so the DB-backed ACCESS_TOKEN_MIN (hydrated at startup)
    # takes effect without restart-time capture.
    try:
        return int(settings.ACCESS_TOKEN_MIN or 30)
    except (TypeError, ValueError):
        return 30

pwd = CryptContext(schemes=["argon2"], deprecated="auto")

def _now() -> datetime:
    now = datetime.now(timezone.utc)
    log.debugx(
        "Huidige UTC tijd bepaald",
        now=now.isoformat(),
    )
    return now

def ip_to_bin(ip: str | None) -> bytes | None:
    log.debugx(
        "IP naar bytes converteren gestart",
        has_ip=bool(ip),
        ip=ip,
    )
    try:
        result = ipaddress.ip_address(ip).packed if ip else None
        log.debugx(
            "IP naar bytes converteren afgerond",
            has_ip=bool(ip),
            converted=result is not None,
            byte_length=len(result) if result else 0,
        )
        return result
    except Exception:
        log.warningx(
            "IP naar bytes converteren mislukt",
            ip=ip,
        )
        return None

def hash_password(p: str) -> str:
    log.infox(
        "Wachtwoord hashen gestart",
        password_length=len(p or ""),
    )
    result = pwd.hash(p)
    log.infox(
        "Wachtwoord hashen afgerond",
        hash_length=len(result or ""),
        hash_scheme=result.split("$")[1] if isinstance(result, str) and result.startswith("$") and len(result.split("$")) > 1 else None,
    )
    return result

def verify_password(plain: str, hashed: str) -> bool:
    log.infox(
        "Wachtwoord verificatie gestart",
        plain_length=len(plain or ""),
        has_hash=bool(hashed),
        hash_length=len(hashed or ""),
    )
    result = pwd.verify(plain, hashed)
    log.infox(
        "Wachtwoord verificatie afgerond",
        verified=result,
    )
    return result

def make_access_token(user_id: int, email: str, roles: List[str] | None = None) -> str:
    log.infox(
        "Access token aanmaken gestart",
        user_id=user_id,
        email=email,
        role_count=len(roles or []),
        roles=roles or [],
        jwt_alg=JWT_ALG,
    )
    roles = roles or []
    access_min = _access_min()
    payload = {
        "sub": str(user_id),
        "email": email,
        "roles": roles,
        "iat": int(_now().timestamp()),
        "exp": int((_now() + timedelta(minutes=access_min)).timestamp()),
    }
    log.debugx(
        "Access token payload opgebouwd",
        user_id=user_id,
        email=email,
        roles=roles,
        iat=payload.get("iat"),
        exp=payload.get("exp"),
        ttl_minutes=access_min,
    )
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)
    log.infox(
        "Access token aanmaken afgerond",
        user_id=user_id,
        email=email,
        token_length=len(token or ""),
        exp=payload.get("exp"),
    )
    return token

def decode_access_token(token: str) -> dict:
    log.infox(
        "Access token decoderen gestart",
        token_length=len(token or ""),
        jwt_alg=JWT_ALG,
    )
    decoded = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    log.infox(
        "Access token decoderen afgerond",
        subject=decoded.get("sub") if isinstance(decoded, dict) else None,
        email=decoded.get("email") if isinstance(decoded, dict) else None,
        role_count=len(decoded.get("roles") or []) if isinstance(decoded, dict) else None,
        exp=decoded.get("exp") if isinstance(decoded, dict) else None,
    )
    return decoded

class AuthService:
    def __init__(self, users: UserRepository):
        log.infox(
            "AuthService initialiseren",
            has_user_repository=users is not None,
            user_repository_type=type(users).__name__,
        )
        self.users = users
        log.infox("AuthService geïnitialiseerd")

    def refresh_access_token(self, db: Session, token: str) -> dict:
        log.infox(
            "Access token refresh gestart",
            has_db=db is not None,
            token_length=len(token or ""),
        )
        decoded_token = decode_access_token(token)
        email = decoded_token["email"]
        sub = decoded_token["sub"]
        expires_in = decoded_token["exp"] - int(_now().timestamp())

        log.infox(
            "Refresh token decoded",
            email=email,
            sub=sub,
            expires_in_seconds=expires_in,
            exp=decoded_token.get("exp"),
        )

        # Checking if there hasn't been played with the token
        user_by_mail = self.users.get_by_email(db, email)
        user_by_id = self.users.get_by_id(db, sub)

        log.debugx(
            "Refresh token gebruiker validatie uitgevoerd",
            email=email,
            sub=sub,
            user_by_mail_found=user_by_mail is not None,
            user_by_id_found=user_by_id is not None,
            expires_in_seconds=expires_in,
        )

        if not expires_in < 0:
            if user_by_mail and user_by_id:
                log.infox(
                    "Refresh token geldig, nieuw access token aanmaken",
                    email=email,
                    sub=sub,
                    expires_in_seconds=expires_in,
                )
                roles = normalize_roles(getattr(user_by_id, "roles", None) or [])
                refresh_token = make_access_token(int(sub), email, roles=roles)
                log.infox(
                    "Access token refresh afgerond",
                    email=email,
                    sub=sub,
                    token_type="bearer",
                )
                return {"access_token": refresh_token, "token_type": "bearer"}
            else:
                log.warningx(
                    "Access token refresh mislukt: gebruiker komt niet overeen",
                    email=email,
                    sub=sub,
                    user_by_mail_found=user_by_mail is not None,
                    user_by_id_found=user_by_id is not None,
                )
                raise ValueError("Incorrect user!")
        else:
            log.warningx(
                "Access token refresh mislukt: token verlopen",
                email=email,
                sub=sub,
                expires_in_seconds=expires_in,
            )
            raise InvalidTokenError("Token has been expired and cannot be refreshed.","Token Expired")

    def login(self, db: Session, email: str, password: str) -> dict:
        """
        Return: {"access_token": "...", "token_type": "bearer"}
        """
        log.infox(
            "Login gestart",
            email=email,
            password_length=len(password or ""),
            has_db=db is not None,
        )
        user = self.users.get_by_email(db, email)

        log.debugx(
            "Login gebruiker opgehaald",
            email=email,
            user_found=user is not None,
            user_id=getattr(user, "id", None),
            is_active=getattr(user, "is_active", None) if user is not None else None,
        )

        # Anti-enumeration: zelfde error voor alles
        if not user or not getattr(user, "is_active", True):
            log.warningx(
                "Login mislukt: gebruiker niet gevonden of niet actief",
                email=email,
                user_found=user is not None,
                is_active=getattr(user, "is_active", None) if user is not None else None,
            )
            raise ValueError("Incorrect email or password")

        if not verify_password(password, user.password_hash):
            log.warningx(
                "Login mislukt: wachtwoord ongeldig",
                email=email,
                user_id=getattr(user, "id", None),
            )
            raise ValueError("Incorrect email or password")

        roles = normalize_roles(getattr(user, "roles", None) or [])
        token = make_access_token(user.id, user.email, roles)
        log.infox(
            "Login afgerond",
            email=email,
            user_id=user.id,
            role_count=len(roles or []),
            token_type="bearer",
        )
        return {"access_token": token, "token_type": "bearer"}