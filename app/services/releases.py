"""Mise en ligne des versions : publication immédiate ou programmée, canal bêta et passage en production.

- Canal `production` : visible par tous. Canal `beta` : visible uniquement par les testeurs de l'app
  (adresses e-mail des comptes utilisateurs de l'app client).
- Une version validée peut être programmée (`status: "scheduled"`, `scheduled_at`) ; le planificateur
  la publie à l'heure prévue.
- Publier une version bêta déjà en ligne la fait passer en production.
"""

import asyncio
import logging

from pymongo.asynchronous.database import AsyncDatabase

from app.models.common import now
from app.services.activity import log_activity, log_system
from app.services.catalog import refresh_app_catalog_fields
from app.services.notify import notify_about_app, version_live_kind

log = logging.getLogger("kaskad.releases")

PRODUCTION = "production"
BETA = "beta"


def is_beta(v: dict) -> bool:
    return v.get("channel") == BETA


async def _run(background, fn, *args, **kwargs) -> None:
    """Tâche de fond de la requête si disponible (réponse immédiate), sinon exécution directe (planificateur)."""
    if background is not None:
        background.add_task(fn, *args, **kwargs)
    else:
        await fn(*args, **kwargs)


async def go_live(db: AsyncDatabase, push, mailer, version: dict, actor: dict | None, background=None, direct: bool = False) -> dict:
    """Met la version en ligne (ou fait passer une bêta en ligne en production).

    `actor` : administrateur qui publie (None : planificateur). `direct` : publication sans demande préalable.
    Prévient par e-mail l'auteur de la soumission (sinon la personne qui a envoyé la version), avec un texte
    qui dépend de la visibilité de l'app (« disponible » seulement si l'app est publiée), et, pour une version
    de production d'une app visible, les utilisateurs qui suivent l'app (notification push).
    """
    promote = version.get("status") == "published" and is_beta(version)
    update: dict = {"updated_at": now(), "scheduled_at": None}
    if promote:
        update.update({"channel": PRODUCTION, "promoted_at": now()})
    else:
        update.update({"status": "published", "published_at": now()})
    await db.versions.update_one({"_id": version["_id"]}, {"$set": update})
    await refresh_app_catalog_fields(db, version["app_id"])
    app = await db.apps.find_one({"_id": version["app_id"]})
    v = await db.versions.find_one({"_id": version["_id"]})

    action = "version.promoted" if promote else "version.published"
    details = {"app": app["name"], "version": v["version_name"], "channel": v.get("channel", PRODUCTION)}
    review = version.get("review") or {}
    if review.get("submitted_by_name"):
        details["requested_by"] = review["submitted_by_name"]
    if actor:
        await log_activity(db, actor, action, "version", v["_id"], details)
    else:
        await log_system(db, action, "version", v["_id"], {**details, "scheduled": True}, account_id=app.get("account_id"))

    # Auteur de la demande (ou de l'envoi de la version) ; jamais l'administrateur qui vient de publier
    if mailer is not None:
        await _run(
            background,
            notify_about_app,
            db,
            mailer,
            review.get("submitted_by") or v.get("created_by"),
            app,
            version_live_kind(app, v, promoted=promote),
            f"/apps/{app['_id']}?tab=versions",
            skip_id=actor["_id"] if actor else None,
            version=v["version_name"],
            lead="direct" if direct else None,
        )

    # Notification push : version de production la plus récente de sa plateforme, app visible
    if v.get("channel", PRODUCTION) == PRODUCTION and push is not None:
        newer = await db.versions.count_documents(
            {
                "app_id": v["app_id"],
                "platform": v["platform"],
                "status": "published",
                "channel": {"$ne": BETA},
                "version_code": {"$gt": v["version_code"]},
            }
        )
        if app.get("status") == "published" and not app.get("account_suspended") and not newer:
            await _run(background, push.notify_new_version, db, app, v)
    return v


async def publish_due(db: AsyncDatabase, push, mailer) -> int:
    """Publie les versions programmées dont l'heure est passée. Renvoie le nombre de versions publiées."""
    count = 0
    async for v in db.versions.find({"status": "scheduled", "scheduled_at": {"$lte": now()}}):
        # Réservation atomique : une seule instance publie la version
        claimed = await db.versions.find_one_and_update(
            {"_id": v["_id"], "status": "scheduled"}, {"$set": {"status": "publishing", "updated_at": now()}}
        )
        if not claimed:
            continue
        try:
            await go_live(db, push, mailer, {**claimed, "status": "draft"}, None)
            count += 1
        except Exception:
            log.exception("scheduled publication failed for %s", v["_id"])
            await db.versions.update_one({"_id": v["_id"]}, {"$set": {"status": "scheduled"}})
    return count


async def scheduler_loop(db: AsyncDatabase, push, mailer, interval_seconds: int = 30) -> None:
    while True:
        try:
            await publish_due(db, push, mailer)
        except Exception:
            log.exception("scheduler failed")
        await asyncio.sleep(interval_seconds)
