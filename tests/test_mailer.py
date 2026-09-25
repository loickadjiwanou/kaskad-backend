"""Transports e-mail Brevo : relais SMTP (STARTTLS) et journalisation quand rien n'est configuré."""

import logging

from app.core.config import Settings
from app.services import mailer as mailer_module
from app.services.emails import verification_email
from app.services.mailer import Mailer


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.calls, self.sent = host, port, [], []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("quit")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        self.sent.append(msg)


def settings(**kw):
    base = {"brevo_smtp_login": None, "brevo_smtp_key": None, "brevo_api_key": None, "mail_sender_email": None}
    return Settings(_env_file=None, **{**base, **kw})


async def test_smtp_transport(monkeypatch):
    monkeypatch.setattr(mailer_module.smtplib, "SMTP", FakeSMTP)
    m = Mailer(settings(brevo_smtp_login="login@smtp-brevo.com", brevo_smtp_key="xsmtpsib-key", mail_sender_email="team@kaskad.dev"))
    assert m.transport == "smtp"
    await m.send(verification_email("fr", "nora@example.com", "Nora", "Studio Nova", "http://c/verify-email?token=t", 48))
    [server] = FakeSMTP.instances
    assert (server.host, server.port) == ("smtp-relay.brevo.com", 587)
    assert server.calls[:2] == ["starttls", ("login", "login@smtp-brevo.com", "xsmtpsib-key")]
    [msg] = server.sent
    assert msg["From"] == "Kaskad <team@kaskad.dev>" and msg["To"] == "Nora <nora@example.com>"
    assert msg["Subject"].startswith("Confirmez votre adresse")
    assert {p.get_content_type() for p in msg.iter_parts()} == {"text/plain", "text/html"}


async def test_api_key_takes_precedence_and_missing_config_logs(caplog):
    assert (
        Mailer(settings(brevo_api_key="xkeysib-x", brevo_smtp_login="l", brevo_smtp_key="k", mail_sender_email="a@b.c")).transport == "api"
    )
    # Sans expéditeur, rien n'est envoyé
    assert Mailer(settings(brevo_smtp_login="l", brevo_smtp_key="k")).transport is None
    m = Mailer(settings())
    with caplog.at_level(logging.WARNING, logger="kaskad.mail"):
        await m.send(verification_email("en", "x@example.com", "X", "Acme", "http://c/verify-email?token=t", 48))
    assert "Brevo not configured" in caplog.text and "http://c/verify-email?token=t" in caplog.text
