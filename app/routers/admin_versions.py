"""Gestion des versions : upload des binaires, analyse de sécurité, publication manuelle, archivage.

Les versions ne sont jamais supprimées physiquement : elles sont archivées.
Une version n'est publiable qu'après validation de l'analyse de sécurité.
"""

import hashlib
import re
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, File, Form, UploadFile
from pymongo.errors import DuplicateKeyError

from app.core.config import get_settings
from app.core.i18n import ApiError
from app.deps import CurrentAdmin, Db, MailerDep, Publisher, PushDep, ScanQueueDep, StorageDep, Writer
from app.models.common import CONTENT_TYPES, EXTENSIONS, FORMATS_BY_PLATFORM, SEMVER_PATTERN, FileFormat, Platform, now, oid
from app.models.schemas import Channel, PublishIn, ReviewReject, ReviewSubmit, VersionUpdate
from app.routers.admin_apps import get_app_or_404
from app.services.activity import log_activity
from app.services.catalog import refresh_app_catalog_fields, version_admin
from app.services.notify import app_live, notify_about_app, review_requested
from app.services.releases import go_live, is_beta
from app.services.review import decision, is_full_admin, is_pending, submission

router = APIRouter(prefix="/admin", tags=["admin: versions"])

CHUNK = 1024 * 1024


def _file_name(app_name: str, version_name: str, platform: str, fmt: str) -> str:
    ext = "AppImage" if fmt == "appimage" else fmt
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{app_name}-{version_name}-{platform}").strip("_")
    return f"{base}.{ext}"


def _future(dt: datetime | None) -> datetime | None:
    """Date de publication programmée (UTC) si elle est dans le futur, sinon None (publication immédiate)."""
    if dt is None:
        return None
    dt = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return dt if dt > now() else None


async def _get_version(db, version_id: str, admin: dict) -> dict:
    """Version d'une app visible par ce membre (voir get_app_or_404)."""
    v = await db.versions.find_one({"_id": oid(version_id, "version_not_found")})
    if not v:
        raise ApiError(404, "version_not_found")
    try:
        await get_app_or_404(db, str(v["app_id"]), admin)
    except ApiError:
        raise ApiError(404, "version_not_found") from None
    return v


@router.get("/apps/{app_id}/versions")
async def list_versions(db: Db, admin: CurrentAdmin, app_id: str, include_abandoned: bool = False):
    app = await get_app_or_404(db, app_id, admin)
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
    admin: Writer,
    app_id: str,
    file: UploadFile = File(...),
    version_name: str = Form(..., min_length=1, max_length=50),
    version_code: int = Form(..., ge=1),
    platform: Platform = Form(...),
    file_format: FileFormat = Form(...),
    changelog: str = Form(default="", max_length=20000),
    changelog_fr: str | None = Form(default=None, max_length=20000),
    changelog_en: str | None = Form(default=None, max_length=20000),
    channel: Channel = Form(default="production"),
    submit: bool = Form(default=False),
    submit_note: str = Form(default="", max_length=2000),
    publish_at: datetime | None = Form(default=None),
):
    """Upload d'un binaire : le SHA-256 est calculé à la volée, puis l'analyse de sécurité est lancée.

    Notes de version : `changelog` (langue principale de l'app) ou `changelog_fr` / `changelog_en`.
    `channel` : `production` ou `beta` (testeurs uniquement). `submit=true` (intégration continue) : la version est
    soumise automatiquement à validation dès que l'analyse de sécurité est validée, avec `submit_note` et `publish_at`.
    """
    settings = get_settings()
    app = await get_app_or_404(db, app_id, admin)
    base_lang = app.get("default_language", "fr")
    texts = {"fr": changelog_fr, "en": changelog_en}
    if texts[base_lang] is None:
        texts[base_lang] = changelog
    other = "en" if base_lang == "fr" else "fr"
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
        "changelog": texts[base_lang] or "",
        "changelog_lang": base_lang,
        "changelog_translations": {other: texts[other]} if texts[other] else {},
        "channel": channel,
        "auto_submit": (
            {"note": submit_note, "publish_at": _future(publish_at), "by": admin["_id"], "by_name": admin.get("name")} if submit else None
        ),
        "file_name": _file_name(app["name"], version_name, platform, file_format),
        "original_file_name": file.filename,
        "status": "draft",
        "review": None,
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
async def get_version(db: Db, admin: CurrentAdmin, version_id: str):
    return version_admin(await _get_version(db, version_id, admin))


@router.patch("/versions/{version_id}")
async def update_version(db: Db, admin: Writer, version_id: str, body: VersionUpdate):
    v = await _get_version(db, version_id, admin)
    # Une version en ligne ne peut être modifiée que par un admin complet
    if v.get("status") == "published" and not is_full_admin(admin):
        raise ApiError(403, "publish_requires_admin")
    if body.version_name and not re.match(SEMVER_PATTERN, body.version_name):
        raise ApiError(422, "invalid_version_name")
    # Le canal se choisit avant la mise en ligne ; ensuite, une bêta passe en production par la publication
    if body.channel is not None and body.channel != v.get("channel", "production") and v.get("status") != "draft":
        raise ApiError(409, "version_locked")
    update = {**body.model_dump(exclude_none=True), "updated_at": now()}
    # Modifier une version soumise annule la demande : elle doit être soumise à nouveau
    if v.get("status") == "draft" and v.get("review"):
        update["review"] = None
    await db.versions.update_one({"_id": v["_id"]}, {"$set": update})
    if v.get("status") == "published":
        await refresh_app_catalog_fields(db, v["app_id"])
    await log_activity(db, admin, "version.updated", "version", v["_id"], {"fields": sorted(body.model_dump(exclude_none=True))})
    return version_admin(await db.versions.find_one({"_id": v["_id"]}))


async def _ensure_testers(db, v: dict) -> None:
    """Version bêta à soumettre ou publier : l'app doit avoir au moins MIN_BETA_TESTERS testeurs."""
    if not is_beta(v) or v.get("status") == "published":
        return
    minimum = get_settings().min_beta_testers
    app = await db.apps.find_one({"_id": v["app_id"]}, {"testers": 1})
    if len((app or {}).get("testers") or []) < minimum:
        raise ApiError(409, "not_enough_testers", min=minimum)


def _submittable(v: dict) -> bool:
    """Version validée par l'analyse et pas encore en ligne, ou bêta en ligne (demande de passage en production)."""
    stored = v.get("upload_status") == "stored" and v.get("security_scan_status") == "passed"
    return stored and (v.get("status") == "draft" or (v.get("status") == "published" and is_beta(v)))


@router.post("/versions/{version_id}/submit")
async def submit_version(db: Db, background: BackgroundTasks, mailer: MailerDep, admin: Writer, version_id: str, body: ReviewSubmit):
    """Soumet une version validée par l'analyse de sécurité à la validation d'un admin complet."""
    v = await _get_version(db, version_id, admin)
    if not _submittable(v):
        raise ApiError(409, "not_submittable")
    if is_pending(v.get("review")):
        raise ApiError(409, "already_submitted")
    await _ensure_testers(db, v)
    promote = v.get("status") == "published"
    review = submission(
        admin, body.note, kind="promote" if promote else "publish", publish_at=None if promote else _future(body.publish_at)
    )
    await db.versions.update_one({"_id": v["_id"]}, {"$set": {"review": review, "updated_at": now()}})
    app = await db.apps.find_one({"_id": v["app_id"]})
    await log_activity(
        db,
        admin,
        "version.promotion_requested" if promote else "version.submitted",
        "version",
        v["_id"],
        {"app": app["name"], "version": v["version_name"], "publish_at": review.get("publish_at")},
    )
    background.add_task(
        review_requested,
        db,
        mailer,
        admin,
        app,
        "promotion" if promote else "version",
        version=v["version_name"],
        note=body.note or None,
    )
    return version_admin(await db.versions.find_one({"_id": v["_id"]}))


@router.post("/versions/{version_id}/reject")
async def reject_version(db: Db, background: BackgroundTasks, mailer: MailerDep, admin: Publisher, version_id: str, body: ReviewReject):
    """Refus motivé d'une version soumise : son auteur voit le motif et peut la corriger puis la soumettre à nouveau."""
    v = await _get_version(db, version_id, admin)
    if not is_pending(v.get("review")):
        raise ApiError(409, "review_not_pending")
    await db.versions.update_one(
        {"_id": v["_id"]}, {"$set": {"review": {**v["review"], **decision(admin, "rejected", body.reason)}, "updated_at": now()}}
    )
    app = await db.apps.find_one({"_id": v["app_id"]})
    await log_activity(
        db, admin, "version.rejected", "version", v["_id"], {"app": app["name"], "version": v["version_name"], "reason": body.reason}
    )
    # L'auteur de la soumission est prévenu, avec le motif
    background.add_task(
        notify_about_app,
        db,
        mailer,
        v["review"].get("submitted_by"),
        app,
        "version_rejected",
        f"/apps/{app['_id']}?tab=versions",
        skip_id=admin["_id"],
        version=v["version_name"],
        reason=body.reason,
    )
    return version_admin(await db.versions.find_one({"_id": v["_id"]}))


@router.delete("/versions/{version_id}/submission")
async def withdraw_version(db: Db, admin: Writer, version_id: str):
    """Retire la soumission (ou efface un refus). Réservé à son auteur ou à un admin complet."""
    v = await _get_version(db, version_id, admin)
    review = v.get("review")
    if not review or v.get("status") not in ("draft", "published"):
        raise ApiError(409, "review_not_pending")
    if review.get("submitted_by") != admin["_id"] and not is_full_admin(admin):
        raise ApiError(403, "forbidden")
    await db.versions.update_one({"_id": v["_id"]}, {"$set": {"review": None, "updated_at": now()}})
    if is_pending(review):
        await log_activity(db, admin, "version.submission_withdrawn", "version", v["_id"], {"version": v["version_name"]})
    return version_admin(await db.versions.find_one({"_id": v["_id"]}))


@router.post("/versions/{version_id}/publish")
async def publish_version(
    db: Db,
    admin: Publisher,
    push: PushDep,
    mailer: MailerDep,
    background: BackgroundTasks,
    version_id: str,
    body: PublishIn | None = None,
):
    """Publication par l'administrateur de la plateforme (valide la demande si la version avait été soumise).

    - version validée par l'analyse : mise en ligne immédiate, ou programmée à `publish_at`
      (par défaut, la date demandée lors de la soumission) ;
    - version programmée : mise en ligne immédiate ;
    - version bêta déjà en ligne : passage en production.
    """
    v = await _get_version(db, version_id, admin)
    if v.get("upload_status") != "stored" or v.get("security_scan_status") != "passed":
        raise ApiError(409, "scan_not_passed")
    if v.get("status") == "published" and not is_beta(v):
        return version_admin(v)
    await _ensure_testers(db, v)
    review = v.get("review")
    approved = {**review, **decision(admin, "approved")} if is_pending(review) else review
    # Date demandée lors de la soumission, sauf date choisie par l'administrateur
    requested = (review or {}).get("publish_at") if is_pending(review) else None
    publish_at = _future((body.publish_at if body else None) or requested)
    if v.get("status") == "draft" and publish_at:
        await db.versions.update_one(
            {"_id": v["_id"]}, {"$set": {"status": "scheduled", "scheduled_at": publish_at, "review": approved, "updated_at": now()}}
        )
        app = await db.apps.find_one({"_id": v["app_id"]})
        await log_activity(
            db,
            admin,
            "version.scheduled",
            "version",
            v["_id"],
            {"app": app["name"], "version": v["version_name"], "publish_at": publish_at},
        )
        # Auteur de la demande, ou personne qui a envoyé la version (programmation directe)
        background.add_task(
            notify_about_app,
            db,
            mailer,
            review.get("submitted_by") if is_pending(review) else v.get("created_by"),
            app,
            "version_scheduled" if app_live(app) else "version_scheduled_hidden",
            f"/apps/{app['_id']}?tab=versions",
            skip_id=admin["_id"],
            version=v["version_name"],
            date=publish_at,
            lead=None if is_pending(review) else "direct",
        )
        return version_admin(await db.versions.find_one({"_id": v["_id"]}))
    if approved is not review:
        await db.versions.update_one({"_id": v["_id"]}, {"$set": {"review": approved}})
    # Demande validée : son auteur est prévenu ; publication directe : la personne qui a envoyé la version
    to_notify = {**v, "review": review if is_pending(review) else None}
    return version_admin(await go_live(db, push, mailer, to_notify, admin, background, direct=not is_pending(review)))


@router.post("/versions/{version_id}/unschedule")
async def unschedule_version(db: Db, admin: Writer, version_id: str):
    """Annule une publication programmée : la version repasse en brouillon (à soumettre de nouveau)."""
    v = await _get_version(db, version_id, admin)
    if v.get("status") != "scheduled":
        raise ApiError(409, "review_not_pending")
    await db.versions.update_one(
        {"_id": v["_id"]}, {"$set": {"status": "draft", "scheduled_at": None, "review": None, "updated_at": now()}}
    )
    await log_activity(db, admin, "version.unscheduled", "version", v["_id"], {"version": v["version_name"]})
    return version_admin(await db.versions.find_one({"_id": v["_id"]}))


@router.post("/versions/{version_id}/archive")
async def archive_version(db: Db, admin: Writer, version_id: str):
    """Retire une version du catalogue (le fichier et l'historique sont conservés).

    Retirer une version en ligne est réservé aux admins complets ; un éditeur peut archiver une version non publiée.
    """
    v = await _get_version(db, version_id, admin)
    if v.get("status") == "published" and not is_full_admin(admin):
        raise ApiError(403, "publish_requires_admin")
    await db.versions.update_one({"_id": v["_id"]}, {"$set": {"status": "archived", "review": None, "updated_at": now()}})
    await refresh_app_catalog_fields(db, v["app_id"])
    await log_activity(db, admin, "version.archived", "version", v["_id"], {"version": v["version_name"]})
    return version_admin(await db.versions.find_one({"_id": v["_id"]}))


@router.post("/versions/{version_id}/rescan")
async def rescan_version(db: Db, queue: ScanQueueDep, admin: Writer, version_id: str):
    v = await _get_version(db, version_id, admin)
    if v.get("upload_status") != "stored":
        raise ApiError(409, "version_unavailable")
    await db.versions.update_one({"_id": v["_id"]}, {"$set": {"security_scan_status": "pending", "review": None, "updated_at": now()}})
    queue.enqueue(v["_id"])
    await log_activity(db, admin, "version.rescan", "version", v["_id"])
    return version_admin(await db.versions.find_one({"_id": v["_id"]}))


@router.get("/versions/{version_id}/download-url")
async def admin_download_url(db: Db, storage: StorageDep, admin: CurrentAdmin, version_id: str):
    """Lien de téléchargement pour la console (quel que soit le statut) — non comptabilisé dans les statistiques."""
    v = await _get_version(db, version_id, admin)
    if v.get("upload_status") != "stored":
        raise ApiError(409, "version_unavailable")
    return {"url": await storage.signed_url(v["storage_key"], v.get("file_name"))}
