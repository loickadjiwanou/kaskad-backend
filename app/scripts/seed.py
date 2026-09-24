"""Données de démonstration (développement uniquement) : catégories, apps et versions publiées.

    python -m app.scripts.seed            # ajoute les données si la base est vide
    python -m app.scripts.seed --reset    # vide le catalogue puis le recrée

Les fichiers générés sont de petits binaires factices dont l'en-tête correspond au format annoncé.
Leur analyse de sécurité est marquée "passed" (seeded) : ne jamais utiliser en production.
"""

import argparse
import asyncio
import hashlib
import io
import struct
import zlib
from datetime import timedelta

from app.core.config import get_settings
from app.db import close_client, ensure_indexes, get_db
from app.models.common import CONTENT_TYPES, FORMATS_BY_PLATFORM, now
from app.routers.admin_versions import _file_name
from app.services.catalog import refresh_app_catalog_fields
from app.services.storage import create_storage

CATEGORIES = [
    ("Productivité", "briefcase-outline"),
    ("Jeux", "gamepad-variant-outline"),
    ("Utilitaires", "tools"),
    ("Multimédia", "play-circle-outline"),
    ("Éducation", "school-outline"),
    ("Développement", "code-braces"),
]

# (nom, description courte, description longue, catégories, plateformes, vedette, historique [(version, code, jours)])
APPS = [
    (
        "Kaskad Notes",
        "Vos notes synchronisées, simples et rapides.",
        "Bloc-notes minimaliste : Markdown, dossiers, recherche plein texte, mode hors ligne.",
        [0],
        ["android", "windows", "macos", "linux"],
        True,
        [("2.4.0", 240, 3), ("2.3.1", 231, 40)],
    ),
    (
        "Flux Tasks",
        "Gestionnaire de tâches et de projets en kanban.",
        "Tableaux kanban, listes, calendrier et rappels intelligents.",
        [0],
        ["android", "windows", "macos"],
        True,
        [("1.8.2", 182, 9), ("1.8.0", 180, 52)],
    ),
    (
        "Pixel Drift",
        "Course arcade en pixel art, 60 circuits.",
        "Enchaînez les dérapages sur 60 circuits rétro. Manette supportée.",
        [1],
        ["android", "windows"],
        True,
        [("3.1.0", 310, 1), ("3.0.4", 304, 30)],
    ),
    (
        "Nebula Run",
        "Runner spatial infini au rythme de la musique.",
        "Esquivez les astéroïdes au rythme d'une bande-son synthwave.",
        [1],
        ["android", "windows", "linux"],
        False,
        [("1.2.0", 120, 14)],
    ),
    (
        "Clearspace",
        "Libérez de l'espace en analysant vos fichiers.",
        "Visualisez ce qui occupe votre stockage et supprimez les doublons.",
        [2],
        ["android", "windows"],
        False,
        [("1.0.3", 103, 6)],
    ),
    (
        "Vault Pass",
        "Coffre-fort de mots de passe chiffré de bout en bout.",
        "Coffre chiffré AES-256, générateur de mots de passe, audit de sécurité.",
        [2, 0],
        ["android", "windows", "macos", "linux"],
        True,
        [("4.0.0", 400, 2), ("3.9.5", 395, 45)],
    ),
    (
        "Wave Player",
        "Lecteur audio et vidéo léger, tous formats.",
        "Lisez tous vos fichiers audio et vidéo, égaliseur 10 bandes.",
        [3],
        ["android", "windows", "macos", "linux"],
        False,
        [("5.2.1", 521, 20)],
    ),
    (
        "Snapframe",
        "Captures d'écran annotées en un raccourci.",
        "Capturez, annotez et partagez en quelques secondes.",
        [3, 0],
        ["windows", "macos", "linux"],
        False,
        [("0.9.0", 90, 4)],
    ),
    (
        "Lingo Cards",
        "Apprenez le vocabulaire par répétition espacée.",
        "Cartes mémo intelligentes pour 12 langues, avec prononciation audio.",
        [4],
        ["android", "windows", "macos"],
        False,
        [("2.0.0", 200, 12)],
    ),
    (
        "Terminal Kit",
        "Terminal moderne avec onglets et SSH intégré.",
        "Terminal accéléré GPU, gestionnaire de connexions SSH et thèmes.",
        [5],
        ["windows", "macos", "linux"],
        False,
        [("1.6.0", 160, 5)],
    ),
]

HEADERS = {
    "apk": b"PK\x03\x04",
    "exe": b"MZ",
    "msi": bytes.fromhex("D0CF11E0A1B11AE1"),
    "pkg": b"xar!",
    "appimage": b"\x7fELF",
    "deb": b"!<arch>\n",
    "rpm": bytes.fromhex("EDABEEDB"),
}


PALETTE = [(47, 107, 255), (34, 211, 238), (139, 60, 232), (30, 79, 214), (91, 156, 255)]


def png(width: int, height: int, top: tuple, bottom: tuple) -> bytes:
    """PNG en dégradé vertical (capture d'écran de démonstration), sans dépendance externe."""
    rows = bytearray()
    for y in range(height):
        t = y / max(height - 1, 1)
        color = bytes(round(a + (b - a) * t) for a, b in zip(top, bottom, strict=True))
        rows += b"\x00" + color * width

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b"")


def fake_binary(fmt: str, seed: str, size: int = 256 * 1024) -> bytes:
    body = hashlib.sha256(seed.encode()).digest() * (size // 32)
    if fmt == "dmg":
        return body + b"koly" + b"\x00" * 508
    return HEADERS[fmt] + body


async def seed(reset: bool) -> None:
    settings = get_settings()
    if settings.is_production:
        raise SystemExit("Refusing to seed demo data in production.")
    db = get_db()
    await ensure_indexes(db)
    storage = create_storage(settings)
    await storage.init()

    if reset:
        for col in ("apps", "versions", "categories", "download_stats"):
            await db[col].delete_many({})
    elif await db.apps.estimated_document_count():
        print("Catalog not empty: nothing to do (use --reset to recreate it).")
        return

    cat_ids = []
    for order, (name, icon) in enumerate(CATEGORIES, start=1):
        cat_ids.append((await db.categories.insert_one({"name": name, "icon": icon, "order": order, "created_at": now()})).inserted_id)

    for i, (name, short, long, cats, platforms, featured, history) in enumerate(APPS):
        app_id = (
            await db.apps.insert_one(
                {
                    "name": name,
                    "short_description": short,
                    "long_description": long,
                    "category_ids": [cat_ids[c] for c in cats],
                    "target_platforms": platforms,
                    "featured": featured,
                    "status": "published",
                    "icon_key": None,
                    "screenshot_keys": [],
                    "available_platforms": [],
                    "downloads_count": (10 - i) * 1234,
                    "created_at": now() - timedelta(days=history[-1][2] + 1),
                    "updated_at": now(),
                }
            )
        ).inserted_id
        shots = []
        for n in range(3):
            key = f"media/apps/{app_id}/screenshot-seed-{n}.png"
            await storage.save(key, io.BytesIO(png(270, 480, PALETTE[(i + n) % 5], PALETTE[(i + n + 2) % 5])), "image/png")
            shots.append(key)
        await db.apps.update_one({"_id": app_id}, {"$set": {"screenshot_keys": shots}})
        for version_name, code, days in history:
            for platform in platforms:
                for fmt in FORMATS_BY_PLATFORM[platform][:2]:
                    data = fake_binary(fmt, f"{name}-{version_name}-{fmt}")
                    file_name = _file_name(name, version_name, platform, fmt)
                    doc = {
                        "app_id": app_id,
                        "version_name": version_name,
                        "version_code": code,
                        "platform": platform,
                        "file_format": fmt,
                        "changelog": f"• Améliorations et corrections ({version_name})",
                        "file_name": file_name,
                        "status": "published",
                        "security_scan_status": "passed",
                        "scan_report": {"seeded": True, "errors": [], "warnings": ["Demo data: not scanned."]},
                        "upload_status": "stored",
                        "sha256_hash": hashlib.sha256(data).hexdigest(),
                        "file_size": len(data),
                        "downloads_count": 0,
                        "published_at": now() - timedelta(days=days),
                        "created_at": now() - timedelta(days=days + 1),
                        "updated_at": now(),
                    }
                    doc["_id"] = (await db.versions.insert_one(doc)).inserted_id
                    key = f"binaries/{app_id}/{doc['_id']}/{file_name}"
                    await storage.save(key, io.BytesIO(data), CONTENT_TYPES[fmt])
                    await db.versions.update_one({"_id": doc["_id"]}, {"$set": {"storage_key": key}})
        await refresh_app_catalog_fields(db, app_id)
    print(f"Seeded {len(CATEGORIES)} categories and {len(APPS)} apps.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="delete the current catalog first")
    args = parser.parse_args()

    async def run():
        try:
            await seed(args.reset)
        finally:
            await close_client()

    asyncio.run(run())


if __name__ == "__main__":
    main()
