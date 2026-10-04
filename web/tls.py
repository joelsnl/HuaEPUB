# Author: joelsnl and Anthropic Claude
"""HTTPS for remote server mode: a generated certificate, or the user's own.

The generated certificate is self-signed, so browsers warn once. The server
screen shows its SHA-256 fingerprint so the person can check they reached
their own PC before trusting it.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

from core.security import write_secret_file

CERT_NAME = "cert.pem"
KEY_NAME = "key.pem"
VALID_DAYS = 825


class CertificateError(Exception):
    pass


@dataclass
class Certificate:
    cert_path: Path
    key_path: Path
    fingerprint: str  # "AB:CD:…" SHA-256
    generated: bool


def _fingerprint(pem: bytes) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    cert = x509.load_pem_x509_certificate(pem)
    raw = cert.fingerprint(hashes.SHA256())
    return ":".join(f"{b:02X}" for b in raw)


def _names_in(pem: bytes) -> List[str]:
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(pem)
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return []
    names = [str(n) for n in san.get_values_for_type(x509.DNSName)]
    names += [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
    return names


def _not_after(pem: bytes) -> _dt.datetime:
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(pem)
    value = getattr(cert, "not_valid_after_utc", None)
    if value is None:
        value = cert.not_valid_after.replace(tzinfo=_dt.timezone.utc)
    return value


def generate(directory: Path, names: Iterable[str]) -> Certificate:
    """Write a fresh self-signed EC certificate covering *names*."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    wanted = _clean_names(names)
    key = ec.generate_private_key(ec.SECP256R1())
    alt = []
    for name in wanted:
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            alt.append(x509.DNSName(name))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "HuaEPUB server")])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cert_path = directory / CERT_NAME
    key_path = directory / KEY_NAME
    write_secret_file(key_path, key_pem.decode("ascii"))
    cert_path.write_bytes(cert_pem)
    return Certificate(cert_path, key_path, _fingerprint(cert_pem), True)


def _clean_names(names: Iterable[str]) -> List[str]:
    out: List[str] = []
    for raw in list(names) + ["localhost", "127.0.0.1"]:
        name = (raw or "").strip().lower().strip("[]")
        if name and name not in out:
            out.append(name)
    return out


def ensure_certificate(directory: Path, names: Iterable[str]) -> Certificate:
    """Reuse the generated certificate when it still covers *names*."""
    directory = Path(directory)
    cert_path = directory / CERT_NAME
    key_path = directory / KEY_NAME
    wanted = _clean_names(names)
    if cert_path.is_file() and key_path.is_file():
        try:
            pem = cert_path.read_bytes()
            have = set(_names_in(pem))
            fresh = _not_after(pem) - _dt.datetime.now(_dt.timezone.utc) > _dt.timedelta(days=14)
            if fresh and set(wanted) <= have:
                return Certificate(cert_path, key_path, _fingerprint(pem), True)
        except Exception:
            pass
    return generate(directory, wanted)


def load_own_certificate(cert_file: str, key_file: str) -> Certificate:
    """The person's own certificate (for example from Let's Encrypt)."""
    cert_path = Path(cert_file).expanduser()
    key_path = Path(key_file).expanduser()
    if not cert_path.is_file():
        raise CertificateError(f"Certificate file not found: {cert_path}")
    if not key_path.is_file():
        raise CertificateError(f"Key file not found: {key_path}")
    try:
        pem = cert_path.read_bytes()
        fingerprint = _fingerprint(pem)
    except Exception as exc:
        raise CertificateError(f"Could not read the certificate: {exc}") from exc
    try:
        ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(str(cert_path),
                                                                             str(key_path))
    except (ssl.SSLError, OSError, ValueError) as exc:
        raise CertificateError(f"The certificate and key do not belong together: {exc}") from exc
    return Certificate(cert_path, key_path, fingerprint, False)
