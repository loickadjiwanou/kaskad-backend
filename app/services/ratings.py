"""Notes et avis des utilisateurs : agrégats dénormalisés sur l'app (moyenne, nombre, répartition 1 à 5 étoiles)."""

from bson import ObjectId
from pymongo.asynchronous.database import AsyncDatabase

from app.models.common import sid


async def refresh_rating(db: AsyncDatabase, app_id: ObjectId) -> None:
    """Recalcule la note de l'app à partir des avis visibles (les avis masqués par la modération ne comptent pas)."""
    rows = await (
        await db.reviews.aggregate(
            [{"$match": {"app_id": app_id, "hidden": {"$ne": True}}}, {"$group": {"_id": "$rating", "n": {"$sum": 1}}}]
        )
    ).to_list(None)
    distribution = {str(i): 0 for i in range(1, 6)}
    for r in rows:
        distribution[str(r["_id"])] = r["n"]
    count = sum(distribution.values())
    average = round(sum(int(k) * v for k, v in distribution.items()) / count, 2) if count else None
    await db.apps.update_one({"_id": app_id}, {"$set": {"rating": {"average": average, "count": count, "distribution": distribution}}})


def rating_out(app: dict) -> dict:
    r = app.get("rating") or {}
    return {
        "average": r.get("average"),
        "count": r.get("count", 0),
        "distribution": r.get("distribution") or {str(i): 0 for i in range(1, 6)},
    }


def review_out(r: dict, user_id=None, admin: bool = False) -> dict:
    out = {
        "id": sid(r["_id"]),
        "app_id": sid(r["app_id"]),
        "rating": r["rating"],
        "body": r.get("body", ""),
        "author_name": r.get("author_name") or "",
        "version_name": r.get("version_name"),
        "language": r.get("language"),
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
        "reply": (
            {"body": r["reply"]["body"], "author_name": r["reply"].get("author_name"), "replied_at": r["reply"].get("replied_at")}
            if r.get("reply")
            else None
        ),
        "is_mine": user_id is not None and r.get("user_id") == user_id,
    }
    if admin:
        out.update(
            {
                "hidden": bool(r.get("hidden")),
                "hidden_reason": r.get("hidden_reason"),
                "reports_count": len(r.get("reports") or []),
                "report_reasons": sorted({x.get("reason") for x in r.get("reports") or [] if x.get("reason")}),
            }
        )
    return out
