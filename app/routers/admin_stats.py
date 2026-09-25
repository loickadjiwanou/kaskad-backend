"""Tableau de bord, statistiques, export CSV, file de modération et journal d'activité."""

import csv
import io
import re
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.deps import CurrentAdmin, Db, FullAdmin, TeamManager
from app.models.common import maybe_oid, now, sid
from app.routers.admin_apps import get_app_or_404
from app.services.accounts import account_scope, is_platform_admin, scoped_app_ids
from app.services.activity import activity_out
from app.services.catalog import app_admin, version_admin
from app.services.stats import downloads_by, downloads_timeseries, raw_downloads, top_apps

router = APIRouter(prefix="/admin", tags=["admin: stats & moderation"])

Interval = Literal["day", "week", "month"]


@router.get("/stats/overview")
async def overview(db: Db, admin: CurrentAdmin):
    """Chiffres du compte développeur (de toute la plateforme pour l'administrateur)."""
    scope = account_scope(admin)
    ids = await scoped_app_ids(db, admin)
    in_apps = {} if ids is None else {"app_id": {"$in": ids}}
    status_counts = {
        r["_id"]: r["n"]
        for r in await (await db.apps.aggregate([{"$match": scope}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}])).to_list(None)
    }
    total_downloads = (
        await db.download_stats.estimated_document_count() if ids is None else await db.download_stats.count_documents(in_apps)
    )
    last_30 = await db.download_stats.count_documents({"timestamp": {"$gte": now() - timedelta(days=30)}, **in_apps})
    pending_review = await db.versions.count_documents(
        {"upload_status": "stored", "status": "draft", "security_scan_status": {"$in": ["pending", "scanning", "passed"]}, **in_apps}
    )
    reviews_pending = (
        await db.versions.count_documents({"review.state": "pending", "status": {"$in": ["draft", "published"]}, **in_apps})
        + await db.apps.count_documents({"status_request.state": "pending", **scope})
        + await db.apps.count_documents({"listing_review.state": "pending", **scope})
    )
    recent = await (
        await db.versions.aggregate(
            [
                {"$match": {"status": "published", **in_apps}},
                {"$sort": {"published_at": -1}},
                {"$limit": 10},
                {"$lookup": {"from": "apps", "localField": "app_id", "foreignField": "_id", "as": "app"}},
                {"$unwind": "$app"},
            ]
        )
    ).to_list(None)
    return {
        "apps": {
            "published": status_counts.get("published", 0),
            "draft": status_counts.get("draft", 0),
            "archived": status_counts.get("archived", 0),
        },
        "total_downloads": total_downloads,
        "downloads_last_30_days": last_30,
        "versions_pending_review": pending_review,
        # Demandes des éditeurs en attente de validation par un admin complet
        "reviews_pending": reviews_pending,
        # Modération des utilisateurs (administrateur) : apps signalées, avis signalés
        "reports_open": await db.app_reports.count_documents({"status": "open", **scope}) if is_platform_admin(admin) else 0,
        "user_reviews_reported": await db.reviews.count_documents({"reports.0": {"$exists": True}}) if is_platform_admin(admin) else 0,
        "recent_publications": [
            {
                "version_id": sid(v["_id"]),
                "app_id": sid(v["app_id"]),
                "app_name": v["app"]["name"],
                "version_name": v["version_name"],
                "platform": v["platform"],
                "file_format": v["file_format"],
                "published_at": v.get("published_at"),
            }
            for v in recent
        ],
    }


@router.get("/stats/downloads")
async def downloads(
    db: Db,
    admin: CurrentAdmin,
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    interval: Interval = "day",
    app_id: str | None = None,
    version_id: str | None = None,
    account_id: str | None = None,  # administrateur de la plateforme : un compte développeur
):
    """Téléchargements dans le temps (global, par app ou par version)."""
    if app_id:
        await get_app_or_404(db, app_id, admin)
    ids = await scoped_app_ids(db, admin, maybe_oid(account_id))
    return await downloads_timeseries(db, start, end, interval, maybe_oid(app_id), maybe_oid(version_id), ids)


@router.get("/stats/breakdown")
async def breakdown(
    db: Db,
    admin: CurrentAdmin,
    by: Literal["platform", "file_format", "version_id", "app_id"] = "platform",
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    app_id: str | None = None,
    account_id: str | None = None,  # administrateur de la plateforme : un compte développeur
):
    if app_id:
        await get_app_or_404(db, app_id, admin)
    return await downloads_by(db, by, start, end, maybe_oid(app_id), await scoped_app_ids(db, admin, maybe_oid(account_id)))


@router.get("/stats/top-apps")
async def most_downloaded(
    db: Db,
    admin: CurrentAdmin,
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=10, ge=1, le=100),
    account_id: str | None = None,  # administrateur de la plateforme : un compte développeur
):
    return await top_apps(db, start, end, limit, await scoped_app_ids(db, admin, maybe_oid(account_id)))


@router.get("/stats/export.csv")
async def export_csv(
    db: Db,
    admin: CurrentAdmin,
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    app_id: str | None = None,
    account_id: str | None = None,  # administrateur de la plateforme : un compte développeur
):
    """Export CSV des téléchargements (une ligne par téléchargement)."""
    if app_id:
        await get_app_or_404(db, app_id, admin)
    ids = await scoped_app_ids(db, admin, maybe_oid(account_id))
    app_docs = await db.apps.find(account_scope(admin, maybe_oid(account_id)), {"name": 1, "account_id": 1}).to_list(None)
    apps = {a["_id"]: a["name"] for a in app_docs}
    accounts = {
        c["_id"]: c["name"] for c in await db.accounts.find({"_id": {"$in": list({a.get("account_id") for a in app_docs})}}).to_list(None)
    }
    app_account = {a["_id"]: accounts.get(a.get("account_id"), "") for a in app_docs}
    versions = {
        v["_id"]: v["version_name"]
        for v in await db.versions.find({} if ids is None else {"app_id": {"$in": ids}}, {"version_name": 1}).to_list(None)
    }

    async def rows():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["timestamp", "account", "app_id", "app_name", "version_id", "version_name", "platform", "file_format"])
        yield buf.getvalue()
        async for r in raw_downloads(db, start, end, maybe_oid(app_id), ids):
            buf.seek(0)
            buf.truncate()
            writer.writerow(
                [
                    r["timestamp"].isoformat(),
                    app_account.get(r["app_id"], ""),
                    sid(r["app_id"]),
                    apps.get(r["app_id"], ""),
                    sid(r["version_id"]),
                    versions.get(r["version_id"], ""),
                    r.get("platform", ""),
                    r.get("file_format", ""),
                ]
            )
            yield buf.getvalue()

    return StreamingResponse(
        rows(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="kaskad-downloads.csv"'}
    )


@router.get("/moderation/queue")
async def moderation_queue(db: Db, admin: FullAdmin):
    """Administrateur de la plateforme — versions en attente : analyse en cours, rejetées, ou validées mais pas encore publiées."""
    ids = await scoped_app_ids(db, admin)
    in_apps = {} if ids is None else {"app_id": {"$in": ids}}
    versions = (
        await db.versions.find({"upload_status": "stored", "status": {"$in": ["draft", "scheduled"]}, **in_apps})
        .sort("created_at", -1)
        .to_list(None)
    )
    apps = {
        a["_id"]: a["name"] for a in await db.apps.find({"_id": {"$in": list({v["app_id"] for v in versions})}}, {"name": 1}).to_list(None)
    }
    return [{**version_admin(v), "app_name": apps.get(v["app_id"])} for v in versions]


@router.get("/moderation/reviews")
async def moderation_reviews(db: Db, admin: FullAdmin):
    """Administrateur de la plateforme — demandes de validation (en attente ou refusées) : versions soumises, changements de statut, modifications de fiche."""
    states = {"$in": ["pending", "rejected"]}
    scope = account_scope(admin)
    ids = await scoped_app_ids(db, admin)
    in_apps = {} if ids is None else {"app_id": {"$in": ids}}
    versions = (
        await db.versions.find({"review.state": states, "status": {"$in": ["draft", "published"]}, **in_apps})
        .sort("review.submitted_at", -1)
        .to_list(None)
    )
    names = {
        a["_id"]: (a["name"], a.get("status"))
        for a in await db.apps.find({"_id": {"$in": list({v["app_id"] for v in versions})}}, {"name": 1, "status": 1}).to_list(None)
    }
    status_requests = await db.apps.find({"status_request.state": states, **scope}).sort("status_request.submitted_at", -1).to_list(None)
    listings = await db.apps.find({"listing_review.state": states, **scope}).sort("listing_review.submitted_at", -1).to_list(None)
    return {
        "versions": [
            {**version_admin(v), "app_name": names.get(v["app_id"], (None, None))[0], "app_status": names.get(v["app_id"], (None, None))[1]}
            for v in versions
        ],
        "status_requests": [app_admin(a) for a in status_requests],
        "listings": [app_admin(a) for a in listings],
    }


@router.get("/activity")
async def activity(
    db: Db,
    admin: TeamManager,  # propriétaires (activité de leur compte) et administrateur de la plateforme (tout)
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    action: str | None = None,
    actor_id: str | None = None,
    account_id: str | None = None,
):
    # Journal du compte ; l'administrateur de la plateforme voit tout (ou un compte avec `account_id`)
    flt: dict = account_scope(admin)
    if account_id and is_platform_admin(admin):
        flt["account_id"] = maybe_oid(account_id)
    if action:
        flt["action"] = {"$regex": f"^{re.escape(action)}"}
    if actor_id:
        flt["actor_id"] = maybe_oid(actor_id)
    items = await db.activity_log.find(flt).sort("created_at", -1).skip((page - 1) * limit).limit(limit).to_list(None)
    return {"items": [activity_out(e) for e in items], "total": await db.activity_log.count_documents(flt), "page": page, "limit": limit}
