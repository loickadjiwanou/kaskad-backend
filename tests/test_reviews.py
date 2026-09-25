"""Notes et avis, signalements d'apps et d'avis, page web publique des apps."""

from tests.helpers import API, bearer, invite, signup
from tests.test_account_features import published_nova_app


def mails_to(app, email):
    return [m for m in app.state.mailer.sent if m.to_email == email]


async def user(client, email, lang="fr"):
    r = await client.post(f"{API}/auth/register", json={"email": email, "password": "user-pass-123"}, headers={"Accept-Language": lang})
    return bearer(r.json())


async def test_ratings_reviews_and_developer_reply(app, client, admin_headers):
    owner, a, _ = await published_nova_app(client, app, admin_headers)
    anon = bearer((await client.post(f"{API}/auth/anonymous", json={"device_id": "device-0000-0001"})).json())
    r = await client.put(f"{API}/apps/{a['id']}/reviews/mine", json={"rating": 5, "body": "Top"}, headers=anon)
    assert r.status_code == 403 and r.json()["code"] == "email_account_required"

    r = await client.post(
        f"{API}/auth/register",
        json={"email": "alice@example.com", "password": "user-pass-123", "name": "Alice"},
        headers={"Accept-Language": "en"},
    )
    assert r.json()["user"]["name"] == "Alice"
    alice = bearer(r.json())
    bob = await user(client, "bob@example.com")
    r = await client.put(
        f"{API}/apps/{a['id']}/reviews/mine",
        json={"rating": 4, "body": "Great app", "author_name": "Ignored"},
        headers={**alice, "Accept-Language": "en"},
    )
    assert r.json()["rating"] == 4 and r.json()["is_mine"] is True
    await client.put(f"{API}/apps/{a['id']}/reviews/mine", json={"rating": 2, "body": "Crashes"}, headers=bob)
    detail = (await client.get(f"{API}/apps/{a['id']}")).json()
    assert detail["rating_average"] == 3 and detail["rating_count"] == 2 and detail["rating_distribution"]["4"] == 1
    # Un seul avis par compte : modification
    await client.put(f"{API}/apps/{a['id']}/reviews/mine", json={"rating": 5, "body": "Even better"}, headers=alice)
    listing = (await client.get(f"{API}/apps/{a['id']}/reviews", params={"sort": "rating_desc"}, headers=alice)).json()
    assert [x["rating"] for x in listing["items"]] == [5, 2] and listing["items"][0]["is_mine"] and listing["rating"]["average"] == 3.5
    assert listing["items"][0]["author_name"] == "Alice"  # nom du compte (le champ envoyé est ignoré)
    assert listing["items"][1]["author_name"] == "bob"  # compte sans nom : partie de l'e-mail avant @
    # Nouveau nom de compte : repris sur ses avis
    r = await client.patch(f"{API}/me", json={"name": "Bob Martin"}, headers=bob)
    assert r.json()["name"] == "Bob Martin"
    listing = (await client.get(f"{API}/apps/{a['id']}/reviews", params={"sort": "rating_desc"})).json()
    assert listing["items"][1]["author_name"] == "Bob Martin"

    # Réponse du développeur (l'auteur est prévenu, dans la langue de son avis)
    review_id = listing["items"][0]["id"]
    r = await client.put(f"{API}/admin/reviews/{review_id}/reply", json={"body": "Thanks Alice!"}, headers=owner)
    assert r.json()["reply"]["author_name"] == "Studio Nova"
    mail = mails_to(app, "alice@example.com")[-1]
    assert mail.subject == "Studio Nova replied to your review of Nova Notes" and "Thanks Alice!" in mail.text
    public = (await client.get(f"{API}/apps/{a['id']}/reviews")).json()
    assert public["items"][0]["reply"]["body"] == "Thanks Alice!"
    viewer = await invite(client, app, owner, "viewer@example.com", "viewer")
    assert (await client.put(f"{API}/admin/reviews/{review_id}/reply", json={"body": "x"}, headers=viewer)).status_code == 403
    other = await signup(client, app, "omar@example.com", "Orbit Labs")
    assert (await client.get(f"{API}/admin/apps/{a['id']}/reviews", headers=other)).status_code == 404
    assert (await client.get(f"{API}/admin/apps/{a['id']}/reviews", params={"replied": "false"}, headers=owner)).json()["total"] == 1

    # Suppression de son avis
    await client.delete(f"{API}/apps/{a['id']}/reviews/mine", headers=bob)
    assert (await client.get(f"{API}/apps/{a['id']}")).json()["rating_count"] == 1
    # Suppression du compte : ses avis disparaissent et la note est recalculée
    await client.delete(f"{API}/me", headers=alice)
    assert (await client.get(f"{API}/apps/{a['id']}")).json()["rating_count"] == 0


async def test_review_moderation(app, client, admin_headers):
    owner, a, _ = await published_nova_app(client, app, admin_headers)
    alice = await user(client, "alice@example.com")
    await client.put(f"{API}/apps/{a['id']}/reviews/mine", json={"rating": 1, "body": "Spam spam"}, headers=alice)
    review_id = (await client.get(f"{API}/apps/{a['id']}/reviews")).json()["items"][0]["id"]
    for h in (await user(client, "r1@example.com"), await user(client, "r2@example.com")):
        assert (await client.post(f"{API}/reviews/{review_id}/report", json={"reason": "abusive"}, headers=h)).status_code == 204
    reported = (await client.get(f"{API}/admin/moderation/user-reviews", headers=admin_headers)).json()
    assert reported["reported"] == 1 and reported["items"][0]["reports_count"] == 2 and reported["items"][0]["app_name"] == "Nova Notes"
    assert (await client.get(f"{API}/admin/stats/overview", headers=admin_headers)).json()["user_reviews_reported"] == 1
    assert (await client.post(f"{API}/admin/reviews/{review_id}/hide", json={"reason": "Spam"}, headers=owner)).status_code == 403

    r = await client.post(f"{API}/admin/reviews/{review_id}/hide", json={"reason": "Spam"}, headers=admin_headers)
    assert r.json()["hidden"] is True and r.json()["reports_count"] == 0
    assert (await client.get(f"{API}/apps/{a['id']}/reviews")).json()["total"] == 0
    assert (await client.get(f"{API}/apps/{a['id']}")).json()["rating_count"] == 0
    await client.post(f"{API}/admin/reviews/{review_id}/restore", headers=admin_headers)
    assert (await client.get(f"{API}/apps/{a['id']}")).json()["rating_count"] == 1


async def test_report_app(app, client, admin_headers):
    owner, a, _ = await published_nova_app(client, app, admin_headers)
    await client.get(f"{API}/admin/auth/me", headers={**admin_headers, "Accept-Language": "fr"})
    # Signalement possible sans compte ; un seul signalement ouvert par personne
    for _ in range(2):
        r = await client.post(f"{API}/apps/{a['id']}/report", json={"reason": "malware", "details": "Antivirus alert"})
        assert r.status_code == 201
    reports = (await client.get(f"{API}/admin/moderation/reports", headers=admin_headers)).json()
    assert reports["open"] == 1 and reports["items"][0]["reason"] == "malware" and reports["items"][0]["app_name"] == "Nova Notes"
    mail = mails_to(app, "admin@example.com")[-1]
    assert mail.subject.startswith("Signalement : Nova Notes") and "logiciel malveillant" in mail.text
    assert (await client.get(f"{API}/admin/moderation/reports", headers=owner)).status_code == 403

    r = await client.post(
        f"{API}/admin/reports/{reports['items'][0]['id']}/resolve",
        json={"resolution": "resolved", "note": "Confirmed", "unpublish": True},
        headers=admin_headers,
    )
    assert r.json()["status"] == "closed" and r.json()["resolution"] == "resolved"
    assert (await client.get(f"{API}/apps/{a['id']}")).status_code == 404
    assert (await client.get(f"{API}/admin/moderation/reports", headers=admin_headers)).json()["open"] == 0


async def test_public_app_page(app, client, admin_headers):
    _, a, _ = await published_nova_app(client, app, admin_headers)
    detail = (await client.get(f"{API}/apps/{a['id']}")).json()
    assert detail["share_url"] == f"http://test/a/{a['id']}"
    r = await client.get(f"/a/{a['id']}", headers={"Accept-Language": "fr"})
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    html = r.text
    assert "Nova Notes" in html and 'property="og:title"' in html and f"kaskad://app/{a['id']}" in html
    assert "intent://app/" in html and "Ouvrir dans Kaskad" in html and "par Studio Nova" in html
    assert "Open in Kaskad" in (await client.get(f"/a/{a['id']}", headers={"Accept-Language": "en"})).text
    assert (await client.get("/a/000000000000000000000000")).status_code == 404
    assert (await client.get("/a/not-an-id")).status_code == 404
