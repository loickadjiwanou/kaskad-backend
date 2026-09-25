"""Notes et avis, signalements.

- Utilisateurs (app client) : noter une app et laisser un avis (compte e-mail requis, un avis par app),
  le modifier ou le supprimer, signaler un avis ou une app.
- Comptes développeurs (console) : lire les avis de leurs apps et y répondre publiquement.
- Administrateur de la plateforme : masquer / rétablir un avis, traiter les signalements.
"""

import hashlib
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Query, Request
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.i18n import ApiError, language
from app.deps import CurrentAdmin, CurrentUser, Db, FullAdmin, MailerDep, OptionalUser, Writer
from app.models.common import now, oid, sid
from app.routers.admin_apps import get_app_or_404
from app.routers.users import display_name
from app.services.accounts import scoped_app_ids
from app.services.activity import log_activity
from app.services.catalog import PUBLIC_APP_FILTER
from app.services.emails import review_reply_email
from app.services.notify import notify_platform_admins
from app.services.ratings import rating_out, refresh_rating, review_out

router = APIRouter(tags=["reviews & reports"])

ReportReason = Literal["malware", "abusive", "copyright", "misleading", "broken", "other"]


def _limit(request: Request, key: str) -> None:
    request.app.state.signup_limiter.check(request, key)


async def _public_app(db, app_id: str) -> dict:
    app = await db.apps.find_one({"_id": oid(app_id, "app_not_found"), **PUBLIC_APP_FILTER})
    if not app:
        raise ApiError(404, "app_not_found")
    return app


# ---------------------------------------------------------------- app client : avis


@router.get("/apps/{app_id}/reviews")
async def list_reviews(
    db: Db,
    user: OptionalUser,
    app_id: str,
    sort: Literal["recent", "rating_desc", "rating_asc"] = "recent",
    rating: int | None = Query(default=None, ge=1, le=5),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
):
    """Avis visibles d'une app, avec la note (moyenne, nombre, répartition)."""
    app = await _public_app(db, app_id)
    flt: dict = {"app_id": app["_id"], "hidden": {"$ne": True}}
    if rating:
        flt["rating"] = rating
    order = {
        "recent": [("updated_at", -1)],
        "rating_desc": [("rating", -1), ("updated_at", -1)],
        "rating_asc": [("rating", 1), ("updated_at", -1)],
    }[sort]
    items = await db.reviews.find(flt).sort(order).skip((page - 1) * limit).limit(limit).to_list(None)
    uid = user["_id"] if user else None
    return {
        "items": [review_out(r, uid) for r in items],
        "total": await db.reviews.count_documents(flt),
        "page": page,
        "limit": limit,
        "rating": rating_out(app),
    }


class ReviewIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    body: str = Field(default="", max_length=2000)
    version_name: str | None = Field(default=None, max_length=50)


def _require_email_account(user: dict) -> None:
    if user.get("anonymous") or not user.get("email"):
        raise ApiError(403, "email_account_required")


@router.get("/apps/{app_id}/reviews/mine")
async def my_review(db: Db, user: CurrentUser, app_id: str):
    app = await _public_app(db, app_id)
    r = await db.reviews.find_one({"app_id": app["_id"], "user_id": user["_id"]})
    return review_out(r, user["_id"]) if r else None


@router.put("/apps/{app_id}/reviews/mine")
async def save_my_review(db: Db, request: Request, user: CurrentUser, app_id: str, body: ReviewIn):
    """Note (1 à 5) et avis de l'utilisateur ; un seul avis par app et par compte (modifiable)."""
    _require_email_account(user)
    app = await _public_app(db, app_id)
    fields = {
        "rating": body.rating,
        "body": body.body.strip(),
        # Nom public repris du compte (modifiable dans le profil de l'app client)
        "author_name": display_name(user),
        "version_name": body.version_name,
        "language": language(request),
        "updated_at": now(),
    }
    await db.reviews.update_one(
        {"app_id": app["_id"], "user_id": user["_id"]},
        {
            "$set": fields,
            "$setOnInsert": {"app_id": app["_id"], "user_id": user["_id"], "account_id": app.get("account_id"), "created_at": now()},
        },
        upsert=True,
    )
    await refresh_rating(db, app["_id"])
    return review_out(await db.reviews.find_one({"app_id": app["_id"], "user_id": user["_id"]}), user["_id"])


@router.delete("/apps/{app_id}/reviews/mine", status_code=204)
async def delete_my_review(db: Db, user: CurrentUser, app_id: str):
    app_oid = oid(app_id, "app_not_found")
    await db.reviews.delete_one({"app_id": app_oid, "user_id": user["_id"]})
    await refresh_rating(db, app_oid)


class ReportIn(BaseModel):
    reason: ReportReason = "other"
    details: str = Field(default="", max_length=2000)


def _reporter(request: Request, user: dict | None) -> str:
    """Identifiant du signalant (compte, sinon empreinte de l'adresse IP) : un signalement par personne."""
    if user:
        return f"user:{user['_id']}"
    ip = request.client.host if request.client else "unknown"
    return "ip:" + hashlib.sha256(f"{get_settings().jwt_secret}:{ip}".encode()).hexdigest()[:24]


@router.post("/reviews/{review_id}/report", status_code=204)
async def report_review(db: Db, request: Request, user: OptionalUser, review_id: str, body: ReportIn | None = None):
    """Signale un avis (contenu abusif, spam…) à la modération de la plateforme."""
    _limit(request, "report-review")
    review = await db.reviews.find_one({"_id": oid(review_id, "not_found")})
    if not review:
        raise ApiError(404, "not_found")
    who = _reporter(request, user)
    if any(r.get("by") == who for r in review.get("reports") or []):
        return
    report = {"by": who, "reason": (body.reason if body else "other"), "at": now()}
    await db.reviews.update_one({"_id": review["_id"]}, {"$push": {"reports": report}, "$set": {"reported_at": now()}})


@router.post("/apps/{app_id}/report", status_code=201)
async def report_app(
    db: Db, request: Request, background: BackgroundTasks, mailer: MailerDep, user: OptionalUser, app_id: str, body: ReportIn
):
    """Signale une app (logiciel malveillant, contenu abusif…) : le signalement arrive dans la Modération."""
    _limit(request, "report-app")
    app = await _public_app(db, app_id)
    who = _reporter(request, user)
    if await db.app_reports.find_one({"app_id": app["_id"], "by": who, "status": "open"}):
        return {"detail": "ok"}
    doc = {
        "app_id": app["_id"],
        "account_id": app.get("account_id"),
        "by": who,
        "user_email": (user or {}).get("email"),
        "reason": body.reason,
        "details": body.details.strip(),
        "language": language(request),
        "status": "open",
        "created_at": now(),
    }
    await db.app_reports.insert_one(doc)
    background.add_task(
        notify_platform_admins,
        db,
        mailer,
        "app_reported",
        "/moderation?tab=reports",
        app=app["name"],
        reason=body.reason,
        note=doc["details"] or None,
    )
    return {"detail": "ok"}


# ---------------------------------------------------------------- console : avis des apps du compte


@router.get("/admin/apps/{app_id}/reviews")
async def admin_list_reviews(
    db: Db,
    admin: CurrentAdmin,
    app_id: str,
    rating: int | None = Query(default=None, ge=1, le=5),
    replied: bool | None = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
):
    """Avis d'une app (masqués compris) avec leurs signalements, pour le compte développeur et la plateforme."""
    app = await get_app_or_404(db, app_id, admin)
    flt: dict = {"app_id": app["_id"]}
    if rating:
        flt["rating"] = rating
    if replied is not None:
        flt["reply"] = {"$ne": None} if replied else None
    items = await db.reviews.find(flt).sort("updated_at", -1).skip((page - 1) * limit).limit(limit).to_list(None)
    return {
        "items": [review_out(r, admin=True) for r in items],
        "total": await db.reviews.count_documents(flt),
        "page": page,
        "limit": limit,
        "rating": rating_out(app),
    }


async def _review_for(db, admin: dict, review_id: str) -> tuple[dict, dict]:
    review = await db.reviews.find_one({"_id": oid(review_id, "not_found")})
    if not review:
        raise ApiError(404, "not_found")
    app = await get_app_or_404(db, str(review["app_id"]), admin)
    return review, app


class ReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


@router.put("/admin/reviews/{review_id}/reply")
async def reply_review(db: Db, background: BackgroundTasks, mailer: MailerDep, admin: Writer, review_id: str, body: ReplyIn):
    """Réponse publique du développeur (au nom du compte) ; l'auteur de l'avis est prévenu par e-mail."""
    review, app = await _review_for(db, admin, review_id)
    first = not review.get("reply")
    reply = {"body": body.body.strip(), "author_name": app.get("account_name") or "", "replied_by": admin["_id"], "replied_at": now()}
    await db.reviews.update_one({"_id": review["_id"]}, {"$set": {"reply": reply}})
    await log_activity(db, admin, "review.replied", "app", app["_id"], {"name": app["name"], "rating": review["rating"]})
    if first:
        author = await db.users.find_one({"_id": review["user_id"]}, {"email": 1})
        if author and author.get("email"):
            url = f"{get_settings().app_link_base}{app['_id']}"
            lang = review.get("language") or "fr"
            background.add_task(
                mailer.send, review_reply_email(lang, author["email"], app["name"], reply["author_name"], reply["body"], url)
            )
    return review_out(await db.reviews.find_one({"_id": review["_id"]}), admin=True)


@router.delete("/admin/reviews/{review_id}/reply")
async def delete_reply(db: Db, admin: Writer, review_id: str):
    review, app = await _review_for(db, admin, review_id)
    await db.reviews.update_one({"_id": review["_id"]}, {"$set": {"reply": None}})
    await log_activity(db, admin, "review.reply_deleted", "app", app["_id"], {"name": app["name"]})
    return review_out(await db.reviews.find_one({"_id": review["_id"]}), admin=True)


# ---------------------------------------------------------------- modération (administrateur de la plateforme)


class HideIn(BaseModel):
    reason: str = Field(default="", max_length=2000)


@router.post("/admin/reviews/{review_id}/hide")
async def hide_review(db: Db, admin: FullAdmin, review_id: str, body: HideIn | None = None):
    """Masque un avis (contenu abusif) : il n'est plus visible ni compté dans la note ; ses signalements sont traités."""
    review, app = await _review_for(db, admin, review_id)
    reason = (body.reason if body else "").strip() or None
    await db.reviews.update_one(
        {"_id": review["_id"]}, {"$set": {"hidden": True, "hidden_reason": reason, "hidden_at": now(), "reports": []}}
    )
    await refresh_rating(db, app["_id"])
    await log_activity(db, admin, "review.hidden", "app", app["_id"], {"name": app["name"], "reason": reason})
    return review_out(await db.reviews.find_one({"_id": review["_id"]}), admin=True)


@router.post("/admin/reviews/{review_id}/restore")
async def restore_review(db: Db, admin: FullAdmin, review_id: str):
    """Rétablit un avis masqué, ou classe ses signalements sans suite."""
    review, app = await _review_for(db, admin, review_id)
    await db.reviews.update_one({"_id": review["_id"]}, {"$set": {"hidden": False, "hidden_reason": None, "reports": []}})
    await refresh_rating(db, app["_id"])
    await log_activity(db, admin, "review.restored", "app", app["_id"], {"name": app["name"]})
    return review_out(await db.reviews.find_one({"_id": review["_id"]}), admin=True)


@router.get("/admin/moderation/user-reviews")
async def moderation_user_reviews(
    db: Db, admin: FullAdmin, filter: Literal["reported", "hidden", "all"] = "reported", page: int = Query(default=1, ge=1), limit: int = 50
):
    """Avis à modérer : signalés, masqués ou tous (toute la plateforme)."""
    flt: dict = {"reports.0": {"$exists": True}} if filter == "reported" else {"hidden": True} if filter == "hidden" else {}
    items = await db.reviews.find(flt).sort([("reported_at", -1), ("created_at", -1)]).skip((page - 1) * limit).limit(limit).to_list(None)
    names = {
        a["_id"]: a["name"] for a in await db.apps.find({"_id": {"$in": list({r["app_id"] for r in items})}}, {"name": 1}).to_list(None)
    }
    return {
        "items": [{**review_out(r, admin=True), "app_name": names.get(r["app_id"])} for r in items],
        "total": await db.reviews.count_documents(flt),
        "reported": await db.reviews.count_documents({"reports.0": {"$exists": True}}),
    }


def report_out(r: dict, app_name: str | None = None) -> dict:
    return {
        "id": sid(r["_id"]),
        "app_id": sid(r["app_id"]),
        "app_name": app_name,
        "reason": r["reason"],
        "details": r.get("details", ""),
        "user_email": r.get("user_email"),
        "status": r["status"],
        "resolution": r.get("resolution"),
        "resolution_note": r.get("resolution_note"),
        "resolved_by_name": r.get("resolved_by_name"),
        "resolved_at": r.get("resolved_at"),
        "created_at": r.get("created_at"),
    }


@router.get("/admin/moderation/reports")
async def list_reports(
    db: Db, admin: FullAdmin, status: Literal["open", "closed"] = "open", page: int = Query(default=1, ge=1), limit: int = 50
):
    """Signalements d'apps reçus depuis l'app client."""
    flt = {"status": "open"} if status == "open" else {"status": {"$ne": "open"}}
    items = await db.app_reports.find(flt).sort("created_at", -1).skip((page - 1) * limit).limit(limit).to_list(None)
    apps = {
        a["_id"]: a
        for a in await db.apps.find({"_id": {"$in": list({r["app_id"] for r in items})}}, {"name": 1, "status": 1}).to_list(None)
    }
    return {
        "items": [
            {**report_out(r, apps.get(r["app_id"], {}).get("name")), "app_status": apps.get(r["app_id"], {}).get("status")} for r in items
        ],
        "total": await db.app_reports.count_documents(flt),
        "open": await db.app_reports.count_documents({"status": "open"}),
    }


class ResolveIn(BaseModel):
    resolution: Literal["dismissed", "resolved"]
    note: str = Field(default="", max_length=2000)
    unpublish: bool = False  # retire aussi l'app du store


@router.post("/admin/reports/{report_id}/resolve")
async def resolve_report(db: Db, admin: FullAdmin, report_id: str, body: ResolveIn):
    """Clôt un signalement (classé sans suite ou traité) ; option : dépublier l'app. Clôt les autres signalements ouverts de l'app."""
    report = await db.app_reports.find_one({"_id": oid(report_id, "not_found")})
    if not report:
        raise ApiError(404, "not_found")
    app = await db.apps.find_one({"_id": report["app_id"]})
    update = {
        "status": "closed",
        "resolution": body.resolution,
        "resolution_note": body.note.strip() or None,
        "resolved_by": admin["_id"],
        "resolved_by_name": admin.get("name"),
        "resolved_at": now(),
    }
    await db.app_reports.update_many({"app_id": report["app_id"], "status": "open"}, {"$set": update})
    if body.unpublish and app and app.get("status") == "published":
        await db.apps.update_one({"_id": app["_id"]}, {"$set": {"status": "archived", "updated_at": now()}})
        await log_activity(db, admin, "app.archived", "app", app["_id"], {"from": "published", "name": app["name"], "reason": "report"})
    await log_activity(
        db,
        admin,
        f"report.{body.resolution}",
        "app",
        report["app_id"],
        {"name": (app or {}).get("name"), "reason": report["reason"], "note": update["resolution_note"]},
    )
    return report_out({**report, **update}, (app or {}).get("name"))


@router.get("/admin/reviews-summary")
async def reviews_summary(db: Db, admin: CurrentAdmin):
    """Note moyenne et nombre d'avis des apps visibles par ce membre (tableau de bord)."""
    ids = await scoped_app_ids(db, admin)
    flt: dict = {"hidden": {"$ne": True}}
    if ids is not None:
        flt["app_id"] = {"$in": ids}
    rows = await (
        await db.reviews.aggregate([{"$match": flt}, {"$group": {"_id": None, "n": {"$sum": 1}, "avg": {"$avg": "$rating"}}}])
    ).to_list(None)
    unanswered = await db.reviews.count_documents({**flt, "reply": None})
    return {"count": rows[0]["n"] if rows else 0, "average": round(rows[0]["avg"], 2) if rows else None, "unanswered": unanswered}
