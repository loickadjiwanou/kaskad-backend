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
    "publish_requires_admin": {
        "fr": "Seul un admin complet peut valider et publier. Soumettez votre demande pour validation.",
        "en": "Only a full admin can approve and publish. Submit your request for review instead.",
    },
    "not_submittable": {
        "fr": "Seule une version non publiée, validée par l'analyse de sécurité, peut être soumise.",
        "en": "Only an unpublished version that passed the security scan can be submitted.",
    },
    "already_submitted": {"fr": "Une demande est déjà en attente de validation.", "en": "A request is already awaiting review."},
    "review_not_pending": {"fr": "Aucune demande en attente de validation.", "en": "There is no request awaiting review."},
    "no_listing_draft": {"fr": "Aucune modification de fiche en attente.", "en": "There are no pending listing changes."},
    "status_unchanged": {"fr": "L'application a déjà ce statut.", "en": "The app already has this status."},
    "email_not_verified": {
        "fr": "Confirmez d'abord votre adresse e-mail : ouvrez le lien reçu par e-mail.",
        "en": "Please confirm your email address first: open the link we sent you.",
    },
    "token_invalid": {"fr": "Ce lien n'est plus valide. Demandez-en un nouveau.", "en": "This link is no longer valid. Request a new one."},
    "invitation_invalid": {
        "fr": "Cette invitation n'est plus valide (expirée, annulée ou déjà utilisée).",
        "en": "This invitation is no longer valid (expired, revoked or already used).",
    },
    "already_member": {
        "fr": "Cette adresse e-mail est déjà associée à un compte Kaskad Console.",
        "en": "This email address already has a Kaskad Console account.",
    },
    "invalid_role": {"fr": "Rôle invalide.", "en": "Invalid role."},
    "cannot_edit_member": {"fr": "Ce membre ne peut pas être modifié.", "en": "This member cannot be changed."},
    "read_only": {
        "fr": "Votre rôle (lecteur) permet uniquement la consultation.",
        "en": "Your role (viewer) only allows viewing.",
    },
    "developer_not_found": {"fr": "Développeur introuvable.", "en": "Developer not found."},
    "account_suspended": {
        "fr": "Ce compte développeur est suspendu. Contactez l'administrateur de la plateforme.",
        "en": "This developer account is suspended. Please contact the platform administrator.",
    },
    "version_locked": {
        "fr": "Le canal ne peut plus être modifié : la version est déjà programmée ou en ligne.",
        "en": "The channel can no longer be changed: the version is already scheduled or live.",
    },
    "invalid_api_key": {"fr": "Clé API invalide ou révoquée.", "en": "Invalid or revoked API key."},
    "not_enough_testers": {
        "fr": "Ajoutez au moins {min} testeurs à l'app avant de soumettre ou de publier une version bêta.",
        "en": "Add at least {min} testers to the app before submitting or publishing a beta version.",
    },
    "email_account_required": {
        "fr": "Créez un compte avec une adresse e-mail pour publier un avis.",
        "en": "Create an account with an email address to post a review.",
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


def message(code: str, lang: str, **params) -> str:
    """Message localisé ; `params` complète les variables du message (ex. `{min}`)."""
    text = MESSAGES.get(code, {}).get(lang) or MESSAGES.get(code, {}).get("en") or code
    try:
        return text.format(**params) if params else text
    except (KeyError, IndexError, ValueError):
        return text
