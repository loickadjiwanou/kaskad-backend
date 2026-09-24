"""Stockage S3 (MinIO / B2), notifications push, « mes apps », suppression de compte, anti brute force, versions sémantiques."""

import io
from datetime import timedelta

import httpx
import pytest
from moto.server import ThreadedMotoServer

from app.core.config import Settings
from app.models.common import now
from app.services.storage import S3Storage
from tests.helpers import API, create_app, published_app_with_version, upload, wait_scans


@pytest.fixture
def s3_server():
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


async def test_s3_storage(s3_server):
    settings = Settings(
        _env_file=None,
        storage_backend="s3",
        s3_endpoint_url=s3_server,
        s3_bucket="kaskad-test",
        s3_access_key="test",
        s3_secret_key="test",
        download_url_ttl_seconds=60,
    )
    storage = S3Storage(settings)
    await storage.init()  # crée le bucket s'il n'existe pas
    await storage.init()  # idempotent

    content = b"!<arch>\n" + b"x" * 5000
    await storage.save("binaries/a/b/app.deb", io.BytesIO(content), "application/vnd.debian.binary-package")
    assert await storage.exists("binaries/a/b/app.deb")

    # URL pré-signée, temporaire, avec nom de fichier de téléchargement
    url = await storage.signed_url("binaries/a/b/app.deb", "Kaskad-1.0.0-linux.deb")
    assert "X-Amz-Signature" in url and "X-Amz-Expires=60" in url
    r = httpx.get(url)
    assert r.status_code == 200 and r.content == content
    assert "Kaskad-1.0.0-linux.deb" in r.headers["content-disposition"]

    async with storage.local_copy("binaries/a/b/app.deb") as path:
        assert path.read_bytes() == content

    [obj] = await storage.list_objects("binaries/")
    assert obj.key == "binaries/a/b/app.deb" and obj.size == len(content)

    # Upload multipart jamais terminé → annulé par le nettoyage
    storage.client.create_multipart_upload(Bucket="kaskad-test", Key="binaries/stale.bin")
    assert await storage.abort_incomplete_uploads(now() + timedelta(minutes=1)) == 1

    await storage.delete("binaries/a/b/app.deb")
    assert not await storage.exists("binaries/a/b/app.deb")


class FakeSender:
    def __init__(self):
        self.sent = []

    async def send(self, client, token, title, body, data):
        self.sent.append({"token": token, "title": title, "body": body, "data": data})
        return token != "dead-token"  # jeton refusé (appareil désinstallé)


async def test_push_notification_on_publish(app, client, admin_headers):
    fake = FakeSender()
    app.state.push.fcm = fake
    _, a, _, v1 = await published_app_with_version(client, app, admin_headers)

    async def follower(device, notify, tokens, language="fr"):
        s = (await client.post(f"{API}/auth/anonymous", json={"device_id": device})).json()
        h = {"Authorization": f"Bearer {s['access_token']}"}
        await client.put(f"{API}/me/library", json={"followed_apps": [{"app_id": a["id"], "notify": notify}]}, headers=h)
        for t in tokens:
            await client.post(f"{API}/me/push-tokens", json={"token": t, "provider": "fcm", "language": language}, headers=h)

    await follower("device-follow-01", True, ["good-token"])
    await follower("device-follow-02", True, ["dead-token"], language="en")
    await follower("device-follow-03", False, ["muted-token"])  # notifications désactivées pour cette app

    v2 = (await upload(client, admin_headers, a["id"], code=200, name="2.0.0")).json()
    await wait_scans(app)
    await client.post(f"{API}/admin/versions/{v2['id']}/publish", headers=admin_headers)

    by_token = {m["token"]: m for m in fake.sent}
    assert set(by_token) == {"good-token", "dead-token"}
    assert by_token["good-token"]["title"] == "Mise à jour disponible"
    assert by_token["good-token"]["body"] == "Kaskad Notes 2.0.0 est disponible au téléchargement."
    assert by_token["dead-token"]["title"] == "Update available"
    assert by_token["good-token"]["data"]["url"] == f"/app/{a['id']}"
    # Le jeton refusé a été supprimé du compte
    user = await app.state.db.users.find_one({"device_id": "device-follow-02"})
    assert user["push_tokens"] == []

    # Publier une version plus ancienne ne renotifie pas
    fake.sent.clear()
    v_old = (await upload(client, admin_headers, a["id"], code=150, name="1.5.0")).json()
    await wait_scans(app)
    await client.post(f"{API}/admin/versions/{v_old['id']}/publish", headers=admin_headers)
    assert fake.sent == []


async def test_my_apps_updates_and_account_deletion(app, client, admin_headers):
    _, a, _, v1 = await published_app_with_version(client, app, admin_headers)
    s = (await client.post(f"{API}/auth/anonymous", json={"device_id": "device-myapps-1"})).json()
    h = {"Authorization": f"Bearer {s['access_token']}"}
    await client.put(
        f"{API}/me/library",
        json={"followed_apps": [{"app_id": a["id"], "notify": True}], "installed_apps": [{"app_id": a["id"], "version_id": v1["id"]}]},
        headers=h,
    )
    [item] = (await client.get(f"{API}/me/apps", headers=h)).json()
    assert item["app"]["id"] == a["id"] and item["installed_version"]["id"] == v1["id"]
    assert item["update_available"] is False and item["followed"] and item["notify"]
    assert (await client.get(f"{API}/me/updates", headers=h)).json() == []

    v2 = (await upload(client, admin_headers, a["id"], code=110, name="1.1.0")).json()
    await wait_scans(app)
    await client.post(f"{API}/admin/versions/{v2['id']}/publish", headers=admin_headers)
    [update] = (await client.get(f"{API}/me/updates", headers=h)).json()
    assert update["latest_version"]["version_name"] == "1.1.0"

    # Récupération groupée d'apps (bibliothèque synchronisée)
    batch = (await client.get(f"{API}/apps", params={"ids": f"{a['id']},000000000000000000000000,bad"})).json()
    assert [x["id"] for x in batch["items"]] == [a["id"]]

    # Suppression du compte : données et sessions supprimées
    assert (await client.delete(f"{API}/me", headers=h)).status_code == 204
    assert await app.state.db.users.find_one({"device_id": "device-myapps-1"}) is None
    assert (await client.get(f"{API}/me", headers=h)).status_code == 401
    r = await client.post(f"{API}/auth/refresh", json={"refresh_token": s["refresh_token"]})
    assert r.status_code == 401


async def test_login_rate_limit(app, client):
    app.state.login_limiter.max_attempts = 3
    body = {"email": "someone@example.com", "password": "wrong-password"}
    codes = [(await client.post(f"{API}/auth/login", json=body)).status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]
    r = await client.post(f"{API}/auth/login", json=body, headers={"Accept-Language": "fr"})
    assert r.json()["code"] == "too_many_attempts" and r.json()["retry_after"] > 0
    # Un autre compte n'est pas bloqué
    other = await client.post(f"{API}/auth/login", json={**body, "email": "other@example.com"})
    assert other.status_code == 401


async def test_successful_logins_do_not_count_against_ip(app, client):
    from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD

    app.state.login_limiter.max_attempts = 2
    app.state.login_limiter.max_per_ip = 3
    for _ in range(10):  # connexions réussies répétées depuis la même IP
        r = await client.post(f"{API}/admin/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        assert r.status_code == 200


async def test_semantic_version_required(app, client, admin_headers):
    a = await create_app(client, admin_headers)
    for bad in ("v1", "1", "latest", "1.2.3.4.5"):
        r = await upload(client, admin_headers, a["id"], name=bad)
        assert r.status_code == 422 and r.json()["code"] == "invalid_version_name", bad
    for i, good in enumerate(("1.0", "1.4.2", "2.0.0-beta.1", "3.1.0+45")):
        assert (await upload(client, admin_headers, a["id"], name=good, code=10 + i)).status_code == 201, good


async def test_anonymous_upgrade_detaches_device(client):
    s = (await client.post(f"{API}/auth/anonymous", json={"device_id": "device-detach-01"})).json()
    h = {"Authorization": f"Bearer {s['access_token']}"}
    await client.post(f"{API}/auth/register", json={"email": "detach@example.com", "password": "password123"}, headers=h)
    again = (await client.post(f"{API}/auth/anonymous", json={"device_id": "device-detach-01"})).json()
    # L'appareil n'ouvre plus le compte email sans mot de passe
    assert again["user"]["id"] != s["user"]["id"] and again["user"]["anonymous"] is True
