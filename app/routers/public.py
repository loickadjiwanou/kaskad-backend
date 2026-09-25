"""API publique consommée par l'app client (mobile + desktop)."""

import re
from typing import Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from pymongo.collation import Collation

from app.core.i18n import ApiError, language
from app.deps import Db, OptionalUser, StorageDep
from app.models.common import Platform, maybe_oid, oid
from app.models.schemas import UpdatesCheckIn, ViewIn
from app.services.catalog import (
    PUBLIC_APP_FILTER,
    PUBLIC_VERSION_FILTER,
    app_detail,
    app_summary,
    category_out,
    channel_filter,
    is_tester,
    verify_beta_token,
    version_public,
)
from app.services.geo import country_for
from app.services.stats import record_download, record_installations, record_view, visitor_hash
from app.services.storage import LocalStorage

router = APIRouter(tags=["public"])

Sort = Literal["popular", "recent", "name"]


@router.get("/health")
async def health(db: Db):
    await db.command("ping")
    return {"status": "ok"}


@router.get("/categories")
async def list_categories(db: Db):
    cats = await db.categories.find().sort([("order", 1), ("name", 1)]).to_list(None)
    return [category_out(c) for c in cats]


def _compatible_first(apps: list[dict], platform: str | None) -> list[dict]:
    if not platform:
        return apps
    return sorted(apps, key=lambda a: platform not in (a.get("available_platforms") or a.get("target_platforms", [])))


@router.get("/home")
async def home(db: Db, request: Request, platform: Platform | None = None):
    """Accueil : apps en vedette, nouveautés, populaires. `platform` place les apps compatibles en premier (sans filtrer)."""
    published = PUBLIC_APP_FILTER
    featured = await db.apps.find({**published, "featured": True}).sort("downloads_count", -1).to_list(20)
    recent = await db.apps.find(published).sort([("last_published_at", -1), ("created_at", -1)]).to_list(12)
    popular = await db.apps.find(published).sort("downloads_count", -1).to_list(12)
    lang = language(request)
    return {
        "featured": [app_summary(a, lang) for a in _compatible_first(featured, platform)],
        "new": [app_summary(a, lang) for a in _compatible_first(recent, platform)[:8]],
        "popular": [app_summary(a, lang) for a in _compatible_first(popular, platform)[:8]],
    }


def _search_filter(q: str | None, category_id: str | None, platform: str | None) -> dict:
    flt: dict = dict(PUBLIC_APP_FILTER)
    if category_id:
        flt["category_ids"] = maybe_oid(category_id)
    if platform:
        flt["available_platforms"] = platform
    words = [w for w in (q or "").strip().split() if w][:8]
    if words:
        # Chaque mot doit apparaître dans le nom ou une description (insensible à la casse)
        flt["$and"] = [
            {"$or": [{f: {"$regex": re.escape(w), "$options": "i"}} for f in ("name", "short_description", "long_description")]}
            for w in words
        ]
    return flt


@router.get("/apps")
async def list_apps(
    db: Db,
    request: Request,
    q: str | None = Query(default=None, max_length=100),
    category_id: str | None = None,
    platform: Platform | None = None,
    sort: Sort = "popular",
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    ids: str | None = Query(default=None, description="Identifiants séparés par des virgules (récupération groupée)"),
    developer_id: str | None = Query(default=None, description="Apps d'un compte développeur"),
):
    flt = _search_filter(q, category_id, platform)
    if developer_id:
        flt["account_id"] = maybe_oid(developer_id)
    if ids:
        flt["_id"] = {"$in": [o for o in (maybe_oid(i.strip()) for i in ids.split(",")[:100]) if o]}
    order = {
        "popular": [("downloads_count", -1), ("name", 1)],
        "recent": [("last_published_at", -1), ("created_at", -1)],
        "name": [("name", 1)],
    }[sort]
    cursor = db.apps.find(flt).sort(order).skip((page - 1) * limit).limit(limit)
    if sort == "name":
        cursor = cursor.collation(Collation(locale="fr", strength=1))
    items = await cursor.to_list(None)
    total = await db.apps.count_documents(flt)
    lang = language(request)
    return {"items": [app_summary(a, lang) for a in items], "total": total, "page": page, "limit": limit}


# La recherche partage les mêmes paramètres que la liste
router.add_api_route("/search", list_apps, methods=["GET"], tags=["public"])


async def _published_app(db, app_id: str) -> dict:
    app = await db.apps.find_one({"_id": oid(app_id, "app_not_found"), **PUBLIC_APP_FILTER})
    if not app:
        raise ApiError(404, "app_not_found")
    return app


@router.get("/apps/{app_id}")
async def get_app(db: Db, request: Request, user: OptionalUser, app_id: str):
    """Fiche dans la langue de l'app client ; les testeurs (utilisateur connecté) voient aussi les versions bêta."""
    app = await _published_app(db, app_id)
    categories = await db.categories.find({"_id": {"$in": app.get("category_ids", [])}}).sort("order", 1).to_list(None)
    flt = {"app_id": app["_id"], **PUBLIC_VERSION_FILTER, **channel_filter(app, user)}
    versions = await db.versions.find(flt).sort("version_code", -1).to_list(None)
    return {**app_detail(app, categories, versions, language(request)), "is_tester": is_tester(app, user)}


@router.get("/developers/{developer_id}")
async def get_developer(db: Db, developer_id: str):
    """Compte développeur public : nom et nombre d'apps publiées (liste : GET /apps?developer_id=)."""
    account = await db.accounts.find_one({"_id": oid(developer_id, "developer_not_found"), "suspended": {"$ne": True}})
    if not account:
        raise ApiError(404, "developer_not_found")
    count = await db.apps.count_documents({"account_id": account["_id"], **PUBLIC_APP_FILTER})
    return {"id": developer_id, "name": account["name"], "apps_count": count}


@router.get("/apps/{app_id}/versions")
async def get_app_versions(db: Db, request: Request, user: OptionalUser, app_id: str, platform: Platform | None = None):
    app = await _published_app(db, app_id)
    flt = {"app_id": app["_id"], **PUBLIC_VERSION_FILTER, **channel_filter(app, user)}
    if platform:
        flt["platform"] = platform
    versions = await db.versions.find(flt).sort("version_code", -1).to_list(None)
    lang = language(request)
    return [version_public(v, lang) for v in versions]


@router.get("/versions/{version_id}/download")
async def download_version(
    request: Request, db: Db, storage: StorageDep, version_id: str, platform: Platform | None = None, t: str | None = None
):
    """Compte le téléchargement puis redirige vers une URL signée temporaire. N'installe jamais rien.

    Une reprise de téléchargement (en-tête Range qui ne commence pas à 0) n'est pas comptée une seconde fois.
    """
    version = await db.versions.find_one({"_id": oid(version_id, "version_not_found"), **PUBLIC_VERSION_FILTER})
    if not version:
        raise ApiError(404, "version_unavailable")
    # Version bêta : lien signé remis aux seuls testeurs (champ `file_url` de leurs fiches)
    if version.get("channel") == "beta" and not verify_beta_token(version["_id"], t):
        raise ApiError(404, "version_unavailable")
    app = await db.apps.find_one({"_id": version["app_id"], **PUBLIC_APP_FILTER})
    if not app:
        raise ApiError(404, "version_unavailable")
    if not _is_resume(request.headers.get("range")):
        await record_download(db, app["_id"], version, platform, country_for(request))
    url = await storage.signed_url(version["storage_key"], version.get("file_name"))
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})


def _is_resume(range_header: str | None) -> bool:
    match = re.match(r"^\s*bytes\s*=\s*(\d+)-", range_header or "")
    return bool(match and int(match.group(1)) > 0)


@router.post("/apps/{app_id}/view", status_code=204)
async def track_view(db: Db, request: Request, user: OptionalUser, app_id: str, body: ViewIn | None = None):
    """Vue de la fiche dans l'app client (une par visiteur et par app toutes les 30 minutes)."""
    app = await db.apps.find_one({"_id": oid(app_id, "app_not_found"), **PUBLIC_APP_FILTER}, {"_id": 1})
    if not app:
        raise ApiError(404, "app_not_found")
    body = body or ViewIn()
    who = (
        f"user:{user['_id']}"
        if user
        else f"device:{body.device_id}"
        if body.device_id
        else f"ip:{request.client.host if request.client else ''}"
    )
    await record_view(db, app["_id"], visitor_hash(who), body.platform, "app", country_for(request))


@router.post("/updates/check")
async def check_updates(db: Db, request: Request, user: OptionalUser, body: UpdatesCheckIn):
    """Pour chaque app installée, renvoie la dernière version publiée plus récente (même plateforme si précisée)."""
    ids = {maybe_oid(i.app_id) for i in body.installed} - {None}
    if not ids:
        return []
    apps = {a["_id"]: a for a in await db.apps.find({"_id": {"$in": list(ids)}, **PUBLIC_APP_FILTER}, {"testers": 1}).to_list(None)}
    candidates_all = await db.versions.find({"app_id": {"$in": list(apps)}, **PUBLIC_VERSION_FILTER}).sort("version_code", -1).to_list(None)
    # Bêta : proposée uniquement aux testeurs de l'app
    versions = [v for v in candidates_all if v.get("channel") != "beta" or is_tester(apps[v["app_id"]], user)]
    # Versions réellement installées (statistiques), par appareil
    if body.device_id:
        await record_installations(
            db,
            visitor_hash(f"device:{body.device_id}"),
            [
                {"app_id": a, "version_id": maybe_oid(i.version_id), "version_code": i.version_code, "platform": i.platform}
                for i in body.installed
                if (a := maybe_oid(i.app_id)) in apps
            ],
            country_for(request),
        )
    lang = language(request)
    results = []
    for item in body.installed:
        app_id = maybe_oid(item.app_id)
        candidates = [v for v in versions if v["app_id"] == app_id and (not item.platform or v["platform"] == item.platform)]
        latest = candidates[0] if candidates else None
        if latest and latest["version_code"] > item.version_code:
            results.append({"app_id": item.app_id, "latest_version": version_public(latest, lang)})
    return results


@router.get("/media/{key:path}", include_in_schema=False)
async def media(storage: StorageDep, key: str):
    """Icônes et captures d'écran (publiques)."""
    if not key.startswith("media/"):
        raise ApiError(404, "not_found")
    if isinstance(storage, LocalStorage):
        path = storage.path(key)
        if not path.is_file():
            raise ApiError(404, "not_found")
        return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})
    url = await storage.signed_url(key, ttl=3600)
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "public, max-age=3000"})


@router.get("/files/{key:path}", include_in_schema=False)
async def local_file(request: Request, storage: StorageDep, key: str, exp: int, sig: str, name: str | None = None):
    """Fichiers du stockage local, via URL signée et temporaire (équivalent des URLs pré-signées S3)."""
    if not isinstance(storage, LocalStorage) or not storage.verify(key, exp, name, sig):
        raise ApiError(403, "forbidden")
    path = storage.path(key)
    if not path.is_file():
        raise ApiError(404, "not_found")
    # FileResponse gère les requêtes Range : les téléchargements interrompus peuvent reprendre
    return FileResponse(path, filename=name, media_type="application/octet-stream")
