"""MCP tool execution tests for the georeference tools (ADR-079).

Real ``GardenPlannerApp`` providers, a real streamable-HTTP MCP client on a worker
thread, the main thread pumping Qt: the same harness as ``test_agent_api_writes``.
Pins: reads are open, writes need the token, each write is ONE undoable command,
refusals leave the plan and the undo stack untouched, and ``set_initial_map``
reuses the existing Google Maps service (no new network module) and fails clearly
without a key or without the address opt-in.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from PIL import Image
from PyQt6.QtWidgets import QMessageBox

from open_garden_planner.agent_api import AgentApiServer
from open_garden_planner.app.application import GardenPlannerApp
from open_garden_planner.app.settings import get_settings
from open_garden_planner.core.georef import GeoReference
from open_garden_planner.core.object_types import ObjectType
from open_garden_planner.services import google_maps_service as gms
from open_garden_planner.ui.canvas.items import BackgroundImageItem, RectangleItem
from tests.agent_api_helpers import start_server

TOKEN = "test-write-token-12345"


@pytest.fixture()
def app(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    get_settings().show_welcome_on_startup = False
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_a, **_k: QMessageBox.StandardButton.Discard
    )
    win = GardenPlannerApp()
    qtbot.addWidget(win)
    win.canvas_view.set_snap_enabled(False)
    try:
        yield win
    finally:
        win._stop_agent_api()


def _call(app: Any, qtbot: Any, calls: list[tuple[str, dict[str, Any]]], *, token: str | None = TOKEN):
    """Run *calls* in one MCP session; return [(is_error, structured, text), ...]."""
    server, _ = start_server(
        app._build_agent_providers(), write_token=TOKEN, writes_enabled=True
    )
    out: list[tuple[bool, Any, str]] = []
    state: dict[str, Any] = {}

    async def run() -> None:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with (
            streamablehttp_client(server.url, headers=headers) as (r, w, _),
            ClientSession(r, w) as session,
        ):
            await session.initialize()
            for name, args in calls:
                res = await session.call_tool(name, args)
                text = " ".join(getattr(c, "text", "") for c in res.content)
                out.append((bool(res.isError), res.structuredContent, text))

    def drive() -> None:
        try:
            asyncio.run(run())
        except Exception as exc:
            state["error"] = exc
        finally:
            state["done"] = True

    try:
        threading.Thread(target=drive, name="mcp-georef-client").start()
        qtbot.waitUntil(lambda: state.get("done", False), timeout=30000)
    finally:
        server.stop()
    assert state.get("error") is None, state.get("error")
    return out


def _undo_depth(app: Any) -> int:
    return len(app.canvas_view.command_manager._undo_stack)


class TestReadsAndConversions:
    def test_not_georeferenced_then_set_and_convert(self, app, qtbot):
        (g0,) = _call(app, qtbot, [("get_georeference", {})], token=None)
        assert g0[1]["georeferenced"] is False

        res = _call(
            app,
            qtbot,
            [
                ("set_georeference", {"origin_lat": 48.1374, "origin_lon": 11.5755, "rotation_deg": 30}),
                ("get_georeference", {}),
                ("canvas_to_gps", {"x": 0, "y": 0}),
                ("gps_to_canvas", {"lat": 48.1374, "lon": 11.5755}),
            ],
        )
        assert [r[0] for r in res] == [False] * 4
        assert res[0][1]["action"] == "set_georeference"
        info = res[1][1]
        assert info["georeferenced"] and info["rotation_deg"] == 30.0 and info["north_angle_deg"] == 30.0
        assert app._project_manager.north_angle_deg == 30.0  # one north
        assert res[2][1]["lat"] == pytest.approx(48.1374) and res[2][1]["lon"] == pytest.approx(11.5755)
        assert res[3][1]["x"] == pytest.approx(0, abs=1e-6)

    def test_conversions_open_to_unauthenticated_and_refuse_when_unset(self, app, qtbot):
        (r,) = _call(app, qtbot, [("canvas_to_gps", {"x": 1, "y": 1})], token=None)
        assert r[0] is True and "not georeferenced" in r[2]


class TestWrites:
    def test_unauthenticated_write_is_refused_and_changes_nothing(self, app, qtbot):
        (r,) = _call(
            app, qtbot, [("set_georeference", {"origin_lat": 1.0, "origin_lon": 1.0})], token=None
        )
        assert r[0] is True
        assert app._project_manager.georef is None and _undo_depth(app) == 0

    def test_set_georeference_is_one_undo_step_with_north(self, app, qtbot):
        pm = app._project_manager
        (r,) = _call(
            app, qtbot,
            [("set_georeference", {"origin_lat": 10.0, "origin_lon": 20.0, "rotation_deg": 45})],
        )
        assert r[0] is False and _undo_depth(app) == 1
        assert pm.georef is not None and pm.north_angle_deg == 45.0
        app.canvas_view.command_manager.undo()
        assert pm.georef is None and pm.north_angle_deg == 0.0
        app.canvas_view.command_manager.redo()
        assert pm.georef.origin_lat == 10.0 and pm.north_angle_deg == 45.0

    def test_partial_update_keeps_other_fields_and_noop_is_refused(self, app, qtbot):
        pm = app._project_manager
        pm.set_georef(GeoReference(10.0, 20.0))
        res = _call(
            app, qtbot,
            [
                ("set_georeference", {"scale_meters_per_unit": 0.02}),
                ("set_georeference", {"scale_meters_per_unit": 0.02}),
                ("set_georeference", {"parcel_boundary": [[10, 20], [10.001, 20], [10.001, 20.001]]}),
                ("set_georeference", {"clear_parcel": True}),
            ],
        )
        assert [r[0] for r in res] == [False, True, False, False]
        assert pm.georef.scale_meters_per_unit == 0.02 and pm.georef.origin_lat == 10.0
        assert pm.georef.parcel_boundary is None
        assert _undo_depth(app) == 3

    @pytest.mark.parametrize(
        "args",
        [
            {},  # no georef yet and no origin
            {"origin_lat": 95.0, "origin_lon": 0.0},
            {"origin_lat": 10.0, "origin_lon": 10.0, "scale_meters_per_unit": -1},
            {"origin_lat": 10.0, "origin_lon": 10.0, "parcel_boundary": [[1, 1]]},
        ],
    )
    def test_invalid_input_refused_without_mutation(self, app, qtbot, args):
        (r,) = _call(app, qtbot, [("set_georeference", args)])
        assert r[0] is True
        assert app._project_manager.georef is None and _undo_depth(app) == 0

    def test_fit_from_explicit_points_and_object_points(self, app, qtbot):
        truth = GeoReference(48.0, 11.0, 0.01, 20.0)
        scene = app.canvas_scene
        corner = RectangleItem(1000, 1500, 100, 100, object_type=ObjectType.RAISED_BED, name="Bed")
        scene.addItem(corner)
        cx, cy = 1050.0, 1550.0  # centre
        pts = []
        for name, (x, y) in {"A": (0, 0), "B": (4000, 0), "C": (4000, 3000)}.items():
            lat, lon = truth.canvas_to_gps(x, y)
            pts.append({"name": name, "lat": lat, "lon": lon, "x": x, "y": y})
        lat, lon = truth.canvas_to_gps(cx, cy)
        pts.append({"lat": lat, "lon": lon, "item_id": str(corner.item_id)})
        (r,) = _call(app, qtbot, [("set_georeference_from_reference_points", {"points": pts})])
        assert r[0] is False, r[2]
        data = r[1]
        assert data["fit_method"] == "similarity" and data["rms_m"] < 0.01
        assert len(data["residuals_m"]) == 4
        assert _undo_depth(app) == 1
        geo = app._project_manager.georef
        assert geo.rotation_deg == pytest.approx(20.0, abs=1e-4)
        assert app._project_manager.north_angle_deg == pytest.approx(20.0, abs=1e-4)
        app.canvas_view.command_manager.undo()
        assert app._project_manager.georef is None and app._project_manager.north_angle_deg == 0.0

    def test_one_point_keeps_north(self, app, qtbot):
        app._project_manager.set_north_angle(33.0)
        (r,) = _call(
            app, qtbot,
            [("set_georeference_from_reference_points",
              {"points": [{"lat": 48.0, "lon": 11.0, "x": 100, "y": 200}]})],
        )
        assert r[0] is False and r[1]["fit_method"] == "translation"
        assert app._project_manager.north_angle_deg == 33.0

    @pytest.mark.parametrize(
        "points",
        [
            [],
            [{"lat": 1, "lon": 1}],  # neither x/y nor item_id
            [{"lat": 1, "lon": 1, "x": 0, "y": 0, "item_id": "x"}],
            [{"lat": 1, "lon": 1, "item_id": "00000000-0000-0000-0000-000000000000"}],
            [{"lat": 1, "lon": 1, "x": 0, "y": 0}, {"lat": 1, "lon": 1, "x": 5, "y": 5}],  # same GPS
            [{"lat": 1, "lon": 1 + i / 1000, "x": i * 1000, "y": 0} for i in range(4)],  # collinear
        ],
    )
    def test_bad_points_refused_without_mutation(self, app, qtbot, points):
        (r,) = _call(app, qtbot, [("set_georeference_from_reference_points", {"points": points})])
        assert r[0] is True
        assert app._project_manager.georef is None and _undo_depth(app) == 0


def _fake_fetch(calls: list[Any]):
    def fetch(bbox, *, api_key=None, timeout=10.0, cancel_check=None):
        calls.append((bbox, api_key))
        return gms.FetchResult(
            image=Image.new("RGB", (400, 300), (30, 120, 40)),
            meters_per_pixel=0.25,
            zoom=19,
            bbox=bbox,
            tile_grid=(1, 1),
        )

    return fetch


class TestSetInitialMap:
    def test_no_key_is_a_clear_error_and_changes_nothing(self, app, qtbot, monkeypatch):
        monkeypatch.setattr(app, "_resolved_google_maps_api_key", lambda: "")
        monkeypatch.setattr(gms, "fetch_bbox", lambda *_a, **_k: pytest.fail("must not fetch"))
        (r,) = _call(app, qtbot, [("set_initial_map", {"latitude": 48.0, "longitude": 11.0})])
        assert r[0] is True and "API key" in r[2]
        assert _undo_depth(app) == 0 and app._project_manager.georef is None

    def test_lat_lon_is_one_undoable_step(self, app, qtbot, monkeypatch):
        monkeypatch.setattr(app, "_resolved_google_maps_api_key", lambda: "k")
        calls: list[Any] = []
        monkeypatch.setattr(gms, "fetch_bbox", _fake_fetch(calls))
        scene, pm = app.canvas_scene, app._project_manager
        old_size = (scene.width_cm, scene.height_cm)
        pm.set_north_angle(50.0)
        before = _undo_depth(app)
        (r,) = _call(
            app, qtbot,
            [("set_initial_map", {"latitude": 48.1374, "longitude": 11.5755, "width_m": 60, "height_m": 40})],
        )
        assert r[0] is False, r[2]
        data = r[1]
        assert data["action"] == "set_initial_map" and data["zoom"] == 19
        assert _undo_depth(app) == before + 1
        assert calls and calls[0][1] == "k"
        # Image 400 x 300 px at 0.25 m/px -> 100 m x 75 m = 10000 x 7500 cm.
        assert (scene.width_cm, scene.height_cm) == pytest.approx((10000.0, 7500.0))
        images = [i for i in scene.items() if isinstance(i, BackgroundImageItem)]
        assert len(images) == 1
        assert pm.north_angle_deg == 0.0
        assert pm.location["latitude"] == pytest.approx(48.1374)
        geo = pm.georef
        assert geo is not None and geo.source == "google_static_maps"
        # The canvas centre is the requested map centre.
        la, lo = geo.canvas_to_gps(scene.width_cm / 2, scene.height_cm / 2)
        assert (la, lo) == pytest.approx((48.1374, 11.5755), abs=1e-7)
        # One Ctrl+Z restores everything.
        app.canvas_view.command_manager.undo()
        assert (scene.width_cm, scene.height_cm) == old_size
        assert not [i for i in scene.items() if isinstance(i, BackgroundImageItem)]
        assert pm.georef is None and pm.location is None and pm.north_angle_deg == 50.0
        app.canvas_view.command_manager.redo()
        assert pm.georef is not None and pm.north_angle_deg == 0.0
        assert len([i for i in scene.items() if isinstance(i, BackgroundImageItem)]) == 1

    @pytest.mark.parametrize(
        "args",
        [
            {},
            {"latitude": 1.0},
            {"latitude": 1.0, "longitude": 1.0, "address": "x"},
            {"latitude": 1.0, "longitude": 1.0, "width_m": 5},
            {"latitude": 1.0, "longitude": 1.0, "width_m": 5000},
            {"latitude": 91.0, "longitude": 1.0},
            {"address": "   "},
        ],
    )
    def test_bad_arguments_refused_before_any_network(self, app, qtbot, monkeypatch, args):
        monkeypatch.setattr(app, "_resolved_google_maps_api_key", lambda: "k")
        monkeypatch.setattr(gms, "fetch_bbox", lambda *_a, **_k: pytest.fail("must not fetch"))
        monkeypatch.setattr(gms, "geocode_address", lambda *_a, **_k: pytest.fail("must not geocode"))
        (r,) = _call(app, qtbot, [("set_initial_map", args)])
        assert r[0] is True
        assert _undo_depth(app) == 0

    def test_address_needs_the_preferences_opt_in(self, app, qtbot, monkeypatch):
        monkeypatch.setattr(app, "_resolved_google_maps_api_key", lambda: "k")
        monkeypatch.setattr(gms, "geocode_address", lambda *_a, **_k: pytest.fail("must not geocode"))
        settings = get_settings()
        original = settings.google_geocoding_enabled
        settings.google_geocoding_enabled = False
        try:
            (r,) = _call(app, qtbot, [("set_initial_map", {"address": "Marienplatz 1, Munich"})])
        finally:
            settings.google_geocoding_enabled = original
        assert r[0] is True and "Preferences" in r[2]
        assert _undo_depth(app) == 0

    def test_address_with_opt_in_geocodes_then_fetches(self, app, qtbot, monkeypatch):
        monkeypatch.setattr(app, "_resolved_google_maps_api_key", lambda: "k")
        seen: list[str] = []

        def geocode(address, *, api_key=None, timeout=10.0, region=None):
            seen.append(address)
            return gms.GeocodeResult(48.1374, 11.5755, "Marienplatz 1, Munich")

        calls: list[Any] = []
        monkeypatch.setattr(gms, "geocode_address", geocode)
        monkeypatch.setattr(gms, "fetch_bbox", _fake_fetch(calls))
        settings = get_settings()
        original = settings.google_geocoding_enabled
        settings.google_geocoding_enabled = True
        try:
            (r,) = _call(app, qtbot, [("set_initial_map", {"address": "Marienplatz 1, Munich"})])
        finally:
            settings.google_geocoding_enabled = original
        assert r[0] is False, r[2]
        assert seen == ["Marienplatz 1, Munich"]
        assert r[1]["formatted_address"] == "Marienplatz 1, Munich"
        assert app._project_manager.georef is not None and _undo_depth(app) == 1

    def test_geocoding_failure_surfaces_google_status(self, app, qtbot, monkeypatch):
        monkeypatch.setattr(app, "_resolved_google_maps_api_key", lambda: "k")

        def geocode(*_a, **_k):
            raise gms.GoogleMapsFetchError("Geocoding failed: REQUEST_DENIED (not enabled)")

        monkeypatch.setattr(gms, "geocode_address", geocode)
        settings = get_settings()
        original = settings.google_geocoding_enabled
        settings.google_geocoding_enabled = True
        try:
            (r,) = _call(app, qtbot, [("set_initial_map", {"address": "x"})])
        finally:
            settings.google_geocoding_enabled = original
        assert r[0] is True and "REQUEST_DENIED" in r[2] and _undo_depth(app) == 0

    def test_download_failure_changes_nothing(self, app, qtbot, monkeypatch):
        monkeypatch.setattr(app, "_resolved_google_maps_api_key", lambda: "k")

        def boom(*_a, **_k):
            raise gms.GoogleMapsFetchError("Invalid image response")

        monkeypatch.setattr(gms, "fetch_bbox", boom)
        (r,) = _call(app, qtbot, [("set_initial_map", {"latitude": 48.0, "longitude": 11.0})])
        assert r[0] is True and "Satellite download failed" in r[2]
        assert _undo_depth(app) == 0

    def test_locked_background_is_refused(self, app, qtbot, monkeypatch):
        monkeypatch.setattr(app, "_resolved_google_maps_api_key", lambda: "k")
        monkeypatch.setattr(gms, "fetch_bbox", lambda *_a, **_k: pytest.fail("download before the lock check"))
        png = _png_bytes()
        old = BackgroundImageItem.from_fetch_result(
            "old.png", png, meters_per_pixel=0.25, bbox_nw=(1.0, 1.0), bbox_se=(0.9, 1.1), zoom=19
        )
        app.canvas_scene.addItem(old)
        old.locked = True
        (r,) = _call(app, qtbot, [("set_initial_map", {"latitude": 48.0, "longitude": 11.0})])
        assert r[0] is True and "locked" in r[2]
        assert _undo_depth(app) == 0


def _png_bytes() -> bytes:
    import io

    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (1, 2, 3)).save(buf, format="PNG")
    return buf.getvalue()


def test_tools_are_registered_only_when_writes_are_active(app, qtbot):
    from open_garden_planner.agent_api.server import build_server

    providers = app._build_agent_providers()

    def names(server) -> set[str]:
        return {t.name for t in asyncio.run(server.list_tools())}

    reads = {"get_georeference", "canvas_to_gps", "gps_to_canvas"}
    writes = {"set_georeference", "set_georeference_from_reference_points", "set_initial_map"}
    off = names(build_server(providers))
    on = names(build_server(providers, writes_enabled=True, write_token=TOKEN))
    assert reads <= off and not (writes & off)
    assert (reads | writes) <= on
    assert isinstance(AgentApiServer, type)
