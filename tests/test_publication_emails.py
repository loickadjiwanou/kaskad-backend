"""E-mails du circuit de publication : bon texte selon la visibilité de l'app, bon destinataire, jamais l'auteur de l'action."""

from tests.helpers import API, bearer, create_app, signup, token_from, upload, wait_scans


def mails_to(app, email):
    return [m for m in app.state.mailer.sent if m.to_email == email]


async def developer(client, app, owner, email="dev@example.com"):
    await client.post(f"{API}/admin/invitations", json={"email": email, "role": "developer"}, headers=owner)
    r = await client.post(
        f"{API}/admin/invitations/accept",
        json={"token": token_from(app, email), "name": "Dev", "password": "member-pass-1"},
        headers={"Accept-Language": "fr"},
    )
    return bearer(r.json())


async def submitted_version(client, app, headers, app_id, **kw):
    v = (await upload(client, headers, app_id, **kw)).json()
    await wait_scans(app)
    r = await client.post(f"{API}/admin/versions/{v['id']}/submit", json={}, headers=headers)
    assert r.status_code == 200, r.text
    return v


async def test_version_emails_follow_app_visibility(app, client, admin_headers):
    owner = await signup(client, app)  # console en français
    dev = await developer(client, app, owner)
    a = await create_app(client, dev, name="Nova Notes")

    # 1. App en brouillon + demande de publication en cours → « téléchargeable dès que l'app sera approuvée »
    await client.post(f"{API}/admin/apps/{a['id']}/status-request", json={"status": "published"}, headers=dev)
    v = await submitted_version(client, app, dev, a["id"])
    await client.post(f"{API}/admin/versions/{v['id']}/publish", headers=admin_headers)
    mail = mails_to(app, "dev@example.com")[-1]
    assert mail.subject.startswith("Version validée : Nova Notes 1.0.0")
    assert "en cours de validation" in mail.text and "disponible au téléchargement" not in mail.text

    # 2. Publication de l'app approuvée → « visible dans le store », ses versions sont téléchargeables
    await client.post(f"{API}/admin/apps/{a['id']}/status-request/approve", headers=admin_headers)
    mail = mails_to(app, "dev@example.com")[-1]
    assert mail.subject.startswith("Application publiée : Nova Notes") and "sont téléchargeables" in mail.text

    # 3. App publiée : nouvelle version validée → « disponible au téléchargement dans le store »
    v2 = await submitted_version(client, app, dev, a["id"], code=200, name="2.0.0")
    await client.post(f"{API}/admin/versions/{v2['id']}/publish", headers=admin_headers)
    mail = mails_to(app, "dev@example.com")[-1]
    assert (
        mail.subject.startswith("Version disponible : Nova Notes 2.0.0")
        and "disponible au téléchargement dans le store Kaskad" in mail.text
    )

    # 4. Bêta sur app publiée → « testeurs uniquement » ; passage en production → « tous les utilisateurs »
    await client.put(
        f"{API}/admin/apps/{a['id']}/testers", json={"emails": ["t1@example.com", "t2@example.com", "t3@example.com"]}, headers=dev
    )
    beta = (await upload(client, dev, a["id"], code=300, name="3.0.0-beta.1")).json()
    await client.patch(f"{API}/admin/versions/{beta['id']}", json={"channel": "beta"}, headers=dev)
    await wait_scans(app)
    await client.post(f"{API}/admin/versions/{beta['id']}/submit", json={}, headers=dev)
    await client.post(f"{API}/admin/versions/{beta['id']}/publish", headers=admin_headers)
    mail = mails_to(app, "dev@example.com")[-1]
    assert mail.subject.startswith("Version bêta disponible") and "testeurs de l'application uniquement" in mail.text
    await client.post(f"{API}/admin/versions/{beta['id']}/submit", json={}, headers=dev)
    await client.post(f"{API}/admin/versions/{beta['id']}/publish", headers=admin_headers)
    assert mails_to(app, "dev@example.com")[-1].subject.startswith("Version en production : Nova Notes 3.0.0-beta.1")

    # 5. Publication directe par l'administrateur (sans demande) → la personne qui a envoyé la version est prévenue
    v4 = (await upload(client, dev, a["id"], code=400, name="4.0.0")).json()
    await wait_scans(app)
    await client.post(f"{API}/admin/versions/{v4['id']}/publish", headers=admin_headers)
    mail = mails_to(app, "dev@example.com")[-1]
    assert mail.subject.startswith("Version disponible : Nova Notes 4.0.0") and "décision a été prise par l'administrateur" in mail.text
    # L'administrateur n'est jamais destinataire de ses propres décisions
    assert not any(m.subject.startswith(("Version", "Application")) for m in mails_to(app, "admin@example.com"))


async def test_app_published_without_version_and_platform_apps(app, client, admin_headers):
    owner = await signup(client, app)
    a = await create_app(client, owner, name="Nova Notes")
    await client.post(f"{API}/admin/apps/{a['id']}/status-request", json={"status": "published"}, headers=owner)
    await client.post(f"{API}/admin/apps/{a['id']}/status-request/approve", headers=admin_headers)
    mail = mails_to(app, "owner@example.com")[-1]
    assert mail.subject.startswith("Application publiée") and "aucune version téléchargeable" in mail.text

    # Apps du compte de la plateforme : l'administrateur agit sur ses propres apps sans s'écrire
    before = len(app.state.mailer.sent)
    own = await create_app(client, admin_headers, name="Kaskad Notes")
    await client.post(f"{API}/admin/apps/{own['id']}/status", json={"status": "published"}, headers=admin_headers)
    v = (await upload(client, admin_headers, own["id"])).json()
    await wait_scans(app)
    await client.post(f"{API}/admin/versions/{v['id']}/publish", headers=admin_headers)
    assert len(app.state.mailer.sent) == before


async def test_api_key_submissions_and_scan_failures_reach_the_owner(app, client, admin_headers):
    owner = await signup(client, app)
    a = await create_app(client, owner, name="Nova Notes")
    await client.post(f"{API}/admin/apps/{a['id']}/status", json={"status": "published"}, headers=admin_headers)
    key = (await client.post(f"{API}/admin/api-keys", json={"name": "CI"}, headers=owner)).json()["key"]
    ci = {"Authorization": f"Bearer {key}"}
    data = {"version_name": "1.0.0", "version_code": "1", "platform": "linux", "file_format": "deb", "changelog": "x", "submit": "true"}

    # Version soumise par la clé API puis validée : e-mail au propriétaire du compte (la clé n'a pas d'adresse)
    files = {"file": ("app.deb", b"!<arch>\n" + b"debian-binary   " + b"3" * 2000, "application/octet-stream")}
    v = (await client.post(f"{API}/admin/apps/{a['id']}/versions", data=data, files=files, headers=ci)).json()
    await wait_scans(app)
    await client.post(f"{API}/admin/versions/{v['id']}/publish", headers=admin_headers)
    assert mails_to(app, "owner@example.com")[-1].subject.startswith("Version disponible : Nova Notes 1.0.0")

    # Analyse de sécurité échouée (contenu qui ne correspond pas au format) : e-mail avec le détail
    files = {"file": ("app.deb", b"not a debian package" * 50, "application/octet-stream")}
    await client.post(
        f"{API}/admin/apps/{a['id']}/versions", data={**data, "version_code": "2", "version_name": "2.0.0"}, files=files, headers=ci
    )
    await wait_scans(app)
    mail = mails_to(app, "owner@example.com")[-1]
    assert mail.subject.startswith("Analyse de sécurité échouée : Nova Notes 2.0.0") and "Détail :" in mail.text

    # Bêta envoyée par la clé API sans assez de testeurs : soumission bloquée, e-mail explicatif
    files = {"file": ("app.deb", b"!<arch>\n" + b"debian-binary   " + b"4" * 2000, "application/octet-stream")}
    await client.post(
        f"{API}/admin/apps/{a['id']}/versions",
        data={**data, "version_code": "3", "version_name": "3.0.0-beta.1", "channel": "beta"},
        files=files,
        headers=ci,
    )
    await wait_scans(app)
    mail = mails_to(app, "owner@example.com")[-1]
    assert mail.subject.startswith("Soumission bloquée : Nova Notes 3.0.0-beta.1") and "au moins 3 testeurs" in mail.text


async def test_rejections_and_scheduling_on_hidden_app(app, client, admin_headers):
    from datetime import UTC, datetime, timedelta

    owner = await signup(client, app)
    a = await create_app(client, owner, name="Nova Notes")
    when = datetime.now(UTC) + timedelta(days=1)
    v = (await upload(client, owner, a["id"])).json()
    await wait_scans(app)
    await client.post(f"{API}/admin/versions/{v['id']}/submit", json={"publish_at": when.isoformat()}, headers=owner)
    await client.post(f"{API}/admin/versions/{v['id']}/publish", headers=admin_headers)
    mail = mails_to(app, "owner@example.com")[-1]
    assert mail.subject.startswith("Version programmée") and "l'application n'est pas encore publiée" in mail.text

    await client.post(f"{API}/admin/apps/{a['id']}/status-request", json={"status": "published"}, headers=owner)
    await client.post(f"{API}/admin/apps/{a['id']}/status-request/reject", json={"reason": "Screenshots missing"}, headers=admin_headers)
    mail = mails_to(app, "owner@example.com")[-1]
    assert mail.subject.startswith("Demande refusée : Nova Notes") and "Screenshots missing" in mail.text
