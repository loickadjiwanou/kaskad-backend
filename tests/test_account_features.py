"""Mot de passe oublié, e-mails de suivi des demandes, développeur sur les fiches publiques, suspension de compte."""

from tests.helpers import API, bearer, create_app, invite, signup, token_from, upload, wait_scans


def mails_to(app, email):
    return [m for m in app.state.mailer.sent if m.to_email == email]


async def published_nova_app(client, app, admin_headers):
    """Compte « Studio Nova » avec une app publiée (version soumise puis validée par la plateforme)."""
    owner = await signup(client, app, "nora@example.com", "Studio Nova")
    a = await create_app(client, owner, name="Nova Notes")
    v = (await upload(client, owner, a["id"])).json()
    await wait_scans(app)
    await client.post(f"{API}/admin/versions/{v['id']}/submit", json={}, headers=owner)
    await client.post(f"{API}/admin/versions/{v['id']}/publish", headers=admin_headers)
    await client.post(f"{API}/admin/apps/{a['id']}/status", json={"status": "published"}, headers=admin_headers)
    return owner, a, v


async def test_forgot_and_reset_password(app, client):
    owner = await signup(client, app)
    r = await client.post(f"{API}/admin/auth/forgot-password", json={"email": "owner@example.com"}, headers={"Accept-Language": "fr"})
    assert r.status_code == 202
    mail = mails_to(app, "owner@example.com")[-1]
    assert mail.subject.startswith("Réinitialisez votre mot de passe") and "/reset-password?token=" in mail.text
    # Adresse inconnue : même réponse, aucun e-mail
    sent = len(app.state.mailer.sent)
    assert (await client.post(f"{API}/admin/auth/forgot-password", json={"email": "ghost@example.com"})).status_code == 202
    assert len(app.state.mailer.sent) == sent

    token = token_from(app, "owner@example.com")
    assert (await client.post(f"{API}/admin/auth/reset-password", json={"token": token, "password": "short"})).status_code == 422
    r = await client.post(f"{API}/admin/auth/reset-password", json={"token": token, "password": "brand-new-pass"})
    assert r.status_code == 200 and r.json()["admin"]["email"] == "owner@example.com"
    # Lien à usage unique, anciennes sessions révoquées, ancien mot de passe refusé
    assert (await client.post(f"{API}/admin/auth/reset-password", json={"token": token, "password": "another-pass"})).json()[
        "code"
    ] == "token_invalid"
    assert (await client.get(f"{API}/admin/auth/me", headers=owner)).status_code == 200  # jeton d'accès encore valide jusqu'à expiration
    login = lambda pwd: client.post(f"{API}/admin/auth/login", json={"email": "owner@example.com", "password": pwd})  # noqa: E731
    assert (await login("owner-pass-1")).status_code == 401
    assert (await login("brand-new-pass")).status_code == 200


async def test_review_notification_emails(app, client, admin_headers):
    owner = await signup(client, app)
    # Le développeur utilise la console en anglais : ses e-mails sont en anglais
    await client.post(f"{API}/admin/invitations", json={"email": "dev@example.com", "role": "developer"}, headers=owner)
    r = await client.post(
        f"{API}/admin/invitations/accept",
        json={"token": token_from(app, "dev@example.com"), "name": "Dev", "password": "member-pass-1"},
        headers={"Accept-Language": "en"},
    )
    dev = bearer(r.json())
    # L'administrateur utilise la console en français
    await client.get(f"{API}/admin/auth/me", headers={**admin_headers, "Accept-Language": "fr"})

    a = await create_app(client, dev, name="Nova Notes")
    v = (await upload(client, dev, a["id"])).json()
    await wait_scans(app)
    await client.post(f"{API}/admin/versions/{v['id']}/submit", json={"note": "Ready"}, headers=dev)
    admin_mail = mails_to(app, "admin@example.com")[-1]
    assert admin_mail.subject.startswith("À valider : Nova Notes") and "Dev (Studio Nova)" in admin_mail.text
    assert "/moderation" in admin_mail.text

    await client.post(f"{API}/admin/versions/{v['id']}/reject", json={"reason": "Changelog is empty"}, headers=admin_headers)
    mail = mails_to(app, "dev@example.com")[-1]
    assert mail.subject.startswith("Version rejected: Nova Notes 1.0.0") and "Reason: Changelog is empty" in mail.text

    await client.patch(f"{API}/admin/versions/{v['id']}", json={"changelog": "• Fixes"}, headers=dev)
    await client.post(f"{API}/admin/versions/{v['id']}/submit", json={}, headers=dev)
    # L'administrateur est averti que l'app n'est pas encore publiée
    assert "pas encore publiée" in mails_to(app, "admin@example.com")[-1].text
    await client.post(f"{API}/admin/versions/{v['id']}/publish", headers=admin_headers)
    # App pas encore publiée : la version est validée mais PAS « disponible au téléchargement »
    mail = mails_to(app, "dev@example.com")[-1]
    assert mail.subject.startswith("Version approved: Nova Notes 1.0.0")
    assert "can't be downloaded yet" in mail.text and "Request publishing" in mail.text and "available for download" not in mail.text

    # Demande de publication de l'app : approuvée → l'app est visible dans le store, avec sa version
    await client.post(f"{API}/admin/apps/{a['id']}/status-request", json={"status": "published"}, headers=dev)
    assert mails_to(app, "admin@example.com")[-1].subject.startswith("À valider")
    await client.post(f"{API}/admin/apps/{a['id']}/status-request/approve", headers=admin_headers)
    mail = mails_to(app, "dev@example.com")[-1]
    assert mail.subject.startswith("App published: Nova Notes")
    assert "approved by the platform admin" in mail.text and "visible in the Kaskad store" in mail.text and "can be downloaded" in mail.text
    # Dépublication directe par l'administrateur : le propriétaire du compte est prévenu, pas l'administrateur
    before = len(mails_to(app, "admin@example.com"))
    await client.post(f"{API}/admin/apps/{a['id']}/status", json={"status": "archived"}, headers=admin_headers)
    assert len(mails_to(app, "admin@example.com")) == before
    mail = mails_to(app, "owner@example.com")[-1]
    assert mail.subject.startswith("Application dépubliée : Nova Notes") and "décision a été prise par l'administrateur" in mail.text


async def test_developer_shown_on_public_listing(app, client, admin_headers):
    _, a, _ = await published_nova_app(client, app, admin_headers)
    detail = (await client.get(f"{API}/apps/{a['id']}")).json()
    dev_id = detail["developer"]["id"]
    assert detail["developer"]["name"] == "Studio Nova"
    assert (await client.get(f"{API}/developers/{dev_id}")).json() == {"id": dev_id, "name": "Studio Nova", "apps_count": 1}
    listing = (await client.get(f"{API}/apps", params={"developer_id": dev_id})).json()
    assert [x["name"] for x in listing["items"]] == ["Nova Notes"]
    # Le nom suit le renommage du compte
    owner = await client.post(f"{API}/admin/auth/login", json={"email": "nora@example.com", "password": "owner-pass-1"})
    await client.patch(f"{API}/admin/account", json={"name": "Nova Games"}, headers=bearer(owner.json()))
    assert (await client.get(f"{API}/apps/{a['id']}")).json()["developer"]["name"] == "Nova Games"


async def test_account_suspension(app, client, admin_headers):
    owner, a, v = await published_nova_app(client, app, admin_headers)
    member = await invite(client, app, owner, "viewer@example.com", "viewer")
    accounts = {x["name"]: x for x in (await client.get(f"{API}/admin/accounts", headers=admin_headers)).json()}
    nova = accounts["Studio Nova"]

    # Le compte de la plateforme ne peut pas être suspendu ; seul l'administrateur suspend
    assert (
        await client.patch(f"{API}/admin/accounts/{accounts['Kaskad']['id']}", json={"suspended": True}, headers=admin_headers)
    ).status_code == 403
    assert (await client.patch(f"{API}/admin/accounts/{nova['id']}", json={"suspended": True}, headers=owner)).status_code == 403

    r = await client.patch(
        f"{API}/admin/accounts/{nova['id']}", json={"suspended": True, "reason": "Malware reported"}, headers=admin_headers
    )
    assert r.json()["suspended"] is True
    # Store : apps et fiche développeur masquées, téléchargement refusé
    assert (await client.get(f"{API}/apps/{a['id']}")).status_code == 404
    assert (await client.get(f"{API}/apps")).json()["total"] == 0
    assert (await client.get(f"{API}/developers/{nova['id']}")).status_code == 404
    assert (await client.get(f"{API}/versions/{v['id']}/download")).status_code == 404
    # Membres : sessions refusées, connexion refusée, invitation impossible
    assert (await client.get(f"{API}/admin/auth/me", headers=member)).status_code == 401
    r = await client.post(f"{API}/admin/auth/login", json={"email": "nora@example.com", "password": "owner-pass-1"})
    assert r.status_code == 403 and r.json()["code"] == "account_suspended"
    mail = mails_to(app, "nora@example.com")[-1]
    assert "Studio Nova" in mail.subject and "Malware reported" in mail.text
    listed = {x["name"]: x for x in (await client.get(f"{API}/admin/accounts", headers=admin_headers)).json()}
    assert listed["Studio Nova"]["suspension_reason"] == "Malware reported"

    # Réactivation
    await client.patch(f"{API}/admin/accounts/{nova['id']}", json={"suspended": False}, headers=admin_headers)
    assert (await client.get(f"{API}/apps/{a['id']}")).status_code == 200
    assert (await client.post(f"{API}/admin/auth/login", json={"email": "nora@example.com", "password": "owner-pass-1"})).status_code == 200
