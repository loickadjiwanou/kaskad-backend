"""Gestion des fiches applications (console admin)."""

import io
import re
import secrets
from urllib.parse import unquote

from fastapi import APIRouter, BackgroundTasks, File, Query, Request, UploadFile

from app.core.config import get_settings
from app.core.i18n import ApiError, language
from app.deps import CurrentAdmin, Db, MailerDep, Publisher, StorageDep, Writer
from app.models.common import AppStatus, maybe_oid, now, oid
from app.models.schemas import AppIn, AppStatusIn, AppUpdate, ReviewReject, ReviewSubmit, ScreenshotsOrder, StatusRequestIn, TestersIn
from app.services.accounts import account_scope, is_platform_admin
from app.services.activity import log_activity
from app.services.catalog import PUBLIC_VERSION_FILTER, app_admin, app_detail
from app.services.emails import tester_email
from app.services.notify import notify_about_app, review_requested
from app.services.review import (
    LISTING_FIELDS,
    decision,
    delete_unreferenced,
    editable_listing,
    is_full_admin,
    is_pending,
    media_keys,
    submission,
    uses_draft,
)
from app.services.storage import Storage

router = APIRouter(prefix="/admin/apps", tags=["admin: apps"])

IMAGE_TYPES = {b"\x89PNG": ("png", "image/png"), b"\xff\xd8\xff": ("jpg", "image/jpeg"), b"RIFF": ("webp", "image/webp")}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_SCREENSHOTS = 12


async def get_app_or_404(db, app_id: str, admin: dict | None = None) -> dict:
    """App visible par ce membre (son compte), ou n'importe quelle app pour l'administrateur de la plateforme."""
    app = await db.apps.find_one({"_id": oid(app_id, "app_not_found"), **(account_scope(admin) if admin else {})})
    if not app:
        raise ApiError(404, "app_not_found")
    return app


async def _category_ids(db, ids: list[str]) -> list:
    oids = [o for o in (maybe_oid(i) for i in ids) if o]
    existing = {c["_id"] for c in await db.categories.find({"_id": {"$in": oids}}, {"_id": 1}).to_list(None)}
    if len(existing) != len(set(oids)):
        raise ApiError(404, "category_not_found")
    return list(dict.fromkeys(oids))


@router.get("")
async def list_apps(
    db: Db,
    admin: CurrentAdmin,
    status: AppStatus | None = None,
    q: str | None = None,
    category_id: str | None = None,
    account_id: str | None = None,  # administrateur de la plateforme : apps d'un compte développeur
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
):
    flt: dict = account_scope(admin, maybe_oid(account_id))
    if status:
        flt["status"] = status
    if category_id:
        flt["category_ids"] = maybe_oid(category_id)
    if q:
        flt["name"] = {"$regex": re.escape(q), "$options": "i"}
    items = await db.apps.find(flt).sort("updated_at", -1).skip((page - 1) * limit).limit(limit).to_list(None)
    # Nombre de versions en attente de validation par app (utile dans la liste de la console)
    pending = {
        r["_id"]: r["n"]
        for r in await (
            await db.versions.aggregate(
                [
                    {"$match": {"upload_status": "stored", "status": "draft"}},
                    {"$group": {"_id": "$app_id", "n": {"$sum": 1}}},
                ]
            )
        ).to_list(None)
    }
    # Administrateur de la plateforme : nom du compte développeur de chaque app
    accounts = {}
    if is_platform_admin(admin):
        ids = list({a.get("account_id") for a in items})
        accounts = {c["_id"]: c["name"] for c in await db.accounts.find({"_id": {"$in": ids}}, {"name": 1}).to_list(None)}
    return {
        "items": [
            {**app_admin(a), "pending_versions": pending.get(a["_id"], 0), "account_name": accounts.get(a.get("account_id"))} for a in items
        ],
        "total": await db.apps.count_documents(flt),
        "page": page,
        "limit": limit,
    }


@router.post("", status_code=201)
async def create_app(db: Db, admin: Writer, body: AppIn):
    doc = {
        **body.model_dump(exclude={"category_ids"}),
        "category_ids": await _category_ids(db, body.category_ids),
        "account_id": admin["account_id"],
        "account_name": (await db.accounts.find_one({"_id": admin["account_id"]}, {"name": 1}) or {}).get("name"),
        "status": "draft",
        "icon_key": None,
        "screenshot_keys": [],
        "available_platforms": [],
        "downloads_count": 0,
        "latest_version_name": None,
        "last_published_at": None,
        "created_by": admin["_id"],
        "created_at": now(),
        "updated_at": now(),
    }
    doc["_id"] = (await db.apps.insert_one(doc)).inserted_id
    await log_activity(db, admin, "app.created", "app", doc["_id"], {"name": body.name})
    return app_admin(doc)


@router.get("/{app_id}")
async def get_app(db: Db, admin: CurrentAdmin, app_id: str):
    return app_admin(await get_app_or_404(db, app_id, admin))


@router.get("/{app_id}/preview")
async def preview_app(db: Db, admin: CurrentAdmin, app_id: str, draft: bool = False):
    """Fiche telle qu'elle sera affichée dans l'app client (même format que GET /apps/{id}), quel que soit le statut.

    `draft=true` : fiche avec les modifications en attente (brouillon de fiche) appliquées.
    """
    app = await get_app_or_404(db, app_id, admin)
    if draft and app.get("listing_draft"):
        app = {**app, **{f: app["listing_draft"].get(f) for f in LISTING_FIELDS}}
    categories = await db.categories.find({"_id": {"$in": app.get("category_ids", [])}}).sort("order", 1).to_list(None)
    versions = await db.versions.find({"app_id": app["_id"], **PUBLIC_VERSION_FILTER}).sort("version_code", -1).to_list(None)
    return app_detail(app, categories, versions)


async def _update_listing(db, admin: dict, app: dict, update: dict) -> dict:
    """Applique une modification de fiche : en ligne si l'app n'est pas visible, sinon dans le brouillon de fiche.

    Toute modification du brouillon annule une demande de validation en cours (il faut la soumettre à nouveau).
    """
    if uses_draft(app):
        draft = {**editable_listing(app), **update, "updated_at": now(), "updated_by_name": admin.get("name")}
        await db.apps.update_one(
            {"_id": app["_id"]}, {"$set": {"listing_draft": draft, "updated_at": now()}, "$unset": {"listing_review": ""}}
        )
    else:
        await db.apps.update_one({"_id": app["_id"]}, {"$set": {**update, "updated_at": now()}})
    return await db.apps.find_one({"_id": app["_id"]})


@router.patch("/{app_id}")
async def update_app(db: Db, admin: Writer, app_id: str, body: AppUpdate):
    app = await get_app_or_404(db, app_id, admin)
    update = body.model_dump(exclude_none=True, exclude={"category_ids"})
    if "android_package" in body.model_fields_set and body.android_package in (None, ""):
        update["android_package"] = None
    if body.category_ids is not None:
        update["category_ids"] = await _category_ids(db, body.category_ids)
    updated = await _update_listing(db, admin, app, update)
    await log_activity(db, admin, "app.updated", "app", app["_id"], {"fields": sorted(update), "draft": bool(updated.get("listing_draft"))})
    return app_admin(updated)


def status_kind(app: dict) -> str:
    """E-mail du nouveau statut : publiée (avec ou sans version téléchargeable), dépubliée, brouillon."""
    if app["status"] == "published":
        return "app_published" if app.get("latest_version_name") else "app_published_empty"
    return f"app_{app['status']}"


async def _apply_status(db, admin: dict, app: dict, status: str, background=None, mailer=None, request: dict | None = None) -> dict:
    """Change le statut de l'app et prévient le compte développeur : l'auteur de la demande approuvée,
    ou le propriétaire du compte pour une décision directe de l'administrateur (jamais l'auteur de l'action)."""
    await db.apps.update_one({"_id": app["_id"]}, {"$set": {"status": status, "updated_at": now()}, "$unset": {"status_request": ""}})
    details = {"from": app.get("status"), "name": app["name"]}
    if request and request.get("submitted_by_name"):
        details["requested_by"] = request["submitted_by_name"]
    await log_activity(db, admin, f"app.{status}", "app", app["_id"], details)
    updated = await db.apps.find_one({"_id": app["_id"]})
    if background is not None and mailer is not None and status != app.get("status"):
        background.add_task(
            notify_about_app,
            db,
            mailer,
            request.get("submitted_by") if request else None,
            updated,
            status_kind(updated),
            f"/apps/{app['_id']}",
            skip_id=admin["_id"],
            lead="approved" if request else "direct",
        )
    return app_admin(updated)


@router.post("/{app_id}/status")
async def set_status(db: Db, background: BackgroundTasks, mailer: MailerDep, admin: Publisher, app_id: str, body: AppStatusIn):
    """Brouillon / publié / archivé (dépublié), réservé aux admins complets. Les apps ne sont jamais supprimées physiquement."""
    app = await get_app_or_404(db, app_id, admin)
    return await _apply_status(db, admin, app, body.status, background, mailer)


@router.post("/{app_id}/status-request")
async def request_status(db: Db, background: BackgroundTasks, mailer: MailerDep, admin: Writer, app_id: str, body: StatusRequestIn):
    """Demande de changement de statut (publier / dépublier / brouillon), à valider par un admin complet."""
    app = await get_app_or_404(db, app_id, admin)
    if body.status == app.get("status"):
        raise ApiError(409, "status_unchanged")
    if is_pending(app.get("status_request")):
        raise ApiError(409, "already_submitted")
    request = submission(admin, body.note, status=body.status)
    await db.apps.update_one({"_id": app["_id"]}, {"$set": {"status_request": request}})
    await log_activity(db, admin, "app.status_requested", "app", app["_id"], {"name": app["name"], "status": body.status})
    background.add_task(review_requested, db, mailer, admin, app, "status", status=body.status, note=body.note or None)
    return app_admin(await db.apps.find_one({"_id": app["_id"]}))


@router.post("/{app_id}/status-request/approve")
async def approve_status_request(db: Db, background: BackgroundTasks, mailer: MailerDep, admin: Publisher, app_id: str):
    app = await get_app_or_404(db, app_id, admin)
    request = app.get("status_request")
    if not is_pending(request):
        raise ApiError(409, "review_not_pending")
    return await _apply_status(db, admin, app, request["status"], background, mailer, request=request)


@router.post("/{app_id}/status-request/reject")
async def reject_status_request(db: Db, background: BackgroundTasks, mailer: MailerDep, admin: Publisher, app_id: str, body: ReviewReject):
    app = await get_app_or_404(db, app_id, admin)
    request = app.get("status_request")
    if not is_pending(request):
        raise ApiError(409, "review_not_pending")
    await db.apps.update_one({"_id": app["_id"]}, {"$set": {"status_request": {**request, **decision(admin, "rejected", body.reason)}}})
    await log_activity(
        db,
        admin,
        "app.status_request_rejected",
        "app",
        app["_id"],
        {"name": app["name"], "status": request["status"], "reason": body.reason},
    )
    background.add_task(
        notify_about_app,
        db,
        mailer,
        request.get("submitted_by"),
        app,
        "status_rejected",
        f"/apps/{app['_id']}",
        skip_id=admin["_id"],
        status=request["status"],
        reason=body.reason,
    )
    return app_admin(await db.apps.find_one({"_id": app["_id"]}))


@router.delete("/{app_id}/status-request")
async def withdraw_status_request(db: Db, admin: Writer, app_id: str):
    """Retire la demande (en attente) ou efface un refus. Réservé à son auteur ou à un admin complet."""
    app = await get_app_or_404(db, app_id, admin)
    request = app.get("status_request")
    if not request:
        raise ApiError(409, "review_not_pending")
    if request.get("submitted_by") != admin["_id"] and not is_full_admin(admin):
        raise ApiError(403, "forbidden")
    await db.apps.update_one({"_id": app["_id"]}, {"$unset": {"status_request": ""}})
    if is_pending(request):
        await log_activity(db, admin, "app.status_request_withdrawn", "app", app["_id"], {"name": app["name"], "status": request["status"]})
    return app_admin(await db.apps.find_one({"_id": app["_id"]}))


@router.put("/{app_id}/testers")
async def set_testers(
    db: Db, request: Request, background: BackgroundTasks, mailer: MailerDep, admin: Writer, app_id: str, body: TestersIn
):
    """Testeurs du canal bêta : adresses e-mail des comptes de l'app client qui voient les versions bêta.

    Chaque nouveau testeur reçoit un e-mail (langue de la console) expliquant comment accéder aux versions bêta.
    """
    app = await get_app_or_404(db, app_id, admin)
    emails = sorted({e.lower() for e in body.emails})
    added = sorted(set(emails) - {e.lower() for e in app.get("testers") or []})
    await db.apps.update_one({"_id": app["_id"]}, {"$set": {"testers": emails, "updated_at": now()}})
    await log_activity(
        db, admin, "app.testers_updated", "app", app["_id"], {"name": app["name"], "count": len(emails), "added": len(added)}
    )
    s = get_settings()
    lang = language(request)
    url = f"{s.app_link_base}{app['_id']}"
    account = app.get("account_name") or ""
    for email in added:
        background.add_task(mailer.send, tester_email(lang, email, app["name"], account, url))
    return app_admin(await db.apps.find_one({"_id": app["_id"]}))


# ---------------------------------------------------------------- brouillon de fiche


@router.post("/{app_id}/listing/submit")
async def submit_listing(db: Db, background: BackgroundTasks, mailer: MailerDep, admin: Writer, app_id: str, body: ReviewSubmit):
    """Soumet les modifications de fiche (brouillon) à la validation d'un admin complet."""
    app = await get_app_or_404(db, app_id, admin)
    if not app.get("listing_draft"):
        raise ApiError(409, "no_listing_draft")
    if is_pending(app.get("listing_review")):
        raise ApiError(409, "already_submitted")
    await db.apps.update_one({"_id": app["_id"]}, {"$set": {"listing_review": submission(admin, body.note)}})
    await log_activity(db, admin, "app.listing_submitted", "app", app["_id"], {"name": app["name"]})
    background.add_task(review_requested, db, mailer, admin, app, "listing", note=body.note or None)
    return app_admin(await db.apps.find_one({"_id": app["_id"]}))


@router.post("/{app_id}/listing/publish")
async def publish_listing(db: Db, background: BackgroundTasks, mailer: MailerDep, storage: StorageDep, admin: Publisher, app_id: str):
    """Met en ligne le brouillon de fiche (validation d'une demande, ou publication directe par un admin complet)."""
    app = await get_app_or_404(db, app_id, admin)
    draft = app.get("listing_draft")
    if not draft:
        raise ApiError(409, "no_listing_draft")
    before = media_keys(app)
    live = {f: draft.get(f) for f in LISTING_FIELDS}
    await db.apps.update_one(
        {"_id": app["_id"]},
        {"$set": {**live, "updated_at": now()}, "$unset": {"listing_draft": "", "listing_review": ""}},
    )
    updated = await db.apps.find_one({"_id": app["_id"]})
    await delete_unreferenced(storage, updated, before)
    review = app.get("listing_review")
    details = {"name": live.get("name") or app["name"]}
    if is_pending(review):
        details["requested_by"] = review.get("submitted_by_name")
    # Auteur de la demande validée, ou propriétaire du compte si l'administrateur publie directement
    background.add_task(
        notify_about_app,
        db,
        mailer,
        review.get("submitted_by") if is_pending(review) else None,
        updated,
        "listing_published",
        f"/apps/{app['_id']}",
        skip_id=admin["_id"],
        lead=None if is_pending(review) else "direct",
    )
    await log_activity(db, admin, "app.listing_published", "app", app["_id"], details)
    return app_admin(updated)


@router.post("/{app_id}/listing/reject")
async def reject_listing(db: Db, background: BackgroundTasks, mailer: MailerDep, admin: Publisher, app_id: str, body: ReviewReject):
    app = await get_app_or_404(db, app_id, admin)
    review = app.get("listing_review")
    if not is_pending(review):
        raise ApiError(409, "review_not_pending")
    await db.apps.update_one({"_id": app["_id"]}, {"$set": {"listing_review": {**review, **decision(admin, "rejected", body.reason)}}})
    await log_activity(db, admin, "app.listing_rejected", "app", app["_id"], {"name": app["name"], "reason": body.reason})
    background.add_task(
        notify_about_app,
        db,
        mailer,
        review.get("submitted_by"),
        app,
        "listing_rejected",
        f"/apps/{app['_id']}",
        skip_id=admin["_id"],
        reason=body.reason,
    )
    return app_admin(await db.apps.find_one({"_id": app["_id"]}))


@router.delete("/{app_id}/listing/review")
async def withdraw_listing_review(db: Db, admin: Writer, app_id: str):
    """Retire la soumission (le brouillon est conservé). Réservé à son auteur ou à un admin complet."""
    app = await get_app_or_404(db, app_id, admin)
    review = app.get("listing_review")
    if not review:
        raise ApiError(409, "review_not_pending")
    if review.get("submitted_by") != admin["_id"] and not is_full_admin(admin):
        raise ApiError(403, "forbidden")
    await db.apps.update_one({"_id": app["_id"]}, {"$unset": {"listing_review": ""}})
    if is_pending(review):
        await log_activity(db, admin, "app.listing_withdrawn", "app", app["_id"], {"name": app["name"]})
    return app_admin(await db.apps.find_one({"_id": app["_id"]}))


@router.delete("/{app_id}/listing")
async def discard_listing(db: Db, storage: StorageDep, admin: Writer, app_id: str):
    """Abandonne les modifications de fiche : la fiche en ligne reste inchangée."""
    app = await get_app_or_404(db, app_id, admin)
    if not app.get("listing_draft"):
        raise ApiError(409, "no_listing_draft")
    before = media_keys(app)
    await db.apps.update_one({"_id": app["_id"]}, {"$unset": {"listing_draft": "", "listing_review": ""}, "$set": {"updated_at": now()}})
    updated = await db.apps.find_one({"_id": app["_id"]})
    await delete_unreferenced(storage, updated, before)
    await log_activity(db, admin, "app.listing_discarded", "app", app["_id"], {"name": app["name"]})
    return app_admin(updated)


# ---------------------------------------------------------------- médias


async def _store_image(storage: Storage, app_id, file: UploadFile, kind: str) -> str:
    data = await file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ApiError(413, "file_too_large")
    match = next(((ext, ctype) for magic, (ext, ctype) in IMAGE_TYPES.items() if data.startswith(magic)), None)
    if not match or (match[0] == "webp" and data[8:12] != b"WEBP"):
        raise ApiError(422, "invalid_image")
    ext, ctype = match
    key = f"media/apps/{app_id}/{kind}-{secrets.token_hex(8)}.{ext}"
    await storage.save(key, io.BytesIO(data), ctype)
    return key


@router.post("/{app_id}/icon")
async def upload_icon(db: Db, storage: StorageDep, admin: Writer, app_id: str, file: UploadFile = File(...)):
    app = await get_app_or_404(db, app_id, admin)
    key = await _store_image(storage, app["_id"], file, "icon")
    previous = editable_listing(app).get("icon_key")
    updated = await _update_listing(db, admin, app, {"icon_key": key})
    await delete_unreferenced(storage, updated, [previous])
    await log_activity(db, admin, "app.icon_updated", "app", app["_id"], {"draft": bool(updated.get("listing_draft"))})
    return app_admin(updated)


@router.post("/{app_id}/screenshots")
async def add_screenshots(db: Db, storage: StorageDep, admin: Writer, app_id: str, files: list[UploadFile] = File(...)):
    app = await get_app_or_404(db, app_id, admin)
    current = editable_listing(app).get("screenshot_keys") or []
    if len(current) + len(files) > MAX_SCREENSHOTS:
        raise ApiError(422, "too_many_screenshots")
    keys = [await _store_image(storage, app["_id"], f, "screenshot") for f in files]
    updated = await _update_listing(db, admin, app, {"screenshot_keys": [*current, *keys]})
    await log_activity(
        db, admin, "app.screenshots_added", "app", app["_id"], {"count": len(keys), "draft": bool(updated.get("listing_draft"))}
    )
    return app_admin(updated)


def _key_from_url(url: str) -> str:
    return unquote(url.split(f"{get_settings().api_prefix}/media/", 1)[-1])


@router.put("/{app_id}/screenshots")
async def set_screenshots(db: Db, storage: StorageDep, admin: Writer, app_id: str, body: ScreenshotsOrder):
    """Réordonne / retire des captures : la liste d'URLs envoyée devient la galerie."""
    app = await get_app_or_404(db, app_id, admin)
    current = editable_listing(app).get("screenshot_keys") or []
    keys = [k for k in (_key_from_url(u) for u in body.urls) if k in current]
    updated = await _update_listing(db, admin, app, {"screenshot_keys": keys})
    await delete_unreferenced(storage, updated, set(current) - set(keys))
    await log_activity(
        db, admin, "app.screenshots_updated", "app", app["_id"], {"count": len(keys), "draft": bool(updated.get("listing_draft"))}
    )
    return app_admin(updated)
