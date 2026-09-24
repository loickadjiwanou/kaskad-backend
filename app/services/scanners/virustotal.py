"""Client VirusTotal (API v3) : recherche par SHA-256 puis, si le fichier est inconnu, envoi et attente de l'analyse."""

import asyncio
from pathlib import Path

import httpx

API = "https://www.virustotal.com/api/v3"
DIRECT_UPLOAD_LIMIT = 32 * 1024 * 1024


def _verdict(stats: dict) -> dict:
    malicious = stats.get("malicious", 0)
    suspicious = stats.get("suspicious", 0)
    status = "infected" if malicious > 0 else "suspicious" if suspicious > 0 else "clean"
    return {"engine": "virustotal", "status": status, "stats": stats}


async def scan_file(path: Path, sha256: str, api_key: str, poll_interval: float = 20, max_wait: float = 900) -> dict:
    headers = {"x-apikey": api_key}
    async with httpx.AsyncClient(timeout=120, headers=headers) as client:
        # 1. Fichier déjà connu de VirusTotal ?
        r = await client.get(f"{API}/files/{sha256}")
        if r.status_code == 200:
            stats = r.json()["data"]["attributes"].get("last_analysis_stats", {})
            if sum(stats.values()) > 0:
                return {**_verdict(stats), "source": "lookup"}

        # 2. Envoi du fichier (URL d'upload dédiée au-delà de 32 Mo)
        upload_url = f"{API}/files"
        if path.stat().st_size > DIRECT_UPLOAD_LIMIT:
            upload_url = (await client.get(f"{API}/files/upload_url")).raise_for_status().json()["data"]
        with open(path, "rb") as f:
            r = await client.post(upload_url, files={"file": (path.name, f)})
        r.raise_for_status()
        analysis_id = r.json()["data"]["id"]

        # 3. Attente du résultat
        waited = 0.0
        while waited < max_wait:
            await asyncio.sleep(poll_interval)
            waited += poll_interval
            r = await client.get(f"{API}/analyses/{analysis_id}")
            r.raise_for_status()
            attrs = r.json()["data"]["attributes"]
            if attrs.get("status") == "completed":
                return {**_verdict(attrs.get("stats", {})), "source": "upload", "analysis_id": analysis_id}
        return {"engine": "virustotal", "status": "error", "raw": "analysis timeout", "analysis_id": analysis_id}
