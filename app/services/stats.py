"""Statistiques : téléchargements, vues de fiche, conversion, pays et versions réellement installées.

- `download_stats` : un document par téléchargement (reprises non comptées).
- `app_views` : une vue de fiche par visiteur et par app toutes les 30 minutes (app client ou page web publique).
- `installations` : dernière version signalée par chaque appareil lors de la vérification des mises à jour
  (identifiant d'appareil haché) ; seuls les appareils vus récemment comptent.
"""

import hashlib
from datetime import datetime, timedelta

from bson import ObjectId
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import get_settings
from app.models.common import now, sid

METRICS = {"downloads": "download_stats", "views": "app_views"}
VIEW_DEDUP = timedelta(minutes=30)


def visitor_hash(value: str) -> str:
    """Identifiant de visiteur ou d'appareil non réversible (jamais l'adresse IP ou l'identifiant en clair)."""
    return hashlib.sha256(f"{get_settings().jwt_secret}:visitor:{value}".encode()).hexdigest()[:32]


async def record_download(db: AsyncDatabase, app_id: ObjectId, version: dict, platform: str | None, country: str | None = None) -> None:
    await db.download_stats.insert_one(
        {
            "app_id": app_id,
            "version_id": version["_id"],
            "timestamp": now(),
            "platform": platform or version["platform"],
            "file_format": version["file_format"],
            "country": country,
        }
    )
    await db.versions.update_one({"_id": version["_id"]}, {"$inc": {"downloads_count": 1}})
    await db.apps.update_one({"_id": app_id}, {"$inc": {"downloads_count": 1}})


async def record_view(db: AsyncDatabase, app_id: ObjectId, visitor: str, platform: str | None, source: str, country: str | None) -> bool:
    """Compte une vue de fiche ; ignorée si le même visiteur a vu l'app dans les 30 dernières minutes."""
    since = now() - VIEW_DEDUP
    if await db.app_views.find_one({"app_id": app_id, "visitor": visitor, "timestamp": {"$gte": since}}, {"_id": 1}):
        return False
    await db.app_views.insert_one(
        {"app_id": app_id, "visitor": visitor, "timestamp": now(), "platform": platform, "source": source, "country": country}
    )
    await db.apps.update_one({"_id": app_id}, {"$inc": {"views_count": 1}})
    return True


async def record_installations(db: AsyncDatabase, device: str, items: list[dict], country: str | None) -> None:
    """Versions installées signalées par un appareil ({app_id, version_id, version_code, platform})."""
    for item in items:
        await db.installations.update_one(
            {"device": device, "app_id": item["app_id"]},
            {
                "$set": {
                    "version_id": item.get("version_id"),
                    "version_code": item["version_code"],
                    "platform": item.get("platform"),
                    "country": country,
                    "last_seen": now(),
                },
                "$setOnInsert": {"first_seen": now()},
            },
            upsert=True,
        )


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


async def timeseries(
    db: AsyncDatabase,
    start: datetime | None,
    end: datetime | None,
    interval: str = "day",
    app_id: ObjectId | None = None,
    version_id: ObjectId | None = None,
    app_ids: list | None = None,
    metric: str = "downloads",
) -> list[dict]:
    pipeline = [
        {"$match": _range_filter(start, end, app_id, None if metric == "views" else version_id, app_ids)},
        {"$group": {"_id": {"$dateTrunc": {"date": "$timestamp", "unit": interval}}, "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]
    rows = await (await db[METRICS[metric]].aggregate(pipeline)).to_list(None)
    return [{"date": r["_id"], "count": r["count"]} for r in rows]


async def breakdown(db: AsyncDatabase, field: str, start, end, app_id=None, app_ids=None, metric: str = "downloads") -> list[dict]:
    pipeline = [
        {"$match": _range_filter(start, end, app_id, None, app_ids)},
        {"$group": {"_id": f"${field}", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    rows = await (await db[METRICS[metric]].aggregate(pipeline)).to_list(None)
    return [{"key": sid(r["_id"]) if isinstance(r["_id"], ObjectId) else r["_id"], "count": r["count"]} for r in rows]


async def funnel(db: AsyncDatabase, start, end, app_id=None, app_ids=None) -> dict:
    """Vues de fiche, visiteurs uniques, téléchargements et taux de conversion (téléchargements / vues)."""
    match = _range_filter(start, end, app_id, None, app_ids)
    views = await db.app_views.count_documents(match)
    visitors = await (await db.app_views.aggregate([{"$match": match}, {"$group": {"_id": "$visitor"}}, {"$count": "n"}])).to_list(None)
    downloads = await db.download_stats.count_documents(match)
    return {
        "views": views,
        "visitors": visitors[0]["n"] if visitors else 0,
        "downloads": downloads,
        # Peut dépasser 1 : téléchargements sans vue comptée (lien direct, plusieurs fichiers téléchargés)
        "conversion": round(downloads / views, 4) if views else None,
    }


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
    views = {}
    if rows:
        view_rows = await (
            await db.app_views.aggregate(
                [
                    {"$match": {**_range_filter(start, end, None, None, None), "app_id": {"$in": [r["_id"] for r in rows]}}},
                    {"$group": {"_id": "$app_id", "count": {"$sum": 1}}},
                ]
            )
        ).to_list(None)
        views = {r["_id"]: r["count"] for r in view_rows}
    return [
        {
            "app_id": sid(r["_id"]),
            "name": (r.get("app") or {}).get("name"),
            "downloads": r["count"],
            "views": views.get(r["_id"], 0),
            "conversion": round(r["count"] / views[r["_id"]], 4) if views.get(r["_id"]) else None,
        }
        for r in rows
    ]


async def installed_base(db: AsyncDatabase, app_id: ObjectId | None = None, app_ids: list | None = None) -> dict:
    """Appareils actifs qui ont l'app installée : par version (une app) ou par app (compte / plateforme)."""
    match: dict = {"last_seen": {"$gte": now() - timedelta(days=get_settings().installations_active_days)}}
    if app_ids is not None:
        match["app_id"] = {"$in": app_ids}
    if app_id:
        match["app_id"] = app_id
    total = await db.installations.count_documents(match)
    if not app_id:
        rows = await (
            await db.installations.aggregate(
                [
                    {"$match": match},
                    {"$group": {"_id": "$app_id", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}},
                    {"$limit": 20},
                    {"$lookup": {"from": "apps", "localField": "_id", "foreignField": "_id", "as": "app"}},
                    {"$unwind": {"path": "$app", "preserveNullAndEmptyArrays": True}},
                ]
            )
        ).to_list(None)
        return {
            "total": total,
            "items": [{"app_id": sid(r["_id"]), "name": (r.get("app") or {}).get("name"), "count": r["count"]} for r in rows],
        }

    rows = await (
        await db.installations.aggregate(
            [
                {"$match": match},
                {"$group": {"_id": {"code": "$version_code", "platform": "$platform"}, "count": {"$sum": 1}}},
                {"$sort": {"_id.code": -1}},
            ]
        )
    ).to_list(None)
    versions = await db.versions.find(
        {"app_id": app_id}, {"version_code": 1, "version_name": 1, "platform": 1, "status": 1, "channel": 1}
    ).to_list(None)
    names = {(v["version_code"], v["platform"]): v["version_name"] for v in versions}
    names_any = {v["version_code"]: v["version_name"] for v in versions}
    # Dernière version publiée en production, par plateforme
    latest: dict = {}
    for v in versions:
        if v.get("status") == "published" and v.get("channel") != "beta":
            latest[v["platform"]] = max(latest.get(v["platform"], 0), v["version_code"])
    items = []
    on_latest = 0
    for r in rows:
        code, platform = r["_id"]["code"], r["_id"].get("platform")
        is_latest = bool(platform and latest.get(platform) and code >= latest[platform]) or (
            not platform and code >= max(latest.values(), default=0) > 0
        )
        on_latest += r["count"] if is_latest else 0
        items.append(
            {
                "version_code": code,
                "version_name": names.get((code, platform)) or names_any.get(code) or str(code),
                "platform": platform,
                "count": r["count"],
                "latest": is_latest,
            }
        )
    return {"total": total, "on_latest": on_latest, "items": items}


async def raw_downloads(db: AsyncDatabase, start, end, app_id=None, app_ids=None):
    """Itérateur des téléchargements (export CSV)."""
    cursor = db.download_stats.find(_range_filter(start, end, app_id, None, app_ids)).sort("timestamp", 1)
    async for row in cursor:
        yield row
