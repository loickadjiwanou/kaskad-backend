"""Console : double authentification (TOTP + codes de secours).

- Connexion en deux étapes : mot de passe → `mfa_token` (5 min) → code à 6 chiffres ou code de secours.
- Obligatoire pour l'administrateur de la plateforme (ADMIN_REQUIRE_2FA) et pour les membres d'un compte
  dont le propriétaire l'exige ; les autres membres l'activent librement.
- Le propriétaire (ou l'administrateur de la plateforme) peut la réinitialiser pour un membre qui a perdu son appareil.
"""

import jwt
from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel, Field

from app.core import totp
from app.core.i18n import ApiError
from app.core.security import decode_token, verify_password
from app.deps import CurrentAdmin, Db, LoginLimiter, MailerDep, TeamManager
from app.models.common import maybe_oid, now, oid
from app.routers.admin_auth import _account, _ensure_account_active, _remember_language, _session, admin_out
from app.services.accounts import OWNER, PLATFORM_ADMIN, is_platform_admin
from app.services.activity import log_activity
from app.services.auth import revoke_all
from app.services.mfa import mfa_enabled, mfa_mandatory
from app.services.notify import notify_member

router = APIRouter(prefix="/admin", tags=["admin: two-step verification"])


def _check_code(admin: dict, code: str) -> tuple[dict, str | None]:
    """Vérifie un code TOTP ou un code de secours. Renvoie (mise à jour à appliquer, type de code) ; type None = refusé."""
    secret = totp.decrypt_secret(admin.get("totp_secret"))
    if secret:
        counter = totp.verify(secret, code, admin.get("totp_last_counter"))
        if counter is not None:
            return {"totp_last_counter": counter}, "totp"
    if totp.looks_like_recovery(code):
        hashed = totp.hash_recovery(code)
        if hashed in (admin.get("totp_recovery_hashes") or []):
            return {"totp_recovery_hashes": [h for h in admin["totp_recovery_hashes"] if h != hashed]}, "recovery"
    return {}, None


def status_out(admin: dict, account: dict | None) -> dict:
    return {
        "enabled": mfa_enabled(admin),
        "required": mfa_mandatory(admin, account),
        "enabled_at": admin.get("totp_enabled_at"),
        "recovery_codes_left": len(admin.get("totp_recovery_hashes") or []) if mfa_enabled(admin) else 0,
    }


# ---------------------------------------------------------------- connexion (étape 2)


class MfaLoginIn(BaseModel):
    mfa_token: str = Field(min_length=10, max_length=2000)
    code: str = Field(min_length=6, max_length=32)


@router.post("/auth/login/2fa")
async def login_second_step(
    db: Db, request: Request, limiter: LoginLimiter, background: BackgroundTasks, mailer: MailerDep, body: MfaLoginIn
):
    """Code de l'application d'authentification (ou code de secours) après le mot de passe → session."""
    try:
        payload = decode_token(body.mfa_token)
    except jwt.PyJWTError:
        raise ApiError(401, "mfa_token_invalid") from None
    if payload.get("typ") != "mfa" or payload.get("scope") != "admin":
        raise ApiError(401, "mfa_token_invalid")
    limiter.check(request, f"mfa:{payload['sub']}")
    admin = await db.admins.find_one({"_id": maybe_oid(payload["sub"])})
    if not admin or not admin.get("active", True) or not mfa_enabled(admin):
        raise ApiError(401, "mfa_token_invalid")
    await _ensure_account_active(db, admin)
    update, kind = _check_code(admin, body.code)
    if not kind:
        raise ApiError(401, "mfa_invalid_code")
    await db.admins.update_one({"_id": admin["_id"]}, {"$set": update})
    limiter.reset(request, f"mfa:{payload['sub']}")
    await _remember_language(db, admin, request)
    if kind == "recovery":
        left = len(update["totp_recovery_hashes"])
        await log_activity(db, admin, "member.2fa_recovery_used", "admin", admin["_id"], {"count": left})
        background.add_task(notify_member, db, mailer, admin["_id"], "mfa_recovery_used", "/account", count=left)
    return await _session(db, await db.admins.find_one({"_id": admin["_id"]}))


# ---------------------------------------------------------------- configuration


@router.get("/auth/2fa")
async def mfa_status(db: Db, admin: CurrentAdmin):
    return status_out(admin, await _account(db, admin))


class PasswordIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)


@router.post("/auth/2fa/setup")
async def mfa_setup(db: Db, admin: CurrentAdmin, body: PasswordIn):
    """Nouvelle clé (QR code) à scanner ; la double authentification n'est active qu'après confirmation d'un code."""
    if admin.get("api_key"):
        raise ApiError(403, "forbidden")
    if mfa_enabled(admin):
        raise ApiError(409, "mfa_already_enabled")
    if not verify_password(body.password, admin.get("password_hash")):
        raise ApiError(401, "invalid_credentials")
    secret = totp.new_secret()
    await db.admins.update_one({"_id": admin["_id"]}, {"$set": {"totp_pending_secret": totp.encrypt_secret(secret)}})
    return {"secret": secret, "otpauth_url": totp.provisioning_uri(secret, admin["email"])}


class CodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=32)


@router.post("/auth/2fa/enable")
async def mfa_enable(db: Db, admin: CurrentAdmin, background: BackgroundTasks, mailer: MailerDep, body: CodeIn):
    """Confirme la clé avec un premier code : active la double authentification et renvoie les codes de secours (une seule fois)."""
    if mfa_enabled(admin):
        raise ApiError(409, "mfa_already_enabled")
    secret = totp.decrypt_secret(admin.get("totp_pending_secret"))
    if not secret:
        raise ApiError(400, "mfa_setup_missing")
    counter = totp.verify(secret, body.code)
    if counter is None:
        raise ApiError(400, "mfa_invalid_code")
    codes, hashes = totp.new_recovery_codes()
    await db.admins.update_one(
        {"_id": admin["_id"]},
        {
            "$set": {
                "totp_enabled": True,
                "totp_secret": totp.encrypt_secret(secret),
                "totp_last_counter": counter,
                "totp_recovery_hashes": hashes,
                "totp_enabled_at": now(),
            },
            "$unset": {"totp_pending_secret": ""},
        },
    )
    await log_activity(db, admin, "member.2fa_enabled", "admin", admin["_id"], {"email": admin["email"]})
    background.add_task(notify_member, db, mailer, admin["_id"], "mfa_enabled", "/account")
    updated = await db.admins.find_one({"_id": admin["_id"]})
    account = await _account(db, updated)
    return {"recovery_codes": codes, "status": status_out(updated, account), "admin": admin_out(updated, account)}


class DisableIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=6, max_length=32)


def _clear_mfa() -> dict:
    return {
        "$set": {"totp_enabled": False},
        "$unset": {
            "totp_secret": "",
            "totp_pending_secret": "",
            "totp_last_counter": "",
            "totp_recovery_hashes": "",
            "totp_enabled_at": "",
        },
    }


@router.post("/auth/2fa/disable")
async def mfa_disable(db: Db, admin: CurrentAdmin, background: BackgroundTasks, mailer: MailerDep, body: DisableIn):
    """Désactivation (mot de passe + code) ; impossible quand elle est obligatoire pour ce membre."""
    if not mfa_enabled(admin):
        raise ApiError(400, "mfa_not_enabled")
    if mfa_mandatory(admin, await _account(db, admin)):
        raise ApiError(403, "mfa_cannot_disable")
    if not verify_password(body.password, admin.get("password_hash")):
        raise ApiError(401, "invalid_credentials")
    _, kind = _check_code(admin, body.code)
    if not kind:
        raise ApiError(400, "mfa_invalid_code")
    await db.admins.update_one({"_id": admin["_id"]}, _clear_mfa())
    await log_activity(db, admin, "member.2fa_disabled", "admin", admin["_id"], {"email": admin["email"]})
    background.add_task(notify_member, db, mailer, admin["_id"], "mfa_disabled", "/account")
    updated = await db.admins.find_one({"_id": admin["_id"]})
    return status_out(updated, await _account(db, updated))


@router.post("/auth/2fa/recovery-codes")
async def mfa_new_recovery_codes(db: Db, admin: CurrentAdmin, body: CodeIn):
    """Nouveaux codes de secours (les anciens ne sont plus valables) ; demande un code de l'application."""
    if not mfa_enabled(admin):
        raise ApiError(400, "mfa_not_enabled")
    secret = totp.decrypt_secret(admin.get("totp_secret"))
    counter = totp.verify(secret, body.code, admin.get("totp_last_counter")) if secret else None
    if counter is None:
        raise ApiError(400, "mfa_invalid_code")
    codes, hashes = totp.new_recovery_codes()
    await db.admins.update_one({"_id": admin["_id"]}, {"$set": {"totp_recovery_hashes": hashes, "totp_last_counter": counter}})
    await log_activity(db, admin, "member.2fa_recovery_regenerated", "admin", admin["_id"])
    return {"recovery_codes": codes}


# ---------------------------------------------------------------- réinitialisation pour un membre


@router.post("/members/{member_id}/reset-2fa")
async def reset_member_mfa(db: Db, admin: TeamManager, background: BackgroundTasks, mailer: MailerDep, member_id: str):
    """Appareil perdu : le propriétaire (ou l'administrateur de la plateforme) désactive la double authentification
    d'un membre et ferme ses sessions ; le membre la configure de nouveau (s'il y est obligé) à sa prochaine connexion."""
    target = await db.admins.find_one({"_id": oid(member_id, "not_found")})
    if not target or (not is_platform_admin(admin) and target.get("account_id") != admin.get("account_id")):
        raise ApiError(404, "not_found")
    # On ne réinitialise ni la sienne (désactivation normale), ni celle de l'administrateur ; le propriétaire : administrateur seulement
    if target["_id"] == admin["_id"] or target.get("role") == PLATFORM_ADMIN:
        raise ApiError(403, "cannot_edit_member")
    if target.get("role") == OWNER and not is_platform_admin(admin):
        raise ApiError(403, "cannot_edit_member")
    if not mfa_enabled(target):
        raise ApiError(400, "mfa_not_enabled")
    await db.admins.update_one({"_id": target["_id"]}, _clear_mfa())
    await revoke_all(db, target["_id"])
    await log_activity(
        db, admin, "member.2fa_reset", "admin", target["_id"], {"email": target["email"]}, account_id=target.get("account_id")
    )
    background.add_task(notify_member, db, mailer, target["_id"], "mfa_reset", "/login", member=admin.get("name") or admin["email"])
    return admin_out(await db.admins.find_one({"_id": target["_id"]}))
