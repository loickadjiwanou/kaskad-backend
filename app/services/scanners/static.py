"""Analyse statique des binaires (lecture seule, aucune exécution).

- Contrôle de la signature de format (magic bytes) pour chaque format installable.
- APK : manifeste (package, versions, SDK, permissions) et empreintes des certificats de signature (androguard).
- EXE : vérification de la signature Authenticode (signify).
"""

import hashlib
from pathlib import Path


def _read(path: Path, offset: int, size: int) -> bytes:
    with open(path, "rb") as f:
        if offset < 0:
            f.seek(max(path.stat().st_size + offset, 0))
        else:
            f.seek(offset)
        return f.read(size)


def check_magic(path: Path, file_format: str) -> tuple[bool, str]:
    """Vérifie que le contenu correspond bien au format annoncé."""
    head = _read(path, 0, 16)
    if file_format == "apk":
        return head.startswith(b"PK\x03\x04"), "APK (ZIP) header"
    if file_format == "exe":
        return head.startswith(b"MZ"), "PE (MZ) header"
    if file_format == "msi":
        return head.startswith(bytes.fromhex("D0CF11E0A1B11AE1")), "MSI (OLE compound) header"
    if file_format == "dmg":
        # Image disque UDIF : bloc "koly" dans les 512 derniers octets
        return _read(path, -512, 4) == b"koly", "DMG (UDIF koly trailer)"
    if file_format == "pkg":
        return head.startswith(b"xar!"), "PKG (xar) header"
    if file_format == "appimage":
        return head.startswith(b"\x7fELF"), "AppImage (ELF) header"
    if file_format == "deb":
        return head.startswith(b"!<arch>\n"), "DEB (ar archive) header"
    if file_format == "rpm":
        return head.startswith(bytes.fromhex("EDABEEDB")), "RPM lead"
    return False, "unknown format"


def inspect_apk(path: Path) -> dict:
    from androguard.core.apk import APK
    from loguru import logger

    logger.remove()  # androguard est très verbeux
    apk = APK(str(path))
    certs = apk.get_certificates_der_v3() or apk.get_certificates_der_v2() or [c.dump() for c in apk.get_certificates_v1()]
    fingerprints = sorted({hashlib.sha256(der).hexdigest() for der in certs})
    try:
        version_code = int(apk.get_androidversion_code() or 0)
    except ValueError:
        version_code = None
    return {
        "package": apk.get_package(),
        "version_code": version_code,
        "version_name": apk.get_androidversion_name(),
        "min_sdk": apk.get_min_sdk_version(),
        "target_sdk": apk.get_target_sdk_version(),
        "permissions": sorted(apk.get_permissions() or []),
        "signed": bool(fingerprints),
        "cert_sha256": fingerprints,
    }


def inspect_authenticode(path: Path) -> dict:
    from signify.authenticode import AuthenticodeFile

    with open(path, "rb") as f:
        try:
            signed = AuthenticodeFile.from_stream(f, path.name)
        except Exception as e:  # format non pris en charge
            return {"signed": False, "supported": False, "detail": str(e)}
        signatures = list(signed.iter_signatures())
        if not signatures:
            return {"signed": False, "supported": True}
        result, error = signed.explain_verify()
        name = getattr(result, "name", str(result))
        if name == "OK":
            verdict = "valid"
        elif name in ("INVALID_DIGEST", "INCONSISTENT_DIGEST_ALGORITHM", "INVALID_ADDITIONAL_HASH", "VERIFY_ERROR"):
            verdict = "tampered"  # le fichier a été modifié après signature
        elif name in ("CERTIFICATE_ERROR", "COUNTERSIGNER_ERROR"):
            verdict = "untrusted"  # certificat expiré, non approuvé ou chaîne incomplète
        else:
            verdict = "unverifiable"  # limite de l'outil de vérification (format de clé non pris en charge…)
        signer = None
        try:
            signer = str(signatures[0].signer_info.issuer)
        except Exception:
            pass
        return {
            "signed": True,
            "supported": True,
            "valid": verdict == "valid",
            "verdict": verdict,
            "result": name,
            "detail": str(error) if error else None,
            "issuer": signer,
        }
