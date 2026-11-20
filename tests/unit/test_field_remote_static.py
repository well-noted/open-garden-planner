"""The Field Remote phone page (WP-E): served from memory, within budget, and free of the
patterns the CSP and the XSS threat in design 1.5 rule out.
"""

from __future__ import annotations

import re

import pytest
from starlette.testclient import TestClient

from open_garden_planner.core.georef import GeoReference
from open_garden_planner.field_remote import server, static

TOKEN = "t" * 43
PORT = 8766
BASE = f"https://127.0.0.1:{PORT}"
SOURCES = dict(static.ASSETS)
SCRIPTS = {url: a.body.decode("utf-8") for url, a in SOURCES.items() if url.endswith(".js")}


def client() -> TestClient:
    app = server.build_app(server.ServerState(token=TOKEN), "127.0.0.1", PORT, guard_kwargs={"failure_delay_s": 0.0})
    return TestClient(app, base_url=BASE)


def test_the_expected_files_are_bundled() -> None:
    assert set(SOURCES) == {
        "/", "/static/app.css", "/static/app.js", "/static/api.js", "/static/geo.js", "/static/map.js",
        "/static/survey.js", "/static/record.js",
    }


def test_size_budget() -> None:
    total = sum(len(a.body) for a in SOURCES.values())
    assert total <= static.BUDGET_BYTES, f"{total} bytes"


def test_page_and_scripts_load_without_a_token_but_the_api_does_not() -> None:
    c = client()
    page = c.get("/")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    for url in SOURCES:
        r = c.get(url)
        assert r.status_code == 200, url
        # every response carries the CSP, no-referrer and no-store (design 1.5)
        assert "default-src 'self'" in r.headers["content-security-policy"]
        assert r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"
        assert r.headers["x-content-type-options"] == "nosniff"
    assert c.get("/static/app.js").headers["content-type"].startswith("text/javascript")
    assert c.get("/api/state").status_code == 401
    assert TOKEN not in page.text


def test_unknown_static_paths_and_traversal_are_not_served() -> None:
    c = client()
    for path in ("/static/", "/static/nope.js", "/static/../server.py", "/static/%2e%2e/server.py", "/static/app.js/x"):
        assert c.get(path).status_code in (401, 404), path
    assert "server.py" not in "".join(SOURCES)


def test_html_has_no_inline_script_style_or_external_urls() -> None:
    html = SOURCES["/"].body.decode("utf-8")
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, re.S | re.I)
    assert scripts and all("src=" in attrs and not body.strip() for attrs, body in scripts)
    assert not re.search(r"<style\b|\sstyle\s*=|\son[a-z]+\s*=|javascript:", html, re.I)
    for name, text in [("index.html", html), *((u, a.body.decode("utf-8")) for u, a in SOURCES.items() if u.endswith(".css"))]:
        assert not re.search(r"(?:https?:)?//[a-z0-9.-]+\.[a-z]{2,}", re.sub(r"https?://www\.w3\.org\S*", "", text), re.I), name


@pytest.mark.parametrize("url", sorted(SCRIPTS))
def test_scripts_use_no_dynamic_code_or_markup_sinks(url: str) -> None:
    text = re.sub(r"//[^\n]*", "", SCRIPTS[url])
    banned = [
        r"\beval\s*\(", r"\bnew\s+Function\b", r"\binnerHTML\b", r"\bouterHTML\b", r"insertAdjacentHTML",
        r"document\.write", r"\bsetTimeout\s*\(\s*['\"]", r"\bsetInterval\s*\(\s*['\"]", r"localStorage",
        r"\bhttps?://", r"importScripts", r"\.srcdoc\b", r"setAttribute\(\s*['\"](?:style|on)",
    ]
    for pattern in banned:
        assert not re.search(pattern, text), f"{url}: {pattern}"


def test_the_token_never_goes_into_a_url() -> None:
    api = SCRIPTS["/static/api.js"]
    assert "Authorization: 'Bearer '" in api and "?token" not in api and "sessionStorage" in api
    assert "replaceState" in api  # the fragment is stripped after it is read


def test_photos_are_sent_as_the_original_file() -> None:
    app = SCRIPTS["/static/app.js"]
    assert 'append(\'photo\', photo' in app
    assert not re.search(r"toBlob|toDataURL|drawImage", app)  # never re-encoded (EXIF stays)


def test_capture_input_keeps_the_exif_camera_path() -> None:
    html = SOURCES["/"].body.decode("utf-8")
    assert re.search(r'type="file"[^>]*accept="image/\*"[^>]*capture="environment"', html)


def test_georef_summary_gives_the_phone_what_it_needs_to_invert_a_plan_point() -> None:
    from open_garden_planner.field_remote import projection

    geo = GeoReference(origin_lat=48.0, origin_lon=11.0, scale_meters_per_unit=0.01)
    summary = projection.georef_summary({"georef": geo.to_dict()})
    assert summary["m_per_deg"] > 100_000 and "scale_meters_per_unit" in summary and "rotation_deg" in summary
