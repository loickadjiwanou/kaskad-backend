"""Console : connexion, inscription (compte développeur), confirmation d'e-mail, membres et invitations.

Rôles : `admin` (administrateur de la plateforme, unique), `owner` (propriétaire d'un compte développeur),
`developer` et `viewer` (membres invités par le propriétaire). Le rôle `admin` ne peut jamais être attribué.
"""

import hashlib
import secrets
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Query, Request
from pydantic import BaseModel, EmailStr, Field
from pymongo.errors import DuplicateKeyError

from app.core.config import get_settings
from app.core.i18n import ApiError, language
from app.core.security import hash_password, verify_password
from app.deps import CurrentAdmin, Db, FullAdmin, LoginLimiter, MailerDep, TeamManager
from app.models.common import maybe_oid, now, oid, sid
from app.models.schemas import AdminSession, EmailPassword, RefreshIn
from app.services.accounts import (
    INVITABLE_ROLES,
    OWNER,
    PLATFORM_ADMIN,
    account_out,
    create_account,
    is_platform_admin,
)
from app.services.activity import log_activity
from app.services.api_keys import api_key_out, new_api_key
from app.services.auth import issue_tokens, revoke_all, revoke_refresh_token, rotate_refresh_token
from app.services.emails import invitation_email, reset_password_email, verification_email
from app.services.notify import notify_member

router = APIRouter(prefix="/admin", tags=["admin: auth & team"])


def admin_out(a: dict, account: dict | None = None) -> dict:
    return {
        "id": sid(a["_id"]),
        "email": a["email"],
        "name": a.get("name", ""),
        "role": a.get("role", "viewer"),
        "active": a.get("active", True),
        "email_verified": a.get("email_verified", True),
        "account_id": sid(a.get("account_id")),
        **({"account": account_out(account)} if account is not None else {}),
        "created_at": a.get("created_at"),
        "last_login_at": a.get("last_login_at"),
    }


async def _account(db, admin: dict) -> dict | None:
    return await db.accounts.find_one({"_id": admin.get("account_id")}) if admin.get("account_id") else None


def _new_token() -> tuple[str, str]:
    """Jeton envoyé par e-mail (en clair) et son empreinte (seule stockée)."""
    token = secrets.token_urlsafe(32)
    return token, _hash(token)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _ensure_account_active(db, admin: dict) -> None:
    """Membres d'un compte développeur suspendu : connexion refusée (jamais l'administrateur de la plateforme)."""
    if admin.get("role") == PLATFORM_ADMIN or not admin.get("account_id"):
        return
    if await db.accounts.find_one({"_id": admin["account_id"], "suspended": True}, {"_id": 1}):
        raise ApiError(403, "account_suspended")


async def _remember_language(db, admin: dict, request: Request) -> None:
    """Langue de la console du membre : utilisée pour les e-mails de suivi qui lui sont envoyés."""
    lang = language(request)
    if admin.get("language") != lang:
        await db.admins.update_one({"_id": admin["_id"]}, {"$set": {"language": lang}})


def _limit(request: Request, key: str) -> None:
    request.app.state.signup_limiter.check(request, key)


async def _session(db, admin: dict) -> dict:
    await db.admins.update_one({"_id": admin["_id"]}, {"$set": {"last_login_at": now()}})
    return {**(await issue_tokens(db, admin["_id"], "admin")), "admin": admin_out(admin, await _account(db, admin))}


# ---------------------------------------------------------------- connexion


@router.post("/auth/login", response_model=AdminSession)
async def login(db: Db, request: Request, limiter: LoginLimiter, body: EmailPassword):
    limiter.check(request, f"admin:{body.email}")
    admin = await db.admins.find_one({"email": body.email.lower()})
    if not admin or not verify_password(body.password, admin.get("password_hash")):
        raise ApiError(401, "invalid_credentials")
    if not admin.get("active", True):
        raise ApiError(403, "account_disabled")
    if not admin.get("email_verified", True):
        raise ApiError(403, "email_not_verified")
    await _ensure_account_active(db, admin)
    limiter.reset(request, f"admin:{body.email}")
    await _remember_language(db, admin, request)
    return await _session(db, admin)


@router.post("/auth/refresh")
async def refresh(db: Db, body: RefreshIn):
    admin_id, tokens = await rotate_refresh_token(db, body.refresh_token, "admin")
    admin = await db.admins.find_one({"_id": admin_id})
    if not admin or not admin.get("active", True):
        raise ApiError(401, "invalid_token")
    return tokens


@router.post("/auth/logout", status_code=204)
async def logout(db: Db, body: RefreshIn):
    await revoke_refresh_token(db, body.refresh_token, "admin")


# ---------------------------------------------------------------- inscription


class SignupIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    account_name: str = Field(min_length=2, max_length=80)


async def _send_verification(db, mailer, request: Request, admin: dict, account_name: str) -> None:
    s = get_settings()
    token, token_hash = _new_token()
    await db.email_tokens.delete_many({"admin_id": admin["_id"], "kind": "verify"})
    await db.email_tokens.insert_one(
        {
            "admin_id": admin["_id"],
            "kind": "verify",
            "token_hash": token_hash,
            "expires_at": now() + timedelta(hours=s.email_verification_ttl_hours),
            "created_at": now(),
        }
    )
    url = f"{s.console_url.rstrip('/')}/verify-email?token={token}"
    lang = language(request)
    await mailer.send(verification_email(lang, admin["email"], admin["name"], account_name, url, s.email_verification_ttl_hours))


@router.post("/auth/signup", status_code=201)
async def signup(db: Db, request: Request, mailer: MailerDep, body: SignupIn):
    """Crée un compte développeur et son propriétaire, puis envoie l'e-mail de confirmation (langue de la console)."""
    _limit(request, "signup")
    email = body.email.lower()
    if await db.admins.find_one({"email": email}):
        raise ApiError(409, "email_taken")
    account = await create_account(db, body.account_name.strip())
    doc = {
        "email": email,
        "password_hash": hash_password(body.password),
        "name": body.name.strip(),
        "role": OWNER,
        "account_id": account["_id"],
        "active": True,
        "email_verified": False,
        "language": language(request),
        "created_at": now(),
    }
    try:
        doc["_id"] = (await db.admins.insert_one(doc)).inserted_id
    except DuplicateKeyError:
        await db.accounts.delete_one({"_id": account["_id"]})
        raise ApiError(409, "email_taken") from None
    await db.accounts.update_one({"_id": account["_id"]}, {"$set": {"owner_id": doc["_id"]}})
    await log_activity(db, doc, "account.created", "account", account["_id"], {"name": account["name"]})
    await _send_verification(db, mailer, request, doc, account["name"])
    return {"email": email, "verification_sent": True}


class EmailIn(BaseModel):
    email: EmailStr


@router.post("/auth/resend-verification", status_code=202)
async def resend_verification(db: Db, request: Request, mailer: MailerDep, body: EmailIn):
    """Renvoie l'e-mail de confirmation. Réponse identique que l'adresse existe ou non."""
    _limit(request, f"resend:{body.email}")
    admin = await db.admins.find_one({"email": body.email.lower()})
    if admin and not admin.get("email_verified", True):
        account = await _account(db, admin)
        await _send_verification(db, mailer, request, admin, account["name"] if account else "")
    return {"detail": "ok"}


class TokenIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)


@router.post("/auth/verify-email", response_model=AdminSession)
async def verify_email(db: Db, body: TokenIn):
    """Confirme l'adresse e-mail et ouvre directement une session."""
    stored = await db.email_tokens.find_one_and_delete({"token_hash": _hash(body.token), "kind": "verify"})
    if not stored or stored["expires_at"].replace(tzinfo=None) < now().replace(tzinfo=None):
        raise ApiError(400, "token_invalid")
    await db.admins.update_one({"_id": stored["admin_id"]}, {"$set": {"email_verified": True, "updated_at": now()}})
    admin = await db.admins.find_one({"_id": stored["admin_id"]})
    if not admin or not admin.get("active", True):
        raise ApiError(403, "account_disabled")
    return await _session(db, admin)


# ---------------------------------------------------------------- mot de passe oublié


@router.post("/auth/forgot-password", status_code=202)
async def forgot_password(db: Db, request: Request, mailer: MailerDep, body: EmailIn):
    """Envoie un lien de réinitialisation (langue de la console). Réponse identique que l'adresse existe ou non."""
    _limit(request, f"reset:{body.email}")
    admin = await db.admins.find_one({"email": body.email.lower(), "active": {"$ne": False}})
    if admin:
        s = get_settings()
        token, token_hash = _new_token()
        await db.email_tokens.delete_many({"admin_id": admin["_id"], "kind": "reset"})
        await db.email_tokens.insert_one(
            {
                "admin_id": admin["_id"],
                "kind": "reset",
                "token_hash": token_hash,
                "expires_at": now() + timedelta(minutes=s.password_reset_ttl_minutes),
                "created_at": now(),
            }
        )
        url = f"{s.console_url.rstrip('/')}/reset-password?token={token}"
        email = reset_password_email(language(request), admin["email"], admin.get("name") or "", url, s.password_reset_ttl_minutes)
        await mailer.send(email)
    return {"detail": "ok"}


class ResetIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=8, max_length=256)


@router.post("/auth/reset-password", response_model=AdminSession)
async def reset_password(db: Db, request: Request, body: ResetIn):
    """Nouveau mot de passe depuis le lien reçu : les autres sessions sont révoquées, une nouvelle session est ouverte."""
    stored = await db.email_tokens.find_one_and_delete({"token_hash": _hash(body.token), "kind": "reset"})
    if not stored or stored["expires_at"].replace(tzinfo=None) < now().replace(tzinfo=None):
        raise ApiError(400, "token_invalid")
    admin = await db.admins.find_one({"_id": stored["admin_id"]})
    if not admin or not admin.get("active", True):
        raise ApiError(403, "account_disabled")
    await _ensure_account_active(db, admin)
    # Le lien reçu prouve aussi la possession de l'adresse e-mail
    await db.admins.update_one(
        {"_id": admin["_id"]}, {"$set": {"password_hash": hash_password(body.password), "email_verified": True, "updated_at": now()}}
    )
    await revoke_all(db, admin["_id"])
    await log_activity(db, admin, "member.password_reset", "admin", admin["_id"])
    await _remember_language(db, admin, request)
    return await _session(db, await db.admins.find_one({"_id": admin["_id"]}))


# ---------------------------------------------------------------- profil et compte


@router.get("/auth/me")
async def me(db: Db, request: Request, admin: CurrentAdmin):
    await _remember_language(db, admin, request)
    return admin_out(admin, await _account(db, admin))


class ProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    current_password: str | None = None
    new_password: str | None = Field(default=None, min_length=8, max_length=256)


@router.patch("/auth/me")
async def update_me(db: Db, admin: CurrentAdmin, body: ProfileUpdate):
    update: dict = {"updated_at": now()}
    if body.name:
        update["name"] = body.name
    if body.new_password:
        if not verify_password(body.current_password or "", admin.get("password_hash")):
            raise ApiError(401, "invalid_credentials")
        update["password_hash"] = hash_password(body.new_password)
    await db.admins.update_one({"_id": admin["_id"]}, {"$set": update})
    updated = await db.admins.find_one({"_id": admin["_id"]})
    return admin_out(updated, await _account(db, updated))


class AccountUpdate(BaseModel):
    name: str = Field(min_length=2, max_length=80)


@router.patch("/account")
async def rename_account(db: Db, admin: TeamManager, body: AccountUpdate):
    """Renomme le compte développeur (propriétaire)."""
    await db.accounts.update_one({"_id": admin["account_id"]}, {"$set": {"name": body.name.strip(), "updated_at": now()}})
    await db.apps.update_many({"account_id": admin["account_id"]}, {"$set": {"account_name": body.name.strip()}})
    await log_activity(db, admin, "account.renamed", "account", admin["account_id"], {"name": body.name})
    return account_out(await _account(db, admin))


@router.get("/accounts")
async def list_accounts(db: Db, _: FullAdmin):
    """Administrateur de la plateforme : tous les comptes développeurs."""
    accounts = await db.accounts.find().sort("created_at", -1).to_list(None)
    members = {
        r["_id"]: r["n"] for r in await (await db.admins.aggregate([{"$group": {"_id": "$account_id", "n": {"$sum": 1}}}])).to_list(None)
    }
    apps = {r["_id"]: r["n"] for r in await (await db.apps.aggregate([{"$group": {"_id": "$account_id", "n": {"$sum": 1}}}])).to_list(None)}
    owners = {a["_id"]: a for a in await db.admins.find({"_id": {"$in": [x.get("owner_id") for x in accounts]}}).to_list(None)}
    return [
        {
            **account_out(acc),
            "owner": {"name": owners[acc["owner_id"]]["name"], "email": owners[acc["owner_id"]]["email"]}
            if acc.get("owner_id") in owners
            else None,
            "owner_verified": owners.get(acc.get("owner_id"), {}).get("email_verified", True),
            "members_count": members.get(acc["_id"], 0),
            "apps_count": apps.get(acc["_id"], 0),
            "platform": owners.get(acc.get("owner_id"), {}).get("role") == PLATFORM_ADMIN,
            "suspended": bool(acc.get("suspended")),
            "suspended_at": acc.get("suspended_at"),
            "suspension_reason": acc.get("suspension_reason"),
        }
        for acc in accounts
    ]


class AccountSuspension(BaseModel):
    suspended: bool
    reason: str | None = Field(default=None, max_length=2000)


@router.patch("/accounts/{account_id}")
async def suspend_account(
    db: Db, background: BackgroundTasks, mailer: MailerDep, admin: FullAdmin, account_id: str, body: AccountSuspension
):
    """Administrateur de la plateforme : suspend (ou réactive) un compte développeur entier.

    Suspendu : ses apps disparaissent du store, ses membres sont déconnectés et ne peuvent plus se connecter.
    Le propriétaire est prévenu par e-mail.
    """
    account = await db.accounts.find_one({"_id": oid(account_id, "not_found")})
    if not account:
        raise ApiError(404, "not_found")
    if account["_id"] == admin.get("account_id"):
        raise ApiError(403, "cannot_edit_member")  # compte de la plateforme
    reason = (body.reason or "").strip() or None
    update = {
        "suspended": body.suspended,
        "suspended_at": now() if body.suspended else None,
        "suspension_reason": reason if body.suspended else None,
    }
    await db.accounts.update_one({"_id": account["_id"]}, {"$set": {**update, "updated_at": now()}})
    await db.apps.update_many({"account_id": account["_id"]}, {"$set": {"account_suspended": body.suspended}})
    if body.suspended:
        async for member in db.admins.find({"account_id": account["_id"]}, {"_id": 1}):
            await revoke_all(db, member["_id"])
    await log_activity(
        db,
        admin,
        "account.suspended" if body.suspended else "account.reactivated",
        "account",
        account["_id"],
        {"name": account["name"], "reason": reason},
        account_id=account["_id"],
    )
    background.add_task(
        notify_member,
        db,
        mailer,
        account.get("owner_id"),
        "account_suspended" if body.suspended else "account_reactivated",
        "/",
        account=account["name"],
        reason=reason if body.suspended else None,
    )
    return {**account_out({**account, **update}), **update}


# ---------------------------------------------------------------- clés API (intégration continue)


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@router.get("/api-keys")
async def list_api_keys(db: Db, admin: TeamManager):
    keys = await db.api_keys.find({"account_id": admin["account_id"], "revoked_at": None}).sort("created_at", -1).to_list(None)
    return [api_key_out(k) for k in keys]


@router.post("/api-keys", status_code=201)
async def create_api_key(db: Db, admin: TeamManager, body: ApiKeyIn):
    """Crée une clé API pour le compte. La clé complète n'est renvoyée qu'une fois (seule son empreinte est stockée)."""
    if admin.get("api_key"):
        raise ApiError(403, "forbidden")
    key, key_hash = new_api_key()
    doc = {
        "account_id": admin["account_id"],
        "name": body.name.strip(),
        "prefix": key[:12],
        "key_hash": key_hash,
        "created_by": admin["_id"],
        "created_by_name": admin.get("name"),
        "created_at": now(),
        "last_used_at": None,
        "revoked_at": None,
    }
    doc["_id"] = (await db.api_keys.insert_one(doc)).inserted_id
    await log_activity(db, admin, "api_key.created", "api_key", doc["_id"], {"name": doc["name"], "prefix": doc["prefix"]})
    return {**api_key_out(doc), "key": key}


@router.delete("/api-keys/{key_id}", status_code=204)
async def revoke_api_key(db: Db, admin: TeamManager, key_id: str):
    key = await db.api_keys.find_one({"_id": oid(key_id, "not_found"), "account_id": admin["account_id"], "revoked_at": None})
    if not key:
        raise ApiError(404, "not_found")
    await db.api_keys.update_one({"_id": key["_id"]}, {"$set": {"revoked_at": now()}})
    await log_activity(db, admin, "api_key.revoked", "api_key", key["_id"], {"name": key["name"], "prefix": key["prefix"]})


# ---------------------------------------------------------------- membres


def _target_account(admin: dict, account_id: str | None):
    """Compte concerné : le sien ; l'administrateur de la plateforme peut consulter n'importe quel compte."""
    if account_id and is_platform_admin(admin):
        return maybe_oid(account_id)
    return admin.get("account_id")


@router.get("/members")
async def list_members(db: Db, admin: CurrentAdmin, account_id: str | None = None):
    target = _target_account(admin, account_id)
    members = await db.admins.find({"account_id": target}).sort("created_at", 1).to_list(None)
    return [admin_out(m) for m in members]


class MemberUpdate(BaseModel):
    role: Literal["developer", "viewer"] | None = None
    active: bool | None = None


@router.patch("/members/{member_id}")
async def update_member(db: Db, admin: TeamManager, member_id: str, body: MemberUpdate):
    """Rôle (développeur / lecteur) ou accès d'un membre. Le propriétaire et l'administrateur ne sont pas modifiables."""
    target = await db.admins.find_one({"_id": oid(member_id, "not_found")})
    if not target or (not is_platform_admin(admin) and target.get("account_id") != admin.get("account_id")):
        raise ApiError(404, "not_found")
    if target["_id"] == admin["_id"] or target.get("role") in (OWNER, PLATFORM_ADMIN):
        # Exception : l'administrateur de la plateforme peut suspendre le propriétaire d'un compte
        if not (is_platform_admin(admin) and target.get("role") == OWNER and body.role is None):
            raise ApiError(403, "cannot_edit_member")
    update = {**body.model_dump(exclude_none=True), "updated_at": now()}
    await db.admins.update_one({"_id": target["_id"]}, {"$set": update})
    if body.active is False:
        await revoke_all(db, target["_id"])  # déconnecte ses sessions
    await log_activity(
        db,
        admin,
        "member.updated",
        "admin",
        target["_id"],
        {"email": target["email"], **body.model_dump(exclude_none=True)},
        account_id=target.get("account_id"),
    )
    return admin_out(await db.admins.find_one({"_id": target["_id"]}))


# ---------------------------------------------------------------- invitations


def invitation_out(inv: dict) -> dict:
    return {
        "id": sid(inv["_id"]),
        "email": inv["email"],
        "role": inv["role"],
        "invited_by_name": inv.get("invited_by_name"),
        "language": inv.get("language"),
        "created_at": inv.get("created_at"),
        "expires_at": inv.get("expires_at"),
        "expired": inv["expires_at"].replace(tzinfo=None) < now().replace(tzinfo=None),
    }


class InvitationIn(BaseModel):
    email: EmailStr
    role: Literal["developer", "viewer"]


async def _send_invitation(db, mailer, request: Request, admin: dict, inv: dict) -> dict:
    """Nouveau jeton, nouvelle échéance, e-mail dans la langue actuelle de la console."""
    s = get_settings()
    token, token_hash = _new_token()
    lang = language(request)
    update = {
        "token_hash": token_hash,
        "language": lang,
        "expires_at": now() + timedelta(days=s.invitation_ttl_days),
        "invited_by": admin["_id"],
        "invited_by_name": admin.get("name"),
        "updated_at": now(),
    }
    await db.invitations.update_one({"_id": inv["_id"]}, {"$set": update})
    account = await db.accounts.find_one({"_id": inv["account_id"]})
    url = f"{s.console_url.rstrip('/')}/invite/{token}"
    await mailer.send(
        invitation_email(lang, inv["email"], admin.get("name") or admin["email"], account["name"], inv["role"], url, s.invitation_ttl_days)
    )
    return {**inv, **update}


@router.get("/invitations")
async def list_invitations(db: Db, admin: TeamManager):
    invs = await db.invitations.find({"account_id": admin["account_id"], "accepted_at": None}).sort("created_at", -1).to_list(None)
    return [invitation_out(i) for i in invs]


@router.post("/invitations", status_code=201)
async def create_invitation(db: Db, request: Request, mailer: MailerDep, admin: TeamManager, body: InvitationIn):
    """Invite une personne dans le compte (rôle développeur ou lecteur) et lui envoie un e-mail."""
    _limit(request, f"invite:{admin['_id']}")
    if body.role not in INVITABLE_ROLES:
        raise ApiError(422, "invalid_role")
    email = body.email.lower()
    if await db.admins.find_one({"email": email}):
        raise ApiError(409, "already_member")
    # Une nouvelle invitation remplace l'éventuelle invitation en attente pour la même adresse
    await db.invitations.delete_many({"account_id": admin["account_id"], "email": email, "accepted_at": None})
    inv = {
        "account_id": admin["account_id"],
        "email": email,
        "role": body.role,
        "token_hash": secrets.token_hex(16),  # remplacé à l'envoi
        "accepted_at": None,
        "created_at": now(),
        "expires_at": now(),
    }
    inv["_id"] = (await db.invitations.insert_one(inv)).inserted_id
    inv = await _send_invitation(db, mailer, request, admin, inv)
    await log_activity(db, admin, "member.invited", "invitation", inv["_id"], {"email": email, "role": body.role})
    return invitation_out(inv)


async def _own_invitation(db, admin: dict, invitation_id: str) -> dict:
    inv = await db.invitations.find_one({"_id": oid(invitation_id, "not_found"), "account_id": admin["account_id"], "accepted_at": None})
    if not inv:
        raise ApiError(404, "not_found")
    return inv


@router.post("/invitations/{invitation_id}/resend")
async def resend_invitation(db: Db, request: Request, mailer: MailerDep, admin: TeamManager, invitation_id: str):
    _limit(request, f"invite:{admin['_id']}")
    inv = await _send_invitation(db, mailer, request, admin, await _own_invitation(db, admin, invitation_id))
    return invitation_out(inv)


@router.delete("/invitations/{invitation_id}", status_code=204)
async def revoke_invitation(db: Db, admin: TeamManager, invitation_id: str):
    inv = await _own_invitation(db, admin, invitation_id)
    await db.invitations.delete_one({"_id": inv["_id"]})
    await log_activity(db, admin, "member.invitation_revoked", "invitation", inv["_id"], {"email": inv["email"]})


async def _valid_invitation(db, token: str) -> dict:
    inv = await db.invitations.find_one({"token_hash": _hash(token), "accepted_at": None})
    if not inv or inv["expires_at"].replace(tzinfo=None) < now().replace(tzinfo=None):
        raise ApiError(400, "invitation_invalid")
    return inv


@router.get("/invitations/lookup")
async def lookup_invitation(db: Db, token: str = Query(min_length=10, max_length=200)):
    """Page d'acceptation (publique) : compte, rôle et auteur de l'invitation."""
    inv = await _valid_invitation(db, token)
    account = await db.accounts.find_one({"_id": inv["account_id"]})
    return {
        "email": inv["email"],
        "role": inv["role"],
        "account_name": account["name"] if account else "",
        "invited_by_name": inv.get("invited_by_name"),
        "already_registered": bool(await db.admins.find_one({"email": inv["email"]})),
    }


class AcceptIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=256)


@router.post("/invitations/accept", response_model=AdminSession)
async def accept_invitation(db: Db, request: Request, body: AcceptIn):
    """Crée le compte du membre invité (adresse confirmée par le lien) et ouvre une session."""
    inv = await _valid_invitation(db, body.token)
    if await db.accounts.find_one({"_id": inv["account_id"], "suspended": True}, {"_id": 1}):
        raise ApiError(403, "account_suspended")
    if await db.admins.find_one({"email": inv["email"]}):
        raise ApiError(409, "already_member")
    doc = {
        "email": inv["email"],
        "password_hash": hash_password(body.password),
        "name": body.name.strip(),
        "role": inv["role"],
        "account_id": inv["account_id"],
        "active": True,
        "email_verified": True,
        "language": language(request),
        "created_at": now(),
    }
    try:
        doc["_id"] = (await db.admins.insert_one(doc)).inserted_id
    except DuplicateKeyError:
        raise ApiError(409, "already_member") from None
    await db.invitations.update_one({"_id": inv["_id"]}, {"$set": {"accepted_at": now(), "admin_id": doc["_id"]}})
    await log_activity(db, doc, "member.joined", "admin", doc["_id"], {"email": doc["email"], "role": doc["role"]})
    return await _session(db, doc)
