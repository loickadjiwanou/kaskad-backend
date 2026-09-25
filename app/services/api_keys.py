"""Clés API des comptes développeurs, pour publier depuis l'intégration continue (GitHub Actions, GitLab CI…).

Une clé agit avec les droits d'un développeur de son compte : créer et modifier des apps, envoyer des versions
et les soumettre à validation. Elle ne gère ni l'équipe ni les clés, et ne publie jamais directement.
Seule l'empreinte SHA-256 de la clé est stockée ; la clé complète n'est affichée qu'à sa création.
"""

import hashlib
import secrets
from datetime import timedelta

from pymongo.asynchronous.database import AsyncDatabase

from app.core.i18n import ApiError
from app.models.common import now, sid

API_KEY_PREFIX = "ksk_"


def new_api_key() -> tuple[str, str]:
    key = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return key, hash_key(key)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def api_key_out(k: dict) -> dict:
    return {
        "id": sid(k["_id"]),
        "name": k["name"],
        "prefix": k["prefix"],
        "created_by_name": k.get("created_by_name"),
        "created_at": k.get("created_at"),
        "last_used_at": k.get("last_used_at"),
    }


async def api_key_admin(db: AsyncDatabase, key: str) -> dict:
    """Identité utilisée par les routes de la console pour une requête authentifiée par clé API."""
    record = await db.api_keys.find_one({"key_hash": hash_key(key), "revoked_at": None})
    if not record:
        raise ApiError(401, "invalid_api_key")
    if await db.accounts.find_one({"_id": record["account_id"], "suspended": True}, {"_id": 1}):
        raise ApiError(401, "account_suspended")
    # Date de dernière utilisation (au plus une écriture par minute)
    last = record.get("last_used_at")
    if not last or last.replace(tzinfo=None) < (now() - timedelta(minutes=1)).replace(tzinfo=None):
        await db.api_keys.update_one({"_id": record["_id"]}, {"$set": {"last_used_at": now()}})
    return {
        "_id": record["_id"],
        "name": f"API · {record['name']}",
        "email": "",
        "role": "developer",
        "account_id": record["account_id"],
        "active": True,
        "email_verified": True,
        "api_key": True,
    }
