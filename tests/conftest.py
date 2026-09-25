import re
import tempfile
import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from pymongo_inmemory.context import Context
from pymongo_inmemory.mongod import Mongod

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "admin-password"


@pytest.fixture(scope="session")
def mongo_uri():
    """Vrai mongod (téléchargé une fois par pymongo_inmemory), partagé par toute la session de tests."""
    mongod = Mongod(Context())
    mongod.start()
    try:
        yield mongod.connection_string
    finally:
        mongod.stop()


@pytest.fixture
async def app(mongo_uri, monkeypatch):
    storage_dir = tempfile.mkdtemp(prefix="kaskad-storage-")
    env = {
        "ENVIRONMENT": "test",
        "MONGODB_URI": mongo_uri,
        "MONGODB_DB": f"kaskad_test_{uuid.uuid4().hex[:8]}",  # base isolée par test
        "LOCAL_STORAGE_DIR": storage_dir,
        "STORAGE_BACKEND": "local",
        "JWT_SECRET": "test-secret-for-the-kaskad-api-suite-0123456789",
        "ADMIN_EMAIL": ADMIN_EMAIL,
        "ADMIN_PASSWORD": ADMIN_PASSWORD,
        "PUBLIC_BASE_URL": "http://test",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for k in ("CLAMAV_HOST", "VIRUSTOTAL_API_KEY", "FIREBASE_CREDENTIALS_FILE"):
        monkeypatch.delenv(k, raising=False)

    from app.core.config import Settings, get_settings

    # Les tests n'utilisent jamais le fichier .env du développeur (identifiants, stockage S3…)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    from app.main import create_app

    application = create_app()
    async with LifespanManager(application):
        application.state.mailer = FakeMailer()  # e-mails capturés (jamais envoyés à Brevo)
        application.state.scan_queue.mailer = application.state.mailer
        yield application
    get_settings.cache_clear()


class FakeMailer:
    configured = True

    def __init__(self):
        self.sent = []

    async def send(self, email) -> None:
        self.sent.append(email)

    def last_link(self, to: str) -> str:
        email = next(e for e in reversed(self.sent) if e.to_email == to)
        return re.search(r"https?://\S+", email.text).group(0)


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def admin_headers(client):
    r = await client.post("/api/v1/admin/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# Fichiers de test minimaux dont le contenu correspond au format annoncé (jamais exécutés)
SAMPLE_FILES = {
    "deb": ("app.deb", b"!<arch>\n" + b"debian-binary   " + b"0" * 2000),
    "exe": ("setup.exe", b"MZ" + b"\x00" * 4000),
    "appimage": ("app.AppImage", b"\x7fELF" + b"\x00" * 3000),
    "rpm": ("app.rpm", bytes.fromhex("EDABEEDB") + b"\x00" * 1000),
    "dmg": ("app.dmg", b"\x00" * 2000 + b"koly" + b"\x00" * 508),
}
