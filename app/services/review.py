"""Circuit de validation (type Google Play Console).

Les éditeurs de contenu soumettent ; seuls les admins complets publient :
- versions : soumission après validation de l'analyse de sécurité, puis publication ou refus motivé ;
- statut d'une app (publier / dépublier / repasser en brouillon) : demande, puis approbation ou refus ;
- fiche d'une app visible : les modifications sont enregistrées dans un brouillon de fiche
  (`listing_draft`), invisible du catalogue jusqu'à sa publication.
"""

from app.models.common import now, sid
from app.services.catalog import media_url

# Champs de la fiche soumis à validation quand l'app est visible
LISTING_FIELDS = (
    "name",
    "short_description",
    "long_description",
    "category_ids",
    "target_platforms",
    "featured",
    "android_package",
    "icon_key",
    "screenshot_keys",
    "default_language",
    "translations",
)


def is_full_admin(admin: dict) -> bool:
    return admin.get("role") == "admin"


def submission(admin: dict, note: str = "", **extra) -> dict:
    """Nouvelle demande en attente de validation."""
    return {
        **extra,
        "state": "pending",
        "note": note or "",
        "submitted_by": admin["_id"],
        "submitted_by_name": admin.get("name"),
        "submitted_at": now(),
        "reviewed_by": None,
        "reviewed_by_name": None,
        "reviewed_at": None,
        "reason": None,
    }


def decision(admin: dict, state: str, reason: str | None = None) -> dict:
    """Champs à fusionner dans une demande lors de sa validation ou de son refus."""
    return {"state": state, "reviewed_by": admin["_id"], "reviewed_by_name": admin.get("name"), "reviewed_at": now(), "reason": reason}


def review_out(r: dict | None) -> dict | None:
    if not r:
        return None
    return {**r, "submitted_by": sid(r.get("submitted_by")), "reviewed_by": sid(r.get("reviewed_by"))}


def is_pending(r: dict | None) -> bool:
    return bool(r) and r.get("state") == "pending"


# ---------------------------------------------------------------- fiche


def live_listing(app: dict) -> dict:
    return {f: app.get(f) for f in LISTING_FIELDS} | {"screenshot_keys": list(app.get("screenshot_keys") or [])}


def uses_draft(app: dict) -> bool:
    """Les modifications passent par un brouillon quand l'app est visible (ou qu'un brouillon existe déjà)."""
    return app.get("status") == "published" or bool(app.get("listing_draft"))


def editable_listing(app: dict) -> dict:
    return app.get("listing_draft") or live_listing(app)


def listing_out(draft: dict | None) -> dict | None:
    if not draft:
        return None
    return {
        "name": draft.get("name"),
        "short_description": draft.get("short_description", ""),
        "long_description": draft.get("long_description", ""),
        "category_ids": [sid(c) for c in draft.get("category_ids") or []],
        "target_platforms": draft.get("target_platforms") or [],
        "featured": bool(draft.get("featured")),
        "android_package": draft.get("android_package"),
        "default_language": draft.get("default_language") or "fr",
        "translations": draft.get("translations") or {},
        "icon_url": media_url(draft.get("icon_key")),
        "screenshots": [media_url(k) for k in draft.get("screenshot_keys") or []],
        "updated_at": draft.get("updated_at"),
        "updated_by_name": draft.get("updated_by_name"),
    }


def media_keys(app: dict) -> set[str]:
    """Fichiers média référencés par la fiche en ligne et par son brouillon."""
    keys = set()
    for listing in (app, app.get("listing_draft") or {}):
        if listing.get("icon_key"):
            keys.add(listing["icon_key"])
        keys.update(listing.get("screenshot_keys") or [])
    return keys


async def delete_unreferenced(storage, app_after: dict, candidates) -> None:
    """Supprime du stockage les médias qui ne sont plus utilisés ni en ligne ni dans le brouillon."""
    used = media_keys(app_after)
    for key in set(candidates) - used:
        if key:
            await storage.delete(key)
