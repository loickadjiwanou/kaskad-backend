"""Statistiques enrichies : vues de fiche, conversion, pays, versions réellement installées."""

from app.core.config import get_settings
from tests.helpers import API, published_app_with_version, signup, upload, wait_scans

BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/126"}


async def test_views_conversion_countries_installations(app, client, admin_headers, monkeypatch):
    monkeypatch.setenv("GEOIP_HEADER", "CF-IPCountry")
    get_settings.cache_clear()
    _, a, _, v = await published_app_with_version(client, app, admin_headers)
    aid = a["id"]

    # Vues : une par visiteur et par app toutes les 30 minutes
    for device, country in (("device-aaaa-0001", "FR"), ("device-aaaa-0001", "FR"), ("device-bbbb-0002", "CI")):
        r = await client.post(f"{API}/apps/{aid}/view", json={"platform": "linux", "device_id": device}, headers={"CF-IPCountry": country})
        assert r.status_code == 204
    assert (await client.post(f"{API}/apps/000000000000000000000000/view", json={})).status_code == 404
    # Page web publique : comptée pour un navigateur, pas pour un robot ni un aperçu de lien
    await client.get(f"/a/{aid}", headers={**BROWSER, "CF-IPCountry": "BE"})
    await client.get(f"/a/{aid}", headers={"User-Agent": "Slackbot-LinkExpanding 1.0", "CF-IPCountry": "US"})

    # Téléchargements avec pays
    for country in ("FR", "FR", "SN"):
        r = await client.get(f"{API}/versions/{v['id']}/download", headers={"CF-IPCountry": country}, follow_redirects=False)
        assert r.status_code in (302, 307)

    funnel = (await client.get(f"{API}/admin/stats/funnel", headers=admin_headers)).json()
    assert funnel == {"views": 3, "visitors": 3, "downloads": 3, "conversion": 1.0}
    by_country = (await client.get(f"{API}/admin/stats/breakdown", params={"by": "country"}, headers=admin_headers)).json()
    assert by_country[0] == {"key": "FR", "count": 2}
    views_country = (
        await client.get(f"{API}/admin/stats/breakdown", params={"by": "country", "metric": "views"}, headers=admin_headers)
    ).json()
    assert {r["key"] for r in views_country} == {"FR", "CI", "BE"}
    sources = (await client.get(f"{API}/admin/stats/breakdown", params={"by": "source", "metric": "views"}, headers=admin_headers)).json()
    assert {r["key"]: r["count"] for r in sources} == {"app": 2, "web": 1}
    series = (await client.get(f"{API}/admin/stats/downloads", params={"metric": "views", "app_id": aid}, headers=admin_headers)).json()
    assert sum(p["count"] for p in series) == 3
    overview = (await client.get(f"{API}/admin/stats/overview", headers=admin_headers)).json()
    assert overview["views_last_30_days"] == 3 and overview["conversion_last_30_days"] == 1.0
    top = (await client.get(f"{API}/admin/stats/top-apps", headers=admin_headers)).json()
    assert top[0]["views"] == 3 and top[0]["conversion"] == 1.0
    csv_text = (await client.get(f"{API}/admin/stats/export.csv", headers=admin_headers)).text
    assert csv_text.splitlines()[0].endswith(",country") and ",SN" in csv_text

    # Versions réellement installées (vérification des mises à jour, par appareil)
    v2 = (await upload(client, admin_headers, aid, code=200, name="2.0.0")).json()
    await wait_scans(app)
    await client.post(f"{API}/admin/versions/{v2['id']}/publish", headers=admin_headers)
    installed = lambda code, vid: {"app_id": aid, "version_id": vid, "version_code": code, "platform": "linux"}  # noqa: E731
    for device, item in (
        ("dev-one-0001", installed(100, v["id"])),
        ("dev-two-0002", installed(200, v2["id"])),
        ("dev-two-0002", installed(200, v2["id"])),
    ):
        r = await client.post(f"{API}/updates/check", json={"installed": [item], "device_id": device})
        assert r.status_code == 200
    await client.post(f"{API}/updates/check", json={"installed": [installed(100, v["id"])]})  # sans appareil : non compté
    base = (await client.get(f"{API}/admin/stats/installed", params={"app_id": aid}, headers=admin_headers)).json()
    assert base["total"] == 2 and base["on_latest"] == 1
    assert [(i["version_name"], i["count"], i["latest"]) for i in base["items"]] == [("2.0.0", 1, True), ("1.0.0", 1, False)]
    per_app = (await client.get(f"{API}/admin/stats/installed", headers=admin_headers)).json()
    assert per_app["total"] == 2 and per_app["items"][0]["name"] == a["name"]

    # Un autre compte développeur ne voit pas ces statistiques
    other = await signup(client, app, "omar@example.com", "Orbit Labs")
    assert (await client.get(f"{API}/admin/stats/installed", params={"app_id": aid}, headers=other)).status_code == 404
    assert (await client.get(f"{API}/admin/stats/funnel", headers=other)).json()["views"] == 0
