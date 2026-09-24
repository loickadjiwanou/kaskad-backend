"""Gestion des versions : upload des binaires, analyse de sécurité, publication manuelle, archivage.

Les versions ne sont jamais supprimées physiquement : elles sont archivées.
Une version n'est publiable qu'après validation de l'analyse de sécurité.
"""

import hashlib
import re

from fastapi import APIRouter, BackgroundTasks, File, Form, UploadFile
from pymongo.errors import DuplicateKeyError

from app.core.config import get_settings
from app.core.i18n import ApiError
from app.deps import CurrentAdmin, Db, PushDep, ScanQueueDep, StorageDep
from app.models.common import CONTENT_TYPES, EXTENSIONS, FORMATS_BY_PLATFORM, SEMVER_PATTERN, FileFormat, Platform, now, oid
from app.models.schemas import VersionUpdate
from app.routers.admin_apps import get_app_or_404
from app.services.activity import log_activity
from app.services.catalog import refresh_app_catalog_fields, version_admin

router = APIRouter(prefix="/admin", tags=["admin: versions"])

CHUNK = 1024 * 1024


def _file_name(app_name: str, version_name: str, platform: str, fmt: str) -> str:
    ext = "AppImage" if fmt == "appimage" else fmt
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{app_name}-{version_name}-{platform}").strip("_")
    return f"{base}.{ext}"


async def _get_version(db, version_id: str) -> dict:
    v = await db.versions.find_one({"_id": oid(version_id, "version_not_found")})
    if not v:
        raise ApiError(404, "version_not_found")
    return v


@router.get("/apps/{app_id}/versions")
async def list_versions(db: Db, _: CurrentAdmin, app_id: str, include_abandoned: bool = False):
    app = await get_app_or_404(db, app_id)
    flt: dict = {"app_id": app["_id"]}
    if not include_abandoned:
        flt["upload_status"] = "stored"
    versions = await db.versions.find(flt).sort([("version_code", -1), ("created_at", -1)]).to_list(None)
    return [version_admin(v) for v in versions]


@router.post("/apps/{app_id}/versions", status_code=201)
async def upload_version(
    db: Db,
    storage: StorageDep,
    queue: ScanQueueDep,
    admin: CurrentAdmin,
    app_id: str,
    file: UploadFile = File(...),
    version_name: str = Form(..., min_length=1, max_length=50),
    version_code: int = Form(..., ge=1),
    platform: Platform = Form(...),
    file_format: FileFormat = Form(...),
    changelog: str = Form(default="", max_length=20000),
):
    """Upload d'un binaire : le SHA-256 est calculé à la volée, puis l'analyse de sécurité est lancée."""
    settings = get_settings()
    app = await get_app_or_404(db, app_id)
    if not re.match(SEMVER_PATTERN, version_name):
        raise ApiError(422, "invalid_version_name")
    if file_format not in FORMATS_BY_PLATFORM[platform]:
        raise ApiError(422, "invalid_format")
    if not (file.filename or "").lower().endswith(EXTENSIONS[file_format]):
        raise ApiError(422, "invalid_format")
    if await db.versions.find_one(
        {"app_id": app["_id"], "version_code": version_code, "file_format": file_format, "upload_status": "stored"}
    ):
        raise ApiError(409, "version_exists")

    doc = {
        "app_id": app["_id"],
        "version_name": version_name,
        "version_code": version_code,
        "platform": platform,
        "file_format": file_format,
        "changelog": changelog,
        "file_name": _file_name(app["name"], version_name, platform, file_format),
        "original_file_name": file.filename,
        "status": "draft",
        "security_scan_status": "pending",
        "upload_status": "uploading",
        "downloads_count": 0,
        "published_at": None,
        "created_by": admin["_id"],
        "created_at": now(),
        "updated_at": now(),
    }
    doc["_id"] = (await db.versions.insert_one(doc)).inserted_id
    key = f"binaries/{app['_id']}/{doc['_id']}/{doc['file_name']}"
    await db.versions.update_one({"_id": doc["_id"]}, {"$set": {"storage_key": key}})

    try:
        # Empreinte SHA-256 et taille calculées pendant la lecture
        sha, size, limit = hashlib.sha256(), 0, settings.max_upload_size_mb * 1024 * 1024
        while chunk := await file.read(CHUNK):
            size += len(chunk)
            if size > limit:
                raise ApiError(413, "file_too_large")
            sha.update(chunk)
        if size == 0:
            raise ApiError(422, "empty_file")
        await file.seek(0)
        await storage.save(key, file.file, CONTENT_TYPES[file_format])
        await db.versions.update_one(
            {"_id": doc["_id"]},
            {"$set": {"upload_status": "stored", "sha256_hash": sha.hexdigest(), "file_size": size, "updated_at": now()}},
        )
    except DuplicateKeyError:
        await storage.delete(key)
        await db.versions.update_one({"_id": doc["_id"]}, {"$set": {"upload_status": "failed"}})
        raise ApiError(409, "version_exists") from None
    except BaseException:
        await storage.delete(key)
        await db.versions.update_one({"_id": doc["_id"]}, {"$set": {"upload_status": "failed", "updated_at": now()}})
        raise

    queue.enqueue(doc["_id"])
    await log_activity(
        db,
        admin,
        "version.uploaded",
        "version",
        doc["_id"],
        {"app": app["name"], "version": version_name, "format": file_format, "size": size},
    )
    return version_admin(await db.versions.find_one({"_id": doc["_id"]}))


@router.get("/versions/{version_id}")
async def get_version(db: Db, _: CurrentAdmin, version_id: str):
    return version_admin(await _get_version(db, version_id))


@router.patch("/versions/{version_id}")
async def update_version(db: Db, admin: CurrentAdmin, version_id: str, body: VersionUpdate):
    v = await _get_version(db, version_id)
    if body.version_name and not re.match(SEMVER_PATTERN, body.version_name):
        raise ApiError(422, "invalid_version_name")
    update = {**body.model_dump(exclude_none=True), "updated_at": now()}
    await db.versions.update_one({"_id": v["_id"]}, {"$set": update})
    if v.get("status") == "published":
        await refresh_app_catalog_fields(db, v["app_id"])
    await log_activity(db, admin, "version.updated", "version", v["_id"], {"fields": sorted(body.model_dump(exclude_none=True))})
    return version_admin(await db.versions.find_one({"_id": v["_id"]}))


@router.post("/versions/{version_id}/publish")
async def publish_version(db: Db, admin: CurrentAdmin, push: PushDep, background: BackgroundTasks, version_id: str):
    """Publication manuelle, uniquement après validation de l'analyse de sécurité. Notifie les abonnés de l'app."""
    v = await _get_version(db, version_id)
    if v.get("upload_status") != "stored" or v.get("security_scan_status") != "passed":
        raise ApiError(409, "scan_not_passed")
    if v.get("status") != "published":
        await db.versions.update_one({"_id": v["_id"]}, {"$set": {"status": "published", "published_at": now(), "updated_at": now()}})
        await refresh_app_catalog_fields(db, v["app_id"])
        app = await db.apps.find_one({"_id": v["app_id"]})
        await log_activity(db, admin, "version.published", "version", v["_id"], {"app": app["name"], "version": v["version_name"]})
        # Notifications push seulement si l'app est visible et que c'est la version la plus récente de sa plateforme
        newer = await db.versions.count_documents(
            {"app_id": v["app_id"], "platform": v["platform"], "status": "published", "version_code": {"$gt": v["version_code"]}}
        )
        if app.get("status") == "published" and not newer:
            version = await db.versions.find_one({"_id": v["_id"]})
            background.add_task(push.notify_new_version, db, app, version)
    return version_admin(await db.versions.find_one({"_id": v["_id"]}))


@router.post("/versions/{version_id}/archive")
async def archive_version(db: Db, admin: CurrentAdmin, version_id: str):
    """Retire une version du catalogue (le fichier et l'historique sont conservés)."""
    v = await _get_version(db, version_id)
    await db.versions.update_one({"_id": v["_id"]}, {"$set": {"status": "archived", "updated_at": now()}})
    await refresh_app_catalog_fields(db, v["app_id"])
    await log_activity(db, admin, "version.archived", "version", v["_id"], {"version": v["version_name"]})
    return version_admin(await db.versions.find_one({"_id": v["_id"]}))


@router.post("/versions/{version_id}/rescan")
async def rescan_version(db: Db, queue: ScanQueueDep, admin: CurrentAdmin, version_id: str):
    v = await _get_version(db, version_id)
    if v.get("upload_status") != "stored":
        raise ApiError(409, "version_unavailable")
    await db.versions.update_one({"_id": v["_id"]}, {"$set": {"security_scan_status": "pending", "updated_at": now()}})
    queue.enqueue(v["_id"])
    await log_activity(db, admin, "version.rescan", "version", v["_id"])
    return version_admin(await db.versions.find_one({"_id": v["_id"]}))


@router.get("/versions/{version_id}/download-url")
async def admin_download_url(db: Db, storage: StorageDep, _: CurrentAdmin, version_id: str):
    """Lien de téléchargement pour la console (quel que soit le statut) — non comptabilisé dans les statistiques."""
    v = await _get_version(db, version_id)
    if v.get("upload_status") != "stored":
        raise ApiError(409, "version_unavailable")
    return {"url": await storage.signed_url(v["storage_key"], v.get("file_name"))}
