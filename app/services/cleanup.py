"""Nettoyage automatique des uploads incomplets et des fichiers orphelins du stockage."""

import asyncio
import logging
from datetime import timedelta

from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import Settings
from app.models.common import now
from app.services.review import media_keys
from app.services.storage import Storage

log = logging.getLogger("kaskad.cleanup")


async def run_cleanup(db: AsyncDatabase, storage: Storage, settings: Settings) -> dict:
    cutoff = now() - timedelta(hours=settings.orphan_upload_max_age_hours)
    stats = {"abandoned_uploads": 0, "orphan_files": 0, "aborted_multipart": 0}

    # 1. Versions restées en cours d'upload (client coupé, serveur redémarré…) : marquées abandonnées
    async for v in db.versions.find({"upload_status": "uploading", "created_at": {"$lt": cutoff}}):
        if v.get("storage_key"):
            await storage.delete(v["storage_key"])
        await db.versions.update_one({"_id": v["_id"]}, {"$set": {"upload_status": "abandoned", "updated_at": now()}})
        stats["abandoned_uploads"] += 1

    # 2. Fichiers présents dans le stockage mais référencés par aucune version / app
    referenced: set[str] = set()
    async for v in db.versions.find({"upload_status": {"$in": ["stored", "uploading"]}}, {"storage_key": 1}):
        if v.get("storage_key"):
            referenced.add(v["storage_key"])
    async for a in db.apps.find({}, {"icon_key": 1, "screenshot_keys": 1, "listing_draft": 1}):
        referenced.update(media_keys(a))  # fiche en ligne + brouillon de fiche
    for prefix in ("binaries/", "media/"):
        for obj in await storage.list_objects(prefix):
            if obj.key not in referenced and obj.last_modified < cutoff and not obj.key.endswith(".part"):
                await storage.delete(obj.key)
                stats["orphan_files"] += 1

    # 3. Uploads multipart S3 jamais terminés / fichiers temporaires locaux
    stats["aborted_multipart"] = await storage.abort_incomplete_uploads(cutoff)
    if any(stats.values()):
        log.info("cleanup: %s", stats)
    return stats


async def cleanup_loop(db: AsyncDatabase, storage: Storage, settings: Settings) -> None:
    while True:
        try:
            await run_cleanup(db, storage, settings)
        except Exception:
            log.exception("cleanup failed")
        await asyncio.sleep(settings.cleanup_interval_minutes * 60)
