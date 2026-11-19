"""Field Remote request guards, through the real ASGI stack with starlette's TestClient (ADR-081).

Each test names the threat from design section 1.5 that it pins.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from open_garden_planner.field_remote import limits, server

TOKEN = "t" * 43
PORT = 8766
BASE = f"https://127.0.0.1:{PORT}"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


async def _echo(request: Request) -> Response:
    blocked = server.require_records_enabled(request)
    if blocked is not None:
        return blocked
    return JSONResponse({"n": len(await request.body())})


class Clock:
    now = 5000.0

    def __call__(self) -> float:
        return self.now


def make_client(state: server.ServerState | None = None, **guard: Any) -> tuple[TestClient, server.ServerState]:
    state = state or server.ServerState(token=TOKEN)
    guard.setdefault("failure_delay_s", 0.0)
    app = server.build_app(
        state,
        "127.0.0.1",
        PORT,
        extra_routes=[Route("/api/echo", _echo, methods=["POST"])],
        guard_kwargs=guard,
    )
    return TestClient(app, base_url=BASE), state


def test_status_needs_the_token_and_reports_no_secrets() -> None:
    client, _ = make_client()
    ok = client.get("/api/status", headers=AUTH)
    assert ok.status_code == 200
    body = ok.json()
    assert body["api"] == server.API_VERSION and body["records_enabled"] is False
    assert TOKEN not in ok.text and "path" not in ok.text.lower()
    denied = client.get("/api/status")
    assert denied.status_code == 401 and denied.headers["www-authenticate"] == "Bearer"
    assert client.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/status", headers={"Authorization": "Basic " + TOKEN}).status_code == 401


def test_token_in_the_query_is_refused_even_when_correct() -> None:
    # Token in URL: history, proxy logs, Referer. Rejected before any credential check.
    client, _ = make_client()
    for query in (f"?token={TOKEN}", f"?access_token={TOKEN}", f"?x=1&TOKEN={TOKEN}"):
        response = client.get("/api/status" + query, headers=AUTH)
        assert response.status_code == 400 and response.json()["error"] == "token_in_url"
        assert client.get("/api/status" + query).status_code == 400  # and it never authenticates


def test_unknown_host_is_refused_dns_rebinding() -> None:
    client, _ = make_client()
    for host in ("attacker.example", "attacker.example:8766", "127.0.0.1", "127.0.0.1:9999", "10.0.0.1:8766"):
        response = client.get("/api/status", headers={**AUTH, "Host": host})
        assert response.status_code == 403 and response.json()["error"] == "bad_host", host
    assert client.get("/api/status", headers={**AUTH, "Host": f"localhost:{PORT}"}).status_code == 200


def test_public_path_does_not_skip_the_host_check() -> None:
    client, _ = make_client()
    assert client.get("/").status_code == 200  # the pairing page loads before a token exists
    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 403


def test_foreign_origin_is_refused_csrf_and_no_cors_headers_are_sent() -> None:
    client, _ = make_client()
    evil = client.get("/api/status", headers={**AUTH, "Origin": "https://evil.example"})
    assert evil.status_code == 403 and evil.json()["error"] == "bad_origin"
    preflight = client.options(
        "/api/status",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert preflight.status_code == 403
    same = client.get("/api/status", headers={**AUTH, "Origin": BASE})
    assert same.status_code == 200
    assert not any(name.startswith("access-control-") for name in same.headers)


def test_security_headers_on_every_response_including_rejections() -> None:
    client, _ = make_client()
    for response in (client.get("/api/status", headers=AUTH), client.get("/api/status"), client.get("/nope", headers=AUTH)):
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        assert "default-src 'self'" in response.headers["content-security-policy"]


def test_failures_lock_the_address_out_even_for_the_right_token() -> None:
    clock = Clock()
    client, _ = make_client(clock=clock)
    for _ in range(limits.MAX_FAILURES):
        assert client.get("/api/status", headers={"Authorization": "Bearer nope"}).status_code == 401
    locked = client.get("/api/status", headers=AUTH)
    assert locked.status_code == 429 and locked.headers["retry-after"] == str(int(limits.LOCKOUT_SECONDS))
    clock.now += limits.LOCKOUT_SECONDS + 1
    assert client.get("/api/status", headers=AUTH).status_code == 200


def test_a_good_request_clears_earlier_failures() -> None:
    client, _ = make_client()
    for _ in range(limits.MAX_FAILURES - 1):
        client.get("/api/status", headers={"Authorization": "Bearer nope"})
    assert client.get("/api/status", headers=AUTH).status_code == 200
    for _ in range(limits.MAX_FAILURES - 1):
        client.get("/api/status", headers={"Authorization": "Bearer nope"})
    assert client.get("/api/status", headers=AUTH).status_code == 200


def test_rate_limit_separates_reads_and_writes() -> None:
    client, state = make_client(clock=Clock())
    state.records_enabled = True
    reads = [client.get("/api/status", headers=AUTH).status_code for _ in range(int(limits.READ_RATE) + 5)]
    assert reads.count(200) == int(limits.READ_RATE) and reads[-1] == 429
    writes = [client.post("/api/echo", headers=AUTH, content=b"x").status_code for _ in range(10)]
    assert writes.count(200) == int(limits.WRITE_RATE) and writes[-1] == 429


def test_body_over_the_cap_is_refused_by_length_and_while_streaming() -> None:
    client, state = make_client(max_body=100)
    state.records_enabled = True
    assert client.post("/api/echo", headers=AUTH, content=b"x" * 100).json() == {"n": 100}
    assert client.post("/api/echo", headers=AUTH, content=b"x" * 101).status_code == 413

    def stream() -> Any:  # no Content-Length: chunked, so only the receive wrapper can stop it
        for _ in range(5):
            yield b"x" * 30

    assert client.post("/api/echo", headers=AUTH, content=stream()).status_code == 413


def test_records_permission_toggle_is_off_by_default_and_applies_live() -> None:
    client, state = make_client()
    assert state.records_enabled is False
    off = client.post("/api/echo", headers=AUTH, content=b"{}")
    assert off.status_code == 403 and off.json()["error"] == "records_disabled"
    state.records_enabled = True
    assert client.post("/api/echo", headers=AUTH, content=b"{}").status_code == 200
    assert client.get("/api/status", headers=AUTH).json()["records_enabled"] is True


def test_rotating_the_token_takes_effect_on_the_next_request() -> None:
    client, state = make_client()
    assert client.get("/api/status", headers=AUTH).status_code == 200
    state.token = "r" * 43
    assert client.get("/api/status", headers=AUTH).status_code == 401
    assert client.get("/api/status", headers={"Authorization": "Bearer " + "r" * 43}).status_code == 200


def test_no_record_or_plan_routes_exist_in_the_skeleton() -> None:
    names = {getattr(route, "path", "") for route in server.default_routes()}
    assert names == {"/", "/api/status"}  # WP-A ships no record routes and no plan writes


def test_server_construction_refuses_a_wildcard_bind(tmp_path: Any) -> None:
    state = server.ServerState(token=TOKEN)
    with pytest.raises(ValueError):
        server.FieldRemoteServer(state, None, bind="0.0.0.0")  # type: ignore[arg-type]
