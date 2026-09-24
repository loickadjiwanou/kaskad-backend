"""Notifications push à la publication d'une nouvelle version, pour les utilisateurs qui suivent l'app.

- Android : Firebase Cloud Messaging (API HTTP v1, compte de service).
- iOS : APNs directement (l'app iOS enregistre des jetons APNs natifs, non utilisables par FCM).
Les jetons refusés par FCM / APNs (appareil désinstallé…) sont supprimés.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx
import jwt
from anyio import to_thread
from bson import ObjectId
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import Settings

log = logging.getLogger("kaskad.push")

TEXTS = {
    "fr": ("Mise à jour disponible", "{name} {version} est disponible au téléchargement."),
    "en": ("Update available", "{name} {version} is available to download."),
}


class FcmSender:
    SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

    def __init__(self, credentials_file: str):
        from google.oauth2 import service_account

        self.credentials = service_account.Credentials.from_service_account_file(credentials_file, scopes=[self.SCOPE])
        self.project_id = json.loads(Path(credentials_file).read_text())["project_id"]

    async def _token(self) -> str:
        if not self.credentials.valid:
            from google.auth.transport.requests import Request

            await to_thread.run_sync(self.credentials.refresh, Request())
        return self.credentials.token

    async def send(self, client: httpx.AsyncClient, token: str, title: str, body: str, data: dict) -> bool:
        """Retourne False si le jeton n'est plus valide."""
        url = f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send"
        message = {
            "message": {
                "token": token,
                "notification": {"title": title, "body": body},
                "data": {k: str(v) for k, v in data.items()},
                "android": {"priority": "high"},
            }
        }
        r = await client.post(url, json=message, headers={"Authorization": f"Bearer {await self._token()}"})
        if r.status_code in (404, 410) or "UNREGISTERED" in r.text:
            return False
        if r.status_code >= 400:
            log.warning("FCM error %s: %s", r.status_code, r.text[:300])
        return True


class ApnsSender:
    def __init__(self, s: Settings):
        self.key = Path(s.apns_key_file).read_text()
        self.key_id, self.team_id, self.topic = s.apns_key_id, s.apns_team_id, s.apns_bundle_id
        self.host = "https://api.sandbox.push.apple.com" if s.apns_use_sandbox else "https://api.push.apple.com"
        self._jwt: tuple[str, float] | None = None

    def _auth(self) -> str:
        # Jeton fournisseur APNs (ES256), valable 1 h : renouvelé toutes les 50 min
        if not self._jwt or time.time() - self._jwt[1] > 3000:
            token = jwt.encode({"iss": self.team_id, "iat": int(time.time())}, self.key, algorithm="ES256", headers={"kid": self.key_id})
            self._jwt = (token, time.time())
        return self._jwt[0]

    async def send(self, client: httpx.AsyncClient, token: str, title: str, body: str, data: dict) -> bool:
        payload = {"aps": {"alert": {"title": title, "body": body}, "sound": "default"}, **data}
        r = await client.post(
            f"{self.host}/3/device/{token}",
            json=payload,
            headers={
                "authorization": f"bearer {self._auth()}",
                "apns-topic": self.topic,
                "apns-push-type": "alert",
                "apns-priority": "10",
            },
        )
        if r.status_code == 410 or (r.status_code == 400 and "BadDeviceToken" in r.text):
            return False
        if r.status_code >= 400:
            log.warning("APNs error %s: %s", r.status_code, r.text[:300])
        return True


class PushService:
    def __init__(self, settings: Settings):
        self.fcm = self.apns = None
        if settings.firebase_credentials_file:
            try:
                self.fcm = FcmSender(settings.firebase_credentials_file)
            except Exception:
                log.exception("FCM disabled: invalid credentials file")
        if settings.apns_key_file and settings.apns_key_id and settings.apns_team_id:
            try:
                self.apns = ApnsSender(settings)
            except Exception:
                log.exception("APNs disabled: invalid key")

    @property
    def enabled(self) -> bool:
        return bool(self.fcm or self.apns)

    async def notify_new_version(self, db: AsyncDatabase, app: dict, version: dict) -> dict:
        """Envoie la notification à tous les abonnés (notifications actives) de l'app."""
        stats = {"sent": 0, "invalid": 0, "skipped": 0}
        if not self.enabled:
            return stats
        users = db.users.find(
            {"followed_apps": {"$elemMatch": {"app_id": app["_id"], "notify": True}}, "push_tokens.0": {"$exists": True}},
            {"push_tokens": 1},
        )
        data = {"url": f"/app/{app['_id']}", "app_id": str(app["_id"]), "version_id": str(version["_id"])}
        semaphore = asyncio.Semaphore(20)

        async with httpx.AsyncClient(http2=True, timeout=20) as client:

            async def send_one(user_id: ObjectId, t: dict):
                sender = self.fcm if t["provider"] == "fcm" else self.apns
                if not sender:
                    stats["skipped"] += 1
                    return
                title, body = TEXTS.get(t.get("language") or "fr", TEXTS["fr"])
                body = body.format(name=app["name"], version=version["version_name"])
                async with semaphore:
                    try:
                        ok = await sender.send(client, t["token"], title, body, data)
                    except Exception:
                        log.exception("push send failed")
                        return
                if ok:
                    stats["sent"] += 1
                else:
                    stats["invalid"] += 1
                    await db.users.update_one({"_id": user_id}, {"$pull": {"push_tokens": {"token": t["token"]}}})

            tasks = [send_one(u["_id"], t) async for u in users for t in u.get("push_tokens", [])]
            await asyncio.gather(*tasks)
        return stats
