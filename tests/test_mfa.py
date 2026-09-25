"""Double authentification de la console (TOTP + codes de secours)."""

from app.core import totp
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD
from tests.helpers import API, bearer, invite, signup, token_from


def code(secret: str, offset: int = 0) -> str:
    return totp.code_at(secret, totp.current_counter() + offset)


async def enable(client, headers, password):
    r = await client.post(f"{API}/admin/auth/2fa/setup", json={"password": password}, headers=headers)
    assert r.status_code == 200, r.text
    secret = r.json()["secret"]
    assert r.json()["otpauth_url"].startswith("otpauth://totp/Kaskad") and f"secret={secret}" in r.json()["otpauth_url"]
    r = await client.post(f"{API}/admin/auth/2fa/enable", json={"code": code(secret)}, headers=headers)
    assert r.status_code == 200, r.text
    return secret, r.json()["recovery_codes"]


def test_totp_rfc6238_vector():
    # RFC 6238, annexe B (SHA-1, secret "12345678901234567890") : T = 59 s → 94287082 (8 chiffres) → 287082
    import base64

    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp.code_at(secret, 59 // 30) == "287082"
    assert totp.verify(secret, "287082", at=59) == 1
    assert totp.verify(secret, "287082", last_counter=1, at=59) is None  # rejeu refusé


async def test_enable_login_recovery_disable(app, client):
    owner = await signup(client, app, "owner@example.com")
    r = await client.post(f"{API}/admin/auth/2fa/setup", json={"password": "wrong"}, headers=owner)
    assert r.status_code == 401
    r = await client.post(f"{API}/admin/auth/2fa/setup", json={"password": "owner-pass-1"}, headers=owner)
    secret = r.json()["secret"]
    assert (await client.post(f"{API}/admin/auth/2fa/enable", json={"code": "000000"}, headers=owner)).json()["code"] == "mfa_invalid_code"
    r = await client.post(f"{API}/admin/auth/2fa/enable", json={"code": code(secret)}, headers=owner)
    codes = r.json()["recovery_codes"]
    assert len(codes) == 10 and r.json()["status"]["enabled"] and r.json()["status"]["recovery_codes_left"] == 10
    assert any(m.to_email == "owner@example.com" and "Double authentification activée" in m.subject for m in app.state.mailer.sent)

    # Connexion en deux étapes
    r = await client.post(f"{API}/admin/auth/login", json={"email": "owner@example.com", "password": "owner-pass-1"})
    assert r.json()["mfa_required"] is True and "access_token" not in r.json()
    token = r.json()["mfa_token"]
    assert (await client.post(f"{API}/admin/auth/login/2fa", json={"mfa_token": token, "code": "123456"})).status_code == 401
    fresh = code(secret, 1)
    r = await client.post(f"{API}/admin/auth/login/2fa", json={"mfa_token": token, "code": fresh})
    assert r.status_code == 200 and r.json()["admin"]["mfa_enabled"] is True
    # Le même code ne sert qu'une fois
    assert (await client.post(f"{API}/admin/auth/login/2fa", json={"mfa_token": token, "code": fresh})).json()["code"] == "mfa_invalid_code"
    assert (await client.post(f"{API}/admin/auth/login/2fa", json={"mfa_token": "not-a-token-at-all", "code": fresh})).status_code == 401

    # Code de secours : une seule utilisation, e-mail d'alerte
    r = await client.post(f"{API}/admin/auth/login/2fa", json={"mfa_token": token, "code": codes[0].upper()})
    assert r.status_code == 200
    session = bearer(r.json())
    assert (await client.post(f"{API}/admin/auth/login/2fa", json={"mfa_token": token, "code": codes[0]})).status_code == 401
    assert (await client.get(f"{API}/admin/auth/2fa", headers=session)).json()["recovery_codes_left"] == 9
    assert any(("Code de secours utilisé" in m.subject or "Recovery code used" in m.subject) for m in app.state.mailer.sent)

    # Nouveaux codes de secours (le temps passe : les codes des périodes suivantes redeviennent utilisables)
    await app.state.db.admins.update_one({"email": "owner@example.com"}, {"$set": {"totp_last_counter": 0}})
    r = await client.post(f"{API}/admin/auth/2fa/recovery-codes", json={"code": code(secret, -1)}, headers=session)
    new_codes = r.json()["recovery_codes"]
    assert len(new_codes) == 10 and not set(new_codes) & set(codes)

    # Mot de passe oublié : la double authentification reste demandée
    await client.post(f"{API}/admin/auth/forgot-password", json={"email": "owner@example.com"})
    r = await client.post(
        f"{API}/admin/auth/reset-password", json={"token": token_from(app, "owner@example.com"), "password": "new-pass-123"}
    )
    assert r.json()["mfa_required"] is True

    # Désactivation : mot de passe + code
    await app.state.db.admins.update_one({"email": "owner@example.com"}, {"$set": {"totp_last_counter": 0}})
    r = await client.post(f"{API}/admin/auth/login", json={"email": "owner@example.com", "password": "new-pass-123"})
    r = await client.post(f"{API}/admin/auth/login/2fa", json={"mfa_token": r.json()["mfa_token"], "code": code(secret)})
    session = bearer(r.json())
    assert (
        await client.post(f"{API}/admin/auth/2fa/disable", json={"password": "bad", "code": code(secret)}, headers=session)
    ).status_code == 401
    r = await client.post(f"{API}/admin/auth/2fa/disable", json={"password": "new-pass-123", "code": codes[1]}, headers=session)
    assert r.json()["code"] == "mfa_invalid_code"  # anciens codes de secours invalidés
    r = await client.post(f"{API}/admin/auth/2fa/disable", json={"password": "new-pass-123", "code": new_codes[0]}, headers=session)
    assert r.status_code == 200 and r.json()["enabled"] is False
    r = await client.post(f"{API}/admin/auth/login", json={"email": "owner@example.com", "password": "new-pass-123"})
    assert "access_token" in r.json()


async def test_platform_admin_must_set_up_2fa(app, client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("ADMIN_REQUIRE_2FA", "true")
    get_settings.cache_clear()
    r = await client.post(f"{API}/admin/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    admin = bearer(r.json())
    assert r.json()["admin"]["mfa_setup_required"] is True
    r = await client.get(f"{API}/admin/apps", headers=admin)
    assert r.status_code == 403 and r.json()["code"] == "mfa_setup_required"
    assert (await client.get(f"{API}/admin/auth/me", headers=admin)).json()["mfa_setup_required"] is True
    secret, _ = await enable(client, admin, ADMIN_PASSWORD)
    assert (await client.get(f"{API}/admin/apps", headers=admin)).status_code == 200
    r = await client.post(f"{API}/admin/auth/2fa/disable", json={"password": ADMIN_PASSWORD, "code": code(secret, 1)}, headers=admin)
    assert r.status_code == 403 and r.json()["code"] == "mfa_cannot_disable"


async def test_owner_requires_2fa_and_resets_member(app, client):
    owner = await signup(client, app, "owner@example.com")
    dev = await invite(client, app, owner, "dev@example.com")
    r = await client.patch(f"{API}/admin/account", json={"require_2fa": True}, headers=owner)
    assert r.status_code == 400 and r.json()["code"] == "mfa_enable_first"
    await enable(client, owner, "owner-pass-1")
    r = await client.patch(f"{API}/admin/account", json={"require_2fa": True}, headers=owner)
    assert r.json()["require_2fa"] is True
    # Le développeur doit configurer la double authentification avant d'accéder à la console
    assert (await client.get(f"{API}/admin/apps", headers=dev)).json()["code"] == "mfa_setup_required"
    await enable(client, dev, "member-pass-1")
    assert (await client.get(f"{API}/admin/apps", headers=dev)).status_code == 200
    members = (await client.get(f"{API}/admin/members", headers=owner)).json()
    dev_member = next(m for m in members if m["email"] == "dev@example.com")
    assert dev_member["mfa_enabled"] is True
    # Appareil perdu : le propriétaire réinitialise ; les sessions du membre sont fermées
    r = await client.post(f"{API}/admin/members/{dev_member['id']}/reset-2fa", headers=owner)
    assert r.status_code == 200 and r.json()["mfa_enabled"] is False
    assert (await client.get(f"{API}/admin/apps", headers=dev)).json()["code"] == "mfa_setup_required"
    assert any(m.to_email == "dev@example.com" and ("réinitialisée" in m.subject or "reset" in m.subject) for m in app.state.mailer.sent)
    # Un développeur ne réinitialise rien ; personne ne réinitialise le propriétaire sauf l'administrateur
    owner_id = next(m for m in members if m["email"] == "owner@example.com")["id"]
    r = await client.post(f"{API}/admin/auth/login", json={"email": "dev@example.com", "password": "member-pass-1"})
    dev = bearer(r.json())
    assert (await client.post(f"{API}/admin/members/{owner_id}/reset-2fa", headers=dev)).status_code == 403
