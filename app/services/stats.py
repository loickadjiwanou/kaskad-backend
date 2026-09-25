"""Compteurs et statistiques de téléchargement."""

from datetime import datetime, timedelta

from bson import ObjectId
from pymongo.asynchronous.database import AsyncDatabase

from app.models.common import now, sid


async def record_download(db: AsyncDatabase, app_id: ObjectId, version: dict, platform: str | None) -> None:
    await db.download_stats.insert_one(
        {
            "app_id": app_id,
            "version_id": version["_id"],
            "timestamp": now(),
            "platform": platform or version["platform"],
            "file_format": version["file_format"],
        }
    )
    await db.versions.update_one({"_id": version["_id"]}, {"$inc": {"downloads_count": 1}})
    await db.apps.update_one({"_id": app_id}, {"$inc": {"downloads_count": 1}})


def _range_filter(
    start: datetime | None, end: datetime | None, app_id: ObjectId | None, version_id: ObjectId | None, app_ids: list | None = None
) -> dict:
    """`app_ids` : apps du compte développeur (None = toutes, administrateur de la plateforme)."""
    end = end or now()
    start = start or end - timedelta(days=30)
    match: dict = {"timestamp": {"$gte": start, "$lte": end}}
    if app_ids is not None:
        match["app_id"] = {"$in": app_ids}
    if app_id:
        match["app_id"] = app_id
    if version_id:
        match["version_id"] = version_id
    return match


async def downloads_timeseries(
    db: AsyncDatabase,
    start: datetime | None,
    end: datetime | None,
    interval: str = "day",
    app_id: ObjectId | None = None,
    version_id: ObjectId | None = None,
    app_ids: list | None = None,
) -> list[dict]:
    pipeline = [
        {"$match": _range_filter(start, end, app_id, version_id, app_ids)},
        {"$group": {"_id": {"$dateTrunc": {"date": "$timestamp", "unit": interval}}, "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]
    rows = await (await db.download_stats.aggregate(pipeline)).to_list(None)
    return [{"date": r["_id"], "count": r["count"]} for r in rows]


async def downloads_by(db: AsyncDatabase, field: str, start, end, app_id=None, app_ids=None) -> list[dict]:
    pipeline = [
        {"$match": _range_filter(start, end, app_id, None, app_ids)},
        {"$group": {"_id": f"${field}", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    rows = await (await db.download_stats.aggregate(pipeline)).to_list(None)
    return [{"key": sid(r["_id"]) if isinstance(r["_id"], ObjectId) else r["_id"], "count": r["count"]} for r in rows]


async def top_apps(db: AsyncDatabase, start, end, limit: int = 10, app_ids=None) -> list[dict]:
    pipeline = [
        {"$match": _range_filter(start, end, None, None, app_ids)},
        {"$group": {"_id": "$app_id", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": limit},
        {"$lookup": {"from": "apps", "localField": "_id", "foreignField": "_id", "as": "app"}},
        {"$unwind": {"path": "$app", "preserveNullAndEmptyArrays": True}},
    ]
    rows = await (await db.download_stats.aggregate(pipeline)).to_list(None)
    return [{"app_id": sid(r["_id"]), "name": (r.get("app") or {}).get("name"), "downloads": r["count"]} for r in rows]


async def raw_downloads(db: AsyncDatabase, start, end, app_id=None, app_ids=None):
    """Itérateur des téléchargements (export CSV)."""
    cursor = db.download_stats.find(_range_filter(start, end, app_id, None, app_ids)).sort("timestamp", 1)
    async for row in cursor:
        yield row
