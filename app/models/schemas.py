from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

from app.models.common import AdminRole, AppStatus, Platform


# ---------- Authentification
class EmailPassword(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class AnonymousLogin(BaseModel):
    device_id: str = Field(min_length=8, max_length=128)


class RefreshIn(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    id: str
    email: str | None = None
    anonymous: bool


class UserSession(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserOut


class AdminOut(BaseModel):
    id: str
    email: str
    name: str
    role: AdminRole
    active: bool
    created_at: datetime | None = None


class AdminSession(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    admin: AdminOut


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


# ---------- Utilisateurs finaux
class FollowedApp(BaseModel):
    app_id: str
    notify: bool = True


class InstalledApp(BaseModel):
    app_id: str
    version_id: str


class LibraryIn(BaseModel):
    favorites: list[str] = Field(default_factory=list, max_length=1000)
    followed_apps: list[FollowedApp] = Field(default_factory=list, max_length=1000)
    installed_apps: list[InstalledApp] = Field(default_factory=list, max_length=1000)


class PushTokenIn(BaseModel):
    token: str = Field(min_length=8, max_length=4096)
    provider: Literal["fcm", "apns"]
    platform: str | None = None
    language: Literal["fr", "en"] | None = None


class InstalledCheck(BaseModel):
    app_id: str
    version_id: str | None = None
    version_code: int
    platform: Platform | None = None


class UpdatesCheckIn(BaseModel):
    installed: list[InstalledCheck] = Field(default_factory=list, max_length=500)


# ---------- Catégories
class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    icon: str = Field(default="shape-outline", max_length=80)
    order: int | None = None


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    icon: str | None = Field(default=None, max_length=80)
    order: int | None = None


class CategoryOrder(BaseModel):
    ids: list[str]


class CategoryReassign(BaseModel):
    to_category_id: str
    app_ids: list[str] | None = None  # None = toutes les apps de la catégorie


# ---------- Applications
class AppIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    short_description: str = Field(default="", max_length=200)
    long_description: str = Field(default="", max_length=20000)
    category_ids: list[str] = Field(default_factory=list)
    target_platforms: list[Platform] = Field(default_factory=list)
    featured: bool = False
    android_package: str | None = Field(default=None, max_length=255)


class AppUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    short_description: str | None = Field(default=None, max_length=200)
    long_description: str | None = Field(default=None, max_length=20000)
    category_ids: list[str] | None = None
    target_platforms: list[Platform] | None = None
    featured: bool | None = None
    android_package: str | None = Field(default=None, max_length=255)


class AppStatusIn(BaseModel):
    status: AppStatus


class ScreenshotsOrder(BaseModel):
    urls: list[str]


# ---------- Versions
class VersionUpdate(BaseModel):
    changelog: str | None = Field(default=None, max_length=20000)
    version_name: str | None = Field(default=None, min_length=1, max_length=50)


# ---------- Administrateurs
class AdminCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    name: str = Field(min_length=1, max_length=100)
    role: AdminRole = "editor"


class AdminUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    role: AdminRole | None = None
    active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=256)
