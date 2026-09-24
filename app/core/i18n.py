"""Messages d'erreur renvoyés aux clients, en français ou en anglais selon l'en-tête Accept-Language."""

from fastapi import HTTPException, Request

MESSAGES: dict[str, dict[str, str]] = {
    "not_authenticated": {"fr": "Authentification requise.", "en": "Authentication required."},
    "invalid_token": {"fr": "Session invalide ou expirée.", "en": "Invalid or expired session."},
    "forbidden": {"fr": "Action non autorisée.", "en": "You are not allowed to do this."},
    "invalid_credentials": {"fr": "Email ou mot de passe incorrect.", "en": "Invalid email or password."},
    "email_taken": {"fr": "Un compte existe déjà avec cet email.", "en": "An account already exists with this email."},
    "weak_password": {
        "fr": "Le mot de passe doit contenir au moins 8 caractères.",
        "en": "Password must be at least 8 characters.",
    },
    "account_disabled": {"fr": "Ce compte est désactivé.", "en": "This account is disabled."},
    "not_found": {"fr": "Élément introuvable.", "en": "Not found."},
    "app_not_found": {"fr": "Application introuvable.", "en": "App not found."},
    "version_not_found": {"fr": "Version introuvable.", "en": "Version not found."},
    "category_not_found": {"fr": "Catégorie introuvable.", "en": "Category not found."},
    "version_unavailable": {"fr": "Cette version n'est pas disponible.", "en": "This version is not available."},
    "invalid_id": {"fr": "Identifiant invalide.", "en": "Invalid identifier."},
    "invalid_format": {
        "fr": "Format de fichier incompatible avec la plateforme.",
        "en": "File format does not match the platform.",
    },
    "file_too_large": {"fr": "Fichier trop volumineux.", "en": "File too large."},
    "empty_file": {"fr": "Le fichier est vide.", "en": "The file is empty."},
    "version_exists": {
        "fr": "Cette version existe déjà pour ce format.",
        "en": "This version already exists for this format.",
    },
    "scan_not_passed": {
        "fr": "La version ne peut pas être publiée tant que l'analyse de sécurité n'est pas validée.",
        "en": "The version cannot be published until the security scan has passed.",
    },
    "category_in_use": {
        "fr": "Cette catégorie contient encore des applications : indiquez une catégorie de remplacement.",
        "en": "This category still has apps: provide a replacement category.",
    },
    "last_admin": {"fr": "Impossible de retirer le dernier administrateur.", "en": "Cannot remove the last admin."},
    "too_many_attempts": {
        "fr": "Trop de tentatives de connexion. Réessayez dans quelques minutes.",
        "en": "Too many login attempts. Please try again in a few minutes.",
    },
    "too_many_screenshots": {"fr": "Nombre maximal de captures d'écran atteint (12).", "en": "Maximum number of screenshots reached (12)."},
    "invalid_version_name": {
        "fr": "Numéro de version invalide : utilisez un numéro sémantique (ex. 1.4.2 ou 2.0.0-beta.1).",
        "en": "Invalid version number: use a semantic version (e.g. 1.4.2 or 2.0.0-beta.1).",
    },
    "invalid_image": {"fr": "Image invalide (PNG, JPEG ou WebP).", "en": "Invalid image (PNG, JPEG or WebP)."},
}


def language(request: Request | None) -> str:
    header = (request.headers.get("accept-language") if request else "") or ""
    return "fr" if header.lower().startswith("fr") else "en"


class ApiError(HTTPException):
    """Erreur applicative : code stable + message localisé (voir exception_handler dans main.py)."""

    def __init__(self, status_code: int, code: str, **extra):
        super().__init__(status_code=status_code, detail=code)
        self.code = code
        self.extra = extra


def message(code: str, lang: str) -> str:
    return MESSAGES.get(code, {}).get(lang) or MESSAGES.get(code, {}).get("en") or code
