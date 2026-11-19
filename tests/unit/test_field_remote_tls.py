"""Field Remote self-signed certificate (ADR-081)."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import ssl
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from open_garden_planner.field_remote import tls


def test_generates_p256_cert_with_the_bind_address_in_the_san(tmp_path: Path) -> None:
    material = tls.generate_self_signed(tmp_path, ["192.168.1.20"])
    cert = x509.load_pem_x509_certificate(material.cert_path.read_bytes())
    public = cert.public_key()
    assert isinstance(public, ec.EllipticCurvePublicKey)
    assert public.curve.name == "secp256r1"
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert ipaddress.ip_address("192.168.1.20") in san.get_values_for_type(x509.IPAddress)
    assert "localhost" in san.get_values_for_type(x509.DNSName)
    assert (cert.not_valid_after_utc - cert.not_valid_before_utc).days >= tls.VALID_DAYS


def test_fingerprint_is_sha256_of_the_der_and_matches_ssl(tmp_path: Path) -> None:
    material = tls.generate_self_signed(tmp_path, ["127.0.0.1"])
    der = ssl.PEM_cert_to_DER_cert(material.cert_path.read_text(encoding="ascii"))
    assert material.fingerprint_hex == hashlib.sha256(der).hexdigest()


def test_key_is_a_loadable_pkcs8_key_and_not_part_of_the_material_repr(tmp_path: Path) -> None:
    material = tls.generate_self_signed(tmp_path, ["127.0.0.1"])
    key_bytes = material.key_path.read_bytes()
    assert isinstance(serialization.load_pem_private_key(key_bytes, password=None), ec.EllipticCurvePrivateKey)
    assert "PRIVATE KEY" not in repr(material)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes; Windows uses icacls, not asserted here")
def test_key_file_mode_is_owner_only(tmp_path: Path) -> None:
    material = tls.generate_self_signed(tmp_path, ["127.0.0.1"])
    assert (os.stat(material.key_path).st_mode & 0o077) == 0


def test_load_or_create_reuses_then_regenerates(tmp_path: Path) -> None:
    first = tls.load_or_create(tmp_path, ["127.0.0.1"])
    assert tls.load_or_create(tmp_path, ["127.0.0.1"]).fingerprint_hex == first.fingerprint_hex
    # a bind change the certificate does not cover regenerates it
    changed = tls.load_or_create(tmp_path, ["192.168.1.20"])
    assert changed.fingerprint_hex != first.fingerprint_hex
    # and so does an explicit request
    assert tls.load_or_create(tmp_path, ["192.168.1.20"], force=True).fingerprint_hex != changed.fingerprint_hex


def test_corrupt_certificate_is_replaced(tmp_path: Path) -> None:
    first = tls.generate_self_signed(tmp_path, ["127.0.0.1"])
    first.cert_path.write_text("not a certificate", encoding="ascii")
    assert tls.load_or_create(tmp_path, ["127.0.0.1"]).fingerprint_hex != first.fingerprint_hex


def test_generation_never_logs_the_private_key(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    material = tls.generate_self_signed(tmp_path, ["127.0.0.1"])
    assert "PRIVATE KEY" not in caplog.text
    assert material.key_path.read_text(encoding="ascii").splitlines()[1] not in caplog.text
