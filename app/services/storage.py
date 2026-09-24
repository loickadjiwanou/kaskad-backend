"""Stockage des binaires et médias, hors MongoDB (qui ne garde que les métadonnées).

- S3Storage : tout service compatible S3 (MinIO auto-hébergé, Backblaze B2…), URLs pré-signées.
- LocalStorage : disque local (développement / tests), URLs signées par HMAC servies par l'API.
"""

import hashlib
import hmac
import os
import shutil
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Protocol
from urllib.parse import quote, urlencode

from anyio import to_thread

from app.core.config import Settings, get_settings


@dataclass
class StoredObject:
    key: str
    last_modified: datetime
    size: int


class Storage(Protocol):
    async def save(self, key: str, fileobj: BinaryIO, content_type: str) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def exists(self, key: str) -> bool: ...
    async def signed_url(self, key: str, filename: str | None = None, ttl: int | None = None) -> str: ...
    def local_copy(self, key: str) -> "AsyncIterator[Path]": ...
    async def list_objects(self, prefix: str) -> list[StoredObject]: ...
    async def abort_incomplete_uploads(self, older_than: datetime) -> int: ...
    async def init(self) -> None: ...


def _safe_key(key: str) -> str:
    key = key.lstrip("/")
    if ".." in Path(key).parts:
        raise ValueError("invalid storage key")
    return key


# --------------------------------------------------------------------------- local


class LocalStorage:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = Path(settings.local_storage_dir).resolve()

    async def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        p = (self.root / _safe_key(key)).resolve()
        if not str(p).startswith(str(self.root)):
            raise ValueError("invalid storage key")
        return p

    async def save(self, key: str, fileobj: BinaryIO, content_type: str) -> None:
        def _write():
            p = self.path(key)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".part")
            with open(tmp, "wb") as out:
                shutil.copyfileobj(fileobj, out, length=1024 * 1024)
            os.replace(tmp, p)

        await to_thread.run_sync(_write)

    async def delete(self, key: str) -> None:
        await to_thread.run_sync(lambda: self.path(key).unlink(missing_ok=True))

    async def exists(self, key: str) -> bool:
        return self.path(key).is_file()

    def sign(self, key: str, expires: int, filename: str | None) -> str:
        msg = f"{key}|{expires}|{filename or ''}".encode()
        return hmac.new(self.settings.jwt_secret.encode(), msg, hashlib.sha256).hexdigest()

    def verify(self, key: str, expires: int, filename: str | None, signature: str) -> bool:
        return expires >= int(time.time()) and hmac.compare_digest(self.sign(key, expires, filename), signature)

    async def signed_url(self, key: str, filename: str | None = None, ttl: int | None = None) -> str:
        s = self.settings
        expires = int(time.time()) + (ttl or s.download_url_ttl_seconds)
        params = {"exp": expires, "sig": self.sign(key, expires, filename)}
        if filename:
            params["name"] = filename
        return f"{s.public_base_url}{s.api_prefix}/files/{quote(key)}?{urlencode(params)}"

    @asynccontextmanager
    async def local_copy(self, key: str) -> AsyncIterator[Path]:
        yield self.path(key)

    async def list_objects(self, prefix: str) -> list[StoredObject]:
        def _list():
            base = self.path(prefix) if prefix else self.root
            if not base.exists():
                return []
            out = []
            for p in base.rglob("*"):
                if p.is_file():
                    st = p.stat()
                    out.append(
                        StoredObject(
                            key=str(p.relative_to(self.root)),
                            last_modified=datetime.fromtimestamp(st.st_mtime, UTC),
                            size=st.st_size,
                        )
                    )
            return out

        return await to_thread.run_sync(_list)

    async def abort_incomplete_uploads(self, older_than: datetime) -> int:
        # Fichiers ".part" laissés par une écriture interrompue
        def _clean():
            count = 0
            for p in self.root.rglob("*.part"):
                if datetime.fromtimestamp(p.stat().st_mtime, UTC) < older_than:
                    p.unlink(missing_ok=True)
                    count += 1
            return count

        return await to_thread.run_sync(_clean)


# --------------------------------------------------------------------------- S3


class S3Storage:
    def __init__(self, settings: Settings):
        import boto3
        from botocore.config import Config

        self.settings = settings
        self.bucket = settings.s3_bucket
        kwargs = {
            "region_name": settings.s3_region,
            "aws_access_key_id": settings.s3_access_key,
            "aws_secret_access_key": settings.s3_secret_key,
            "config": Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        }
        self.client = boto3.client("s3", endpoint_url=settings.s3_endpoint_url, **kwargs)
        # Client dédié à la signature si l'URL publique diffère de l'URL interne (ex. Docker)
        self.presign_client = (
            boto3.client("s3", endpoint_url=settings.s3_public_endpoint_url, **kwargs) if settings.s3_public_endpoint_url else self.client
        )

    async def init(self) -> None:
        def _ensure_bucket():
            try:
                self.client.head_bucket(Bucket=self.bucket)
            except Exception:
                self.client.create_bucket(Bucket=self.bucket)

        await to_thread.run_sync(_ensure_bucket)

    async def save(self, key: str, fileobj: BinaryIO, content_type: str) -> None:
        # upload_fileobj bascule automatiquement en upload multipart pour les gros fichiers
        await to_thread.run_sync(
            lambda: self.client.upload_fileobj(fileobj, self.bucket, _safe_key(key), ExtraArgs={"ContentType": content_type})
        )

    async def delete(self, key: str) -> None:
        await to_thread.run_sync(lambda: self.client.delete_object(Bucket=self.bucket, Key=_safe_key(key)))

    async def exists(self, key: str) -> bool:
        def _head():
            try:
                self.client.head_object(Bucket=self.bucket, Key=_safe_key(key))
                return True
            except Exception:
                return False

        return await to_thread.run_sync(_head)

    async def signed_url(self, key: str, filename: str | None = None, ttl: int | None = None) -> str:
        params = {"Bucket": self.bucket, "Key": _safe_key(key)}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        return await to_thread.run_sync(
            lambda: self.presign_client.generate_presigned_url(
                "get_object", Params=params, ExpiresIn=ttl or self.settings.download_url_ttl_seconds
            )
        )

    @asynccontextmanager
    async def local_copy(self, key: str) -> AsyncIterator[Path]:
        fd, tmp = tempfile.mkstemp(prefix="kaskad-scan-", suffix=Path(key).suffix)
        os.close(fd)
        try:
            await to_thread.run_sync(lambda: self.client.download_file(self.bucket, _safe_key(key), tmp))
            yield Path(tmp)
        finally:
            Path(tmp).unlink(missing_ok=True)

    async def list_objects(self, prefix: str) -> list[StoredObject]:
        def _list():
            out = []
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                for o in page.get("Contents", []):
                    out.append(StoredObject(key=o["Key"], last_modified=o["LastModified"], size=o["Size"]))
            return out

        return await to_thread.run_sync(_list)

    async def abort_incomplete_uploads(self, older_than: datetime) -> int:
        def _abort():
            count = 0
            resp = self.client.list_multipart_uploads(Bucket=self.bucket)
            for u in resp.get("Uploads", []):
                if u["Initiated"] < older_than:
                    self.client.abort_multipart_upload(Bucket=self.bucket, Key=u["Key"], UploadId=u["UploadId"])
                    count += 1
            return count

        return await to_thread.run_sync(_abort)


def create_storage(settings: Settings | None = None) -> Storage:
    settings = settings or get_settings()
    return S3Storage(settings) if settings.storage_backend == "s3" else LocalStorage(settings)
