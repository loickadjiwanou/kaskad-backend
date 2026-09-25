"""Double authentification : codes TOTP (RFC 6238, compatibles Google Authenticator, 1Password, Authy…)
et codes de secours à usage unique.

Le secret TOTP est chiffré en base (clé dérivée de JWT_SECRET) : changer JWT_SECRET invalide les
applications d'authentification configurées ; les codes de secours (empreintes) restent valables.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings

DIGITS = 6
PERIOD = 30
RECOVERY_CODES = 10


def new_secret() -> str:
    """Secret base32 de 160 bits (sans remplissage)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    return base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)


def code_at(secret: str, counter: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 10**DIGITS:0{DIGITS}d}"


def current_counter(at: float | None = None) -> int:
    return int((time.time() if at is None else at) // PERIOD)


def verify(secret: str, code: str, last_counter: int | None = None, window: int = 1, at: float | None = None) -> int | None:
    """Renvoie le compteur accepté (à mémoriser contre le rejeu), ou None.

    Tolère ±`window` périodes de décalage d'horloge ; un code déjà utilisé (compteur ≤ `last_counter`) est refusé.
    """
    code = "".join(c for c in (code or "") if c.isdigit())
    if len(code) != DIGITS:
        return None
    now = current_counter(at)
    for counter in range(now - window, now + window + 1):
        if last_counter is not None and counter <= last_counter:
            continue
        if hmac.compare_digest(code_at(secret, counter), code):
            return counter
    return None


def provisioning_uri(secret: str, account: str, issuer: str = "Kaskad") -> str:
    """URL otpauth:// affichée en QR code dans la console."""
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?" + urlencode({"secret": secret, "issuer": issuer, "digits": DIGITS, "period": PERIOD})


# ---------------------------------------------------------------- stockage


def _fernet() -> Fernet:
    digest = hashlib.sha256(f"kaskad-totp:{get_settings().jwt_secret}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_secret(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None


# ---------------------------------------------------------------- codes de secours


def _normalize_recovery(code: str) -> str:
    return "".join(c for c in (code or "").lower() if c.isalnum())


def hash_recovery(code: str) -> str:
    return hashlib.sha256(f"kaskad-recovery:{_normalize_recovery(code)}".encode()).hexdigest()


def new_recovery_codes() -> tuple[list[str], list[str]]:
    """Codes lisibles (affichés une seule fois) et leurs empreintes (seules stockées)."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    codes = []
    for _ in range(RECOVERY_CODES):
        raw = "".join(secrets.choice(alphabet) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes, [hash_recovery(c) for c in codes]


def looks_like_recovery(code: str) -> bool:
    return len(_normalize_recovery(code)) == 10
