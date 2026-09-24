"""Parcours complet : éditeur (console) → analyse → publication → client (catalogue, téléchargement, mises à jour)."""

import hashlib

from tests.conftest import SAMPLE_FILES
from tests.helpers import API, create_app, published_app_with_version, upload, wait_scans


async def test_health(client):
    r = await client.get(f"{API}/health")
    assert r.json() == {"status": "ok"}


async def test_upload_scan_publish_and_public_catalog(app, client, admin_headers):
    cat, a, _, v = await published_app_with_version(client, app, admin_headers)

    # SHA-256 calculé à l'upload
    content = SAMPLE_FILES["deb"][1]
    assert v["sha256_hash"] == hashlib.sha256(content).hexdigest()
    assert v["file_size"] == len(content)

    # Catalogue public (mêmes formes que l'app mobile)
    cats = (await client.get(f"{API}/categories")).json()
    assert cats == [{"id": cat["id"], "name": "Productivité", "icon": "briefcase-outline", "order": 1}]

    home = (await client.get(f"{API}/home", params={"platform": "linux"})).json()
    assert [x["id"] for x in home["featured"]] == [a["id"]]
    assert home["new"][0]["latest_version_name"] == "1.0.0"
    assert home["popular"][0]["platforms"] == ["linux"]

    listing = (await client.get(f"{API}/apps", params={"category_id": cat["id"], "platform": "linux"})).json()
    assert listing["total"] == 1 and listing["page"] == 1
    assert (await client.get(f"{API}/apps", params={"platform": "android"})).json()["total"] == 0

    search = (await client.get(f"{API}/search", params={"q": "notes rapide"})).json()
    assert search["total"] == 1
    assert (await client.get(f"{API}/search", params={"q": "introuvable"})).json()["total"] == 0

    detail = (await client.get(f"{API}/apps/{a['id']}")).json()
    assert detail["categories"][0]["name"] == "Productivité"
    [pub] = detail["versions"]
    assert pub["security_scan_status"] == "passed"
    assert pub["file_url"].endswith(f"/versions/{v['id']}/download")
    assert set(pub) >= {"version_name", "version_code", "platform", "file_format", "file_size", "sha256_hash", "changelog", "published_at"}


async def test_download_redirect_counts_and_supports_range(app, client, admin_headers):
    _, a, _, v = await published_app_with_version(client, app, admin_headers)
    r = await client.get(f"{API}/versions/{v['id']}/download")
    assert r.status_code == 302
    signed = r.headers["location"].removeprefix("http://test")

    content = SAMPLE_FILES["deb"][1]
    full = await client.get(signed)
    assert full.status_code == 200 and full.content == content
    assert "Kaskad_Notes-1.0.0-linux.deb" in full.headers["content-disposition"]

    # Reprise de téléchargement (en-tête Range)
    part = await client.get(signed, headers={"Range": "bytes=100-"})
    assert part.status_code == 206 and part.content == content[100:]

    # URL signée altérée → refusée
    assert (await client.get(signed.replace("sig=", "sig=0"))).status_code == 403

    detail = (await client.get(f"{API}/apps/{a['id']}")).json()
    assert detail["downloads_count"] == 1

    # Reprise via l'API (Range non nul) : redirection sans nouveau comptage
    resumed = await client.get(f"{API}/versions/{v['id']}/download", headers={"Range": "bytes=500-"})
    assert resumed.status_code == 302
    assert (await client.get(f"{API}/apps/{a['id']}")).json()["downloads_count"] == 1


async def test_unpublished_content_is_hidden(app, client, admin_headers):
    a = await create_app(client, admin_headers)
    v = (await upload(client, admin_headers, a["id"])).json()
    await wait_scans(app)
    assert (await client.get(f"{API}/apps/{a['id']}")).status_code == 404  # app en brouillon
    await client.post(f"{API}/admin/apps/{a['id']}/status", json={"status": "published"}, headers=admin_headers)
    assert (await client.get(f"{API}/apps/{a['id']}")).json()["versions"] == []  # version non publiée
    assert (await client.get(f"{API}/versions/{v['id']}/download")).status_code == 404


async def test_update_check(app, client, admin_headers):
    _, a, _, v1 = await published_app_with_version(client, app, admin_headers)
    body = {"installed": [{"app_id": a["id"], "version_id": v1["id"], "version_code": 100, "platform": "linux"}]}
    assert (await client.post(f"{API}/updates/check", json=body)).json() == []

    v2 = (await upload(client, admin_headers, a["id"], code=110, name="1.1.0")).json()
    await wait_scans(app)
    await client.post(f"{API}/admin/versions/{v2['id']}/publish", headers=admin_headers)
    [res] = (await client.post(f"{API}/updates/check", json=body)).json()
    assert res["app_id"] == a["id"] and res["latest_version"]["version_name"] == "1.1.0"

    # Autre plateforme : pas de mise à jour proposée
    body["installed"][0]["platform"] = "windows"
    assert (await client.post(f"{API}/updates/check", json=body)).json() == []


async def test_publish_requires_passed_scan_and_archive(app, client, admin_headers):
    a = await create_app(client, admin_headers)
    # Contenu qui ne correspond pas au format annoncé → analyse rejetée
    bad = (await upload(client, admin_headers, a["id"], fmt="deb", content=b"not a debian package")).json()
    await wait_scans(app)
    version = (await client.get(f"{API}/admin/versions/{bad['id']}", headers=admin_headers)).json()
    assert version["security_scan_status"] == "failed"
    assert any("does not match" in e for e in version["scan_report"]["errors"])
    r = await client.post(
        f"{API}/admin/versions/{bad['id']}/publish",
        headers=admin_headers,
        params={},
    )
    assert r.status_code == 409 and r.json()["code"] == "scan_not_passed"

    good = (await upload(client, admin_headers, a["id"], code=101, name="1.0.1")).json()
    await wait_scans(app)
    await client.post(f"{API}/admin/versions/{good['id']}/publish", headers=admin_headers)
    archived = (await client.post(f"{API}/admin/versions/{good['id']}/archive", headers=admin_headers)).json()
    assert archived["status"] == "archived"
    # Jamais de suppression physique : la version reste listée côté console
    versions = (await client.get(f"{API}/admin/apps/{a['id']}/versions", headers=admin_headers)).json()
    assert {x["id"] for x in versions} == {bad["id"], good["id"]}


async def test_upload_validation(app, client, admin_headers):
    a = await create_app(client, admin_headers)
    r = await upload(client, admin_headers, a["id"], fmt="exe", platform="linux")
    assert r.status_code == 422 and r.json()["code"] == "invalid_format"
    r = await upload(client, admin_headers, a["id"], fmt="deb", filename="wrong.exe")
    assert r.status_code == 422
    r = await upload(client, admin_headers, a["id"], fmt="deb", content=b"")
    assert r.status_code == 422 and r.json()["code"] == "empty_file"
    assert (await upload(client, admin_headers, a["id"])).status_code == 201
    r = await upload(client, admin_headers, a["id"])  # même version / même format
    assert r.status_code == 409 and r.json()["code"] == "version_exists"


async def test_unsigned_exe_passes_with_warning(app, client, admin_headers):
    a = await create_app(client, admin_headers)
    v = (await upload(client, admin_headers, a["id"], fmt="exe", platform="windows")).json()
    await wait_scans(app)
    v = (await client.get(f"{API}/admin/versions/{v['id']}", headers=admin_headers)).json()
    assert v["security_scan_status"] == "passed"
    assert any("not signed" in w or "not supported" in w for w in v["scan_report"]["warnings"])


async def test_stats_and_csv_export(app, client, admin_headers):
    _, a, _, v = await published_app_with_version(client, app, admin_headers)
    for _ in range(3):
        await client.get(f"{API}/versions/{v['id']}/download", params={"platform": "linux"})
    overview = (await client.get(f"{API}/admin/stats/overview", headers=admin_headers)).json()
    assert overview["total_downloads"] == 3 and overview["apps"]["published"] == 1
    assert overview["recent_publications"][0]["app_name"] == "Kaskad Notes"
    series = (await client.get(f"{API}/admin/stats/downloads", headers=admin_headers)).json()
    assert sum(p["count"] for p in series) == 3
    top = (await client.get(f"{API}/admin/stats/top-apps", headers=admin_headers)).json()
    assert top == [{"app_id": a["id"], "name": "Kaskad Notes", "downloads": 3}]
    by_platform = (await client.get(f"{API}/admin/stats/breakdown", params={"by": "platform"}, headers=admin_headers)).json()
    assert by_platform == [{"key": "linux", "count": 3}]
    csv = await client.get(f"{API}/admin/stats/export.csv", headers=admin_headers)
    lines = csv.text.strip().splitlines()
    assert lines[0].startswith("timestamp,app_id,app_name") and len(lines) == 4


async def test_moderation_queue_and_activity_log(app, client, admin_headers):
    a = await create_app(client, admin_headers)
    v = (await upload(client, admin_headers, a["id"])).json()
    await wait_scans(app)
    queue = (await client.get(f"{API}/admin/moderation/queue", headers=admin_headers)).json()
    assert [q["id"] for q in queue] == [v["id"]] and queue[0]["app_name"] == "Kaskad Notes"
    log = (await client.get(f"{API}/admin/activity", headers=admin_headers)).json()
    actions = [e["action"] for e in log["items"]]
    assert {"app.created", "version.uploaded", "version.scan_passed"} <= set(actions)


async def test_media_upload_and_preview(app, client, admin_headers):
    a = await create_app(client, admin_headers)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    r = await client.post(f"{API}/admin/apps/{a['id']}/icon", files={"file": ("icon.png", png, "image/png")}, headers=admin_headers)
    icon_url = r.json()["icon_url"]
    assert (await client.get(icon_url.removeprefix("http://test"))).content == png
    r = await client.post(
        f"{API}/admin/apps/{a['id']}/screenshots",
        files=[("files", ("1.png", png, "image/png")), ("files", ("2.png", png, "image/png"))],
        headers=admin_headers,
    )
    shots = r.json()["screenshots"]
    assert len(shots) == 2
    r = await client.put(f"{API}/admin/apps/{a['id']}/screenshots", json={"urls": [shots[1]]}, headers=admin_headers)
    assert r.json()["screenshots"] == [shots[1]]
    bad = await client.post(f"{API}/admin/apps/{a['id']}/icon", files={"file": ("x.png", b"nope", "image/png")}, headers=admin_headers)
    assert bad.status_code == 422
    preview = (await client.get(f"{API}/admin/apps/{a['id']}/preview", headers=admin_headers)).json()
    assert preview["screenshots"] == [shots[1]] and preview["status"] == "draft"
