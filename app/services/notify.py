"""E-mails de suivi (dans la langue de chaque destinataire, envoyés en tâche de fond).

- au membre qui a soumis une demande (ou, pour une clé API / un membre désactivé, au propriétaire du compte) :
  version validée / disponible / programmée / refusée, statut de l'app, fiche, analyse de sécurité échouée ;
- au propriétaire du compte : décisions prises directement par l'administrateur sur ses apps ;
- à l'administrateur de la plateforme : chaque nouvelle demande à valider ;
- au propriétaire : suspension ou réactivation de son compte développeur.

Le texte des e-mails de version dépend de la visibilité de l'app : une version validée n'est
« disponible au téléchargement » que si l'application est publiée dans le store.
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


def app_live(app: dict) -> bool:
    """App visible dans le store (publiée, compte développeur non suspendu)."""
    return app.get("status") == "published" and not app.get("account_suspended")


async def _recipient(db: AsyncDatabase, member_id, app: dict) -> dict | None:
    """Membre concerné s'il est actif ; sinon (clé API, membre désactivé) le propriétaire du compte de l'app."""
    if isinstance(member_id, ObjectId):
        member = await db.admins.find_one({"_id": member_id, "active": {"$ne": False}})
        if member and member.get("email"):
            return member
    account = await db.accounts.find_one({"_id": app.get("account_id")}, {"owner_id": 1}) if app.get("account_id") else None
    if account and account.get("owner_id"):
        return await db.admins.find_one({"_id": account["owner_id"], "active": {"$ne": False}})
    return None


async def notify_about_app(db: AsyncDatabase, mailer: Mailer, member_id, app: dict, kind: str, path: str, skip_id=None, **ctx) -> None:
    """Prévient le membre concerné par une app (repli sur le propriétaire du compte), sauf l'auteur de l'action (`skip_id`)."""
    member = await _recipient(db, member_id, app)
    if not member or (skip_id is not None and member["_id"] == skip_id):
        return
    lang = member.get("language") or "fr"
    await mailer.send(notification_email(lang, kind, member["email"], member.get("name"), console_link(path), app=app["name"], **ctx))


def version_live_kind(app: dict, version: dict, promoted: bool = False) -> str:
    """E-mail à envoyer quand une version est mise en ligne, selon la visibilité de l'app et le canal."""
    if not app_live(app):
        # Test fermé : la bêta est bien disponible, pour les testeurs uniquement
        if version.get("channel") == "beta" and app.get("status") == "draft" and not app.get("account_suspended"):
            return "version_published_beta_testing"
        request = app.get("status_request") or {}
        pending = request.get("state") == "pending" and request.get("status") == "published"
        return "version_approved_hidden_pending" if pending else "version_approved_hidden"
    if promoted:
        return "version_promoted"
    return "version_published_beta" if version.get("channel") == "beta" else "version_published"


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
        # Version soumise alors que l'app n'est pas publiée : l'administrateur le sait avant de valider
        app_hidden=subject_kind in ("version", "promotion") and not app_live(app),
        **ctx,
    )
