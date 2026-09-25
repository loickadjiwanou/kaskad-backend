from typing import Annotated

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pymongo.asynchronous.database import AsyncDatabase

from app.core.i18n import ApiError
from app.core.ratelimit import LoginRateLimiter
from app.core.security import decode_token
from app.models.common import maybe_oid
from app.services.accounts import can_manage_team, can_write
from app.services.mailer import Mailer
from app.services.notifications import PushService
from app.services.scanning import ScanQueue
from app.services.storage import Storage

bearer = HTTPBearer(auto_error=False)


def get_db(request: Request) -> AsyncDatabase:
    return request.app.state.db


def get_storage(request: Request) -> Storage:
    return request.app.state.storage


def get_scan_queue(request: Request) -> ScanQueue:
    return request.app.state.scan_queue


def get_push(request: Request) -> PushService:
    return request.app.state.push


def get_login_limiter(request: Request) -> LoginRateLimiter:
    return request.app.state.login_limiter


def get_mailer(request: Request) -> Mailer:
    return request.app.state.mailer


LoginLimiter = Annotated[LoginRateLimiter, Depends(get_login_limiter)]
Db = Annotated[AsyncDatabase, Depends(get_db)]
StorageDep = Annotated[Storage, Depends(get_storage)]
ScanQueueDep = Annotated[ScanQueue, Depends(get_scan_queue)]
PushDep = Annotated[PushService, Depends(get_push)]
MailerDep = Annotated[Mailer, Depends(get_mailer)]
Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]


def _access_payload(creds: HTTPAuthorizationCredentials | None, scope: str) -> dict:
    if not creds:
        raise ApiError(401, "not_authenticated")
    try:
        payload = decode_token(creds.credentials)
    except jwt.PyJWTError:
        raise ApiError(401, "invalid_token") from None
    if payload.get("typ") != "access" or payload.get("scope") != scope:
        raise ApiError(401, "invalid_token")
    return payload


async def current_admin(db: Db, creds: Credentials) -> dict:
    payload = _access_payload(creds, "admin")
    admin = await db.admins.find_one({"_id": maybe_oid(payload["sub"])})
    if not admin or not admin.get("active", True):
        raise ApiError(401, "invalid_token")
    return admin


CurrentAdmin = Annotated[dict, Depends(current_admin)]


async def require_full_admin(admin: CurrentAdmin) -> dict:
    """Administrateur de la plateforme (unique) : comptes développeurs, catégories."""
    if admin.get("role") != "admin":
        raise ApiError(403, "forbidden")
    return admin


FullAdmin = Annotated[dict, Depends(require_full_admin)]


async def require_writer(admin: CurrentAdmin) -> dict:
    """Modification du contenu : refusée aux lecteurs (rôle `viewer`, consultation seule)."""
    if not can_write(admin):
        raise ApiError(403, "read_only")
    return admin


Writer = Annotated[dict, Depends(require_writer)]


async def require_team_manager(admin: CurrentAdmin) -> dict:
    """Gestion des membres et invitations : propriétaire du compte (ou administrateur de la plateforme pour le sien)."""
    if not can_manage_team(admin):
        raise ApiError(403, "forbidden")
    return admin


TeamManager = Annotated[dict, Depends(require_team_manager)]


async def require_publisher(admin: CurrentAdmin) -> dict:
    """Publication et validation des demandes : réservées aux admins complets (les éditeurs soumettent)."""
    if admin.get("role") != "admin":
        raise ApiError(403, "publish_requires_admin")
    return admin


Publisher = Annotated[dict, Depends(require_publisher)]


async def current_user(db: Db, creds: Credentials) -> dict:
    payload = _access_payload(creds, "user")
    user = await db.users.find_one({"_id": maybe_oid(payload["sub"])})
    if not user:
        raise ApiError(401, "invalid_token")
    return user


CurrentUser = Annotated[dict, Depends(current_user)]


async def optional_user(db: Db, creds: Credentials) -> dict | None:
    if not creds:
        return None
    try:
        return await current_user(db, creds)
    except ApiError:
        return None


OptionalUser = Annotated[dict | None, Depends(optional_user)]
