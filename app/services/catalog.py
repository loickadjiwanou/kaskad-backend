"""Conversion des documents MongoDB vers les réponses de l'API (formes attendues par l'app client)."""

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


def download_url(version_id: ObjectId) -> str:
    s = get_settings()
    return f"{s.public_base_url}{s.api_prefix}/versions/{version_id}/download"


def category_out(c: dict) -> dict:
    return {"id": sid(c["_id"]), "name": c["name"], "icon": c.get("icon", "shape-outline"), "order": c.get("order", 0)}


def app_summary(a: dict) -> dict:
    return {
        "id": sid(a["_id"]),
        "name": a["name"],
        "short_description": a.get("short_description", ""),
        "icon_url": media_url(a.get("icon_key")),
        "category_ids": [sid(c) for c in a.get("category_ids", [])],
        # Plateformes réellement disponibles (versions publiées), sinon plateformes ciblées déclarées
        "platforms": a.get("available_platforms") or a.get("target_platforms", []),
        "status": a.get("status", "draft"),
        "downloads_count": a.get("downloads_count", 0),
        "featured": bool(a.get("featured")),
        "latest_version_name": a.get("latest_version_name"),
        "created_at": a.get("created_at"),
        "updated_at": a.get("last_published_at") or a.get("updated_at"),
    }


def version_public(v: dict) -> dict:
    return {
        "id": sid(v["_id"]),
        "app_id": sid(v["app_id"]),
        "version_name": v["version_name"],
        "version_code": v["version_code"],
        "platform": v["platform"],
        "file_format": v["file_format"],
        "file_url": download_url(v["_id"]),
        "file_size": v.get("file_size"),
        "sha256_hash": v.get("sha256_hash"),
        "changelog": v.get("changelog", ""),
        "security_scan_status": v.get("security_scan_status"),
        "published_at": v.get("published_at"),
        "created_at": v.get("created_at"),
    }


def version_admin(v: dict) -> dict:
    return {
        **version_public(v),
        "status": v.get("status"),
        "upload_status": v.get("upload_status"),
        "file_name": v.get("file_name"),
        "scan_report": v.get("scan_report"),
        "apk_info": v.get("apk_info"),
        "created_by": sid(v.get("created_by")),
        "downloads_count": v.get("downloads_count", 0),
        "updated_at": v.get("updated_at"),
    }


def app_detail(a: dict, categories: list[dict], versions: list[dict]) -> dict:
    return {
        **app_summary(a),
        "long_description": a.get("long_description", ""),
        "screenshots": [media_url(k) for k in a.get("screenshot_keys", [])],
        "categories": [category_out(c) for c in categories],
        "versions": [version_public(v) for v in versions],
    }


def app_admin(a: dict) -> dict:
    return {
        **app_summary(a),
        "long_description": a.get("long_description", ""),
        "screenshots": [media_url(k) for k in a.get("screenshot_keys", [])],
        "target_platforms": a.get("target_platforms", []),
        "available_platforms": a.get("available_platforms", []),
        "android_package": a.get("android_package"),
        "last_published_at": a.get("last_published_at"),
        "updated_at": a.get("updated_at"),
    }


# Versions visibles publiquement : publiées, stockées et validées par l'analyse de sécurité
PUBLIC_VERSION_FILTER = {"status": "published", "security_scan_status": "passed", "upload_status": "stored"}


async def refresh_app_catalog_fields(db: AsyncDatabase, app_id: ObjectId) -> None:
    """Recalcule les champs dénormalisés d'une app à partir de ses versions publiées."""
    published = await db.versions.find({"app_id": app_id, **PUBLIC_VERSION_FILTER}).to_list(None)
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
