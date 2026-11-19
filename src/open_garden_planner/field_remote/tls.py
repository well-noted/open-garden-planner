"""Self-signed certificate for Field Remote (design 1.3). No Qt.

Chrome exposes Geolocation and motion sensors only in secure contexts, so the phone page is
HTTPS. The certificate is a per-install EC P-256 self-signed one; its SHA-256 fingerprint is
shown to the user (and put in the pairing URL, where it is not a secret). The private key stays
in the user's data directory and is never logged or returned by any function here.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import ipaddress
import logging
import os
import socket
import subprocess  # nosec B404 - fixed icacls argv only, see _restrict_to_user
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

VALID_DAYS = 397  # the longest lifetime browsers accept for a leaf certificate
RENEW_BEFORE_DAYS = 30
CERT_NAME = "field_remote_cert.pem"
KEY_NAME = "field_remote_key.pem"


@dataclass(frozen=True)
class TlsMaterial:
    """Where the certificate and key are, and the (public) certificate fingerprint."""

    cert_path: Path
    key_path: Path
    fingerprint_hex: str


def fingerprint_of_pem(pem: bytes) -> str:
    """SHA-256 of the certificate's DER encoding, lowercase hex (what Chrome shows)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    cert = x509.load_pem_x509_certificate(pem)
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def _restrict_to_user(path: Path) -> None:
    """Make the key readable by the current user only (best effort, never raises)."""
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    if sys.platform != "win32":
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(  # nosec B603 B607 - fixed argv, no shell, user name is one argv item
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            check=False,
            capture_output=True,
            timeout=10,
        )


def _san_for(addresses: list[str], names: list[str]) -> list[object]:
    from cryptography import x509

    san: list[object] = []
    for address in addresses:
        san.append(x509.IPAddress(ipaddress.ip_address(address)))
    for name in names:
        san.append(x509.DNSName(name))
    return san


def generate_self_signed(directory: Path, addresses: list[str], extra_names: list[str] | None = None) -> TlsMaterial:
    """Create (or replace) the certificate and key under ``directory``."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    directory.mkdir(parents=True, exist_ok=True)
    names = ["localhost", *(extra_names or [])]
    with contextlib.suppress(OSError):
        machine = socket.gethostname()
        if machine and machine not in names:
            names.append(machine)

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Open Garden Planner Field Remote")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(_san_for(addresses, names)), critical=False)  # type: ignore[arg-type]
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cert_path, key_path = directory / CERT_NAME, directory / KEY_NAME
    # The key is created with 0600 from the first byte, not written wide and narrowed later.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key_pem)
    _restrict_to_user(key_path)
    cert_path.write_bytes(cert_pem)
    logger.info("Field Remote certificate generated (fingerprint %s)", fingerprint_of_pem(cert_pem))
    return TlsMaterial(cert_path, key_path, fingerprint_of_pem(cert_pem))


def _usable(cert_path: Path, addresses: list[str]) -> bool:
    """The stored certificate covers every bind address and is not about to expire."""
    from cryptography import x509

    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        have = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
        left = cert.not_valid_after_utc - datetime.datetime.now(datetime.UTC)
    except (OSError, ValueError, x509.ExtensionNotFound):
        return False
    return left > datetime.timedelta(days=RENEW_BEFORE_DAYS) and {
        str(ipaddress.ip_address(a)) for a in addresses
    } <= have


def load_or_create(
    directory: Path, addresses: list[str], extra_names: list[str] | None = None, *, force: bool = False
) -> TlsMaterial:
    """Reuse the stored certificate, or regenerate on demand, on a bind change or near expiry."""
    cert_path, key_path = directory / CERT_NAME, directory / KEY_NAME
    if not force and cert_path.is_file() and key_path.is_file() and _usable(cert_path, addresses):
        return TlsMaterial(cert_path, key_path, fingerprint_of_pem(cert_path.read_bytes()))
    return generate_self_signed(directory, addresses, extra_names)
