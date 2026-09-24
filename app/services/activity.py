"""Journal d'activité (qui a fait quoi, quand) consulté depuis la console admin."""

from bson import ObjectId
from pymongo.asynchronous.database import AsyncDatabase

from app.models.common import now, sid


async def log_activity(db: AsyncDatabase, admin: dict, action: str, target_type: str, target_id, details: dict | None = None):
    await db.activity_log.insert_one(
        {
            "actor_id": admin["_id"],
            "actor_email": admin.get("email"),
            "actor_name": admin.get("name"),
            "action": action,
            "target_type": target_type,
            "target_id": target_id if isinstance(target_id, ObjectId) or target_id is None else str(target_id),
            "details": details or {},
            "created_at": now(),
        }
    )


async def log_system(db: AsyncDatabase, action: str, target_type: str, target_id, details: dict | None = None):
    await db.activity_log.insert_one(
        {
            "actor_id": None,
            "actor_email": "system",
            "actor_name": "Kaskad",
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "details": details or {},
            "created_at": now(),
        }
    )


def activity_out(e: dict) -> dict:
    return {
        "id": sid(e["_id"]),
        "actor_id": sid(e.get("actor_id")),
        "actor_email": e.get("actor_email"),
        "actor_name": e.get("actor_name"),
        "action": e["action"],
        "target_type": e.get("target_type"),
        "target_id": sid(e["target_id"]) if isinstance(e.get("target_id"), ObjectId) else e.get("target_id"),
        "details": e.get("details", {}),
        "created_at": e["created_at"],
    }
