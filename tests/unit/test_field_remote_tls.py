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


# -- review round: atomic writes, key/cert match, Windows ACL --------------------------------------


def test_pair_is_written_atomically_without_temp_leftovers(tmp_path: Path) -> None:
    tls.generate_self_signed(tmp_path, ["127.0.0.1"])
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([tls.CERT_NAME, tls.KEY_NAME])


def test_a_key_that_does_not_match_the_certificate_forces_regeneration(tmp_path: Path) -> None:
    first = tls.generate_self_signed(tmp_path, ["127.0.0.1"])
    other_dir = tmp_path / "other"
    other = tls.generate_self_signed(other_dir, ["127.0.0.1"])
    first.key_path.write_bytes(other.key_path.read_bytes())  # a crash mid-update, or a hand copy
    fresh = tls.load_or_create(tmp_path, ["127.0.0.1"])
    assert fresh.fingerprint_hex not in (first.fingerprint_hex, other.fingerprint_hex)
    # and the new pair really loads as a TLS server identity
    ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(fresh.cert_path, fresh.key_path)


def test_stored_fingerprint(tmp_path: Path) -> None:
    assert tls.stored_fingerprint(tmp_path) == ""
    material = tls.generate_self_signed(tmp_path, ["127.0.0.1"])
    assert tls.stored_fingerprint(tmp_path) == material.fingerprint_hex


@pytest.mark.skipif(sys.platform != "win32", reason="icacls is Windows-only")
def test_key_acl_on_windows_grants_only_the_current_user(tmp_path: Path) -> None:
    import subprocess

    material = tls.generate_self_signed(tmp_path, ["127.0.0.1"])
    out = subprocess.run(["icacls", str(material.key_path)], capture_output=True, text=True, check=True).stdout
    entries = [line.strip() for line in out.splitlines() if "(" in line and ":" in line]
    assert len(entries) == 1, out  # inheritance removed; one explicit grant
    user = os.environ["USERNAME"].lower()
    assert user in entries[0].lower() and "(F)" in entries[0], out
    for broad in ("everyone", "users", "authenticated users"):
        assert not any(broad in e.lower().split(":")[0].split("\\")[-1] for e in entries), out


def test_restrict_logs_a_warning_when_icacls_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(tls, "_current_user_sid", lambda: "S-1-5-21-1")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **_k: subprocess.CompletedProcess(a, 5, b"", b"denied")
    )
    key = tmp_path / "k.pem"
    key.write_bytes(b"x")
    with caplog.at_level("WARNING"):
        assert tls._restrict_to_user(key) is False
    assert "icacls exited with 5" in caplog.text
