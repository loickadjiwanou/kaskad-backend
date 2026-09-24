from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration de l'API (variables d'environnement ou fichier .env)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Général
    environment: Literal["development", "production", "test"] = "development"
    api_prefix: str = "/api/v1"
    # URL publique de l'API (sert à construire les URLs des médias et des fichiers en stockage local)
    public_base_url: str = "http://localhost:8000"
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:8081",  # Expo web
            "http://localhost:5173",  # console admin (Vite)
            "kaskad://app",  # Electron
        ]
    )

    # --- MongoDB
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_db: str = "kaskad"
    # Identifiants MongoDB (optionnels) : plus simple que de les encoder dans l'URI
    mongodb_username: str | None = None
    mongodb_password: str | None = None
    mongodb_auth_source: str = "admin"  # base où l'utilisateur est défini

    # --- JWT
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    admin_access_ttl_minutes: int = 30
    admin_refresh_ttl_days: int = 7
    user_access_ttl_minutes: int = 60
    user_refresh_ttl_days: int = 90

    # Anti brute force : tentatives de connexion autorisées par compte et par adresse IP
    login_max_attempts: int = 10
    login_window_minutes: int = 15

    # Premier compte admin créé au démarrage s'il n'en existe aucun
    admin_email: str | None = None
    admin_password: str | None = None

    # --- Stockage des binaires (jamais dans MongoDB)
    storage_backend: Literal["local", "s3"] = "local"
    local_storage_dir: str = "./storage"
    s3_endpoint_url: str | None = None  # ex. http://minio:9000 ou https://s3.eu-central-003.backblazeb2.com
    s3_public_endpoint_url: str | None = None  # si l'URL vue par les clients diffère (ex. localhost vs minio)
    s3_region: str = "us-east-1"
    s3_bucket: str = "kaskad"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    # Durée de validité des URLs de téléchargement signées (anti-hotlinking)
    download_url_ttl_seconds: int = 600
    max_upload_size_mb: int = 2048
    # Nettoyage des uploads incomplets / orphelins
    orphan_upload_max_age_hours: int = 6
    cleanup_interval_minutes: int = 60

    # --- Analyse de sécurité
    clamav_host: str | None = None
    clamav_port: int = 3310
    virustotal_api_key: str | None = None
    # Production : exiger au moins un antivirus (ClamAV ou VirusTotal) pour valider un fichier
    scan_require_antivirus: bool = False
    # Exiger une signature Authenticode valide pour les EXE / MSI
    require_authenticode: bool = False

    # --- Notifications push
    firebase_credentials_file: str | None = None  # compte de service Firebase (FCM HTTP v1)
    apns_key_file: str | None = None  # clé .p8
    apns_key_id: str | None = None
    apns_team_id: str | None = None
    apns_bundle_id: str = "com.kaskad.store"
    apns_use_sandbox: bool = True

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
