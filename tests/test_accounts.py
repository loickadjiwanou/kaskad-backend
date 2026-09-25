"""Comptes développeurs : inscription, confirmation d'e-mail, invitations, rôles et cloisonnement des comptes."""

from tests.helpers import API, bearer, create_app, invite, signup, token_from, upload, wait_scans


async def test_signup_requires_email_confirmation_in_console_language(app, client):
    body = {"name": "Nora", "email": "Nora@Example.com", "password": "owner-pass-1", "account_name": "Studio Nova"}
    r = await client.post(f"{API}/admin/auth/signup", json=body, headers={"Accept-Language": "fr-FR"})
    assert r.status_code == 201 and r.json()["email"] == "nora@example.com"
    [mail] = app.state.mailer.sent
    assert mail.to_email == "nora@example.com" and mail.subject.startswith("Confirmez votre adresse")
    assert "Studio Nova" in mail.html and "/verify-email?token=" in mail.text

    assert (await client.post(f"{API}/admin/auth/signup", json=body)).json()["code"] == "email_taken"
    r = await client.post(f"{API}/admin/auth/login", json={"email": "nora@example.com", "password": "owner-pass-1"})
    assert r.status_code == 403 and r.json()["code"] == "email_not_verified"

    # Renvoi (langue actuelle de la console : anglais) ; l'ancien lien n'est plus valide
    old = token_from(app, "nora@example.com")
    await client.post(f"{API}/admin/auth/resend-verification", json={"email": "nora@example.com"}, headers={"Accept-Language": "en"})
    assert app.state.mailer.sent[-1].subject.startswith("Confirm your email")
    assert (await client.post(f"{API}/admin/auth/verify-email", json={"token": old})).json()["code"] == "token_invalid"
    # Adresse inconnue : même réponse, aucun e-mail
    r = await client.post(f"{API}/admin/auth/resend-verification", json={"email": "ghost@example.com"})
    assert r.status_code == 202 and len(app.state.mailer.sent) == 2

    r = await client.post(f"{API}/admin/auth/verify-email", json={"token": token_from(app, "nora@example.com")})
    session = r.json()
    assert session["admin"]["role"] == "owner" and session["admin"]["account"]["name"] == "Studio Nova"
    me = (await client.get(f"{API}/admin/auth/me", headers=bearer(session))).json()
    assert me["email_verified"] is True and me["account"]["name"] == "Studio Nova"
    r = await client.post(f"{API}/admin/auth/login", json={"email": "nora@example.com", "password": "owner-pass-1"})
    assert r.status_code == 200


async def test_accounts_are_isolated(app, client, admin_headers):
    nova = await signup(client, app, "nora@example.com", "Studio Nova")
    orbit = await signup(client, app, "omar@example.com", "Orbit Labs", name="Omar")
    a = await create_app(client, nova, name="Nova Notes")
    await create_app(client, orbit, name="Orbit Maps")

    assert [x["name"] for x in (await client.get(f"{API}/admin/apps", headers=nova)).json()["items"]] == ["Nova Notes"]
    assert (await client.get(f"{API}/admin/apps/{a['id']}", headers=orbit)).status_code == 404
    assert (await client.get(f"{API}/admin/apps/{a['id']}/versions", headers=orbit)).status_code == 404
    assert (await client.patch(f"{API}/admin/apps/{a['id']}", json={"name": "x"}, headers=orbit)).status_code == 404
    assert (await client.get(f"{API}/admin/stats/downloads", params={"app_id": a["id"]}, headers=orbit)).status_code == 404
    assert (await client.get(f"{API}/admin/stats/overview", headers=orbit)).json()["apps"]["draft"] == 1

    # Journal : chaque compte ne voit que le sien
    orbit_log = (await client.get(f"{API}/admin/activity", headers=orbit)).json()["items"]
    assert all(e["actor_name"] == "Omar" for e in orbit_log)

    # L'administrateur de la plateforme voit tous les comptes et toutes les apps
    names = {x["name"]: x["account_name"] for x in (await client.get(f"{API}/admin/apps", headers=admin_headers)).json()["items"]}
    assert names == {"Nova Notes": "Studio Nova", "Orbit Maps": "Orbit Labs"}
    accounts = {x["name"]: x for x in (await client.get(f"{API}/admin/accounts", headers=admin_headers)).json()}
    assert accounts["Studio Nova"]["apps_count"] == 1 and accounts["Studio Nova"]["owner"]["email"] == "nora@example.com"
    assert accounts["Kaskad"]["platform"] is True
    assert (await client.get(f"{API}/admin/accounts", headers=nova)).status_code == 403

    # Modération : réservée à l'administrateur de la plateforme
    assert (await client.get(f"{API}/admin/moderation/reviews", headers=nova)).status_code == 403
    assert (await client.get(f"{API}/admin/moderation/queue", headers=nova)).status_code == 403

    # Catégories : gérées par la plateforme uniquement
    assert (await client.post(f"{API}/admin/categories", json={"name": "Jeux"}, headers=nova)).status_code == 403
    assert (await client.get(f"{API}/admin/categories", headers=nova)).status_code == 200


async def test_invitations_and_roles(app, client, admin_headers):
    owner = await signup(client, app)
    # Invitation envoyée dans la langue de la console au moment de l'envoi
    r = await client.post(
        f"{API}/admin/invitations", json={"email": "dev@example.com", "role": "developer"}, headers={**owner, "Accept-Language": "en"}
    )
    assert r.status_code == 201 and r.json()["language"] == "en"
    mail = app.state.mailer.sent[-1]
    assert mail.subject == "Nora invited you to Kaskad Console" and "Developer" in mail.text and "/invite/" in mail.text

    lookup = (await client.get(f"{API}/admin/invitations/lookup", params={"token": token_from(app, "dev@example.com")})).json()
    assert lookup == {
        "email": "dev@example.com",
        "role": "developer",
        "account_name": "Studio Nova",
        "invited_by_name": "Nora",
        "already_registered": False,
    }
    token = token_from(app, "dev@example.com")
    r = await client.post(f"{API}/admin/invitations/accept", json={"token": token, "name": "Dev", "password": "member-pass-1"})
    dev = bearer(r.json())
    assert r.json()["admin"]["role"] == "developer" and r.json()["admin"]["account"]["name"] == "Studio Nova"
    # Lien à usage unique
    r = await client.post(f"{API}/admin/invitations/accept", json={"token": token, "name": "X", "password": "member-pass-1"})
    assert r.json()["code"] == "invitation_invalid"

    viewer = await invite(client, app, owner, "viewer@example.com", "viewer", "Vic")

    # Rôles attribuables : développeur ou lecteur seulement (jamais admin ni propriétaire)
    for role in ("admin", "owner"):
        assert (
            await client.post(f"{API}/admin/invitations", json={"email": "x@example.com", "role": role}, headers=owner)
        ).status_code == 422
    r = await client.post(f"{API}/admin/invitations", json={"email": "dev@example.com", "role": "viewer"}, headers=owner)
    assert r.json()["code"] == "already_member"
    assert (
        await client.post(f"{API}/admin/invitations", json={"email": "y@example.com", "role": "viewer"}, headers=dev)
    ).status_code == 403

    # Développeur : gère les apps et soumet, ne publie pas
    a = await create_app(client, dev)
    v = (await upload(client, dev, a["id"])).json()
    await wait_scans(app)
    assert (await client.post(f"{API}/admin/versions/{v['id']}/submit", json={}, headers=dev)).status_code == 200
    assert (await client.post(f"{API}/admin/versions/{v['id']}/publish", headers=dev)).status_code == 403
    # La plateforme valide
    assert (await client.post(f"{API}/admin/versions/{v['id']}/publish", headers=admin_headers)).status_code == 200

    # Journal d'activité : réservé au propriétaire (toute l'activité du compte, dont celle des membres)
    assert (await client.get(f"{API}/admin/activity", headers=dev)).status_code == 403
    assert (await client.get(f"{API}/admin/activity", headers=viewer)).status_code == 403
    log = (await client.get(f"{API}/admin/activity", headers=owner)).json()["items"]
    actions = {(e["actor_name"], e["action"]) for e in log}
    assert {("Dev", "app.created"), ("Dev", "version.submitted"), ("Dev", "member.joined")} <= actions
    assert ("Administrator", "version.published") in actions  # validation par la plateforme sur l'app du compte

    # Lecteur : consultation seule
    assert (await client.get(f"{API}/admin/apps/{a['id']}", headers=viewer)).status_code == 200
    r = await client.post(f"{API}/admin/apps", json={"name": "Nope"}, headers=viewer)
    assert r.status_code == 403 and r.json()["code"] == "read_only"
    assert (await client.patch(f"{API}/admin/apps/{a['id']}", json={"name": "x"}, headers=viewer)).json()["code"] == "read_only"

    # Gestion des membres par le propriétaire
    members = {m["email"]: m for m in (await client.get(f"{API}/admin/members", headers=owner)).json()}
    assert {m["role"] for m in members.values()} == {"owner", "developer", "viewer"}
    r = await client.patch(f"{API}/admin/members/{members['viewer@example.com']['id']}", json={"role": "developer"}, headers=owner)
    assert r.json()["role"] == "developer"
    assert (
        await client.patch(f"{API}/admin/members/{members['owner@example.com']['id']}", json={"role": "viewer"}, headers=owner)
    ).status_code == 403
    assert (
        await client.patch(f"{API}/admin/members/{members['dev@example.com']['id']}", json={"role": "admin"}, headers=owner)
    ).status_code == 422
    await client.patch(f"{API}/admin/members/{members['dev@example.com']['id']}", json={"active": False}, headers=owner)
    assert (await client.get(f"{API}/admin/auth/me", headers=dev)).status_code == 401

    # Invitations en attente : renvoi et annulation
    r = await client.post(f"{API}/admin/invitations", json={"email": "late@example.com", "role": "viewer"}, headers=owner)
    inv = r.json()
    first = token_from(app, "late@example.com")
    await client.post(f"{API}/admin/invitations/{inv['id']}/resend", headers={**owner, "Accept-Language": "fr"})
    assert app.state.mailer.sent[-1].subject == "Nora vous invite sur Kaskad Console"
    assert (await client.get(f"{API}/admin/invitations/lookup", params={"token": first})).status_code == 400
    assert [i["email"] for i in (await client.get(f"{API}/admin/invitations", headers=owner)).json()] == ["late@example.com"]
    assert (await client.delete(f"{API}/admin/invitations/{inv['id']}", headers=owner)).status_code == 204
    assert (await client.get(f"{API}/admin/invitations/lookup", params={"token": token_from(app, "late@example.com")})).status_code == 400


async def test_platform_admin_is_unique(app, client, admin_headers):
    me = (await client.get(f"{API}/admin/auth/me", headers=admin_headers)).json()
    assert me["role"] == "admin" and me["account"]["name"] == "Kaskad"
    members = (await client.get(f"{API}/admin/members", headers=admin_headers)).json()
    r = await client.patch(f"{API}/admin/members/{members[0]['id']}", json={"active": False}, headers=admin_headers)
    assert r.status_code == 403
    # Les anciens rôles sont migrés au démarrage : un second « admin » devient développeur
    db = app.state.db
    await db.admins.insert_one({"email": "old@example.com", "name": "Old", "role": "admin", "password_hash": "x"})
    from app.services.accounts import bootstrap

    await bootstrap(db)
    old = await db.admins.find_one({"email": "old@example.com"})
    assert old["role"] == "developer" and old["account_id"] == (await db.admins.find_one({"role": "admin"}))["account_id"]
    assert await db.admins.count_documents({"role": "admin"}) == 1


async def test_statistics_only_cover_the_members_account(app, client, admin_headers):
    from bson import ObjectId

    from app.models.common import now

    nova = await signup(client, app, "nora@example.com", "Studio Nova")
    orbit = await signup(client, app, "omar@example.com", "Orbit Labs", name="Omar")
    a = await create_app(client, nova, name="Nova Notes")
    b = await create_app(client, orbit, name="Orbit Maps")
    db = app.state.db
    rows = [{"app_id": ObjectId(a["id"]), "platform": "linux"}] * 3 + [{"app_id": ObjectId(b["id"]), "platform": "windows"}] * 5
    await db.download_stats.insert_many(
        [{**r, "version_id": ObjectId(), "file_format": "deb" if r["platform"] == "linux" else "exe", "timestamp": now()} for r in rows]
    )

    async def get(path, headers, **params):
        return (await client.get(f"{API}/admin/stats/{path}", params=params, headers=headers)).json()

    assert sum(p["count"] for p in await get("downloads", nova)) == 3
    assert await get("breakdown", nova, by="platform") == [{"key": "linux", "count": 3}]
    assert [t["name"] for t in await get("top-apps", nova)] == ["Nova Notes"]
    assert (await get("overview", nova))["total_downloads"] == 3
    # Un membre ne peut pas élargir son périmètre avec account_id
    orbit_id = (await client.get(f"{API}/admin/auth/me", headers=orbit)).json()["account_id"]
    assert sum(p["count"] for p in await get("downloads", nova, account_id=orbit_id)) == 3
    csv = (await client.get(f"{API}/admin/stats/export.csv", headers=nova)).text
    assert csv.count("\n") == 4 and "Orbit" not in csv and "Studio Nova,{}".format(a["id"]) in csv

    # Administrateur de la plateforme : toute la plateforme, ou un compte
    assert sum(p["count"] for p in await get("downloads", admin_headers)) == 8
    assert sum(p["count"] for p in await get("downloads", admin_headers, account_id=orbit_id)) == 5
    assert [t["name"] for t in await get("top-apps", admin_headers)] == ["Orbit Maps", "Nova Notes"]
    apps = (await client.get(f"{API}/admin/apps", params={"account_id": orbit_id}, headers=admin_headers)).json()["items"]
    assert [x["name"] for x in apps] == ["Orbit Maps"]
