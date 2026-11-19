"""Token, pairing URL and Host/Origin rules for Field Remote. Stdlib only, no Qt.

Unlike the Agent API (which also reads ``?token=``), the token is accepted ONLY in the
``Authorization: Bearer`` header. A token in a URL lands in browser history, proxy logs and
Referer headers; the pairing URL carries it in the fragment, which is never sent.
"""

from __future__ import annotations

import ipaddress
import secrets
import urllib.parse

#: Query-string keys that look like a credential. Any of them is refused outright (design 1.4).
_TOKEN_QUERY_KEYS = frozenset({"token", "access_token", "auth", "authorization", "key", "api_key"})


def new_token() -> str:
    """A fresh 256-bit URL-safe token."""
    return secrets.token_urlsafe(32)


def token_matches(presented: str | None, expected: str | None) -> bool:
    """Constant-time compare that fails closed.

    ``secrets.compare_digest`` raises ``TypeError`` on non-ASCII ``str`` input, and a hostile
    header is untrusted, so the ASCII guard turns that into a plain rejection.
    """
    if not presented or not expected:
        return False
    if not (presented.isascii() and expected.isascii()):
        return False
    return secrets.compare_digest(presented, expected)


def bearer_from_headers(headers: list[tuple[bytes, bytes]]) -> str | None:
    """The bearer token from the ``Authorization`` header, or None. Never the query string."""
    for key, value in headers:
        if key == b"authorization":
            raw = value.decode("latin-1")
            if raw[:7].lower() == "bearer ":
                return raw[7:].strip() or None
            return None
    return None


def query_carries_token(query_string: bytes) -> bool:
    """True if the query string has a credential-looking key (checked case-insensitively)."""
    if not query_string:
        return False
    for key, _ in urllib.parse.parse_qsl(query_string.decode("latin-1"), keep_blank_values=True):
        if key.lower() in _TOKEN_QUERY_KEYS:
            return True
    return False


def host_header(headers: list[tuple[bytes, bytes]]) -> str | None:
    for key, value in headers:
        if key == b"host":
            return value.decode("latin-1").strip().lower()
    return None


def origin_header(headers: list[tuple[bytes, bytes]]) -> str | None:
    for key, value in headers:
        if key == b"origin":
            return value.decode("latin-1").strip().lower()
    return None


def allowed_hosts(bind: str, port: int, extra_names: tuple[str, ...] = ()) -> frozenset[str]:
    """Exact ``Host`` header values accepted (DNS-rebinding defence).

    A rebinding page reaches this server under ITS OWN name, so the Host header is that name.
    Only the bind address (plus ``localhost`` when loopback, plus names the user listed, such as
    a Tailscale ``*.ts.net`` name) is accepted, and always with the real port.
    """
    names = {bind.lower(), *(n.lower() for n in extra_names)}
    if ipaddress.ip_address(bind).is_loopback:
        names.add("localhost")
    hosts: set[str] = set()
    for name in names:
        shown = f"[{name}]" if ":" in name else name  # an IPv6 literal is bracketed in Host
        hosts.add(f"{shown}:{port}")
    return frozenset(hosts)


def origin_allowed(origin: str | None, hosts: frozenset[str]) -> bool:
    """No Origin (same-origin GET, non-browser client) is fine: the bearer token still gates it.
    A present Origin must be one of OUR https origins; anything else is a cross-site page (CSRF)."""
    if origin is None:
        return True
    return origin.startswith("https://") and origin.removeprefix("https://") in hosts


def pairing_url(host: str, port: int, token: str, fingerprint_hex: str) -> str:
    """``https://host:port/#token=..&fp=..``. Fragment only: it is never sent in a request, so
    it stays out of logs and Referer. ``fp`` is the (non-secret) certificate SHA-256."""
    shown = f"[{host}]" if ":" in host else host
    fragment = urllib.parse.urlencode({"token": token, "fp": fingerprint_hex})
    return f"https://{shown}:{port}/#{fragment}"


def format_fingerprint(fingerprint_hex: str) -> str:
    """``AB:CD:...`` for showing to a person who compares it with the phone's warning page."""
    upper = fingerprint_hex.upper()
    return ":".join(upper[i : i + 2] for i in range(0, len(upper), 2))
