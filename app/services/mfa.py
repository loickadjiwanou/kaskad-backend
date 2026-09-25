"""Règles de la double authentification de la console."""

from app.core.config import get_settings

# Routes utilisables avant d'avoir configuré la double authentification obligatoire
SETUP_PATHS = ("/admin/auth/me", "/admin/auth/2fa")


def mfa_enabled(admin: dict) -> bool:
    return bool(admin.get("totp_enabled"))


def mfa_mandatory(admin: dict, account: dict | None) -> bool:
    """Obligatoire pour l'administrateur de la plateforme (ADMIN_REQUIRE_2FA) et pour les membres
    d'un compte dont le propriétaire l'exige."""
    if admin.get("role") == "admin":
        return get_settings().admin_require_2fa
    return bool((account or {}).get("require_2fa"))


def mfa_setup_required(admin: dict, account: dict | None) -> bool:
    return mfa_mandatory(admin, account) and not mfa_enabled(admin)


def allowed_during_setup(path: str) -> bool:
    return any(p in path for p in SETUP_PATHS)
