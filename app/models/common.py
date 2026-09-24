from datetime import UTC, datetime
from typing import Literal

from bson import ObjectId
from bson.errors import InvalidId

from app.core.i18n import ApiError

Platform = Literal["android", "windows", "macos", "linux"]
FileFormat = Literal["apk", "exe", "msi", "dmg", "pkg", "appimage", "deb", "rpm"]
AppStatus = Literal["draft", "published", "archived"]
VersionStatus = Literal["draft", "published", "archived"]
ScanStatus = Literal["pending", "scanning", "passed", "failed"]
AdminRole = Literal["admin", "editor"]

# Formats installables autorisés par plateforme
FORMATS_BY_PLATFORM: dict[str, tuple[str, ...]] = {
    "android": ("apk",),
    "windows": ("exe", "msi"),
    "macos": ("dmg", "pkg"),
    "linux": ("appimage", "deb", "rpm"),
}
EXTENSIONS: dict[str, str] = {
    "apk": ".apk",
    "exe": ".exe",
    "msi": ".msi",
    "dmg": ".dmg",
    "pkg": ".pkg",
    "appimage": ".appimage",
    "deb": ".deb",
    "rpm": ".rpm",
}
CONTENT_TYPES: dict[str, str] = {
    "apk": "application/vnd.android.package-archive",
    "exe": "application/vnd.microsoft.portable-executable",
    "msi": "application/x-msi",
    "dmg": "application/x-apple-diskimage",
    "pkg": "application/octet-stream",
    "appimage": "application/octet-stream",
    "deb": "application/vnd.debian.binary-package",
    "rpm": "application/x-rpm",
}


# Numéro de version sémantique : MAJEUR.MINEUR[.CORRECTIF[.BUILD]] + suffixe optionnel (-beta.1, +45…)
SEMVER_PATTERN = r"^\d+\.\d+(\.\d+){0,2}([-+][0-9A-Za-z.-]+)?$"


def now() -> datetime:
    return datetime.now(UTC)


def oid(value: str | ObjectId, code: str = "invalid_id") -> ObjectId:
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise ApiError(404 if code != "invalid_id" else 400, code) from None


def maybe_oid(value: str | None) -> ObjectId | None:
    if not value:
        return None
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


def sid(value: ObjectId | None) -> str | None:
    return str(value) if value is not None else None
