"""Pages web publiques des apps : lien partageable (`/a/{app_id}`), aperçu riche dans les réseaux sociaux
(balises Open Graph) et bouton qui ouvre l'app dans le client Kaskad s'il est installé."""

from html import escape
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core.config import get_settings
from app.core.i18n import language
from app.deps import Db
from app.models.common import maybe_oid
from app.services.catalog import PUBLIC_APP_FILTER, app_detail, category_out, share_url

router = APIRouter(include_in_schema=False)

TEXTS = {
    "fr": {
        "open": "Ouvrir dans Kaskad",
        "get": "Télécharger Kaskad",
        "hint": "L'app Kaskad s'ouvre sur la fiche de cette application. Pas encore installée ? Téléchargez Kaskad, puis revenez sur ce lien.",
        "about": "À propos",
        "screenshots": "Captures d'écran",
        "reviews": "{count} avis",
        "no_reviews": "Pas encore d'avis",
        "version": "Version {version}",
        "downloads": "{count} téléchargements",
        "by": "par {developer}",
        "not_found_title": "Application introuvable",
        "not_found": "Cette application n'existe pas ou n'est plus disponible sur Kaskad.",
        "footer": "Kaskad — le store d'applications multi-plateforme. Les fichiers sont téléchargés puis installés par vous : rien n'est installé automatiquement.",
    },
    "en": {
        "open": "Open in Kaskad",
        "get": "Get Kaskad",
        "hint": "The Kaskad app opens on this app's page. Not installed yet? Get Kaskad, then come back to this link.",
        "about": "About",
        "screenshots": "Screenshots",
        "reviews": "{count} reviews",
        "no_reviews": "No reviews yet",
        "version": "Version {version}",
        "downloads": "{count} downloads",
        "by": "by {developer}",
        "not_found_title": "App not found",
        "not_found": "This app doesn't exist or is no longer available on Kaskad.",
        "footer": "Kaskad — the cross-platform app store. Files are downloaded and installed by you: nothing is installed automatically.",
    },
}

PLATFORM_LABELS = {"android": "Android", "windows": "Windows", "macos": "macOS", "linux": "Linux"}

STYLE = """
:root{--bg:#f1f5f9;--card:#fff;--text:#0f172a;--muted:#475569;--border:#e2e8f0;--primary:#2f6bff;--star:#f59e0b}
@media (prefers-color-scheme:dark){:root{--bg:#020617;--card:#0f172a;--text:#f8fafc;--muted:#94a3b8;--border:#1e293b;--primary:#5b9cff}}
*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased}
.wrap{max-width:860px;margin:0 auto;padding:24px 16px 48px}
.brand{display:flex;align-items:center;gap:8px;font-weight:800;letter-spacing:-.3px;color:var(--text);text-decoration:none;margin-bottom:20px}
.brand span{width:28px;height:28px;border-radius:8px;background:linear-gradient(135deg,#2f6bff,#22d3ee 55%,#8b3ce8);display:inline-block}
.card{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:24px}
.head{display:flex;gap:20px;align-items:center;flex-wrap:wrap}
.icon{width:96px;height:96px;border-radius:24px;object-fit:cover;flex-shrink:0}
.initials{display:grid;place-items:center;color:#fff;font-weight:800;font-size:34px;background:linear-gradient(135deg,#2f6bff,#8b3ce8)}
h1{margin:0;font-size:28px;letter-spacing:-.5px}.dev{color:var(--primary);font-weight:700;margin-top:4px}
.meta{color:var(--muted);font-size:14px;margin-top:8px;display:flex;gap:12px;flex-wrap:wrap}
.stars{color:var(--star);letter-spacing:1px}.short{font-size:17px;color:var(--muted);margin:16px 0 0}
.actions{display:flex;gap:12px;flex-wrap:wrap;margin-top:20px}
.btn{display:inline-flex;align-items:center;justify-content:center;padding:12px 22px;border-radius:999px;font-weight:700;text-decoration:none;font-size:15px}
.primary{background:var(--primary);color:#fff}.ghost{border:1px solid var(--border);color:var(--text)}
.hint{color:var(--muted);font-size:13px;margin-top:12px}
h2{font-size:19px;margin:28px 0 12px}.shots{display:flex;gap:12px;overflow-x:auto;padding-bottom:6px;scrollbar-width:none}
.shots img{height:360px;border-radius:16px;border:1px solid var(--border)}
.about{white-space:pre-line;line-height:1.6}.chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}
.chip{border:1px solid var(--border);border-radius:999px;padding:4px 12px;font-size:13px;color:var(--muted)}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:32px;line-height:1.5}
"""


def _page(lang: str, title: str, body: str, meta: str = "") -> HTMLResponse:
    html = f"""<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)}</title>{meta}
<style>{STYLE}</style></head><body><div class="wrap"><a class="brand" href="#"><span></span>Kaskad</a>{body}
<footer>{escape(TEXTS[lang]["footer"])}</footer></div></body></html>"""
    return HTMLResponse(html)


def _stars(avg: float | None) -> str:
    if not avg:
        return ""
    full = round(avg)
    return "★" * full + "☆" * (5 - full)


@router.get("/a/{app_id}", response_class=HTMLResponse)
async def app_page(db: Db, request: Request, app_id: str):
    lang = language(request)
    t = TEXTS[lang]
    oid = maybe_oid(app_id)
    app = await db.apps.find_one({"_id": oid, **PUBLIC_APP_FILTER}) if oid else None
    if not app:
        body = f'<div class="card"><h1>{escape(t["not_found_title"])}</h1><p class="short">{escape(t["not_found"])}</p></div>'
        response = _page(lang, t["not_found_title"], body)
        response.status_code = 404
        return response

    s = get_settings()
    categories = await db.categories.find({"_id": {"$in": app.get("category_ids", [])}}).sort("order", 1).to_list(None)
    d = app_detail(app, categories, [], lang)
    url = share_url(app["_id"])
    deep_link = f"{s.app_link_base}{app['_id']}"
    fallback = s.kaskad_download_url or url
    scheme, _, rest = deep_link.partition("://")
    # Android : intent (ouvre l'app installée, sinon la page de téléchargement de Kaskad)
    intent = f"intent://{rest}#Intent;scheme={scheme};package={s.android_package_id};S.browser_fallback_url={quote(fallback, safe='')};end"

    name = escape(d["name"])
    icon = (
        f'<img class="icon" src="{escape(d["icon_url"])}" alt="">'
        if d.get("icon_url")
        else f'<div class="icon initials">{escape("".join(w[0] for w in d["name"].split()[:2]).upper())}</div>'
    )
    rating = (
        f'<span><span class="stars">{_stars(d["rating_average"])}</span> {f"{d['rating_average']:.1f}".replace(".", "," if lang == "fr" else ".")} · {escape(t["reviews"].format(count=d["rating_count"]))}</span>'
        if d.get("rating_count")
        else f"<span>{escape(t['no_reviews'])}</span>"
    )
    platforms = " · ".join(PLATFORM_LABELS.get(p, p) for p in d.get("platforms", []))
    meta_line = [rating]
    if platforms:
        meta_line.append(f"<span>{escape(platforms)}</span>")
    if d.get("latest_version_name"):
        meta_line.append(f"<span>{escape(t['version'].format(version=d['latest_version_name']))}</span>")
    meta_line.append(f"<span>{escape(t['downloads'].format(count=d.get('downloads_count', 0)))}</span>")
    developer = f'<div class="dev">{escape(t["by"].format(developer=d["developer"]["name"]))}</div>' if d.get("developer") else ""
    get_btn = f'<a class="btn ghost" href="{escape(s.kaskad_download_url)}">{escape(t["get"])}</a>' if s.kaskad_download_url else ""
    shots = "".join(f'<img src="{escape(u)}" alt="" loading="lazy">' for u in d.get("screenshots", []))
    chips = "".join(f'<span class="chip">{escape(category_out(c)["name"])}</span>' for c in categories)

    body = f"""
<div class="card">
  <div class="head">{icon}<div><h1>{name}</h1>{developer}<div class="meta">{"".join(meta_line)}</div></div></div>
  <p class="short">{escape(d.get("short_description") or "")}</p>
  <div class="actions"><a class="btn primary" id="open" href="{escape(deep_link)}" data-intent="{escape(intent)}">{escape(t["open"])}</a>{get_btn}</div>
  <div class="hint">{escape(t["hint"])}</div>
  {f'<div class="chips">{chips}</div>' if chips else ""}
</div>
{f'<h2>{escape(t["screenshots"])}</h2><div class="shots">{shots}</div>' if shots else ""}
{f'<h2>{escape(t["about"])}</h2><div class="card about">{escape(d["long_description"])}</div>' if d.get("long_description") else ""}
<script>
  // Android : l'intent ouvre l'app si elle est installée, sinon la page de téléchargement
  if (/Android/i.test(navigator.userAgent)) {{ var a = document.getElementById("open"); a.href = a.dataset.intent; }}
</script>"""
    description = d.get("short_description") or ""
    image = d.get("icon_url") or (d["screenshots"][0] if d.get("screenshots") else "")
    meta = f"""
<meta name="description" content="{escape(description)}">
<meta property="og:type" content="website"><meta property="og:site_name" content="Kaskad">
<meta property="og:title" content="{name}"><meta property="og:description" content="{escape(description)}">
<meta property="og:url" content="{escape(url)}">{f'<meta property="og:image" content="{escape(image)}">' if image else ""}
<meta name="twitter:card" content="summary"><link rel="canonical" href="{escape(url)}">"""
    return _page(lang, f"{d['name']} — Kaskad", body, meta)
