"""Comptes développeurs (type Google Play Console).

- `admin` : administrateur de la plateforme (ADMIN_EMAIL, unique, jamais attribuable). Valide et publie
  les demandes de tous les comptes, gère les catégories. Il possède aussi le compte de la plateforme.
- Chaque inscription crée un compte développeur dont l'inscrit est le propriétaire (`owner`).
  Le propriétaire invite des membres : `developer` (gère les apps et les soumet à validation)
  ou `viewer` (consultation seule). Un membre appartient à un seul compte.
"""

import logging

from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.common import now, sid

log = logging.getLogger("kaskad")

PLATFORM_ADMIN = "admin"
OWNER = "owner"
DEVELOPER = "developer"
VIEWER = "viewer"
INVITABLE_ROLES = (DEVELOPER, VIEWER)
PLATFORM_ACCOUNT_NAME = "Kaskad"


def is_platform_admin(admin: dict) -> bool:
    return admin.get("role") == PLATFORM_ADMIN


def can_write(admin: dict) -> bool:
    return admin.get("role") in (PLATFORM_ADMIN, OWNER, DEVELOPER)


def can_manage_team(admin: dict) -> bool:
    return admin.get("role") in (PLATFORM_ADMIN, OWNER)


def account_scope(admin: dict, account_id=None) -> dict:
    """Filtre MongoDB des éléments visibles : le compte du membre ; tout pour l'administrateur de la plateforme,
    qui peut aussi se restreindre à un compte (`account_id`)."""
    if is_platform_admin(admin):
        return {"account_id": account_id} if account_id else {}
    return {"account_id": admin.get("account_id")}


async def scoped_app_ids(db: AsyncDatabase, admin: dict, account_id=None) -> list | None:
    """IDs des apps visibles (None = toutes les apps : administrateur de la plateforme sans filtre de compte)."""
    scope = account_scope(admin, account_id)
    if not scope:
        return None
    return [a["_id"] for a in await db.apps.find(scope, {"_id": 1}).to_list(None)]


def account_out(acc: dict | None) -> dict | None:
    if not acc:
        return None
    return {"id": sid(acc["_id"]), "name": acc["name"], "owner_id": sid(acc.get("owner_id")), "created_at": acc.get("created_at")}


async def create_account(db: AsyncDatabase, name: str, owner_id=None) -> dict:
    doc = {"name": name, "owner_id": owner_id, "created_at": now(), "updated_at": now()}
    doc["_id"] = (await db.accounts.insert_one(doc)).inserted_id
    return doc


async def bootstrap(db: AsyncDatabase) -> None:
    """Démarrage : administrateur de la plateforme, compte de la plateforme et migration des données existantes.

    Idempotent : peut s'exécuter à chaque démarrage.
    """
    s = get_settings()
    admin = None
    if s.admin_email:
        admin = await db.admins.find_one({"email": s.admin_email.lower()})
        if not admin and s.admin_password:
            doc = {
                "email": s.admin_email.lower(),
                "password_hash": hash_password(s.admin_password),
                "name": "Administrator",
                "role": PLATFORM_ADMIN,
                "active": True,
                "email_verified": True,
                "created_at": now(),
            }
            doc["_id"] = (await db.admins.insert_one(doc)).inserted_id
            admin = doc
            log.info("Platform admin account created: %s", s.admin_email)
    if not admin:
        admin = await db.admins.find_one({"role": PLATFORM_ADMIN}, sort=[("created_at", 1)])
    if not admin:
        return

    # L'administrateur de la plateforme est unique et possède le compte de la plateforme
    if admin.get("role") != PLATFORM_ADMIN or not admin.get("active", True):
        await db.admins.update_one({"_id": admin["_id"]}, {"$set": {"role": PLATFORM_ADMIN, "active": True}})
    account_id = admin.get("account_id")
    if not account_id or not await db.accounts.find_one({"_id": account_id}):
        account_id = (await create_account(db, PLATFORM_ACCOUNT_NAME, admin["_id"]))["_id"]
        await db.admins.update_one({"_id": admin["_id"]}, {"$set": {"account_id": account_id}})

    # Anciens rôles (admin complet / éditeur de contenu) → développeurs du compte de la plateforme
    await db.admins.update_many(
        {"_id": {"$ne": admin["_id"]}, "role": {"$in": [PLATFORM_ADMIN, "editor"]}},
        {"$set": {"role": DEVELOPER, "account_id": account_id}},
    )
    await db.admins.update_many({"account_id": {"$exists": False}}, {"$set": {"account_id": account_id}})
    await db.admins.update_many({"email_verified": {"$exists": False}}, {"$set": {"email_verified": True}})
    # Contenus existants → compte de la plateforme
    await db.apps.update_many({"account_id": {"$exists": False}}, {"$set": {"account_id": account_id}})
    await db.activity_log.update_many({"account_id": {"$exists": False}}, {"$set": {"account_id": account_id}})
    # Nom et état du compte recopiés sur les apps (catalogue public : nom du développeur, apps masquées si suspendu)
    async for acc in db.accounts.find({}, {"name": 1, "suspended": 1}):
        await db.apps.update_many(
            {"account_id": acc["_id"]}, {"$set": {"account_name": acc["name"], "account_suspended": bool(acc.get("suspended"))}}
        )


async def platform_account_id(db: AsyncDatabase):
    admin = await db.admins.find_one({"role": PLATFORM_ADMIN}, {"account_id": 1})
    return admin.get("account_id") if admin else None
