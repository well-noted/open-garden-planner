"""Field Remote auth: token handling, header-only auth, Host/Origin rules (ADR-081)."""

from __future__ import annotations

import pytest

from open_garden_planner.field_remote import auth
from open_garden_planner.field_remote.server import check_bind_address


def test_new_token_is_long_and_unique() -> None:
    first, second = auth.new_token(), auth.new_token()
    assert first != second
    assert len(first) >= 43  # 256 bits, url-safe base64


@pytest.mark.parametrize(
    ("presented", "expected", "ok"),
    [
        ("abc", "abc", True),
        ("abc", "abd", False),
        ("", "abc", False),
        (None, "abc", False),
        ("abc", None, False),
        ("abc", "", False),
        ("café", "café", False),  # non-ASCII fails closed, never raises TypeError
    ],
)
def test_token_matches(presented: str | None, expected: str | None, ok: bool) -> None:
    assert auth.token_matches(presented, expected) is ok


def test_bearer_comes_only_from_the_authorization_header() -> None:
    assert auth.bearer_from_headers([(b"authorization", b"Bearer secret")]) == "secret"
    assert auth.bearer_from_headers([(b"authorization", b"bearer  secret ")]) == "secret"
    assert auth.bearer_from_headers([(b"authorization", b"Basic secret")]) is None
    assert auth.bearer_from_headers([(b"x-token", b"secret")]) is None
    assert auth.bearer_from_headers([]) is None


@pytest.mark.parametrize("query", [b"token=x", b"TOKEN=x", b"a=1&access_token=x", b"api_key=x", b"token="])
def test_query_with_credential_key_is_flagged(query: bytes) -> None:
    assert auth.query_carries_token(query)


@pytest.mark.parametrize("query", [b"", b"bbox=1,2,3,4", b"layers=a&q=tomato"])
def test_ordinary_query_is_not_flagged(query: bytes) -> None:
    assert not auth.query_carries_token(query)


def test_allowed_hosts_are_exact_and_carry_the_port() -> None:
    assert auth.allowed_hosts("192.168.1.20", 8766) == {"192.168.1.20:8766"}  # no localhost off loopback
    assert auth.allowed_hosts("127.0.0.1", 8766) == {"127.0.0.1:8766", "localhost:8766"}
    assert "[::1]:8766" in auth.allowed_hosts("::1", 8766)
    assert "garden.ts.net:8766" in auth.allowed_hosts("100.64.0.5", 8766, ("Garden.ts.net",))


def test_origin_rules() -> None:
    hosts = auth.allowed_hosts("127.0.0.1", 8766)
    assert auth.origin_allowed(None, hosts)
    assert auth.origin_allowed("https://127.0.0.1:8766", hosts)
    assert not auth.origin_allowed("https://evil.example", hosts)
    assert not auth.origin_allowed("http://127.0.0.1:8766", hosts)  # plain http is not our origin
    assert not auth.origin_allowed("null", hosts)


def test_pairing_url_puts_token_and_fingerprint_in_the_fragment_only() -> None:
    url = auth.pairing_url("192.168.1.20", 8766, "tok_en-1", "ab" * 32)
    base, _, fragment = url.partition("#")
    assert base == "https://192.168.1.20:8766/"
    assert "tok_en-1" not in base and "?" not in url
    assert fragment == f"token=tok_en-1&fp={'ab' * 32}"
    assert auth.pairing_url("::1", 8766, "t", "00").startswith("https://[::1]:8766/#")


def test_fingerprint_formatting() -> None:
    assert auth.format_fingerprint("abcd01") == "AB:CD:01"


@pytest.mark.parametrize(
    "bind", ["127.0.0.1", "::1", "192.168.1.20", "10.0.0.5", "172.16.3.4", "100.101.102.103", "169.254.1.1"]
)
def test_bind_accepts_local_addresses(bind: str) -> None:
    assert check_bind_address(bind) == bind


@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "8.8.8.8", "100.128.0.1", "example.com", "", "localhost"])
def test_bind_refuses_wildcard_public_and_names(bind: str) -> None:
    with pytest.raises(ValueError):
        check_bind_address(bind)
