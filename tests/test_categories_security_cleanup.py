import asyncio
from datetime import timedelta

from app.core.config import get_settings
from app.models.common import now
from app.services.cleanup import run_cleanup
from app.services.scanning import _check_apk_consistency
from tests.helpers import API, create_app, create_category, upload, wait_scans


async def test_category_order_reassign_and_delete(client, admin_headers):
    c1 = await create_category(client, admin_headers, "Jeux")
    c2 = await create_category(client, admin_headers, "Utilitaires")
    a = await create_app(client, admin_headers, category_ids=[c1["id"]])

    ordered = (await client.put(f"{API}/admin/categories/order", json={"ids": [c2["id"], c1["id"]]}, headers=admin_headers)).json()
    assert [c["id"] for c in ordered] == [c2["id"], c1["id"]] and ordered[1]["apps_count"] == 1

    r = await client.delete(f"{API}/admin/categories/{c1['id']}", headers=admin_headers)
    assert r.status_code == 409 and r.json()["code"] == "category_in_use"
    r = await client.delete(f"{API}/admin/categories/{c1['id']}", params={"reassign_to": c2["id"]}, headers=admin_headers)
    assert r.status_code == 204
    app_doc = (await client.get(f"{API}/admin/apps/{a['id']}", headers=admin_headers)).json()
    assert app_doc["category_ids"] == [c2["id"]]


async def _fake_clamd(reader, writer):
    """Faux serveur clamd : détecte la signature de test EICAR."""
    data = b""
    while True:
        size = int.from_bytes(await reader.readexactly(4), "big") if data.startswith(b"zINSTREAM\0") else None
        if size is None:
            data += await reader.readexactly(10)
            continue
        if size == 0:
            break
        data += await reader.readexactly(size)
    verdict = b"stream: Eicar-Test-Signature FOUND\0" if b"EICAR" in data else b"stream: OK\0"
    writer.write(verdict)
    await writer.drain()
    writer.close()


async def test_clamav_detection_blocks_version(app, client, admin_headers):
    server = await asyncio.start_server(_fake_clamd, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    settings = app.state.scan_queue.settings
    settings.clamav_host, settings.clamav_port = "127.0.0.1", port
    try:
        a = await create_app(client, admin_headers)
        infected = (await upload(client, admin_headers, a["id"], content=b"!<arch>\nX5O!P%@AP EICAR-STANDARD-ANTIVIRUS-TEST")).json()
        clean = (await upload(client, admin_headers, a["id"], code=101)).json()
        await wait_scans(app)
        infected = (await client.get(f"{API}/admin/versions/{infected['id']}", headers=admin_headers)).json()
        clean = (await client.get(f"{API}/admin/versions/{clean['id']}", headers=admin_headers)).json()
        assert infected["security_scan_status"] == "failed"
        assert infected["scan_report"]["engines"]["clamav"]["threat"] == "Eicar-Test-Signature"
        assert clean["security_scan_status"] == "passed" and clean["scan_report"]["engines"]["clamav"]["status"] == "clean"
    finally:
        settings.clamav_host = None
        server.close()


async def test_require_antivirus(app, client, admin_headers):
    settings = app.state.scan_queue.settings
    settings.scan_require_antivirus = True
    try:
        a = await create_app(client, admin_headers)
        v = (await upload(client, admin_headers, a["id"])).json()
        await wait_scans(app)
        v = (await client.get(f"{API}/admin/versions/{v['id']}", headers=admin_headers)).json()
        assert v["security_scan_status"] == "failed"
    finally:
        settings.scan_require_antivirus = False


async def test_apk_certificate_must_match_previous_version(app):
    db = app.state.db
    app_id = (await db.apps.insert_one({"name": "A", "android_package": "com.kaskad.a"})).inserted_id
    await db.versions.insert_one(
        {
            "app_id": app_id,
            "file_format": "apk",
            "security_scan_status": "passed",
            "version_code": 1,
            "version_name": "1.0",
            "apk_info": {"cert_sha256": ["aaa"]},
        }
    )
    new_version = {"_id": None, "app_id": app_id, "version_code": 2}

    report = {"errors": [], "warnings": [], "static": {"apk": {}}}
    await _check_apk_consistency(
        db, new_version, {"package": "com.kaskad.a", "signed": True, "cert_sha256": ["bbb"], "version_code": 2}, report
    )
    assert any("certificate differs" in e for e in report["errors"])

    report = {"errors": [], "warnings": [], "static": {"apk": {}}}
    await _check_apk_consistency(
        db, new_version, {"package": "com.other", "signed": True, "cert_sha256": ["aaa"], "version_code": 3}, report
    )
    assert any("does not match the app's package" in e for e in report["errors"])
    assert any("differs from the APK manifest" in w for w in report["warnings"])

    report = {"errors": [], "warnings": [], "static": {"apk": {}}}
    await _check_apk_consistency(
        db, new_version, {"package": "com.kaskad.a", "signed": True, "cert_sha256": ["aaa"], "version_code": 2}, report
    )
    assert report["errors"] == [] and report["static"]["apk"]["same_certificate_as"] == "1.0"


async def test_cleanup_abandoned_uploads_and_orphans(app, client, admin_headers):
    db, storage, settings = app.state.db, app.state.storage, get_settings()
    a = await create_app(client, admin_headers)
    kept = (await upload(client, admin_headers, a["id"])).json()
    await wait_scans(app)

    import io
    import os

    old = now() - timedelta(hours=settings.orphan_upload_max_age_hours + 1)
    await storage.save("binaries/orphan/file.deb", io.BytesIO(b"x"), "application/octet-stream")
    os.utime(storage.path("binaries/orphan/file.deb"), (old.timestamp(), old.timestamp()))
    stuck_id = (await db.versions.insert_one({"app_id": a["id"], "upload_status": "uploading", "created_at": old})).inserted_id

    stats = await run_cleanup(db, storage, settings)
    assert stats["orphan_files"] == 1 and stats["abandoned_uploads"] == 1
    assert (await db.versions.find_one({"_id": stuck_id}))["upload_status"] == "abandoned"
    kept_doc = await db.versions.find_one({"sha256_hash": kept["sha256_hash"]})
    assert await storage.exists(kept_doc["storage_key"])  # les fichiers référencés sont conservés
