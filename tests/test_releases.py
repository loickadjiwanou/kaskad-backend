"""Canal bêta, publication programmée, fiches et notes de version bilingues, clés API (intégration continue)."""

from datetime import UTC, datetime, timedelta

from bson import ObjectId

from app.services.releases import publish_due
from tests.helpers import API, bearer, create_app, signup, upload, wait_scans


def mails_to(app, email):
    return [m for m in app.state.mailer.sent if m.to_email == email]


async def user_headers(client, email):
    r = await client.post(f"{API}/auth/register", json={"email": email, "password": "user-pass-123"})
    assert r.status_code in (200, 201), r.text
    return bearer(r.json())


async def live_app(client, app, admin_headers, headers, **kw):
    a = await create_app(client, headers, **kw)
    await client.post(f"{API}/admin/apps/{a['id']}/status", json={"status": "published"}, headers=admin_headers)
    return a


async def test_listing_and_release_notes_in_two_languages(app, client, admin_headers):
    owner = await signup(client, app)
    a = await create_app(client, owner, name="Nova Notes")
    # Fiche modifiée avant sa mise en ligne : directement (pas de brouillon de fiche)
    await client.patch(
        f"{API}/admin/apps/{a['id']}",
        json={
            "default_language": "fr",
            "translations": {"en": {"short_description": "Your synced notes.", "long_description": "A fast notepad."}},
        },
        headers=owner,
    )
    await client.post(f"{API}/admin/apps/{a['id']}/status", json={"status": "published"}, headers=admin_headers)
    v = (await upload(client, owner, a["id"], name="1.0.0")).json()
    await client.patch(
        f"{API}/admin/versions/{v['id']}",
        json={"changelog": "• Nouveautés", "changelog_translations": {"en": "• What's new"}},
        headers=owner,
    )
    await wait_scans(app)
    await client.post(f"{API}/admin/versions/{v['id']}/publish", headers=admin_headers)

    fr = (await client.get(f"{API}/apps/{a['id']}", headers={"Accept-Language": "fr-FR"})).json()
    en = (await client.get(f"{API}/apps/{a['id']}", headers={"Accept-Language": "en-US"})).json()
    assert fr["short_description"] == "Vos notes synchronisées." and fr["versions"][0]["changelog"] == "• Nouveautés"
    assert en["short_description"] == "Your synced notes." and en["long_description"] == "A fast notepad."
    assert en["versions"][0]["changelog"] == "• What's new"
    listing = (await client.get(f"{API}/apps", headers={"Accept-Language": "en"})).json()
    assert listing["items"][0]["short_description"] == "Your synced notes."
    # Upload avec notes dans les deux langues
    r = await upload(client, owner, a["id"], code=200, name="2.0.0")
    assert r.json()["changelog_lang"] == "fr"


async def test_beta_channel_for_testers_and_promotion(app, client, admin_headers):
    owner = await signup(client, app)
    a = await live_app(client, app, admin_headers, owner, name="Nova Notes")
    prod = (await upload(client, owner, a["id"], code=100, name="1.0.0")).json()
    files = {"file": ("app.deb", b"!<arch>\n" + b"debian-binary   " + b"1" * 2000, "application/octet-stream")}
    data = {"version_name": "1.1.0-beta.1", "version_code": "110", "platform": "linux", "file_format": "deb", "channel": "beta"}
    beta = (await client.post(f"{API}/admin/apps/{a['id']}/versions", data=data, files=files, headers=owner)).json()
    assert beta["channel"] == "beta"
    await wait_scans(app)
    # Bêta : au moins 3 testeurs requis pour la soumettre ou la publier
    r = await client.post(f"{API}/admin/versions/{beta['id']}/submit", json={}, headers={**owner, "Accept-Language": "fr"})
    assert r.status_code == 409 and r.json()["code"] == "not_enough_testers" and "3 testeurs" in r.json()["detail"]
    assert (await client.post(f"{API}/admin/versions/{beta['id']}/publish", headers=admin_headers)).json()["code"] == "not_enough_testers"
    testers = ["Tester@Example.com", "t2@example.com", "t3@example.com"]
    r = await client.put(f"{API}/admin/apps/{a['id']}/testers", json={"emails": testers}, headers={**owner, "Accept-Language": "fr"})
    assert r.json()["testers"] == ["t2@example.com", "t3@example.com", "tester@example.com"] and r.json()["min_beta_testers"] == 3
    # Chaque nouveau testeur est prévenu par e-mail (lien d'ouverture dans l'app Kaskad)
    invite = mails_to(app, "tester@example.com")[-1]
    assert invite.subject == "Vous êtes invité à tester Nova Notes" and f"kaskad://app/{a['id']}" in invite.text
    sent = len(app.state.mailer.sent)
    await client.put(f"{API}/admin/apps/{a['id']}/testers", json={"emails": [*testers, "t4@example.com"]}, headers=owner)
    assert len(app.state.mailer.sent) == sent + 1 and app.state.mailer.sent[-1].to_email == "t4@example.com"
    for v in (prod, beta):
        await client.post(f"{API}/admin/versions/{v['id']}/publish", headers=admin_headers)
    # Le canal ne change plus une fois en ligne
    r = await client.patch(f"{API}/admin/versions/{beta['id']}", json={"channel": "production"}, headers=admin_headers)
    assert r.json()["code"] == "version_locked"

    anon = (await client.get(f"{API}/apps/{a['id']}")).json()
    assert [x["version_name"] for x in anon["versions"]] == ["1.0.0"] and anon["latest_version_name"] == "1.0.0"
    other = (await client.get(f"{API}/apps/{a['id']}", headers=await user_headers(client, "someone@example.com"))).json()
    assert len(other["versions"]) == 1 and other["is_tester"] is False

    tester = await user_headers(client, "tester@example.com")
    mine = (await client.get(f"{API}/apps/{a['id']}", headers=tester)).json()
    assert mine["is_tester"] is True and [x["channel"] for x in mine["versions"]] == ["beta", "production"]
    beta_public = mine["versions"][0]
    # Téléchargement bêta : uniquement avec le lien signé remis au testeur
    assert (await client.get(f"{API}/versions/{beta['id']}/download")).status_code == 404
    assert (await client.get(f"{API}/versions/{beta['id']}/download", params={"t": "0.bad"})).status_code == 404
    assert (await client.get(beta_public["file_url"].removeprefix("http://test"))).status_code == 302
    check = {"installed": [{"app_id": a["id"], "version_code": 100, "platform": "linux"}]}
    assert (await client.post(f"{API}/updates/check", json=check)).json() == []
    upd = (await client.post(f"{API}/updates/check", json=check, headers=tester)).json()
    assert upd[0]["latest_version"]["version_name"] == "1.1.0-beta.1"

    # Passage en production : demandé par le développeur, validé par la plateforme
    r = await client.post(f"{API}/admin/versions/{beta['id']}/submit", json={"note": "Stable"}, headers=owner)
    assert r.json()["review"]["kind"] == "promote"
    assert (
        "passage en production" in mails_to(app, "admin@example.com")[-1].text or "promotion" in mails_to(app, "admin@example.com")[-1].text
    )
    r = await client.post(f"{API}/admin/versions/{beta['id']}/publish", headers=admin_headers)
    assert r.json()["channel"] == "production" and r.json()["promoted_at"]
    anon = (await client.get(f"{API}/apps/{a['id']}")).json()
    assert anon["latest_version_name"] == "1.1.0-beta.1" and len(anon["versions"]) == 2


async def test_scheduled_publication(app, client, admin_headers):
    owner = await signup(client, app)
    a = await live_app(client, app, admin_headers, owner, name="Nova Notes")
    v = (await upload(client, owner, a["id"])).json()
    await wait_scans(app)
    when = datetime.now(UTC) + timedelta(hours=2)
    r = await client.post(f"{API}/admin/versions/{v['id']}/submit", json={"publish_at": when.isoformat()}, headers=owner)
    assert r.json()["review"]["publish_at"]
    r = await client.post(f"{API}/admin/versions/{v['id']}/publish", headers=admin_headers)
    assert r.json()["status"] == "scheduled" and r.json()["scheduled_at"]
    assert mails_to(app, "owner@example.com")[-1].subject.startswith(("Version programmée", "Version scheduled"))
    assert (await client.get(f"{API}/apps/{a['id']}")).json()["versions"] == []

    db = app.state.db
    assert await publish_due(db, None, app.state.mailer) == 0
    await db.versions.update_one({"_id": ObjectId(v["id"])}, {"$set": {"scheduled_at": datetime.now(UTC) - timedelta(minutes=1)}})
    assert await publish_due(db, None, app.state.mailer) == 1
    assert (await client.get(f"{API}/apps/{a['id']}")).json()["versions"][0]["version_name"] == "1.0.0"
    assert mails_to(app, "owner@example.com")[-1].subject.startswith(("Version disponible", "Version available"))

    # Annulation d'une programmation : retour en brouillon
    v2 = (await upload(client, owner, a["id"], code=200, name="2.0.0")).json()
    await wait_scans(app)
    await client.post(f"{API}/admin/versions/{v2['id']}/publish", json={"publish_at": when.isoformat()}, headers=admin_headers)
    r = await client.post(f"{API}/admin/versions/{v2['id']}/unschedule", headers=owner)
    assert r.json()["status"] == "draft" and r.json()["scheduled_at"] is None


async def test_api_keys_for_continuous_integration(app, client, admin_headers):
    owner = await signup(client, app)
    a = await create_app(client, owner, name="Nova Notes")
    r = await client.post(f"{API}/admin/api-keys", json={"name": "GitHub Actions"}, headers=owner)
    key = r.json()["key"]
    assert key.startswith("ksk_") and r.json()["prefix"] == key[:12]
    listed = (await client.get(f"{API}/admin/api-keys", headers=owner)).json()
    assert listed[0]["name"] == "GitHub Actions" and "key" not in listed[0]
    ci = {"Authorization": f"Bearer {key}"}

    # Envoi + soumission automatique dès l'analyse validée
    files = {"file": ("app.deb", b"!<arch>\n" + b"debian-binary   " + b"2" * 2000, "application/octet-stream")}
    data = {
        "version_name": "1.0.0",
        "version_code": "1",
        "platform": "linux",
        "file_format": "deb",
        "changelog_en": "• First",
        "submit": "true",
        "submit_note": "From CI",
    }
    r = await client.post(f"{API}/admin/apps/{a['id']}/versions", data=data, files=files, headers=ci)
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["auto_submit"] is True
    await wait_scans(app)
    got = (await client.get(f"{API}/admin/versions/{v['id']}", headers=ci)).json()
    assert got["review"]["state"] == "pending" and got["review"]["note"] == "From CI"
    assert got["review"]["submitted_by_name"] == "API · GitHub Actions"
    assert mails_to(app, "admin@example.com")[-1].subject.startswith(("À valider", "To review"))

    # Droits d'un développeur : ni publication, ni équipe, ni clés
    assert (await client.post(f"{API}/admin/versions/{v['id']}/publish", headers=ci)).status_code == 403
    assert (await client.get(f"{API}/admin/api-keys", headers=ci)).status_code == 403
    assert (await client.post(f"{API}/admin/invitations", json={"email": "x@example.com", "role": "viewer"}, headers=ci)).status_code == 403
    log = (await client.get(f"{API}/admin/activity", headers=owner)).json()["items"]
    assert any(e["actor_name"] == "API · GitHub Actions" and e["action"] == "version.uploaded" for e in log)

    # Révocation
    await client.delete(f"{API}/admin/api-keys/{listed[0]['id']}", headers=owner)
    r = await client.get(f"{API}/admin/apps", headers=ci)
    assert r.status_code == 401 and r.json()["code"] == "invalid_api_key"


async def test_closed_testing_before_public_launch(app, client, admin_headers):
    """Comme un test fermé Google Play : app pas encore publiée, visible et téléchargeable par ses seuls testeurs (bêta)."""
    owner = await signup(client, app)
    a = await create_app(client, owner, name="Nova Notes")  # brouillon : jamais publiée
    await client.put(
        f"{API}/admin/apps/{a['id']}/testers",
        json={"emails": ["tester@example.com", "t2@example.com", "t3@example.com"]},
        headers={**owner, "Accept-Language": "fr"},
    )
    invite = mails_to(app, "tester@example.com")[-1]
    assert "même avant le lancement public" in invite.text
    prod = (await upload(client, owner, a["id"], code=100, name="1.0.0")).json()
    files = {"file": ("app.deb", b"!<arch>\n" + b"debian-binary   " + b"7" * 2000, "application/octet-stream")}
    data = {"version_name": "1.1.0-beta.1", "version_code": "110", "platform": "linux", "file_format": "deb", "channel": "beta"}
    beta = (await client.post(f"{API}/admin/apps/{a['id']}/versions", data=data, files=files, headers=owner)).json()
    await wait_scans(app)
    for v in (prod, beta):
        await client.post(f"{API}/admin/versions/{v['id']}/submit", json={}, headers=owner)
        await client.post(f"{API}/admin/versions/{v['id']}/publish", headers=admin_headers)
    # Le développeur sait que la bêta est déjà installable par ses testeurs
    mail = mails_to(app, "owner@example.com")[-1]
    assert mail.subject.startswith("Version bêta disponible") and "même si l'application n'est pas encore publiée" in mail.text

    async def session(email):
        r = await client.post(f"{API}/auth/register", json={"email": email, "password": "user-pass-123"})
        return bearer(r.json())

    tester = await session("tester@example.com")
    stranger = await session("someone@example.com")
    # Invisible pour tout le monde sauf les testeurs, et jamais listée dans le store
    assert (await client.get(f"{API}/apps/{a['id']}")).status_code == 404
    assert (await client.get(f"{API}/apps/{a['id']}", headers=stranger)).status_code == 404
    assert (await client.get(f"{API}/apps", headers=tester)).json()["total"] == 0
    detail = (await client.get(f"{API}/apps/{a['id']}", headers=tester)).json()
    assert detail["in_testing"] is True and detail["is_tester"] is True
    # Seule la bêta est proposée (la version de production attend le lancement public)
    assert [v["version_name"] for v in detail["versions"]] == ["1.1.0-beta.1"]
    r = await client.get(detail["versions"][0]["file_url"].replace("http://test", ""), follow_redirects=False)
    assert r.status_code in (302, 307)
    r = await client.get(f"{API}/versions/{prod['id']}/download", follow_redirects=False)
    assert r.status_code == 404
    # Mises à jour : la bêta pour le testeur, rien pour les autres ; « Mes apps » du testeur
    check = {"installed": [{"app_id": a["id"], "version_id": beta["id"], "version_code": 100, "platform": "linux"}]}
    assert (await client.post(f"{API}/updates/check", json=check, headers=tester)).json()[0]["latest_version"][
        "version_name"
    ] == "1.1.0-beta.1"
    assert (await client.post(f"{API}/updates/check", json=check, headers=stranger)).json() == []
    await client.put(f"{API}/me/library", json={"installed_apps": [{"app_id": a["id"], "version_id": beta["id"]}]}, headers=tester)
    mine = (await client.get(f"{API}/me/apps", headers=tester)).json()
    assert mine[0]["app"]["name"] == "Nova Notes" and mine[0]["latest_version"]["version_name"] == "1.1.0-beta.1"
    # Avis et page web publique : pas pendant le test
    assert (await client.get(f"{API}/apps/{a['id']}/reviews", headers=tester)).status_code == 404
    assert (await client.get(f"/a/{a['id']}")).status_code == 404

    # Lancement public : la version de production devient visible par tous
    await client.post(f"{API}/admin/apps/{a['id']}/status", json={"status": "published"}, headers=admin_headers)
    public = (await client.get(f"{API}/apps/{a['id']}")).json()
    assert public["in_testing"] is False and [v["version_name"] for v in public["versions"]] == ["1.0.0"]
    # Dépubliée (retirée du store) : plus accessible, même aux testeurs
    await client.post(f"{API}/admin/apps/{a['id']}/status", json={"status": "archived"}, headers=admin_headers)
    assert (await client.get(f"{API}/apps/{a['id']}", headers=tester)).status_code == 404
