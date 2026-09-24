"""Gestion des fiches applications (console admin)."""

import io
import re
import secrets
from urllib.parse import unquote

from fastapi import APIRouter, File, Query, UploadFile

from app.core.config import get_settings
from app.core.i18n import ApiError
from app.deps import CurrentAdmin, Db, StorageDep
from app.models.common import AppStatus, maybe_oid, now, oid
from app.models.schemas import AppIn, AppStatusIn, AppUpdate, ScreenshotsOrder
from app.services.activity import log_activity
from app.services.catalog import PUBLIC_VERSION_FILTER, app_admin, app_detail
from app.services.storage import Storage

router = APIRouter(prefix="/admin/apps", tags=["admin: apps"])

IMAGE_TYPES = {b"\x89PNG": ("png", "image/png"), b"\xff\xd8\xff": ("jpg", "image/jpeg"), b"RIFF": ("webp", "image/webp")}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_SCREENSHOTS = 12


async def get_app_or_404(db, app_id: str) -> dict:
    app = await db.apps.find_one({"_id": oid(app_id, "app_not_found")})
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
    _: CurrentAdmin,
    status: AppStatus | None = None,
    q: str | None = None,
    category_id: str | None = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
):
    flt: dict = {}
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
    return {
        "items": [{**app_admin(a), "pending_versions": pending.get(a["_id"], 0)} for a in items],
        "total": await db.apps.count_documents(flt),
        "page": page,
        "limit": limit,
    }


@router.post("", status_code=201)
async def create_app(db: Db, admin: CurrentAdmin, body: AppIn):
    doc = {
        **body.model_dump(exclude={"category_ids"}),
        "category_ids": await _category_ids(db, body.category_ids),
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
async def get_app(db: Db, _: CurrentAdmin, app_id: str):
    return app_admin(await get_app_or_404(db, app_id))


@router.get("/{app_id}/preview")
async def preview_app(db: Db, _: CurrentAdmin, app_id: str):
    """Fiche telle qu'elle sera affichée dans l'app client (même format que GET /apps/{id}), quel que soit le statut."""
    app = await get_app_or_404(db, app_id)
    categories = await db.categories.find({"_id": {"$in": app.get("category_ids", [])}}).sort("order", 1).to_list(None)
    versions = await db.versions.find({"app_id": app["_id"], **PUBLIC_VERSION_FILTER}).sort("version_code", -1).to_list(None)
    return app_detail(app, categories, versions)


@router.patch("/{app_id}")
async def update_app(db: Db, admin: CurrentAdmin, app_id: str, body: AppUpdate):
    app = await get_app_or_404(db, app_id)
    update = body.model_dump(exclude_none=True, exclude={"category_ids"})
    if body.category_ids is not None:
        update["category_ids"] = await _category_ids(db, body.category_ids)
    update["updated_at"] = now()
    await db.apps.update_one({"_id": app["_id"]}, {"$set": update})
    await log_activity(db, admin, "app.updated", "app", app["_id"], {"fields": sorted(body.model_dump(exclude_none=True))})
    return app_admin(await db.apps.find_one({"_id": app["_id"]}))


@router.post("/{app_id}/status")
async def set_status(db: Db, admin: CurrentAdmin, app_id: str, body: AppStatusIn):
    """Brouillon / publié / archivé (dépublié). Les apps ne sont jamais supprimées physiquement."""
    app = await get_app_or_404(db, app_id)
    await db.apps.update_one({"_id": app["_id"]}, {"$set": {"status": body.status, "updated_at": now()}})
    await log_activity(db, admin, f"app.{body.status}", "app", app["_id"], {"from": app.get("status"), "name": app["name"]})
    return app_admin(await db.apps.find_one({"_id": app["_id"]}))


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
async def upload_icon(db: Db, storage: StorageDep, admin: CurrentAdmin, app_id: str, file: UploadFile = File(...)):
    app = await get_app_or_404(db, app_id)
    key = await _store_image(storage, app["_id"], file, "icon")
    await db.apps.update_one({"_id": app["_id"]}, {"$set": {"icon_key": key, "updated_at": now()}})
    if app.get("icon_key"):
        await storage.delete(app["icon_key"])
    await log_activity(db, admin, "app.icon_updated", "app", app["_id"])
    return app_admin(await db.apps.find_one({"_id": app["_id"]}))


@router.post("/{app_id}/screenshots")
async def add_screenshots(db: Db, storage: StorageDep, admin: CurrentAdmin, app_id: str, files: list[UploadFile] = File(...)):
    app = await get_app_or_404(db, app_id)
    if len(app.get("screenshot_keys", [])) + len(files) > MAX_SCREENSHOTS:
        raise ApiError(422, "too_many_screenshots")
    keys = [await _store_image(storage, app["_id"], f, "screenshot") for f in files]
    await db.apps.update_one({"_id": app["_id"]}, {"$push": {"screenshot_keys": {"$each": keys}}, "$set": {"updated_at": now()}})
    await log_activity(db, admin, "app.screenshots_added", "app", app["_id"], {"count": len(keys)})
    return app_admin(await db.apps.find_one({"_id": app["_id"]}))


def _key_from_url(url: str) -> str:
    return unquote(url.split(f"{get_settings().api_prefix}/media/", 1)[-1])


@router.put("/{app_id}/screenshots")
async def set_screenshots(db: Db, storage: StorageDep, admin: CurrentAdmin, app_id: str, body: ScreenshotsOrder):
    """Réordonne / retire des captures : la liste d'URLs envoyée devient la galerie."""
    app = await get_app_or_404(db, app_id)
    current = app.get("screenshot_keys", [])
    keys = [k for k in (_key_from_url(u) for u in body.urls) if k in current]
    await db.apps.update_one({"_id": app["_id"]}, {"$set": {"screenshot_keys": keys, "updated_at": now()}})
    for removed in set(current) - set(keys):
        await storage.delete(removed)
    await log_activity(db, admin, "app.screenshots_updated", "app", app["_id"], {"count": len(keys)})
    return app_admin(await db.apps.find_one({"_id": app["_id"]}))
