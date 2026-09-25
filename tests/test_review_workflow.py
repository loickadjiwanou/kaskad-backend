"""Circuit de validation : les éditeurs soumettent, seuls les admins complets publient."""

from tests.helpers import API, create_app, invite, published_app_with_version, upload, wait_scans

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200


async def editor_headers(client, app, admin_headers, email="editor@example.com"):
    """Développeur invité dans le compte de la plateforme (il soumet, l'administrateur publie)."""
    return await invite(client, app, admin_headers, email, "developer", "Ed")


async def test_version_submission_approval_and_rejection(app, client, admin_headers):
    ed = await editor_headers(client, app, admin_headers)
    a = await create_app(client, ed)
    v = (await upload(client, ed, a["id"])).json()
    await wait_scans(app)

    # L'éditeur ne peut pas publier directement
    r = await client.post(f"{API}/admin/versions/{v['id']}/publish", headers=ed)
    assert r.status_code == 403 and r.json()["code"] == "publish_requires_admin"

    r = await client.post(f"{API}/admin/versions/{v['id']}/submit", json={"note": "Prête"}, headers=ed)
    assert r.status_code == 200 and r.json()["review"]["state"] == "pending"
    assert r.json()["review"]["submitted_by_name"] == "Ed"
    assert (await client.post(f"{API}/admin/versions/{v['id']}/submit", json={}, headers=ed)).json()["code"] == "already_submitted"

    reviews = (await client.get(f"{API}/admin/moderation/reviews", headers=admin_headers)).json()
    assert [x["id"] for x in reviews["versions"]] == [v["id"]] and reviews["versions"][0]["app_name"] == a["name"]
    assert (await client.get(f"{API}/admin/stats/overview", headers=admin_headers)).json()["reviews_pending"] == 1

    # Refus motivé → l'éditeur corrige (ce qui efface le refus) et soumet à nouveau
    assert (await client.post(f"{API}/admin/versions/{v['id']}/reject", json={"reason": "x"}, headers=admin_headers)).status_code == 422
    assert (await client.post(f"{API}/admin/versions/{v['id']}/reject", json={"reason": "Notes vides"}, headers=ed)).status_code == 403
    r = await client.post(f"{API}/admin/versions/{v['id']}/reject", json={"reason": "Notes vides"}, headers=admin_headers)
    assert r.json()["review"]["state"] == "rejected" and r.json()["review"]["reason"] == "Notes vides"
    r = await client.patch(f"{API}/admin/versions/{v['id']}", json={"changelog": "• Corrections"}, headers=ed)
    assert r.json()["review"] is None
    await client.post(f"{API}/admin/versions/{v['id']}/submit", json={}, headers=ed)

    # Validation par l'admin complet = publication
    r = await client.post(f"{API}/admin/versions/{v['id']}/publish", headers=admin_headers)
    assert r.json()["status"] == "published" and r.json()["review"]["state"] == "approved"
    assert (await client.get(f"{API}/admin/moderation/reviews", headers=admin_headers)).json()["versions"] == []

    # Version en ligne : modification et retrait réservés aux admins complets
    assert (await client.patch(f"{API}/admin/versions/{v['id']}", json={"changelog": "y"}, headers=ed)).status_code == 403
    assert (await client.post(f"{API}/admin/versions/{v['id']}/archive", headers=ed)).status_code == 403

    actions = [e["action"] for e in (await client.get(f"{API}/admin/activity", headers=admin_headers)).json()["items"]]
    assert {"version.submitted", "version.rejected", "version.published"} <= set(actions)


async def test_submission_requires_passed_scan_and_can_be_withdrawn(app, client, admin_headers):
    ed = await editor_headers(client, app, admin_headers)
    a = await create_app(client, ed)
    bad = (await upload(client, ed, a["id"], code=1, content=b"not a deb" * 50)).json()
    good = (await upload(client, ed, a["id"], code=2, name="1.0.1")).json()
    await wait_scans(app)
    assert (await client.post(f"{API}/admin/versions/{bad['id']}/submit", json={}, headers=ed)).json()["code"] == "not_submittable"

    await client.post(f"{API}/admin/versions/{good['id']}/submit", json={}, headers=ed)
    other = await editor_headers(client, app, admin_headers, "other@example.com")
    assert (await client.delete(f"{API}/admin/versions/{good['id']}/submission", headers=other)).status_code == 403
    r = await client.delete(f"{API}/admin/versions/{good['id']}/submission", headers=ed)
    assert r.status_code == 200 and r.json()["review"] is None


async def test_app_status_requests(app, client, admin_headers):
    ed = await editor_headers(client, app, admin_headers)
    a = await create_app(client, ed)
    r = await client.post(f"{API}/admin/apps/{a['id']}/status", json={"status": "published"}, headers=ed)
    assert r.status_code == 403

    assert (await client.post(f"{API}/admin/apps/{a['id']}/status-request", json={"status": "draft"}, headers=ed)).json()[
        "code"
    ] == "status_unchanged"
    r = await client.post(f"{API}/admin/apps/{a['id']}/status-request", json={"status": "published", "note": "Go"}, headers=ed)
    assert r.json()["status_request"]["state"] == "pending" and r.json()["status_request"]["status"] == "published"
    assert len((await client.get(f"{API}/admin/moderation/reviews", headers=admin_headers)).json()["status_requests"]) == 1

    r = await client.post(f"{API}/admin/apps/{a['id']}/status-request/reject", json={"reason": "Fiche incomplète"}, headers=admin_headers)
    assert r.json()["status"] == "draft" and r.json()["status_request"]["state"] == "rejected"

    await client.delete(f"{API}/admin/apps/{a['id']}/status-request", headers=ed)
    await client.post(f"{API}/admin/apps/{a['id']}/status-request", json={"status": "published"}, headers=ed)
    r = await client.post(f"{API}/admin/apps/{a['id']}/status-request/approve", headers=admin_headers)
    assert r.json()["status"] == "published" and r.json()["status_request"] is None
    assert (await client.get(f"{API}/apps/{a['id']}")).status_code == 200


async def test_listing_changes_on_published_app_go_through_review(app, client, admin_headers):
    _, a, _, _ = await published_app_with_version(client, app, admin_headers)
    ed = await editor_headers(client, app, admin_headers)

    # Modification par l'éditeur : brouillon de fiche, catalogue public inchangé
    r = await client.patch(f"{API}/admin/apps/{a['id']}", json={"name": "Kaskad Notes Pro", "short_description": "Nouveau"}, headers=ed)
    body = r.json()
    assert body["name"] == "Kaskad Notes" and body["draft"]["name"] == "Kaskad Notes Pro"
    icon = (await client.post(f"{API}/admin/apps/{a['id']}/icon", files={"file": ("i.png", PNG, "image/png")}, headers=ed)).json()
    assert icon["icon_url"] is None and icon["draft"]["icon_url"]
    assert (await client.get(f"{API}/apps/{a['id']}")).json()["name"] == "Kaskad Notes"
    preview = (await client.get(f"{API}/admin/apps/{a['id']}/preview", params={"draft": "true"}, headers=ed)).json()
    assert preview["name"] == "Kaskad Notes Pro"

    # Soumission, refus, nouvelle modification (efface le refus), soumission puis validation
    await client.post(f"{API}/admin/apps/{a['id']}/listing/submit", json={}, headers=ed)
    assert (await client.post(f"{API}/admin/apps/{a['id']}/listing/publish", headers=ed)).status_code == 403
    r = await client.post(f"{API}/admin/apps/{a['id']}/listing/reject", json={"reason": "Nom trop long"}, headers=admin_headers)
    assert r.json()["listing_review"]["state"] == "rejected"
    r = await client.patch(f"{API}/admin/apps/{a['id']}", json={"name": "Kaskad Notes 2"}, headers=ed)
    assert r.json()["listing_review"] is None and r.json()["draft"]["short_description"] == "Nouveau"
    await client.post(f"{API}/admin/apps/{a['id']}/listing/submit", json={"note": "Corrigé"}, headers=ed)
    assert len((await client.get(f"{API}/admin/moderation/reviews", headers=admin_headers)).json()["listings"]) == 1

    r = await client.post(f"{API}/admin/apps/{a['id']}/listing/publish", headers=admin_headers)
    assert r.json()["name"] == "Kaskad Notes 2" and r.json()["draft"] is None and r.json()["icon_url"]
    detail = (await client.get(f"{API}/apps/{a['id']}")).json()
    assert detail["name"] == "Kaskad Notes 2" and detail["short_description"] == "Nouveau" and detail["icon_url"]
    assert (await client.get(detail["icon_url"].removeprefix("http://test"))).status_code == 200

    # Abandon d'un brouillon : la fiche en ligne reste intacte, l'image du brouillon est supprimée
    draft = (await client.post(f"{API}/admin/apps/{a['id']}/icon", files={"file": ("j.png", PNG, "image/png")}, headers=ed)).json()
    draft_icon = draft["draft"]["icon_url"].removeprefix("http://test")
    r = await client.delete(f"{API}/admin/apps/{a['id']}/listing", headers=ed)
    assert r.json()["draft"] is None and r.json()["icon_url"] == detail["icon_url"]
    assert (await client.get(draft_icon)).status_code == 404
    assert (await client.get(detail["icon_url"].removeprefix("http://test"))).status_code == 200


async def test_draft_apps_are_edited_directly(app, client, admin_headers):
    ed = await editor_headers(client, app, admin_headers)
    a = await create_app(client, ed)
    r = await client.patch(f"{API}/admin/apps/{a['id']}", json={"name": "Direct"}, headers=ed)
    assert r.json()["name"] == "Direct" and r.json()["draft"] is None
