"""Contenu des e-mails (français / anglais), dans la langue de la console au moment de l'envoi."""

from datetime import datetime
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
    return "\n\n".join([title, *paragraphs, f"{button}\n{url}", *small])


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


# ---------------------------------------------------------------- mot de passe oublié

RESET = {
    "fr": {
        "subject": "Réinitialisez votre mot de passe — Kaskad Console",
        "title": "Réinitialisation du mot de passe",
        "greeting": "Bonjour {name},",
        "body": "Nous avons reçu une demande de réinitialisation du mot de passe de votre compte Kaskad Console.",
        "button": "Choisir un nouveau mot de passe",
        "expiry": "Ce lien est valable {minutes} minutes et ne peut servir qu'une fois.",
        "ignore": "Si vous n'êtes pas à l'origine de cette demande, ignorez cet e-mail : votre mot de passe reste inchangé.",
    },
    "en": {
        "subject": "Reset your password — Kaskad Console",
        "title": "Password reset",
        "greeting": "Hi {name},",
        "body": "We received a request to reset the password of your Kaskad Console account.",
        "button": "Choose a new password",
        "expiry": "This link is valid for {minutes} minutes and can only be used once.",
        "ignore": "If you didn't ask for this, ignore this email: your password stays unchanged.",
    },
}


def reset_password_email(lang: str, to_email: str, name: str, url: str, minutes: int) -> Email:
    t = RESET[lang]
    paragraphs = [t["greeting"].format(name=name), t["body"]]
    small = [t["expiry"].format(minutes=minutes), t["ignore"]]
    return Email(
        to_email=to_email,
        to_name=name,
        subject=t["subject"],
        html=_layout(lang, t["title"], paragraphs, t["button"], url, small),
        text=_text(t["title"], paragraphs, t["button"], url, small),
    )


# ---------------------------------------------------------------- testeurs de la bêta

TESTER = {
    "fr": {
        "subject": "Vous êtes invité à tester {app}",
        "title": "Testez {app} en avant-première",
        "body": "{account} vous a ajouté comme testeur de {app} sur Kaskad. Vous pourrez voir et télécharger ses versions bêta avant leur publication pour tous.",
        "how": "Connectez-vous à l'app Kaskad (mobile ou ordinateur) avec l'adresse {email}. Pas encore de compte ? Créez-le dans l'app avec cette adresse.",
        "button": "Ouvrir {app} dans Kaskad",
        "ignore": "Si vous ne souhaitez pas tester cette application, ignorez cet e-mail.",
    },
    "en": {
        "subject": "You're invited to test {app}",
        "title": "Try {app} before everyone else",
        "body": "{account} added you as a tester of {app} on Kaskad. You'll be able to see and download its beta versions before they are released to everyone.",
        "how": "Sign in to the Kaskad app (mobile or desktop) with {email}. No account yet? Create one in the app with this address.",
        "button": "Open {app} in Kaskad",
        "ignore": "If you don't want to test this app, just ignore this email.",
    },
}


def tester_email(lang: str, to_email: str, app: str, account: str, url: str) -> Email:
    t = TESTER[lang]
    title = t["title"].format(app=app)
    paragraphs = [t["body"].format(account=account, app=app), t["how"].format(email=to_email)]
    button = t["button"].format(app=app)
    small = [t["ignore"]]
    return Email(
        to_email=to_email,
        to_name=None,
        subject=t["subject"].format(app=app),
        html=_layout(lang, title, paragraphs, button, url, small),
        text=_text(title, paragraphs, button, url, small),
    )


# ---------------------------------------------------------------- réponse du développeur à un avis (utilisateur de l'app)

REVIEW_REPLY = {
    "fr": {
        "subject": "{developer} a répondu à votre avis sur {app}",
        "title": "Réponse à votre avis",
        "body": "{developer} a répondu à l'avis que vous avez laissé sur {app} :",
        "button": "Voir dans Kaskad",
        "small": "Vous recevez cet e-mail car vous avez publié un avis dans l'app Kaskad.",
    },
    "en": {
        "subject": "{developer} replied to your review of {app}",
        "title": "Reply to your review",
        "body": "{developer} replied to the review you left on {app}:",
        "button": "View in Kaskad",
        "small": "You receive this email because you posted a review in the Kaskad app.",
    },
}


def review_reply_email(lang: str, to_email: str, app: str, developer: str, reply: str, url: str) -> Email:
    lang = lang if lang in REVIEW_REPLY else "fr"
    t = REVIEW_REPLY[lang]
    paragraphs = [t["body"].format(developer=developer, app=app), f"« {reply} »" if lang == "fr" else f"“{reply}”"]
    return Email(
        to_email=to_email,
        to_name=None,
        subject=t["subject"].format(developer=developer, app=app),
        html=_layout(lang, t["title"], paragraphs, t["button"], url, [t["small"]]),
        text=_text(t["title"], paragraphs, t["button"], url, [t["small"]]),
    )


REPORT_REASONS = {
    "fr": {
        "malware": "logiciel malveillant",
        "abusive": "contenu abusif",
        "copyright": "atteinte aux droits d'auteur",
        "misleading": "fiche trompeuse",
        "broken": "app qui ne fonctionne pas",
        "other": "autre",
    },
    "en": {
        "malware": "malware",
        "abusive": "abusive content",
        "copyright": "copyright infringement",
        "misleading": "misleading listing",
        "broken": "app not working",
        "other": "other",
    },
}


# ---------------------------------------------------------------- suivi des demandes et du compte

STATUS_LABELS = {
    "fr": {"published": "publication", "archived": "dépublication", "draft": "retour en brouillon"},
    "en": {"published": "publishing", "archived": "unpublishing", "draft": "move back to draft"},
}
SUBJECT_LABELS = {
    "fr": {
        "version": "la version {version}",
        "promotion": "le passage en production de la version bêta {version}",
        "status": "la {status}",
        "listing": "les modifications de la fiche",
    },
    "en": {
        "version": "version {version}",
        "promotion": "the promotion to production of beta version {version}",
        "status": "{status}",
        "listing": "the listing changes",
    },
}

NOTIFICATIONS = {
    "fr": {
        "version_published": (
            "Version publiée : {app} {version}",
            "La version {version} de {app} a été validée et publiée. Elle est maintenant disponible au téléchargement.",
        ),
        "version_scheduled": (
            "Version programmée : {app} {version}",
            "La version {version} de {app} a été validée. Elle sera publiée automatiquement le {date}.",
        ),
        "version_rejected": (
            "Version refusée : {app} {version}",
            "La version {version} de {app} a été refusée par l'administrateur de la plateforme.",
        ),
        "status_approved": ("Demande approuvée : {app}", "Votre demande de {status} de {app} a été approuvée."),
        "status_rejected": ("Demande refusée : {app}", "Votre demande de {status} de {app} a été refusée."),
        "listing_published": ("Fiche mise à jour : {app}", "Les modifications de la fiche de {app} ont été validées et sont en ligne."),
        "listing_rejected": ("Modifications refusées : {app}", "Les modifications de la fiche de {app} ont été refusées."),
        "review_requested": ("À valider : {app}", "{member} ({account}) a soumis {subject} de {app} pour validation."),
        "app_reported": (
            "Signalement : {app}",
            "Un utilisateur a signalé {app} (motif : {reason}). Le signalement vous attend dans la Modération.",
        ),
        "account_suspended": (
            "Compte suspendu : {account}",
            "Le compte développeur « {account} » a été suspendu par l'administrateur de la plateforme. Ses applications ne sont plus visibles dans le store et ses membres ne peuvent plus se connecter.",
        ),
        "mfa_enabled": (
            "Double authentification activée",
            "La double authentification vient d'être activée sur votre compte Kaskad Console. Chaque connexion demandera désormais un code de votre application d'authentification.",
        ),
        "mfa_disabled": (
            "Double authentification désactivée",
            "La double authentification a été désactivée sur votre compte Kaskad Console. Si ce n'est pas vous, changez votre mot de passe immédiatement.",
        ),
        "mfa_reset": (
            "Double authentification réinitialisée",
            "{member} a réinitialisé la double authentification de votre compte (appareil perdu). Configurez-la de nouveau à votre prochaine connexion.",
        ),
        "mfa_recovery_used": (
            "Code de secours utilisé",
            "Un code de secours vient d'être utilisé pour vous connecter à Kaskad Console. Il vous en reste {count}. Si ce n'est pas vous, changez votre mot de passe.",
        ),
        "account_reactivated": (
            "Compte réactivé : {account}",
            "Le compte développeur « {account} » a été réactivé. Ses applications sont de nouveau visibles et ses membres peuvent se connecter.",
        ),
    },
    "en": {
        "version_published": (
            "Version published: {app} {version}",
            "Version {version} of {app} was approved and published. It is now available for download.",
        ),
        "version_scheduled": (
            "Version scheduled: {app} {version}",
            "Version {version} of {app} was approved. It will be published automatically on {date}.",
        ),
        "version_rejected": ("Version rejected: {app} {version}", "Version {version} of {app} was rejected by the platform admin."),
        "status_approved": ("Request approved: {app}", "Your {status} request for {app} was approved."),
        "status_rejected": ("Request rejected: {app}", "Your {status} request for {app} was rejected."),
        "listing_published": ("Listing updated: {app}", "The listing changes of {app} were approved and are live."),
        "listing_rejected": ("Changes rejected: {app}", "The listing changes of {app} were rejected."),
        "review_requested": ("To review: {app}", "{member} ({account}) submitted {subject} of {app} for review."),
        "app_reported": ("Report: {app}", "A user reported {app} (reason: {reason}). The report is waiting for you in Moderation."),
        "account_suspended": (
            "Account suspended: {account}",
            "The developer account “{account}” was suspended by the platform admin. Its apps are no longer visible in the store and its members can no longer sign in.",
        ),
        "mfa_enabled": (
            "Two-step verification turned on",
            "Two-step verification was just turned on for your Kaskad Console account. Every sign-in will now ask for a code from your authenticator app.",
        ),
        "mfa_disabled": (
            "Two-step verification turned off",
            "Two-step verification was turned off for your Kaskad Console account. If this wasn't you, change your password right away.",
        ),
        "mfa_reset": (
            "Two-step verification reset",
            "{member} reset two-step verification on your account (lost device). Set it up again at your next sign-in.",
        ),
        "mfa_recovery_used": (
            "Recovery code used",
            "A recovery code was just used to sign in to Kaskad Console. You have {count} left. If this wasn't you, change your password.",
        ),
        "account_reactivated": (
            "Account reactivated: {account}",
            "The developer account “{account}” was reactivated. Its apps are visible again and its members can sign in.",
        ),
    },
}
NOTIFICATION_TEXT = {
    "fr": {
        "greeting": "Bonjour {name},",
        "reason": "Motif : {reason}",
        "note": "Note : {note}",
        "button": "Ouvrir dans la console",
        "fix": "Corrigez ce qui est indiqué puis soumettez à nouveau depuis la console.",
    },
    "en": {
        "greeting": "Hi {name},",
        "reason": "Reason: {reason}",
        "note": "Note: {note}",
        "button": "Open in the console",
        "fix": "Fix what is described, then submit again from the console.",
    },
}


def notification_email(lang: str, kind: str, to_email: str, name: str, url: str, **ctx) -> Email:
    """E-mail de suivi : décision sur une demande, demande à valider, suspension du compte."""
    ctx = {k: v for k, v in ctx.items() if v is not None}
    if isinstance(ctx.get("date"), datetime):
        # Date en temps universel (le destinataire peut être dans n'importe quel fuseau)
        ctx["date"] = ctx["date"].strftime("%d/%m/%Y à %H:%M UTC" if lang == "fr" else "%Y-%m-%d at %H:%M UTC")
    if kind == "app_reported" and "reason" in ctx:
        ctx["reason"] = REPORT_REASONS[lang].get(ctx["reason"], ctx["reason"])
    if "status" in ctx:
        ctx["status"] = STATUS_LABELS[lang].get(ctx["status"], ctx["status"])
    if "subject_kind" in ctx:
        ctx["subject"] = SUBJECT_LABELS[lang][ctx.pop("subject_kind")].format(**ctx)
    subject, body = NOTIFICATIONS[lang][kind]
    t = NOTIFICATION_TEXT[lang]
    fmt = {"app": "", "version": "", "account": "", "member": "", "status": "", "subject": "", "date": "", "count": "", **ctx}
    title = subject.format(**fmt)
    paragraphs = [t["greeting"].format(name=name or ""), body.format(**fmt)]
    if ctx.get("reason") and kind != "app_reported":
        paragraphs.append(t["reason"].format(reason=ctx["reason"]))
    if ctx.get("note"):
        paragraphs.append(t["note"].format(note=ctx["note"]))
    small = [t["fix"]] if kind.endswith("_rejected") else []
    return Email(
        to_email=to_email,
        to_name=name,
        subject=f"{title} — Kaskad Console",
        html=_layout(lang, title, paragraphs, t["button"], url, small),
        text=_text(title, paragraphs, t["button"], url, small),
    )
