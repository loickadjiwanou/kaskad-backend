"""Contenu des e-mails (français / anglais), dans la langue de la console au moment de l'envoi."""

from html import escape

from app.services.mailer import Email

PRIMARY = "#2F6BFF"

ROLE_LABELS = {
    "fr": {"developer": "Développeur", "viewer": "Lecteur"},
    "en": {"developer": "Developer", "viewer": "Viewer"},
}

TEXTS = {
    "verify": {
        "fr": {
            "subject": "Confirmez votre adresse e-mail — Kaskad Console",
            "title": "Bienvenue sur Kaskad Console",
            "greeting": "Bonjour {name},",
            "body": "Votre compte développeur « {account} » a bien été créé. Confirmez votre adresse e-mail pour vous connecter à la console.",
            "button": "Confirmer mon adresse e-mail",
            "expiry": "Ce lien est valable {hours} heures.",
            "ignore": "Si vous n'êtes pas à l'origine de cette inscription, ignorez simplement cet e-mail.",
        },
        "en": {
            "subject": "Confirm your email address — Kaskad Console",
            "title": "Welcome to Kaskad Console",
            "greeting": "Hi {name},",
            "body": "Your developer account “{account}” has been created. Confirm your email address to sign in to the console.",
            "button": "Confirm my email address",
            "expiry": "This link is valid for {hours} hours.",
            "ignore": "If you didn't sign up, you can safely ignore this email.",
        },
    },
    "invite": {
        "fr": {
            "subject": "{inviter} vous invite sur Kaskad Console",
            "title": "Invitation à rejoindre « {account} »",
            "greeting": "Bonjour,",
            "body": "{inviter} vous invite à rejoindre le compte développeur « {account} » sur Kaskad Console, avec le rôle {role}.",
            "button": "Accepter l'invitation",
            "expiry": "Cette invitation est valable {days} jours.",
            "ignore": "Si vous ne connaissez pas cette personne, ignorez simplement cet e-mail.",
        },
        "en": {
            "subject": "{inviter} invited you to Kaskad Console",
            "title": "Invitation to join “{account}”",
            "greeting": "Hi,",
            "body": "{inviter} invited you to join the developer account “{account}” on Kaskad Console, with the {role} role.",
            "button": "Accept the invitation",
            "expiry": "This invitation is valid for {days} days.",
            "ignore": "If you don't know this person, you can safely ignore this email.",
        },
    },
}

FOOTER = {
    "fr": "Kaskad Console — la console de publication du store Kaskad.",
    "en": "Kaskad Console — the publishing console of the Kaskad store.",
}
LINK_HELP = {
    "fr": "Si le bouton ne fonctionne pas, copiez ce lien dans votre navigateur :",
    "en": "If the button doesn't work, copy this link into your browser:",
}


def _layout(lang: str, title: str, paragraphs: list[str], button: str, url: str, small: list[str]) -> str:
    p = "".join(f'<p style="margin:0 0 16px;font-size:15px;line-height:1.6;color:#334155">{escape(x)}</p>' for x in paragraphs)
    s = "".join(f'<p style="margin:0 0 8px;font-size:13px;line-height:1.5;color:#64748B">{escape(x)}</p>' for x in small)
    return f"""<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)}</title></head>
<body style="margin:0;padding:0;background:#F1F5F9;font-family:Inter,Segoe UI,Helvetica,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F1F5F9;padding:32px 16px"><tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#FFFFFF;border-radius:16px;overflow:hidden;border:1px solid #E2E8F0">
<tr><td style="background:{PRIMARY};padding:24px 32px;color:#FFFFFF;font-size:20px;font-weight:800;letter-spacing:-0.3px">Kaskad <span style="font-weight:600;opacity:.8;font-size:13px;letter-spacing:1.5px">CONSOLE</span></td></tr>
<tr><td style="padding:32px">
<h1 style="margin:0 0 20px;font-size:22px;line-height:1.3;color:#0F172A">{escape(title)}</h1>
{p}
<table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px 0 24px"><tr><td style="border-radius:999px;background:{PRIMARY}">
<a href="{escape(url)}" style="display:inline-block;padding:13px 28px;color:#FFFFFF;font-size:15px;font-weight:700;text-decoration:none;border-radius:999px">{escape(button)}</a>
</td></tr></table>
{s}
<p style="margin:16px 0 0;font-size:12px;line-height:1.5;color:#94A3B8">{escape(LINK_HELP[lang])}<br><a href="{escape(url)}" style="color:{PRIMARY};word-break:break-all">{escape(url)}</a></p>
</td></tr>
<tr><td style="padding:16px 32px;border-top:1px solid #E2E8F0;font-size:12px;color:#94A3B8">{escape(FOOTER[lang])}</td></tr>
</table></td></tr></table></body></html>"""


def _text(title: str, paragraphs: list[str], button: str, url: str, small: list[str]) -> str:
    return "\n\n".join([title, *paragraphs, f"{button} : {url}", *small])


def verification_email(lang: str, to_email: str, name: str, account: str, url: str, hours: int) -> Email:
    t = TEXTS["verify"][lang]
    paragraphs = [t["greeting"].format(name=name), t["body"].format(account=account)]
    small = [t["expiry"].format(hours=hours), t["ignore"]]
    return Email(
        to_email=to_email,
        to_name=name,
        subject=t["subject"],
        html=_layout(lang, t["title"], paragraphs, t["button"], url, small),
        text=_text(t["title"], paragraphs, t["button"], url, small),
    )


def invitation_email(lang: str, to_email: str, inviter: str, account: str, role: str, url: str, days: int) -> Email:
    t = TEXTS["invite"][lang]
    role_label = ROLE_LABELS[lang][role]
    title = t["title"].format(account=account)
    paragraphs = [t["greeting"], t["body"].format(inviter=inviter, account=account, role=role_label)]
    small = [t["expiry"].format(days=days), t["ignore"]]
    return Email(
        to_email=to_email,
        to_name=None,
        subject=t["subject"].format(inviter=inviter),
        html=_layout(lang, title, paragraphs, t["button"], url, small),
        text=_text(title, paragraphs, t["button"], url, small),
    )
