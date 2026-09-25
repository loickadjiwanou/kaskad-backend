"""Pipeline d'analyse de sécurité des versions uploadées.

Chaque version passe par : ClamAV et/ou VirusTotal (si configurés) + analyse statique.
Le fichier n'est jamais exécuté : il est seulement lu.
Une version n'est publiable que si son statut d'analyse est "passed".
"""

import asyncio
import logging

from anyio import to_thread
from bson import ObjectId
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import Settings
from app.models.common import now
from app.services.activity import log_system
from app.services.scanners import clamav, static, virustotal
from app.services.storage import Storage

log = logging.getLogger("kaskad.scan")


async def _run_static(db: AsyncDatabase, version: dict, path, settings: Settings, report: dict) -> None:
    fmt = version["file_format"]
    ok, label = await to_thread.run_sync(static.check_magic, path, fmt)
    report["static"] = {"format_check": {"ok": ok, "expected": label}}
    if not ok:
        report["errors"].append(f"File content does not match the {fmt.upper()} format ({label}).")
        return

    if fmt == "apk":
        try:
            info = await to_thread.run_sync(static.inspect_apk, path)
        except Exception as e:
            report["errors"].append(f"APK manifest could not be parsed: {e}")
            return
        report["static"]["apk"] = info
        await _check_apk_consistency(db, version, info, report)

    elif fmt == "exe" or fmt == "msi":
        try:
            auth = await to_thread.run_sync(static.inspect_authenticode, path)
        except Exception as e:
            auth = {"signed": False, "supported": False, "detail": str(e)}
        report["static"]["authenticode"] = auth
        strict = settings.require_authenticode and fmt == "exe"
        verdict = auth.get("verdict")
        if auth.get("signed") and verdict == "tampered":
            report["errors"].append(
                f"Authenticode signature does not match the file content (file modified after signing): {auth.get('detail')}"
            )
        elif auth.get("signed") and verdict == "untrusted":
            (report["errors"] if strict else report["warnings"]).append(f"Authenticode certificate is not trusted: {auth.get('detail')}")
        elif auth.get("signed") and verdict == "unverifiable":
            (report["errors"] if strict else report["warnings"]).append(
                f"Authenticode signature present (issuer: {auth.get('issuer')}) but could not be verified: {auth.get('detail')}"
            )
        elif not auth.get("signed"):
            msg = "Executable is not signed (Authenticode): Windows SmartScreen will warn users."
            if not auth.get("supported"):
                msg = f"Authenticode verification not supported for this file ({fmt.upper()})."
            (report["errors"] if settings.require_authenticode and fmt == "exe" else report["warnings"]).append(msg)


async def _check_apk_consistency(db: AsyncDatabase, version: dict, info: dict, report: dict) -> None:
    app = await db.apps.find_one({"_id": version["app_id"]}) or {}
    expected_package = app.get("android_package")
    if expected_package and info.get("package") != expected_package:
        report["errors"].append(f"APK package '{info.get('package')}' does not match the app's package '{expected_package}'.")
    if not info.get("signed"):
        report["errors"].append("APK is not signed.")
    if info.get("version_code") and info["version_code"] != version["version_code"]:
        report["warnings"].append(
            f"Declared version code {version['version_code']} differs from the APK manifest ({info['version_code']})."
        )

    # Le certificat de signature doit être identique à celui des APK déjà validés (sinon mise à jour impossible)
    previous = (
        await db.versions.find(
            {
                "app_id": version["app_id"],
                "file_format": "apk",
                "security_scan_status": "passed",
                "_id": {"$ne": version["_id"]},
                "apk_info.cert_sha256": {"$exists": True, "$ne": []},
            }
        )
        .sort("version_code", -1)
        .to_list(1)
    )
    if previous:
        prev_certs = set(previous[0]["apk_info"]["cert_sha256"])
        if not prev_certs.intersection(info.get("cert_sha256", [])):
            report["errors"].append(f"APK signing certificate differs from version {previous[0]['version_name']}: users could not update.")
        else:
            report["static"]["apk"]["same_certificate_as"] = previous[0]["version_name"]


async def scan_version(db: AsyncDatabase, storage: Storage, settings: Settings, version_id: ObjectId) -> str:
    version = await db.versions.find_one({"_id": version_id})
    if not version or version.get("upload_status") != "stored":
        return "skipped"
    await db.versions.update_one({"_id": version_id}, {"$set": {"security_scan_status": "scanning", "updated_at": now()}})

    report: dict = {"engines": {}, "errors": [], "warnings": [], "started_at": now()}
    antivirus_ok = False
    try:
        async with storage.local_copy(version["storage_key"]) as path:
            # --- Antivirus
            if settings.clamav_host:
                try:
                    res = await clamav.scan_file(path, settings.clamav_host, settings.clamav_port)
                except Exception as e:
                    res = {"engine": "clamav", "status": "error", "raw": str(e)}
                report["engines"]["clamav"] = res
            if settings.virustotal_api_key:
                try:
                    res = await virustotal.scan_file(path, version["sha256_hash"], settings.virustotal_api_key)
                except Exception as e:
                    res = {"engine": "virustotal", "status": "error", "raw": str(e)}
                report["engines"]["virustotal"] = res

            for name, res in report["engines"].items():
                if res["status"] == "clean":
                    antivirus_ok = True
                elif res["status"] == "infected":
                    report["errors"].append(f"{name}: threat detected ({res.get('threat') or res.get('stats')}).")
                elif res["status"] == "suspicious":
                    report["errors"].append(f"{name}: file flagged as suspicious ({res.get('stats')}).")
                else:
                    report["warnings"].append(f"{name}: scan error ({res.get('raw')}).")

            if not report["engines"]:
                report["warnings"].append("No antivirus engine configured (ClamAV / VirusTotal).")
            if settings.scan_require_antivirus and not antivirus_ok:
                report["errors"].append("An antivirus scan is required but none completed successfully.")

            # --- Analyse statique
            await _run_static(db, version, path, settings, report)
    except FileNotFoundError:
        report["errors"].append("Stored file not found.")
    except Exception as e:  # erreur inattendue : la version reste bloquée (rejetée), ré-analyse possible
        log.exception("scan failed for %s", version_id)
        report["errors"].append(f"Unexpected scan error: {e}")

    status = "failed" if report["errors"] else "passed"
    report["finished_at"] = now()
    update = {"security_scan_status": status, "scan_report": report, "updated_at": now()}
    if apk := report.get("static", {}).get("apk"):
        update["apk_info"] = apk
    await db.versions.update_one({"_id": version_id}, {"$set": update})

    # Le premier APK validé fixe le package Android de l'app
    if status == "passed" and apk and apk.get("package"):
        await db.apps.update_one(
            {"_id": version["app_id"], "android_package": {"$in": [None, ""]}}, {"$set": {"android_package": apk["package"]}}
        )
    app = await db.apps.find_one({"_id": version["app_id"]}, {"name": 1, "account_id": 1}) or {}
    await log_system(
        db,
        f"version.scan_{status}",
        "version",
        version_id,
        {"errors": report["errors"][:5], "app": app.get("name"), "version": version.get("version_name")},
        account_id=app.get("account_id"),
    )
    return status


class ScanQueue:
    """File d'analyse en tâche de fond (un seul worker : les analyses sont coûteuses)."""

    def __init__(self, db: AsyncDatabase, storage: Storage, settings: Settings):
        self.db, self.storage, self.settings = db, storage, settings
        self.queue: asyncio.Queue[ObjectId] = asyncio.Queue()
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        # Reprise des analyses interrompues par un redémarrage
        pending = await self.db.versions.find(
            {"upload_status": "stored", "security_scan_status": {"$in": ["pending", "scanning"]}}, {"_id": 1}
        ).to_list(None)
        for v in pending:
            self.queue.put_nowait(v["_id"])
        self.task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    def enqueue(self, version_id: ObjectId) -> None:
        self.queue.put_nowait(version_id)

    async def join(self) -> None:
        await self.queue.join()

    async def _worker(self) -> None:
        while True:
            version_id = await self.queue.get()
            try:
                await scan_version(self.db, self.storage, self.settings, version_id)
            except Exception:
                log.exception("scan worker error")
            finally:
                self.queue.task_done()
