from tests.helpers import API, published_app_with_version


async def test_admin_auth_and_refresh_rotation(client):
    from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD

    bad = await client.post(f"{API}/admin/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong"})
    assert bad.status_code == 401
    session = (await client.post(f"{API}/admin/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})).json()
    assert session["admin"]["role"] == "admin"
    me = await client.get(f"{API}/admin/auth/me", headers={"Authorization": f"Bearer {session['access_token']}"})
    assert me.json()["email"] == ADMIN_EMAIL

    new = await client.post(f"{API}/admin/auth/refresh", json={"refresh_token": session["refresh_token"]})
    assert new.status_code == 200
    # Jeton de rafraîchissement à usage unique
    reused = await client.post(f"{API}/admin/auth/refresh", json={"refresh_token": session["refresh_token"]})
    assert reused.status_code == 401
    # Un jeton utilisateur ne donne pas accès à l'administration
    anon = (await client.post(f"{API}/auth/anonymous", json={"device_id": "device-12345678"})).json()
    r = await client.get(f"{API}/admin/apps", headers={"Authorization": f"Bearer {anon['access_token']}"})
    assert r.status_code == 401


async def test_localized_errors(client):
    r = await client.post(f"{API}/auth/login", json={"email": "x@y.fr", "password": "nope"}, headers={"Accept-Language": "fr-FR"})
    assert r.json() == {"detail": "Email ou mot de passe incorrect.", "code": "invalid_credentials"}
    r = await client.post(f"{API}/auth/login", json={"email": "x@y.fr", "password": "nope"})
    assert r.json()["detail"] == "Invalid email or password."


async def test_anonymous_then_register_keeps_account(client):
    anon = (await client.post(f"{API}/auth/anonymous", json={"device_id": "device-abcdef12"})).json()
    assert anon["user"]["anonymous"] is True
    again = (await client.post(f"{API}/auth/anonymous", json={"device_id": "device-abcdef12"})).json()
    assert again["user"]["id"] == anon["user"]["id"]  # même appareil → même compte

    headers = {"Authorization": f"Bearer {anon['access_token']}"}
    reg = (await client.post(f"{API}/auth/register", json={"email": "Me@Example.com", "password": "password123"}, headers=headers)).json()
    assert reg["user"] == {"id": anon["user"]["id"], "email": "me@example.com", "anonymous": False}

    dup = await client.post(f"{API}/auth/register", json={"email": "me@example.com", "password": "password123"})
    assert dup.status_code == 409
    weak = await client.post(f"{API}/auth/register", json={"email": "new@example.com", "password": "short"})
    assert weak.status_code == 422
    login = await client.post(f"{API}/auth/login", json={"email": "me@example.com", "password": "password123"})
    assert login.status_code == 200 and login.json()["user"]["id"] == anon["user"]["id"]


async def test_library_sync_and_push_tokens(app, client, admin_headers):
    _, a, _, v = await published_app_with_version(client, app, admin_headers)
    s = (await client.post(f"{API}/auth/anonymous", json={"device_id": "device-lib-0001"})).json()
    h = {"Authorization": f"Bearer {s['access_token']}"}
    body = {
        "favorites": [a["id"], a["id"], "not-an-id"],
        "followed_apps": [{"app_id": a["id"], "notify": True}],
        "installed_apps": [{"app_id": a["id"], "version_id": v["id"]}],
    }
    lib = (await client.put(f"{API}/me/library", json=body, headers=h)).json()
    assert lib["favorites"] == [a["id"]]
    assert lib["followed_apps"] == [{"app_id": a["id"], "notify": True}]
    assert (await client.get(f"{API}/me/library", headers=h)).json() == lib

    r = await client.post(f"{API}/me/push-tokens", json={"token": "fcm-token-123", "provider": "fcm", "platform": "android"}, headers=h)
    assert r.status_code == 204
    user = await app.state.db.users.find_one({"device_id": "device-lib-0001"})
    assert user["push_tokens"][0]["token"] == "fcm-token-123"
    assert (await client.get(f"{API}/me")).status_code == 401


async def test_roles_and_last_admin_protection(client, admin_headers):
    r = await client.post(
        f"{API}/admin/admins",
        json={"email": "editor@example.com", "password": "editor-pass", "name": "Ed", "role": "editor"},
        headers=admin_headers,
    )
    editor = r.json()
    ed_session = (await client.post(f"{API}/admin/auth/login", json={"email": "editor@example.com", "password": "editor-pass"})).json()
    ed = {"Authorization": f"Bearer {ed_session['access_token']}"}
    # L'éditeur gère le contenu mais pas les comptes
    assert (await client.post(f"{API}/admin/categories", json={"name": "Jeux"}, headers=ed)).status_code == 201
    assert (await client.get(f"{API}/admin/admins", headers=ed)).status_code == 403

    admins = (await client.get(f"{API}/admin/admins", headers=admin_headers)).json()
    me = next(x for x in admins if x["role"] == "admin")
    r = await client.patch(f"{API}/admin/admins/{me['id']}", json={"role": "editor"}, headers=admin_headers)
    assert r.status_code == 409 and r.json()["code"] == "last_admin"

    # Compte désactivé : sessions révoquées, connexion refusée
    await client.patch(f"{API}/admin/admins/{editor['id']}", json={"active": False}, headers=admin_headers)
    assert (await client.get(f"{API}/admin/auth/me", headers=ed)).status_code == 401
    r = await client.post(f"{API}/admin/auth/login", json={"email": "editor@example.com", "password": "editor-pass"})
    assert r.status_code == 403
