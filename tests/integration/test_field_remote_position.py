"""Field Remote read API and position puck from the running app (docs 8.10, WP-C).

Real uvicorn + TLS on loopback, the real main-thread snapshot hop, and the real canvas: a
posted position becomes a puck overlay that is never saved and never enters the undo stack.
The phone's HTTP calls run on a worker thread because the server hops to the GUI thread, which
the test thread must keep servicing.
"""

from __future__ import annotations

import socket
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest

from open_garden_planner.app.application import GardenPlannerApp
from open_garden_planner.app.settings import get_settings
from open_garden_planner.core.georef import GeoReference
from open_garden_planner.core.object_types import ObjectType
from open_garden_planner.ui.canvas.items.position_puck import PositionPuckItem


@pytest.fixture(autouse=True)
def _no_welcome_dialog(_reset_app_settings: Any) -> None:
    get_settings().show_welcome_on_startup = False


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Phone:
    """An httpx client that runs each call off the GUI thread and pumps Qt while it waits."""

    def __init__(self, qtbot: Any, port: int, token: str) -> None:
        self._qtbot = qtbot
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._client = httpx.Client(
            base_url=f"https://127.0.0.1:{port}", verify=False, headers={"Authorization": f"Bearer {token}"}, timeout=10
        )

    def call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        future: Future[httpx.Response] = self._pool.submit(lambda: self._client.request(method, path, **kw))
        self._qtbot.waitUntil(future.done, timeout=10_000)
        return future.result()

    def close(self) -> None:
        self._pool.shutdown(wait=True)
        self._client.close()


@pytest.fixture
def phone(qtbot: Any, tmp_path: Path):
    win = GardenPlannerApp()
    qtbot.addWidget(win)
    host = win.field_remote_host
    host._data_dir = tmp_path / "tls"
    port = _free_port()
    get_settings().field_remote_port = port
    win._project_manager.set_georef(GeoReference(origin_lat=48.0, origin_lon=11.0, scale_meters_per_unit=0.01))
    win._project_manager.mark_clean()
    host.start()
    assert host.is_running
    client = Phone(qtbot, port, get_settings().field_remote_token)
    yield win, client
    client.close()
    host.stop()


def _pos(lat: float, lon: float, **kw: Any) -> dict[str, Any]:
    return {"t": 100.0, "lat": lat, "lon": lon, "accuracy_m": 3.0, **kw}


def test_posted_position_shows_a_puck_that_is_not_saved_and_not_in_undo(phone: Any, qtbot: Any) -> None:
    win, client = phone
    scene = win.canvas_scene
    pm = win._project_manager
    assert scene.position_puck is None
    undo_before = len(win.canvas_view.command_manager._undo_stack)
    objects_before = pm.snapshot_dict(scene)["objects"]

    lat, lon = GeoReference(origin_lat=48.0, origin_lon=11.0).canvas_to_gps(1500.0, 800.0)
    res = client.call("POST", "/api/position", json=_pos(lat, lon, heading_deg=90.0, fix_quality="gps"))
    assert res.status_code == 200
    tele = res.json()["telemetry"]
    assert tele["x_cm"] == pytest.approx(1500.0, abs=0.5) and tele["y_cm"] == pytest.approx(800.0, abs=0.5)

    qtbot.waitUntil(lambda: scene.position_puck is not None, timeout=5000)  # queued from the server thread
    puck = scene.position_puck
    assert isinstance(puck, PositionPuckItem)
    assert (puck.pos().x(), puck.pos().y()) == pytest.approx((tele["x_cm"], tele["y_cm"]))
    assert puck.halo_radius_cm == pytest.approx(tele["sigma_m"] * 100.0)  # the filter's sigma, in cm
    assert puck.heading_canvas_deg is None  # one fix has no velocity yet; a heading needs motion
    assert puck.badge_text.startswith("GPS") and f"{tele['sigma_m']:.1f}" in puck.badge_text

    # An overlay, not a plan object: not serialised, no undo step, the plan stays clean.
    assert pm.snapshot_dict(scene)["objects"] == objects_before
    assert len(win.canvas_view.command_manager._undo_stack) == undo_before
    assert pm.is_dirty is False

    # A later position moves the same item. The filter refuses a repeated time or a 15 m jump in
    # a second, so this is one metre of walking a second later; the puck follows the telemetry.
    nxt = GeoReference(origin_lat=48.0, origin_lon=11.0).canvas_to_gps(1600.0, 800.0)
    res = client.call("POST", "/api/position", json={**_pos(*nxt), "t": 101.0})
    assert res.status_code == 200
    moved = res.json()["telemetry"]
    assert moved["gated"] is False and moved["x_cm"] > 1500.0
    qtbot.waitUntil(lambda: scene.position_puck is not None and scene.position_puck.pos().x() == pytest.approx(moved["x_cm"], abs=0.5))
    assert scene.position_puck is puck


def test_view_toggle_hides_the_puck_and_a_new_plan_drops_it(phone: Any, qtbot: Any) -> None:
    win, client = phone
    scene = win.canvas_scene
    client.call("POST", "/api/position", json=_pos(48.0, 11.0))
    qtbot.waitUntil(lambda: scene.position_puck is not None, timeout=5000)
    puck = scene.position_puck
    assert puck.isVisible() is True
    win._phone_position_action.setChecked(False)
    assert puck.isVisible() is False
    win._phone_position_action.setChecked(True)
    assert puck.isVisible() is True
    scene.clear()  # a new or opened plan destroys the items
    assert scene.position_puck is None


def test_puck_is_hidden_from_exports(phone: Any, qtbot: Any) -> None:
    from open_garden_planner.services.export_service import ExportService

    win, client = phone
    client.call("POST", "/api/position", json=_pos(48.0, 11.0))
    qtbot.waitUntil(lambda: win.canvas_scene.position_puck is not None, timeout=5000)
    puck = win.canvas_scene.position_puck
    hidden, _ = ExportService._hide_overlay_items(win.canvas_scene)
    assert puck.isVisible() is False and puck in hidden


def test_read_api_serves_the_live_plan_through_the_main_thread_hop(phone: Any, qtbot: Any) -> None:
    from PyQt6.QtCore import QPointF

    win, client = phone
    from open_garden_planner.core.commands import CreateItemCommand
    from open_garden_planner.ui.canvas.items import RectangleItem

    rect = RectangleItem(100, 200, 300, 150, object_type=ObjectType.RAISED_BED)
    rect.setPos(QPointF(0, 0))
    win.canvas_view.command_manager.execute(CreateItemCommand(win.canvas_scene, rect))

    state = client.call("GET", "/api/state")
    assert state.status_code == 200
    body = state.json()
    assert body["georef"]["georeferenced"] is True and body["saved"] is False and body["records_enabled"] is False
    assert any(layer["name"] for layer in body["layers"])
    again = client.call("GET", "/api/state", headers={"If-None-Match": state.headers["etag"]})
    assert again.status_code == 304

    win.field_remote_host._snap = None  # a plan change drops the cached snapshot (notify does this)
    scene = client.call("GET", "/api/scene").json()
    assert any(o["type"] == "RAISED_BED" and o["points"][0] == [100, 200] for o in scene["objects"])
    win._project_manager.mark_clean()  # no "discard changes?" dialog when the window closes


def test_plan_change_publishes_one_coalesced_event_to_the_stream(phone: Any, qtbot: Any) -> None:
    from open_garden_planner.core.commands import CreateItemCommand
    from open_garden_planner.ui.canvas.items import RectangleItem

    win, _client = phone
    hub = win.field_remote_host.events
    before = hub.last_seq
    for i in range(5):  # a burst, like a drag
        win.canvas_view.command_manager.execute(
            CreateItemCommand(win.canvas_scene, RectangleItem(i * 10, 0, 5, 5, object_type=ObjectType.RAISED_BED))
        )
    kinds = [e["type"] for e in hub.since(before)]
    assert kinds == ["plan_changed"]
    win._project_manager.mark_clean()
