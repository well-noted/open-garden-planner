"""Field Remote lifecycle from the app (docs 8.10, ADR-081).

Real uvicorn, real TLS, real loopback sockets: off by default, starts and stops from the app,
answers only with the token, serves the certificate whose fingerprint the pairing link carries,
and listens only on the chosen address.
"""

from __future__ import annotations

import socket
import ssl
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from open_garden_planner.app.application import GardenPlannerApp
from open_garden_planner.app.field_remote_host import ERROR, OFF, RUNNING
from open_garden_planner.app.settings import get_settings
from open_garden_planner.field_remote.server import SERVER_THREAD_NAME


@pytest.fixture(autouse=True)
def _no_welcome_dialog(_reset_app_settings: Any) -> None:
    get_settings().show_welcome_on_startup = False


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _listening(port: int, address: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((address, port), timeout=0.5):
            return True
    except OSError:
        return False


def _server_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == SERVER_THREAD_NAME and t.is_alive()]


@pytest.fixture
def app(qtbot: Any, tmp_path: Path):
    win = GardenPlannerApp()
    qtbot.addWidget(win)
    win.field_remote_host._data_dir = tmp_path / "tls"  # never the user's real key directory
    yield win
    win.field_remote_host.stop()
    assert not _server_threads()


def _client(port: int, token: str | None = None) -> httpx.Client:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(base_url=f"https://127.0.0.1:{port}", verify=False, headers=headers, timeout=5)


def test_off_by_default_and_nothing_listens(app: GardenPlannerApp) -> None:
    settings = get_settings()
    assert settings.field_remote_enabled is False
    assert settings.field_remote_records is False
    assert settings.field_remote_bind == "127.0.0.1"
    assert settings.field_remote_port != settings.agent_api_port  # its own listener
    host = app.field_remote_host
    host.maybe_start()  # what the 1.5 s startup timer calls
    assert host.status().state == OFF and not host.is_running
    assert host.pairing_url() is None
    assert not _server_threads()


def test_starts_and_stops_from_the_app_and_only_answers_with_the_token(app: GardenPlannerApp, qtbot: Any) -> None:
    settings = get_settings()
    port = _free_port()
    settings.field_remote_enabled = True
    settings.field_remote_port = port
    host = app.field_remote_host
    states: list[str] = []
    host.status_changed.connect(lambda status: states.append(status.state))

    host.maybe_start()
    assert host.status().state == RUNNING and host.is_running
    assert states == [RUNNING]
    token = settings.field_remote_token
    with _client(port) as anonymous, _client(port, token) as paired:
        assert anonymous.get("/api/status").status_code == 401
        assert anonymous.get(f"/api/status?token={token}").status_code == 400
        ok = paired.get("/api/status")
        assert ok.status_code == 200 and ok.json()["records_enabled"] is False
        assert paired.get("/api/status", headers={"Host": "attacker.example"}).status_code == 403

    host.stop()
    assert host.status().state == OFF and states == [RUNNING, OFF]
    assert not _server_threads()
    assert not _listening(port)


def test_the_server_is_in_this_process_and_apart_from_the_agent_api(app: GardenPlannerApp) -> None:
    settings = get_settings()
    settings.field_remote_enabled = True
    settings.field_remote_port = _free_port()
    app.field_remote_host.start()
    assert len(_server_threads()) == 1
    assert app._agent_server is None  # a separate listener: starting this does not start the Agent API


def test_pairing_link_fingerprint_matches_the_served_certificate(app: GardenPlannerApp) -> None:
    settings = get_settings()
    port = _free_port()
    settings.field_remote_port = port
    host = app.field_remote_host
    host.start()
    url = host.pairing_url()
    assert url is not None
    base, _, fragment = url.partition("#")
    assert base == f"https://127.0.0.1:{port}/" and "?" not in url
    fields = dict(part.split("=", 1) for part in fragment.split("&"))
    assert fields["token"] == settings.field_remote_token
    served = ssl.PEM_cert_to_DER_cert(ssl.get_server_certificate(("127.0.0.1", port)))
    import hashlib

    assert hashlib.sha256(served).hexdigest() == fields["fp"] == host.status().fingerprint_hex
    assert fields["token"] not in repr(host.status())  # the status object never carries the token


def test_binds_only_the_chosen_address(app: GardenPlannerApp) -> None:
    settings = get_settings()
    port = _free_port()
    settings.field_remote_port = port
    app.field_remote_host.start()
    assert _listening(port, "127.0.0.1")
    assert not _listening(port, "127.0.0.2")  # not a wildcard bind


@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "8.8.8.8", "not-an-address"])
def test_unsafe_bind_addresses_refuse_to_start(app: GardenPlannerApp, bind: str) -> None:
    settings = get_settings()
    settings.field_remote_bind = bind
    app.field_remote_host.start()
    status = app.field_remote_host.status()
    assert status.state == ERROR and not app.field_remote_host.is_running
    assert not _server_threads()


def test_port_in_use_is_reported_not_raised(app: GardenPlannerApp) -> None:
    settings = get_settings()
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen(1)
        settings.field_remote_port = int(squatter.getsockname()[1])
        app.field_remote_host.start()
    assert app.field_remote_host.status().state == ERROR
    assert "already in use" in app.field_remote_host.status().message


def test_permission_toggle_applies_live_and_token_rotation_invalidates_the_old_token(app: GardenPlannerApp) -> None:
    settings = get_settings()
    port = _free_port()
    settings.field_remote_port = port
    host = app.field_remote_host
    host.start()
    old = settings.field_remote_token
    with _client(port, old) as client:
        assert client.get("/api/status").json()["records_enabled"] is False
        host.set_records_enabled(True)
        assert client.get("/api/status").json()["records_enabled"] is True
        host.rotate_token()
        assert client.get("/api/status").status_code == 401  # the old token died at once
    new = settings.field_remote_token
    assert new != old
    with _client(port, new) as client:
        assert client.get("/api/status").status_code == 200


def test_regenerating_the_certificate_changes_the_fingerprint_and_keeps_running(app: GardenPlannerApp) -> None:
    get_settings().field_remote_port = _free_port()
    host = app.field_remote_host
    host.start()
    first = host.status().fingerprint_hex
    host.regenerate_certificate()
    assert host.is_running and host.status().fingerprint_hex not in ("", first)
