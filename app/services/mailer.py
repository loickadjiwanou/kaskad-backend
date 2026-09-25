"""Envoi des e-mails transactionnels via Brevo.

Deux transports, au choix dans la configuration :
- clé API HTTP (`BREVO_API_KEY`, « xkeysib-… ») : https://developers.brevo.com/reference/sendtransacemail ;
- relais SMTP (`BREVO_SMTP_LOGIN` + `BREVO_SMTP_KEY`, « xsmtpsib-… ») : smtp-relay.brevo.com, port 587 (STARTTLS).

Sans l'un ni l'autre (développement), les e-mails ne sont pas envoyés : leur contenu texte est écrit dans les logs,
ce qui permet de suivre les liens de confirmation et d'invitation en local.
"""

import asyncio
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

import httpx

from app.core.config import Settings

log = logging.getLogger("kaskad.mail")

BREVO_URL = "https://api.brevo.com/v3/smtp/email"


@dataclass
class Email:
    to_email: str
    to_name: str | None
    subject: str
    html: str
    text: str


class Mailer:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def transport(self) -> str | None:
        s = self.settings
        if not s.mail_sender_email:
            return None
        if s.brevo_api_key:
            return "api"
        if s.brevo_smtp_login and s.brevo_smtp_key:
            return "smtp"
        return None

    @property
    def configured(self) -> bool:
        return self.transport is not None

    async def send(self, email: Email) -> None:
        """Envoie l'e-mail ; une erreur d'envoi est journalisée sans interrompre la requête."""
        transport = self.transport
        if transport is None:
            log.warning("Brevo not configured, email not sent.\nTo: %s\nSubject: %s\n\n%s", email.to_email, email.subject, email.text)
            return
        try:
            if transport == "api":
                await self._send_api(email)
            else:
                await asyncio.to_thread(self._send_smtp, email)
        except (httpx.HTTPError, smtplib.SMTPException, OSError) as e:
            log.error("Email to %s failed (%s): %s", email.to_email, transport, e)

    async def _send_api(self, email: Email) -> None:
        s = self.settings
        payload = {
            "sender": {"name": s.mail_sender_name, "email": s.mail_sender_email},
            "to": [{"email": email.to_email, **({"name": email.to_name} if email.to_name else {})}],
            "subject": email.subject,
            "htmlContent": email.html,
            "textContent": email.text,
        }
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(BREVO_URL, json=payload, headers={"api-key": s.brevo_api_key, "accept": "application/json"})
        if r.status_code >= 400:
            log.error("Brevo API error %s for %s: %s", r.status_code, email.to_email, r.text[:500])

    def message(self, email: Email) -> EmailMessage:
        """Message MIME multipart (texte + HTML)."""
        s = self.settings
        msg = EmailMessage()
        msg["From"] = formataddr((s.mail_sender_name, s.mail_sender_email))
        msg["To"] = formataddr((email.to_name, email.to_email)) if email.to_name else email.to_email
        msg["Subject"] = email.subject
        msg["Message-ID"] = make_msgid(domain=s.mail_sender_email.split("@")[-1])
        msg.set_content(email.text)
        msg.add_alternative(email.html, subtype="html")
        return msg

    def _send_smtp(self, email: Email) -> None:
        s = self.settings
        context = ssl.create_default_context()
        if s.brevo_smtp_port == 465:
            server = smtplib.SMTP_SSL(s.brevo_smtp_host, s.brevo_smtp_port, timeout=20, context=context)
        else:
            server = smtplib.SMTP(s.brevo_smtp_host, s.brevo_smtp_port, timeout=20)
        with server:
            if s.brevo_smtp_port != 465:
                server.starttls(context=context)
            server.login(s.brevo_smtp_login, s.brevo_smtp_key)
            server.send_message(self.message(email))
