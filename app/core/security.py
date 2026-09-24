import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from pwdlib import PasswordHash

from app.core.config import get_settings

Scope = Literal["admin", "user"]
_hasher = PasswordHash.recommended()  # Argon2id


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        return _hasher.verify(password, password_hash)
    except Exception:
        return False


def _ttl(scope: Scope, kind: Literal["access", "refresh"]) -> timedelta:
    s = get_settings()
    if kind == "access":
        return timedelta(minutes=s.admin_access_ttl_minutes if scope == "admin" else s.user_access_ttl_minutes)
    return timedelta(days=s.admin_refresh_ttl_days if scope == "admin" else s.user_refresh_ttl_days)


def create_token(subject: str, scope: Scope, kind: Literal["access", "refresh"], extra: dict | None = None) -> tuple[str, str, datetime]:
    """Retourne (jeton, jti, expiration)."""
    s = get_settings()
    now = datetime.now(UTC)
    expires = now + _ttl(scope, kind)
    jti = secrets.token_urlsafe(16)
    payload = {"sub": subject, "scope": scope, "typ": kind, "jti": jti, "iat": now, "exp": expires, **(extra or {})}
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm), jti, expires


def decode_token(token: str) -> dict:
    s = get_settings()
    return jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
