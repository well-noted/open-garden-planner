"""MCP tool execution tests for the field-photo tools (ADR-080).

Same harness as ``test_agent_api_georef``: real ``GardenPlannerApp`` providers, a
real streamable-HTTP MCP client on a worker thread, the main thread pumping Qt.
Pins: ``extract_photo_geotag`` / ``list_field_records`` are open reads, the two
write tools need the token, ``ingest_site_photo`` only touches the sidecar,
``place_geotagged_feature`` is exactly ONE undoable command carrying the species'
mature canopy radius and the field-record metadata, and every refusal leaves the
plan and the undo stack untouched.
"""

from __future__ import annotations

import asyncio
import base64
import io
import threading
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from PyQt6.QtWidgets import QMessageBox

from open_garden_planner.app.application import GardenPlannerApp
from open_garden_planner.app.settings import get_settings
from open_garden_planner.core.georef import GeoReference
from open_garden_planner.services import field_records as fr
from open_garden_planner.services.bundled_species_db import lookup_species
from tests.agent_api_helpers import start_server

TOKEN = "test-write-token-12345"
ORIGIN = (48.0, 11.0)


@pytest.fixture()
def app(qtbot: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    get_settings().show_welcome_on_startup = False
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_a, **_k: QMessageBox.StandardButton.Discard
    )
    win = GardenPlannerApp()
    qtbot.addWidget(win)
    win.canvas_view.set_snap_enabled(False)
    win._project_manager._current_file = tmp_path / "garden.ogp"  # a "saved" plan
    try:
        yield win
    finally:
        win._stop_agent_api()


def _photo_b64(bearing: float | None = 90.0, with_gps: bool = True) -> str:
    img = Image.new("RGB", (8, 6), "green")
    exif = Image.Exif()
    if with_gps:
        gps = exif.get_ifd(0x8825)
        # 1 m north of the plan origin, true bearing as given.
        gps.update({1: "N", 2: (48.0, 0.0, 0.0324), 3: "E", 4: (11.0, 0.0, 0.0)})
        if bearing is not None:
            gps.update({16: "T", 17: bearing})
        exif.get_ifd(0x8769).update({0x9003: "2026:11:19 10:00:00", 0xA405: 26})
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return base64.b64encode(buf.getvalue()).decode()


def _call(app: Any, qtbot: Any, calls: list[tuple[str, dict[str, Any]]], *, token: str | None = TOKEN):
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
        threading.Thread(target=drive, name="mcp-field-client").start()
        qtbot.waitUntil(lambda: state.get("done", False), timeout=30000)
    finally:
        server.stop()
    assert state.get("error") is None, state.get("error")
    return out


def _undo_depth(app: Any) -> int:
    return len(app.canvas_view.command_manager._undo_stack)


def _plan_path(app: Any) -> Path:
    return app._project_manager.current_file


def _find_item(app: Any, item_id: str) -> Any:
    for item in app.canvas_scene.items():
        if str(getattr(item, "item_id", "")) == item_id:
            return item
    raise AssertionError(f"item {item_id} not on the scene")


def _georeference(app: Any, rotation: float = 0.0) -> None:
    """Georeference so that ORIGIN lands at the canvas CENTRE (the scene origin is a corner,
    and an object placed off-canvas would be clamped back inside)."""
    rect = app.canvas_scene.canvas_rect
    centred = GeoReference(*ORIGIN, rotation_deg=rotation)
    lat0, lon0 = centred.canvas_to_gps(-rect.width() / 2.0, -rect.height() / 2.0)
    app._project_manager.set_georef(GeoReference(lat0, lon0, rotation_deg=rotation))
    app._project_manager.set_north_angle(rotation)


def _centre(app: Any) -> tuple[float, float]:
    rect = app.canvas_scene.canvas_rect
    return rect.width() / 2.0, rect.height() / 2.0


class TestExtract:
    def test_open_read_places_camera_and_sightline(self, app, qtbot):
        _georeference(app)
        (r,) = _call(app, qtbot, [("extract_photo_geotag", {"data_base64": _photo_b64(90.0)})], token=None)
        assert r[0] is False, r[2]
        out = r[1]
        assert out["lat"] == pytest.approx(48.0 + 0.0324 / 3600) and out["georeferenced"] is True
        assert out["camera_x_cm"] == pytest.approx(_centre(app)[0], abs=1.0)
        assert out["camera_y_cm"] == pytest.approx(_centre(app)[1] + 0.0324 / 3600 * 111_320 * 100, rel=0.05)
        assert out["sightline"]["dx"] == pytest.approx(1.0) and out["hfov_is_estimate"] is True
        assert out["hfov_source"] == "exif_35mm"

    def test_rotated_plan_turns_the_sightline(self, app, qtbot):
        _georeference(app, rotation=90.0)
        (r,) = _call(app, qtbot, [("extract_photo_geotag", {"data_base64": _photo_b64(0.0)})], token=None)
        assert r[1]["sightline"]["dx"] == pytest.approx(1.0)

    def test_photo_without_gps_is_a_partial_result_not_an_error(self, app, qtbot):
        _georeference(app)
        (r,) = _call(app, qtbot, [("extract_photo_geotag", {"data_base64": _photo_b64(with_gps=False)})], token=None)
        assert r[0] is False and r[1]["lat"] is None and r[1]["warnings"]

    def test_garbage_input_is_a_clear_error(self, app, qtbot):
        (a, b) = _call(
            app, qtbot,
            [("extract_photo_geotag", {"data_base64": "bm90IGFuIGltYWdl"}), ("extract_photo_geotag", {})],
            token=None,
        )
        assert a[0] is True and b[0] is True


class TestIngestAndList:
    def test_unauthenticated_ingest_refused_and_writes_nothing(self, app, qtbot):
        (r,) = _call(app, qtbot, [("ingest_site_photo", {"data_base64": _photo_b64()})], token=None)
        assert r[0] is True
        assert not _plan_path(app).with_name("garden.fieldnotes.json").exists()

    def test_ingest_stores_photo_and_record_without_touching_the_plan(self, app, qtbot):
        _georeference(app)
        res = _call(
            app, qtbot,
            [
                ("ingest_site_photo", {"data_base64": _photo_b64(45.0), "plant_name": "Apple", "age": "3 years", "notes": "grafted"}),
                ("list_field_records", {}),
            ],
        )
        assert [r[0] for r in res] == [False, False], res
        rec = res[0][1]["record"]
        assert rec["fix_mode"] == "exif" and rec["bearing_deg"] == 45.0 and rec["photo_path"]
        assert Path(rec["photo_path"]).is_file()
        listed = res[1][1]
        assert listed["total"] == 1 and listed["records"][0]["id"] == rec["id"]
        assert listed["records"][0]["canvas_x_cm"] is not None
        assert _undo_depth(app) == 0  # a sidecar write is not a plan edit
        assert fr.get_record(_plan_path(app), rec["id"]).plant_name == "Apple"

    def test_unsaved_plan_refused(self, app, qtbot):
        app._project_manager._current_file = None
        res = _call(app, qtbot, [("ingest_site_photo", {"data_base64": _photo_b64()}), ("list_field_records", {})])
        assert res[0][0] is True and "Save the plan" in res[0][2]
        assert res[1][0] is False and res[1][1]["total"] == 0 and res[1][1]["warnings"]

    def test_non_image_refused_and_nothing_stored(self, app, qtbot):
        (r,) = _call(app, qtbot, [("ingest_site_photo", {"data_base64": base64.b64encode(b"MZ\x90 exe").decode()})])
        assert r[0] is True
        assert not _plan_path(app).with_name("garden.fieldnotes").exists()


class TestPlace:
    def _ingest(self, app, qtbot, **extra) -> str:
        args = {"data_base64": _photo_b64(90.0), "plant_name": "Tomato", **extra}
        (r,) = _call(app, qtbot, [("ingest_site_photo", args)])
        assert r[0] is False, r[2]
        return r[1]["record"]["id"]

    def test_place_from_record_is_one_undo_step(self, app, qtbot):
        _georeference(app)
        rid = self._ingest(app, qtbot)
        before = _undo_depth(app)
        (r,) = _call(app, qtbot, [("place_geotagged_feature", {"record_id": rid, "distance_m": 2.0})])
        assert r[0] is False, r[2]
        out = r[1]
        assert _undo_depth(app) == before + 1
        species = lookup_species("Tomato")
        assert out["radius_cm"] == pytest.approx(species["max_spread_cm"] / 2.0)
        assert out["true_bearing_deg"] == 90.0 and out["record_id"] == rid
        item = _find_item(app, out["item_id"])
        block = item.metadata["field_record"]
        assert block["record_id"] == rid and block["bearing_deg"] == 90.0 and block["photo"].startswith("photos/")
        assert block["distance_m"] == 2.0
        # 2 m EAST of the camera on a north-up plan: x grows by 200 cm, y unchanged.
        cam = _call(app, qtbot, [("list_field_records", {})])[0][1]["records"][0]
        assert out["x"] == pytest.approx(cam["canvas_x_cm"] + 200.0, abs=1.0)
        assert out["y"] == pytest.approx(cam["canvas_y_cm"], abs=1.0)
        assert fr.get_record(_plan_path(app), rid).placed_object_id == out["item_id"]
        # Undo removes the plant in that single step.
        app.canvas_view.command_manager.undo()
        assert not any(str(getattr(i, "item_id", "")) == out["item_id"] for i in app.canvas_scene.items())

    def test_rotated_plan_places_along_the_rotated_sightline(self, app, qtbot):
        _georeference(app, rotation=90.0)
        rid = self._ingest(app, qtbot)
        res = _call(
            app, qtbot,
            [("list_field_records", {}), ("place_geotagged_feature", {"record_id": rid, "distance_m": 3.0})],
        )
        cam, out = res[0][1]["records"][0], res[1][1]
        # North is plan-right (rotation 90); facing east (bearing 90) is then plan-DOWN.
        assert out["x"] == pytest.approx(cam["canvas_x_cm"], abs=1.0)
        assert out["y"] == pytest.approx(cam["canvas_y_cm"] - 300.0, abs=1.0)

    def test_already_placed_record_is_refused_unless_allowed(self, app, qtbot):
        _georeference(app)
        rid = self._ingest(app, qtbot)
        res = _call(
            app, qtbot,
            [
                ("place_geotagged_feature", {"record_id": rid}),
                ("place_geotagged_feature", {"record_id": rid}),
                ("place_geotagged_feature", {"record_id": rid, "allow_duplicate": True}),
            ],
        )
        assert [r[0] for r in res] == [False, True, False]
        assert "already placed" in res[1][2]

    def test_position_and_species_without_a_record(self, app, qtbot):
        _georeference(app)
        (r,) = _call(app, qtbot, [("place_geotagged_feature", {"species": "Tomato", "lat": 48.0, "lon": 11.0})])
        assert r[0] is False, r[2]
        assert r[1]["record_id"] is None and r[1]["x"] == pytest.approx(_centre(app)[0], abs=1.0)

    @pytest.mark.parametrize(
        "args",
        [
            {"record_id": "nope"},
            {"species": "Not A Real Plant", "lat": 48.0, "lon": 11.0},
            {"species": "Tomato"},  # no position
            {"species": "Tomato", "lat": 48.0, "lon": 11.0, "distance_m": 5.0},  # distance without bearing
            {"species": "Tomato", "lat": 48.0, "lon": 11.0, "bearing_deg": 10, "distance_m": 9999},
            {"species": "Tomato", "lat": 48.0, "lon": 11.0, "object_type": "RECTANGLE"},
            {},
        ],
    )
    def test_refusals_leave_plan_and_undo_untouched(self, app, qtbot, args):
        _georeference(app)
        count = len(app.canvas_scene.items())
        (r,) = _call(app, qtbot, [("place_geotagged_feature", args)])
        assert r[0] is True
        assert _undo_depth(app) == 0 and len(app.canvas_scene.items()) == count

    def test_not_georeferenced_refused(self, app, qtbot):
        (r,) = _call(app, qtbot, [("place_geotagged_feature", {"species": "Tomato", "lat": 1.0, "lon": 1.0})])
        assert r[0] is True and "not georeferenced" in r[2] and _undo_depth(app) == 0

    def test_unauthenticated_place_refused(self, app, qtbot):
        _georeference(app)
        (r,) = _call(app, qtbot, [("place_geotagged_feature", {"species": "Tomato", "lat": 48.0, "lon": 11.0})], token=None)
        assert r[0] is True and _undo_depth(app) == 0

    def test_locked_active_layer_refused(self, app, qtbot):
        _georeference(app)
        app.canvas_scene.active_layer.locked = True
        (r,) = _call(app, qtbot, [("place_geotagged_feature", {"species": "Tomato", "lat": 48.0, "lon": 11.0})])
        assert r[0] is True and "locked" in r[2] and _undo_depth(app) == 0
