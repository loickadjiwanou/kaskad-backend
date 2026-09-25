from tests.conftest import SAMPLE_FILES

API = "/api/v1"


async def create_category(client, headers, name="Productivité", icon="briefcase-outline"):
    r = await client.post(f"{API}/admin/categories", json={"name": name, "icon": icon}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


async def create_app(client, headers, name="Kaskad Notes", category_ids=(), featured=False):
    body = {
        "name": name,
        "short_description": "Vos notes synchronisées.",
        "long_description": "Un bloc-notes rapide.",
        "category_ids": list(category_ids),
        "target_platforms": ["linux", "windows"],
        "featured": featured,
    }
    r = await client.post(f"{API}/admin/apps", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


async def upload(client, headers, app_id, fmt="deb", platform="linux", code=100, name="1.0.0", content=None, filename=None):
    default_name, default_content = SAMPLE_FILES.get(fmt, (f"app.{fmt}", b"x" * 100))
    files = {"file": (filename or default_name, content if content is not None else default_content, "application/octet-stream")}
    data = {"version_name": name, "version_code": str(code), "platform": platform, "file_format": fmt, "changelog": "• Nouveautés"}
    return await client.post(f"{API}/admin/apps/{app_id}/versions", data=data, files=files, headers=headers)


async def wait_scans(app):
    await app.state.scan_queue.join()


async def published_app_with_version(client, app, headers, fmt="deb", platform="linux"):
    cat = await create_category(client, headers)
    a = await create_app(client, headers, category_ids=[cat["id"]], featured=True)
    v = (await upload(client, headers, a["id"], fmt=fmt, platform=platform)).json()
    await wait_scans(app)
    r = await client.post(f"{API}/admin/versions/{v['id']}/publish", headers=headers)
    assert r.status_code == 200, r.text
    r = await client.post(f"{API}/admin/apps/{a['id']}/status", json={"status": "published"}, headers=headers)
    assert r.status_code == 200
    return cat, a, r.json(), v


def token_from(app, email: str) -> str:
    """Jeton du dernier lien envoyé à cette adresse (confirmation ou invitation)."""
    link = app.state.mailer.last_link(email)
    return link.rsplit("token=", 1)[-1] if "token=" in link else link.rsplit("/", 1)[-1]


def bearer(session: dict) -> dict:
    return {"Authorization": f"Bearer {session['access_token']}"}


async def signup(client, app, email="owner@example.com", account="Studio Nova", name="Nora", lang="fr"):
    """Inscription + confirmation de l'e-mail → en-têtes du propriétaire du nouveau compte."""
    body = {"name": name, "email": email, "password": "owner-pass-1", "account_name": account}
    r = await client.post(f"{API}/admin/auth/signup", json=body, headers={"Accept-Language": lang})
    assert r.status_code == 201, r.text
    r = await client.post(f"{API}/admin/auth/verify-email", json={"token": token_from(app, email)})
    assert r.status_code == 200, r.text
    return bearer(r.json())


async def invite(client, app, headers, email, role="developer", name="Dev"):
    """Invitation + acceptation → en-têtes du nouveau membre."""
    r = await client.post(f"{API}/admin/invitations", json={"email": email, "role": role}, headers=headers)
    assert r.status_code == 201, r.text
    r = await client.post(
        f"{API}/admin/invitations/accept", json={"token": token_from(app, email), "name": name, "password": "member-pass-1"}
    )
    assert r.status_code == 200, r.text
    return bearer(r.json())
