"""Sessions JWT : jeton d'accès court + jeton de rafraîchissement à usage unique (rotation), révocable."""

import jwt
from bson import ObjectId
from pymongo.asynchronous.database import AsyncDatabase

from app.core.i18n import ApiError
from app.core.security import Scope, create_token, decode_token
from app.models.common import now


async def issue_tokens(db: AsyncDatabase, subject_id: ObjectId, scope: Scope) -> dict:
    access, _, _ = create_token(str(subject_id), scope, "access")
    refresh, jti, expires = create_token(str(subject_id), scope, "refresh")
    await db.refresh_tokens.insert_one({"jti": jti, "subject_id": subject_id, "scope": scope, "expires_at": expires, "created_at": now()})
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer"}


def _decode_refresh(token: str, scope: Scope) -> dict:
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        raise ApiError(401, "invalid_token") from None
    if payload.get("typ") != "refresh" or payload.get("scope") != scope:
        raise ApiError(401, "invalid_token")
    return payload


async def rotate_refresh_token(db: AsyncDatabase, token: str, scope: Scope) -> tuple[ObjectId, dict]:
    payload = _decode_refresh(token, scope)
    # Consommation atomique : un jeton déjà utilisé (ou révoqué) est refusé
    stored = await db.refresh_tokens.find_one_and_delete({"jti": payload["jti"], "scope": scope})
    if not stored:
        raise ApiError(401, "invalid_token")
    subject_id = stored["subject_id"]
    return subject_id, await issue_tokens(db, subject_id, scope)


async def revoke_refresh_token(db: AsyncDatabase, token: str, scope: Scope) -> None:
    try:
        payload = _decode_refresh(token, scope)
    except ApiError:
        return
    await db.refresh_tokens.delete_one({"jti": payload["jti"]})


async def revoke_all(db: AsyncDatabase, subject_id: ObjectId) -> None:
    await db.refresh_tokens.delete_many({"subject_id": subject_id})
