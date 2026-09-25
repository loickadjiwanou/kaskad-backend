"""E-mails de suivi (dans la langue de chaque destinataire, envoyés en tâche de fond).

- au membre qui a soumis une demande : version publiée ou refusée, demande de statut ou de fiche traitée ;
- à l'administrateur de la plateforme : chaque nouvelle demande à valider ;
- au propriétaire : suspension ou réactivation de son compte développeur.
"""

import logging

from bson import ObjectId
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import get_settings
from app.services.emails import notification_email
from app.services.mailer import Mailer

log = logging.getLogger("kaskad.mail")


def console_link(path: str) -> str:
    return f"{get_settings().console_url.rstrip('/')}{path}"


async def notify_member(db: AsyncDatabase, mailer: Mailer, admin_id, kind: str, path: str, **ctx) -> None:
    """Prévient un membre (auteur d'une demande, propriétaire d'un compte), s'il est toujours actif."""
    if not isinstance(admin_id, ObjectId):
        return
    member = await db.admins.find_one({"_id": admin_id, "active": {"$ne": False}})
    if not member:
        return
    lang = member.get("language") or "fr"
    await mailer.send(notification_email(lang, kind, member["email"], member.get("name"), console_link(path), **ctx))


async def notify_platform_admins(db: AsyncDatabase, mailer: Mailer, kind: str, path: str, **ctx) -> None:
    async for admin in db.admins.find({"role": "admin", "active": {"$ne": False}}):
        lang = admin.get("language") or "fr"
        await mailer.send(notification_email(lang, kind, admin["email"], admin.get("name"), console_link(path), **ctx))


async def review_requested(db: AsyncDatabase, mailer: Mailer, member: dict, app: dict, subject_kind: str, **ctx) -> None:
    """Nouvelle demande à valider : prévient l'administrateur (sauf s'il en est lui-même l'auteur)."""
    if member.get("role") == "admin":
        return
    account = await db.accounts.find_one({"_id": app.get("account_id")}, {"name": 1})
    await notify_platform_admins(
        db,
        mailer,
        "review_requested",
        "/moderation",
        app=app["name"],
        member=member.get("name") or member["email"],
        account=(account or {}).get("name", ""),
        subject_kind=subject_kind,
        **ctx,
    )
