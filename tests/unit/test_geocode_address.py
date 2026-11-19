"""Google Geocoding client (ADR-079): parsing, error mapping, key scrubbing."""

from __future__ import annotations

import pytest

from open_garden_planner.config.settings import AppSettings
from open_garden_planner.services import google_maps_service as gms

KEY = "AIza-secret-key"
OK = {
    "status": "OK",
    "results": [
        {
            "formatted_address": "Marienplatz 1, 80331 Munchen, Germany",
            "geometry": {"location": {"lat": 48.1374, "lng": 11.5755}},
        }
    ],
}


def _patch(monkeypatch, payload=None, exc=None):
    seen = {}

    def fake(url, *, max_bytes, timeout):
        seen["url"] = url
        if exc is not None:
            raise exc
        return payload

    monkeypatch.setattr(gms, "fetch_json", fake)
    return seen


def test_ok(monkeypatch):
    seen = _patch(monkeypatch, OK)
    r = gms.geocode_address("  Marienplatz 1 Munchen ", api_key=KEY, region="de")
    assert (r.lat, r.lng) == (48.1374, 11.5755)
    assert "Marienplatz" in r.formatted_address
    assert seen["url"].startswith("https://maps.googleapis.com/maps/api/geocode/json?")
    assert "address=Marienplatz+1+Munchen" in seen["url"] and "region=de" in seen["url"]


def test_missing_key(monkeypatch):
    monkeypatch.delenv("OGP_GOOGLE_MAPS_KEY", raising=False)
    _patch(monkeypatch, OK)
    with pytest.raises(gms.GoogleMapsKeyMissingError):
        gms.geocode_address("x", api_key="  ")


@pytest.mark.parametrize("addr", ["", "   ", "a" * 301, None, 5])
def test_bad_address_never_hits_network(monkeypatch, addr):
    seen = _patch(monkeypatch, OK)
    with pytest.raises(gms.GoogleMapsFetchError):
        gms.geocode_address(addr, api_key=KEY)  # type: ignore[arg-type]
    assert "url" not in seen


def test_zero_results(monkeypatch):
    _patch(monkeypatch, {"status": "ZERO_RESULTS", "results": []})
    with pytest.raises(gms.GoogleMapsFetchError, match="No location"):
        gms.geocode_address("nowhere", api_key=KEY)


def test_request_denied_keeps_status_and_scrubs_key(monkeypatch):
    _patch(
        monkeypatch,
        {"status": "REQUEST_DENIED", "error_message": f"API not enabled for key={KEY}"},
    )
    with pytest.raises(gms.GoogleMapsFetchError) as info:
        gms.geocode_address("x", api_key=KEY)
    text = str(info.value)
    assert "REQUEST_DENIED" in text and KEY not in text


def test_transport_error_is_scrubbed(monkeypatch):
    _patch(monkeypatch, exc=ValueError(f"boom https://x/?key={KEY}"))
    with pytest.raises(gms.GoogleMapsFetchError) as info:
        gms.geocode_address("x", api_key=KEY)
    assert KEY not in str(info.value)


@pytest.mark.parametrize(
    "payload",
    [
        [], "x", {"status": "OK"}, {"status": "OK", "results": []},
        {"status": "OK", "results": [{"geometry": {"location": {"lat": "a", "lng": 1}}}]},
        {"status": "OK", "results": [{"geometry": {"location": {"lat": 99, "lng": 1}}}]},
        {"status": "OK", "results": [{"geometry": {"location": {"lat": float("nan"), "lng": 1}}}]},
        {"status": "UNKNOWN_ERROR"},
    ],
)
def test_malformed_replies(monkeypatch, payload):
    _patch(monkeypatch, payload)
    with pytest.raises(gms.GoogleMapsFetchError):
        gms.geocode_address("x", api_key=KEY)


def test_fetch_json_limits(monkeypatch):
    from open_garden_planner.services import image_download as idl

    class Resp:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_content(self, chunk_size):
            yield self._body

    monkeypatch.setattr(idl.requests, "get", lambda *a, **k: Resp(200, b'{"a": 1}'))
    assert idl.fetch_json("u", max_bytes=100, timeout=1) == {"a": 1}
    with pytest.raises(ValueError, match="limit"):
        idl.fetch_json("u", max_bytes=3, timeout=1)
    monkeypatch.setattr(idl.requests, "get", lambda *a, **k: Resp(500, b"{}"))
    with pytest.raises(ValueError, match="HTTP 500"):
        idl.fetch_json("u", max_bytes=100, timeout=1)
    monkeypatch.setattr(idl.requests, "get", lambda *a, **k: Resp(200, b"\xff\xfe"))
    with pytest.raises(ValueError, match="JSON"):
        idl.fetch_json("u", max_bytes=100, timeout=1)


def test_setting_defaults_off_and_round_trips(qtbot):
    s = AppSettings()
    original = s.google_geocoding_enabled
    try:
        s.google_geocoding_enabled = False
        assert s.google_geocoding_enabled is False
        s.google_geocoding_enabled = True
        assert s.google_geocoding_enabled is True
    finally:
        s.google_geocoding_enabled = original
    assert AppSettings.DEFAULT_GOOGLE_GEOCODING_ENABLED is False
