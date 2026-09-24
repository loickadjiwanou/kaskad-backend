from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import get_settings

_client: AsyncMongoClient | None = None


def get_client() -> AsyncMongoClient:
    global _client
    if _client is None:
        s = get_settings()
        auth = {}
        if s.mongodb_username:
            auth = {"username": s.mongodb_username, "password": s.mongodb_password, "authSource": s.mongodb_auth_source}
        _client = AsyncMongoClient(s.mongodb_uri, tz_aware=True, uuidRepresentation="standard", **auth)
    return _client


def get_db() -> AsyncDatabase:
    return get_client()[get_settings().mongodb_db]


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


async def ensure_indexes(db: AsyncDatabase) -> None:
    await db.apps.create_index([("status", ASCENDING), ("downloads_count", DESCENDING)])
    await db.apps.create_index([("category_ids", ASCENDING)])
    await db.apps.create_index([("last_published_at", DESCENDING)])
    await db.versions.create_index([("app_id", ASCENDING), ("version_code", DESCENDING)])
    await db.versions.create_index([("security_scan_status", ASCENDING), ("status", ASCENDING)])
    await db.versions.create_index(
        [("app_id", ASCENDING), ("version_code", ASCENDING), ("file_format", ASCENDING)],
        unique=True,
        partialFilterExpression={"upload_status": "stored"},
    )
    # Demandes de validation (file "À valider" de la console)
    await db.versions.create_index("review.state", sparse=True)
    await db.apps.create_index("status_request.state", sparse=True)
    await db.apps.create_index("listing_review.state", sparse=True)
    await db.apps.create_index("listing_draft.category_ids", sparse=True)
    await db.categories.create_index([("order", ASCENDING)])
    await db.users.create_index("email", unique=True, partialFilterExpression={"email": {"$type": "string"}})
    await db.users.create_index("device_id", unique=True, partialFilterExpression={"device_id": {"$type": "string"}})
    await db.users.create_index("followed_apps.app_id")
    await db.admins.create_index("email", unique=True)
    await db.download_stats.create_index([("timestamp", DESCENDING)])
    await db.download_stats.create_index([("app_id", ASCENDING), ("timestamp", DESCENDING)])
    await db.activity_log.create_index([("created_at", DESCENDING)])
    # Jetons de rafraîchissement : suppression automatique à expiration (TTL)
    await db.refresh_tokens.create_index("expires_at", expireAfterSeconds=0)
    await db.refresh_tokens.create_index("jti", unique=True)
