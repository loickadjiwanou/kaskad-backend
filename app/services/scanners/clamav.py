"""Client ClamAV (clamd) minimal via le protocole INSTREAM : le fichier est envoyé en flux, jamais exécuté."""

import asyncio
import struct
from pathlib import Path

CHUNK = 1024 * 1024


async def scan_file(path: Path, host: str, port: int, timeout: float = 300) -> dict:
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=10)
    try:
        writer.write(b"zINSTREAM\0")
        with open(path, "rb") as f:
            while chunk := f.read(CHUNK):
                writer.write(struct.pack("!L", len(chunk)) + chunk)
                await writer.drain()
        writer.write(struct.pack("!L", 0))
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(4096), timeout=timeout)
    finally:
        writer.close()
    reply = raw.decode(errors="replace").strip("\0").strip()
    # Réponses possibles : "stream: OK", "stream: <Signature> FOUND", "INSTREAM size limit exceeded. ERROR"
    if reply.endswith("OK"):
        return {"engine": "clamav", "status": "clean", "raw": reply}
    if reply.endswith("FOUND"):
        signature = reply.removeprefix("stream:").removesuffix("FOUND").strip()
        return {"engine": "clamav", "status": "infected", "threat": signature, "raw": reply}
    return {"engine": "clamav", "status": "error", "raw": reply}
