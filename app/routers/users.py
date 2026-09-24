"""Comptes utilisateurs finaux (optionnels) : email / mot de passe ou anonyme (ID d'appareil), bibliothèque, jetons push."""

from fastapi import APIRouter, Request
from pymongo.errors import DuplicateKeyError

from app.core.i18n import ApiError
from app.core.security import hash_password, verify_password
from app.deps import CurrentUser, Db, LoginLimiter, OptionalUser
from app.models.common import maybe_oid, now, sid
from app.models.schemas import AnonymousLogin, EmailPassword, LibraryIn, PushTokenIn, RefreshIn, UserSession
from app.services.auth import issue_tokens, revoke_all, revoke_refresh_token, rotate_refresh_token
from app.services.catalog import PUBLIC_VERSION_FILTER, app_summary, version_public

router = APIRouter(tags=["users"])


def user_out(u: dict) -> dict:
    return {"id": sid(u["_id"]), "email": u.get("email"), "anonymous": bool(u.get("anonymous"))}


async def _session(db, user: dict) -> dict:
    return {**(await issue_tokens(db, user["_id"], "user")), "user": user_out(user)}


@router.post("/auth/register", response_model=UserSession)
async def register(db: Db, request: Request, limiter: LoginLimiter, body: EmailPassword, current: OptionalUser):
    """Crée un compte email. Si l'appelant est connecté en anonyme, son compte est converti (bibliothèque conservée)."""
    limiter.check(request, f"register:{body.email}")
    if len(body.password) < 8:
        raise ApiError(422, "weak_password")
    email = body.email.lower()
    if await db.users.find_one({"email": email}):
        raise ApiError(409, "email_taken")
    fields = {"email": email, "password_hash": hash_password(body.password), "anonymous": False, "updated_at": now()}
    try:
        if current and current.get("anonymous"):
            # L'identifiant d'appareil est détaché : une future connexion anonyme créera un nouveau compte
            await db.users.update_one({"_id": current["_id"]}, {"$set": fields, "$unset": {"device_id": ""}})
            user = await db.users.find_one({"_id": current["_id"]})
        else:
            doc = {**fields, "favorites": [], "followed_apps": [], "installed_apps": [], "push_tokens": [], "created_at": now()}
            doc["_id"] = (await db.users.insert_one(doc)).inserted_id
            user = doc
    except DuplicateKeyError:
        raise ApiError(409, "email_taken") from None
    return await _session(db, user)


@router.post("/auth/login", response_model=UserSession)
async def login(db: Db, request: Request, limiter: LoginLimiter, body: EmailPassword):
    limiter.check(request, f"user:{body.email}")
    user = await db.users.find_one({"email": body.email.lower()})
    if not user or not verify_password(body.password, user.get("password_hash")):
        raise ApiError(401, "invalid_credentials")
    limiter.reset(request, f"user:{body.email}")
    return await _session(db, user)


@router.post("/auth/anonymous", response_model=UserSession)
async def login_anonymous(db: Db, body: AnonymousLogin):
    """Compte anonyme lié à un identifiant d'appareil aléatoire (aucune donnée personnelle)."""
    user = await db.users.find_one_and_update(
        {"device_id": body.device_id},
        {
            "$setOnInsert": {
                "device_id": body.device_id,
                "anonymous": True,
                "favorites": [],
                "followed_apps": [],
                "installed_apps": [],
                "push_tokens": [],
                "created_at": now(),
            },
            "$set": {"updated_at": now()},
        },
        upsert=True,
        return_document=True,
    )
    return await _session(db, user)


@router.post("/auth/refresh")
async def refresh(db: Db, body: RefreshIn):
    _, tokens = await rotate_refresh_token(db, body.refresh_token, "user")
    return tokens


@router.post("/auth/logout", status_code=204)
async def logout(db: Db, body: RefreshIn):
    await revoke_refresh_token(db, body.refresh_token, "user")


@router.get("/me")
async def me(user: CurrentUser):
    return user_out(user)


@router.delete("/me", status_code=204)
async def delete_account(db: Db, user: CurrentUser):
    """Suppression définitive du compte et de ses données (bibliothèque, jetons push, sessions)."""
    await revoke_all(db, user["_id"])
    await db.users.delete_one({"_id": user["_id"]})


async def _my_apps(db, user: dict) -> list[dict]:
    followed = {f["app_id"]: f.get("notify", True) for f in user.get("followed_apps", [])}
    installed = {i["app_id"]: i.get("version_id") for i in user.get("installed_apps", [])}
    app_ids = list(dict.fromkeys([*installed, *followed]))
    apps = {a["_id"]: a for a in await db.apps.find({"_id": {"$in": app_ids}, "status": "published"}).to_list(None)}
    installed_versions = {v["_id"]: v for v in await db.versions.find({"_id": {"$in": [v for v in installed.values() if v]}}).to_list(None)}
    public = await db.versions.find({"app_id": {"$in": list(apps)}, **PUBLIC_VERSION_FILTER}).sort("version_code", -1).to_list(None)

    items = []
    for app_id in app_ids:
        app = apps.get(app_id)
        if not app:  # app dépubliée / archivée : plus proposée
            continue
        current = installed_versions.get(installed.get(app_id))
        platform = current["platform"] if current else None
        latest = next((v for v in public if v["app_id"] == app_id and (not platform or v["platform"] == platform)), None)
        items.append(
            {
                "app": app_summary(app),
                "followed": app_id in followed,
                "notify": followed.get(app_id, False),
                "installed_version": version_public(current) if current else None,
                "latest_version": version_public(latest) if latest else None,
                "update_available": bool(current and latest and latest["version_code"] > current["version_code"]),
            }
        )
    return items


@router.get("/me/apps")
async def my_apps(db: Db, user: CurrentUser):
    """Apps suivies et installées du compte, avec la dernière version disponible (même plateforme que la version installée)."""
    return await _my_apps(db, user)


@router.get("/me/updates")
async def my_updates(db: Db, user: CurrentUser):
    """Uniquement les apps installées pour lesquelles une nouvelle version est disponible."""
    return [i for i in await _my_apps(db, user) if i["update_available"]]


def library_out(u: dict) -> dict:
    return {
        "favorites": [sid(a) for a in u.get("favorites", [])],
        "followed_apps": [{"app_id": sid(f["app_id"]), "notify": f.get("notify", True)} for f in u.get("followed_apps", [])],
        "installed_apps": [{"app_id": sid(i["app_id"]), "version_id": sid(i.get("version_id"))} for i in u.get("installed_apps", [])],
    }


@router.get("/me/library")
async def get_library(user: CurrentUser):
    return library_out(user)


@router.put("/me/library")
async def put_library(db: Db, user: CurrentUser, body: LibraryIn):
    """Remplace la bibliothèque du compte (favoris, apps suivies avec préférence de notification, apps installées)."""

    def ids(values):
        return [o for o in (maybe_oid(v) for v in values) if o]

    favorites = list(dict.fromkeys(ids(body.favorites)))
    followed = [{"app_id": o, "notify": f.notify} for f in body.followed_apps if (o := maybe_oid(f.app_id))]
    installed = [{"app_id": a, "version_id": maybe_oid(i.version_id)} for i in body.installed_apps if (a := maybe_oid(i.app_id))]
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"favorites": favorites, "followed_apps": followed, "installed_apps": installed, "updated_at": now()}},
    )
    return library_out(await db.users.find_one({"_id": user["_id"]}))


@router.post("/me/push-tokens", status_code=204)
async def add_push_token(db: Db, user: CurrentUser, body: PushTokenIn):
    # Un jeton appartient à un seul appareil / compte : il est retiré des autres comptes
    await db.users.update_many({"push_tokens.token": body.token}, {"$pull": {"push_tokens": {"token": body.token}}})
    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$push": {
                "push_tokens": {
                    "$each": [
                        {
                            "token": body.token,
                            "provider": body.provider,
                            "platform": body.platform,
                            "language": body.language or "fr",
                            "updated_at": now(),
                        }
                    ],
                    "$slice": -10,  # 10 appareils maximum par compte
                }
            }
        },
    )


@router.delete("/me/push-tokens/{token}", status_code=204)
async def remove_push_token(db: Db, user: CurrentUser, token: str):
    await db.users.update_one({"_id": user["_id"]}, {"$pull": {"push_tokens": {"token": token}}})
