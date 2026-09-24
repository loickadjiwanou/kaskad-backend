"""Authentification de la console et gestion des comptes admin (rôles : admin complet / éditeur de contenu)."""

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

from app.core.i18n import ApiError
from app.core.security import hash_password, verify_password
from app.deps import CurrentAdmin, Db, FullAdmin, LoginLimiter
from app.models.common import now, oid, sid
from app.models.schemas import AdminCreate, AdminSession, AdminUpdate, EmailPassword, RefreshIn
from app.services.activity import log_activity
from app.services.auth import issue_tokens, revoke_all, revoke_refresh_token, rotate_refresh_token

router = APIRouter(prefix="/admin", tags=["admin: auth"])


def admin_out(a: dict) -> dict:
    return {
        "id": sid(a["_id"]),
        "email": a["email"],
        "name": a.get("name", ""),
        "role": a.get("role", "editor"),
        "active": a.get("active", True),
        "created_at": a.get("created_at"),
    }


@router.post("/auth/login", response_model=AdminSession)
async def login(db: Db, request: Request, limiter: LoginLimiter, body: EmailPassword):
    limiter.check(request, f"admin:{body.email}")
    admin = await db.admins.find_one({"email": body.email.lower()})
    if not admin or not verify_password(body.password, admin.get("password_hash")):
        raise ApiError(401, "invalid_credentials")
    if not admin.get("active", True):
        raise ApiError(403, "account_disabled")
    limiter.reset(request, f"admin:{body.email}")
    await db.admins.update_one({"_id": admin["_id"]}, {"$set": {"last_login_at": now()}})
    return {**(await issue_tokens(db, admin["_id"], "admin")), "admin": admin_out(admin)}


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


@router.get("/auth/me")
async def me(admin: CurrentAdmin):
    return admin_out(admin)


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
    return admin_out(await db.admins.find_one({"_id": admin["_id"]}))


# ---------------------------------------------------------------- comptes admin (rôle admin requis)


@router.get("/admins")
async def list_admins(db: Db, _: FullAdmin):
    return [admin_out(a) for a in await db.admins.find().sort("created_at", 1).to_list(None)]


@router.post("/admins", status_code=201)
async def create_admin(db: Db, actor: FullAdmin, body: AdminCreate):
    doc = {
        "email": body.email.lower(),
        "password_hash": hash_password(body.password),
        "name": body.name,
        "role": body.role,
        "active": True,
        "created_at": now(),
    }
    try:
        doc["_id"] = (await db.admins.insert_one(doc)).inserted_id
    except DuplicateKeyError:
        raise ApiError(409, "email_taken") from None
    await log_activity(db, actor, "admin.created", "admin", doc["_id"], {"email": doc["email"], "role": body.role})
    return admin_out(doc)


async def _active_full_admins(db) -> int:
    return await db.admins.count_documents({"role": "admin", "active": True})


@router.patch("/admins/{admin_id}")
async def update_admin(db: Db, actor: FullAdmin, admin_id: str, body: AdminUpdate):
    target = await db.admins.find_one({"_id": oid(admin_id, "not_found")})
    if not target:
        raise ApiError(404, "not_found")
    losing_admin = target.get("role") == "admin" and target.get("active", True) and (body.role == "editor" or body.active is False)
    if losing_admin and await _active_full_admins(db) <= 1:
        raise ApiError(409, "last_admin")
    update = {k: v for k, v in body.model_dump(exclude_none=True).items() if k != "password"}
    if body.password:
        update["password_hash"] = hash_password(body.password)
    update["updated_at"] = now()
    await db.admins.update_one({"_id": target["_id"]}, {"$set": update})
    if body.active is False or body.password:
        await revoke_all(db, target["_id"])  # déconnecte ses sessions
    await log_activity(
        db, actor, "admin.updated", "admin", target["_id"], {k: v for k, v in body.model_dump(exclude_none=True).items() if k != "password"}
    )
    return admin_out(await db.admins.find_one({"_id": target["_id"]}))
