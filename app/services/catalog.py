"""Conversion des documents MongoDB vers les réponses de l'API (formes attendues par l'app client)."""

import hashlib
import hmac
import time
from urllib.parse import quote

from bson import ObjectId
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import get_settings
from app.models.common import now, sid


def media_url(key: str | None) -> str | None:
    if not key:
        return None
    s = get_settings()
    return f"{s.public_base_url}{s.api_prefix}/media/{quote(key)}"


def share_url(app_id: ObjectId) -> str:
    """Page web publique de l'app (lien partageable, ouvre l'app Kaskad si elle est installée)."""
    s = get_settings()
    return f"{(s.public_web_url or s.public_base_url).rstrip('/')}/a/{app_id}"


def download_url(version_id: ObjectId) -> str:
    s = get_settings()
    return f"{s.public_base_url}{s.api_prefix}/versions/{version_id}/download"


def category_out(c: dict) -> dict:
    return {"id": sid(c["_id"]), "name": c["name"], "icon": c.get("icon", "shape-outline"), "order": c.get("order", 0)}


# Apps visibles dans l'app client : publiées, et dont le compte développeur n'est pas suspendu
PUBLIC_APP_FILTER = {"status": "published", "account_suspended": {"$ne": True}}
# Test fermé (comme Google Play) : une app pas encore publiée (brouillon) est accessible à ses seuls testeurs,
# uniquement pour ses versions bêta. Jamais listée dans le store (accueil, recherche, catégories).
REACHABLE_APP_FILTER = {"status": {"$in": ["published", "draft"]}, "account_suspended": {"$ne": True}}


def in_testing(app: dict) -> bool:
    """App en test fermé : pas encore publiée, compte non suspendu."""
    return app.get("status") == "draft" and not app.get("account_suspended")


def developer_out(a: dict) -> dict | None:
    """Compte développeur qui publie l'app (champ dénormalisé `account_name`)."""
    if not a.get("account_id"):
        return None
    return {"id": sid(a["account_id"]), "name": a.get("account_name") or ""}


# ---------------------------------------------------------------- langues (fiches et notes de version)

LANGUAGES = ("fr", "en")


def _translated(a: dict, lang: str | None, field: str) -> str:
    """Texte de la fiche dans la langue demandée, sinon dans la langue principale de l'app."""
    if lang and lang != a.get("default_language", "fr"):
        value = ((a.get("translations") or {}).get(lang) or {}).get(field)
        if value:
            return value
    return a.get(field, "")


def localized_changelog(v: dict, lang: str | None) -> str:
    if lang and lang != v.get("changelog_lang", "fr"):
        value = (v.get("changelog_translations") or {}).get(lang)
        if value:
            return value
    return v.get("changelog", "")


# ---------------------------------------------------------------- canal bêta

BETA_TOKEN_TTL = 24 * 3600


def beta_token(version_id: ObjectId, expires: int | None = None) -> str:
    """Jeton de téléchargement d'une version bêta (remis aux seuls testeurs, valable 24 h)."""
    expires = expires or int(time.time()) + BETA_TOKEN_TTL
    sig = hmac.new(get_settings().jwt_secret.encode(), f"beta:{version_id}:{expires}".encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def verify_beta_token(version_id: ObjectId, token: str | None) -> bool:
    try:
        expires = int((token or "").split(".", 1)[0])
    except ValueError:
        return False
    return expires >= int(time.time()) and hmac.compare_digest(beta_token(version_id, expires), token or "")


def is_tester(app: dict, user: dict | None) -> bool:
    email = (user or {}).get("email")
    return bool(email) and email.lower() in {t.lower() for t in app.get("testers") or []}


def channel_filter(app: dict, user: dict | None) -> dict:
    """Versions visibles par cet utilisateur : production pour tous, bêta en plus pour les testeurs ;
    app en test fermé : versions bêta uniquement (et seulement pour ses testeurs)."""
    if in_testing(app):
        return {"channel": "beta"}
    return {} if is_tester(app, user) else {"channel": {"$ne": "beta"}}


def can_see_app(app: dict | None, user: dict | None) -> bool:
    """App publiée (tout le monde) ou en test fermé (ses testeurs seulement)."""
    if not app or app.get("account_suspended"):
        return False
    return app.get("status") == "published" or (in_testing(app) and is_tester(app, user))


def app_summary(a: dict, lang: str | None = None) -> dict:
    return {
        "id": sid(a["_id"]),
        "name": a["name"],
        "short_description": _translated(a, lang, "short_description"),
        "icon_url": media_url(a.get("icon_key")),
        "category_ids": [sid(c) for c in a.get("category_ids", [])],
        # Plateformes réellement disponibles (versions publiées), sinon plateformes ciblées déclarées
        "platforms": a.get("available_platforms") or a.get("target_platforms", []),
        "status": a.get("status", "draft"),
        "downloads_count": a.get("downloads_count", 0),
        "featured": bool(a.get("featured")),
        "developer": developer_out(a),
        "rating_average": (a.get("rating") or {}).get("average"),
        "rating_count": (a.get("rating") or {}).get("count", 0),
        "latest_version_name": a.get("latest_version_name"),
        "created_at": a.get("created_at"),
        "updated_at": a.get("last_published_at") or a.get("updated_at"),
    }


def version_public(v: dict, lang: str | None = None) -> dict:
    beta = v.get("channel") == "beta"
    return {
        "id": sid(v["_id"]),
        "app_id": sid(v["app_id"]),
        "version_name": v["version_name"],
        "version_code": v["version_code"],
        "platform": v["platform"],
        "file_format": v["file_format"],
        # Bêta : lien signé réservé aux testeurs (le téléchargement public est refusé sans jeton)
        "file_url": f"{download_url(v['_id'])}?t={beta_token(v['_id'])}" if beta else download_url(v["_id"]),
        "download_token": beta_token(v["_id"]) if beta else None,
        "channel": v.get("channel", "production"),
        "file_size": v.get("file_size"),
        "sha256_hash": v.get("sha256_hash"),
        "changelog": localized_changelog(v, lang),
        "security_scan_status": v.get("security_scan_status"),
        "published_at": v.get("published_at"),
        "created_at": v.get("created_at"),
    }


def _review(r):
    from app.services.review import review_out

    return review_out(r)


def version_admin(v: dict) -> dict:
    return {
        **version_public(v),
        "status": v.get("status"),
        "upload_status": v.get("upload_status"),
        "file_name": v.get("file_name"),
        "scan_report": v.get("scan_report"),
        "review": _review(v.get("review")),
        "changelog": v.get("changelog", ""),
        "changelog_lang": v.get("changelog_lang", "fr"),
        "changelog_translations": v.get("changelog_translations") or {},
        "scheduled_at": v.get("scheduled_at"),
        "promoted_at": v.get("promoted_at"),
        "auto_submit": bool(v.get("auto_submit")),
        "apk_info": v.get("apk_info"),
        "created_by": sid(v.get("created_by")),
        "downloads_count": v.get("downloads_count", 0),
        "updated_at": v.get("updated_at"),
    }


def app_detail(a: dict, categories: list[dict], versions: list[dict], lang: str | None = None) -> dict:
    return {
        **app_summary(a, lang),
        "long_description": _translated(a, lang, "long_description"),
        "screenshots": [media_url(k) for k in a.get("screenshot_keys", [])],
        "rating_distribution": (a.get("rating") or {}).get("distribution") or {str(i): 0 for i in range(1, 6)},
        "share_url": share_url(a["_id"]),
        "categories": [category_out(c) for c in categories],
        "versions": [version_public(v, lang) for v in versions],
    }


def app_admin(a: dict) -> dict:
    from app.services.review import listing_out, review_out

    return {
        **app_summary(a),
        # Circuit de validation : brouillon de fiche, demande de validation de la fiche, demande de changement de statut
        "draft": listing_out(a.get("listing_draft")),
        "listing_review": review_out(a.get("listing_review")),
        "status_request": review_out(a.get("status_request")),
        "created_by": sid(a.get("created_by")),
        "long_description": a.get("long_description", ""),
        "screenshots": [media_url(k) for k in a.get("screenshot_keys", [])],
        "target_platforms": a.get("target_platforms", []),
        "available_platforms": a.get("available_platforms", []),
        "android_package": a.get("android_package"),
        "default_language": a.get("default_language", "fr"),
        "translations": a.get("translations") or {},
        "testers": a.get("testers") or [],
        "min_beta_testers": get_settings().min_beta_testers,
        # Page web publique (lien partageable) et répartition des notes
        "share_url": share_url(a["_id"]),
        "rating_distribution": (a.get("rating") or {}).get("distribution") or {str(i): 0 for i in range(1, 6)},
        "short_description": a.get("short_description", ""),
        "last_published_at": a.get("last_published_at"),
        "updated_at": a.get("updated_at"),
    }


# Versions visibles publiquement : publiées, stockées et validées par l'analyse de sécurité
PUBLIC_VERSION_FILTER = {"status": "published", "security_scan_status": "passed", "upload_status": "stored"}


async def refresh_app_catalog_fields(db: AsyncDatabase, app_id: ObjectId) -> None:
    """Recalcule les champs dénormalisés d'une app à partir de ses versions publiées en production."""
    published = await db.versions.find({"app_id": app_id, **PUBLIC_VERSION_FILTER, "channel": {"$ne": "beta"}}).to_list(None)
    platforms = sorted({v["platform"] for v in published})
    latest = max(published, key=lambda v: v["version_code"], default=None)
    last_published = max((v.get("published_at") for v in published if v.get("published_at")), default=None)
    await db.apps.update_one(
        {"_id": app_id},
        {
            "$set": {
                "available_platforms": platforms,
                "latest_version_name": latest["version_name"] if latest else None,
                "last_published_at": last_published,
                "updated_at": now(),
            }
        },
    )
