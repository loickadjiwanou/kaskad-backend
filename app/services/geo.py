"""Pays d'une requête (statistiques uniquement, jamais stocké avec l'adresse IP).

Ordre : en-tête posé par le proxy / CDN (`GEOIP_HEADER`, ex. `CF-IPCountry` derrière Cloudflare),
puis base GeoLite2 / DB-IP locale (`GEOIP_DATABASE`, fichier .mmdb), sinon inconnu (None).
"""

import ipaddress
import logging
from functools import lru_cache

from fastapi import Request

from app.core.config import get_settings

log = logging.getLogger("kaskad.geo")


@lru_cache
def _reader(path: str):
    try:
        import maxminddb

        return maxminddb.open_database(path)
    except Exception as exc:  # fichier absent ou illisible : pas de géolocalisation
        log.warning("GeoIP database unavailable (%s): %s", path, exc)
        return None


def _valid(code: str | None) -> str | None:
    code = (code or "").strip().upper()
    # XX / T1 (Tor) / ZZ : inconnus chez Cloudflare
    return code if len(code) == 2 and code.isalpha() and code not in ("XX", "ZZ") else None


def country_for(request: Request) -> str | None:
    s = get_settings()
    if s.geoip_header:
        code = _valid(request.headers.get(s.geoip_header))
        if code:
            return code
    if s.geoip_database and request.client:
        try:
            ip = ipaddress.ip_address(request.client.host)
        except ValueError:
            return None
        if ip.is_private or ip.is_loopback:
            return None
        reader = _reader(s.geoip_database)
        if reader:
            try:
                record = reader.get(str(ip)) or {}
            except Exception:
                return None
            return _valid((record.get("country") or record.get("registered_country") or {}).get("iso_code"))
    return None
