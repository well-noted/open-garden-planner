"""Plan georeference end to end: undo, ``.ogp`` persistence, satellite binding, MCP tools."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PyQt6.QtWidgets import QMessageBox

from open_garden_planner.app.application import GardenPlannerApp
from open_garden_planner.app.settings import get_settings
from open_garden_planner.core.georeference import PlanGeoreference, gps_to_scene, scene_to_gps
from tests.agent_api_helpers import start_server

TOKEN = "georef-transport-token-abcdef123456"
BERLIN = {
    "origin_lat": 52.52, "origin_lon": 13.405, "origin_x_cm": 100.0, "origin_y_cm": 200.0,
    "cm_per_meter_x": 100.0, "cm_per_meter_y": 100.0, "rotation_deg": 0.0,
}


@pytest.fixture()
def app(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    get_settings().show_welcome_on_startup = False
    monkeypatch.setattr(
        GardenPlannerApp, "_confirm_discard_changes", lambda _self: True, raising=False
    )
    win = GardenPlannerApp()
    qtbot.addWidget(win)
    yield win
    win._stop_agent_api()


def _depth(win: Any) -> int:
    return int(win.canvas_view.command_manager.undo_depth)


# --- commands and the project manager --------------------------------------------


def test_setting_a_georeference_is_one_undo_step(app: Any) -> None:
    pm, cm = app._project_manager, app.canvas_view.command_manager
    seen: list[Any] = []
    pm.georeference_changed.connect(seen.append)
    before = _depth(app)

    result = app._do_agent_set_georeference(dict(BERLIN))

    assert _depth(app) == before + 1
    assert result["action"] == "set_georeference" and result["undo_description"]
    assert pm.georeference == PlanGeoreference.from_dict(BERLIN)
    assert pm.georeference is not None and seen[-1] == pm.georeference

    cm.undo()
    assert pm.georeference is None and seen[-1] is None
    cm.redo()
    assert pm.georeference == PlanGeoreference.from_dict(BERLIN)


def test_clearing_is_one_undo_step_and_a_noop_is_refused(app: Any) -> None:
    app._do_agent_set_georeference(dict(BERLIN))
    depth = _depth(app)
    with pytest.raises(ValueError, match="already has"):
        app._do_agent_set_georeference(dict(BERLIN))
    assert _depth(app) == depth  # a refusal leaves the undo stack untouched

    result = app._do_agent_set_georeference(None)
    assert result["action"] == "clear_georeference" and result["georeference"] is None
    assert _depth(app) == depth + 1
    app.canvas_view.command_manager.undo()
    assert app._project_manager.georeference == PlanGeoreference.from_dict(BERLIN)


def test_an_invalid_georeference_changes_nothing(app: Any) -> None:
    depth = _depth(app)
    for bad in (
        {**BERLIN, "origin_lat": float("nan")},
        {**BERLIN, "origin_lat": True},
        {**BERLIN, "origin_lon": 200.0},
        {**BERLIN, "cm_per_meter_x": -1.0},
        {"origin_lat": 1.0},
    ):
        with pytest.raises(ValueError):
            app._do_agent_set_georeference(bad)
    assert _depth(app) == depth and app._project_manager.georeference is None
    assert not app._project_manager.is_dirty


def test_calibration_reports_residuals_in_one_undo_step(app: Any) -> None:
    app._do_agent_set_georeference(dict(BERLIN))
    base = app._project_manager.georeference
    assert base is not None
    depth = _depth(app)
    points = []
    for x, y in ((0.0, 0.0), (4000.0, 0.0), (0.0, 2500.0), (4000.0, 2500.0)):
        lat, lon = scene_to_gps(x, y, base)
        points.append({"lat": lat, "lon": lon, "x_cm": x + 30.0, "y_cm": y - 10.0, "label": f"p{x}"})

    result = app._do_agent_calibrate_georeference(points, False)

    assert _depth(app) == depth + 1
    assert result["calibration"]["method"] == "affine"
    assert result["calibration"]["rms_error_cm"] < 1e-3
    assert [r["label"] for r in result["calibration"]["residuals_cm"]] == [p["label"] for p in points]
    cal = app._project_manager.georeference
    assert cal is not None and len(cal.control_points) == 4 and cal.affine_matrix is not None
    lat, lon = scene_to_gps(2000.0, 1000.0, base)
    assert gps_to_scene(lat, lon, cal) == pytest.approx((2030.0, 990.0), abs=1e-2)

    app.canvas_view.command_manager.undo()
    assert app._project_manager.georeference == base


def test_calibrating_a_bare_plan_uses_the_first_point_as_the_origin(app: Any) -> None:
    result = app._do_agent_calibrate_georeference(
        [{"lat": 48.0, "lon": 11.0, "x_cm": 500.0, "y_cm": 700.0}], False
    )
    assert result["calibration"]["method"] == "translation"
    georef = app._project_manager.georeference
    assert georef is not None and (georef.origin_lat, georef.origin_x_cm) == (48.0, 500.0)
    # appending a second point refits as a similarity and keeps the first
    lat, lon = scene_to_gps(1500.0, 700.0, georef)
    result = app._do_agent_calibrate_georeference(
        [{"lat": lat, "lon": lon, "x_cm": 1500.0, "y_cm": 700.0}], True
    )
    assert result["calibration"]["method"] == "similarity"
    assert result["calibration"]["point_count"] == 2
    with pytest.raises(ValueError):
        app._do_agent_calibrate_georeference([], False)
    with pytest.raises(ValueError):
        app._do_agent_calibrate_georeference([{"lat": 1.0, "lon": 2.0, "x_cm": "a", "y_cm": 0}], False)


def test_the_read_view_follows_the_plans_north_angle(app: Any) -> None:
    assert app._do_agent_get_georeference()["configured"] is False
    app._do_agent_set_georeference(dict(BERLIN))
    flat = app._do_agent_get_georeference()
    assert flat["configured"] is True and flat["calibrated"] is False
    assert flat["bounds"]["max_lat"] > flat["bounds"]["min_lat"]
    app._project_manager.set_north_angle(90.0)
    turned = app._do_agent_get_georeference()
    assert turned["north_angle_deg"] == 90.0
    assert turned["bounds"]["corners"]["north_east"] != flat["bounds"]["corners"]["north_east"]


# --- persistence --------------------------------------------------------------------


def _save_and_read(app: Any, path: Path) -> dict[str, Any]:
    app._project_manager.save(app.canvas_scene, path)
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_georeference_round_trips_through_an_ogp_file(app: Any, tmp_path: Path) -> None:
    from open_garden_planner.core.project import FILE_VERSION

    app._do_agent_set_georeference(dict(BERLIN))
    lat, lon = scene_to_gps(300.0, 400.0, app._project_manager.georeference)  # type: ignore[arg-type]
    app._do_agent_calibrate_georeference(
        [{"lat": lat, "lon": lon, "x_cm": 310.0, "y_cm": 395.0, "label": "gate"}], False
    )
    stored = app._project_manager.georeference
    path = tmp_path / "geo.ogp"

    data = _save_and_read(app, path)

    assert data["version"] == FILE_VERSION  # additive: no format bump
    assert data["metadata"]["georeference"] == stored.to_dict()  # type: ignore[union-attr]
    assert "georeference" not in data  # lives under metadata only
    assert "modified" in data["metadata"]

    app._project_manager.new_project()
    assert app._project_manager.georeference is None
    app._project_manager.load(app.canvas_scene, path)
    assert app._project_manager.georeference == stored


def test_a_plan_without_a_georeference_writes_no_key_and_loads_none(app: Any, tmp_path: Path) -> None:
    path = tmp_path / "plain.ogp"
    data = _save_and_read(app, path)
    assert "georeference" not in data["metadata"]
    app._project_manager.load(app.canvas_scene, path)
    assert app._project_manager.georeference is None


def test_an_unreadable_stored_georeference_still_opens_the_plan(app: Any, tmp_path: Path) -> None:
    path = tmp_path / "bad.ogp"
    data = _save_and_read(app, path)
    for bad in ({"origin_lat": "x"}, {"origin_lat": 999.0, "origin_lon": 0.0}, [1, 2], "no"):
        data["metadata"]["georeference"] = bad
        path.write_text(json.dumps(data), encoding="utf-8")
        app._project_manager.new_project()
        app._project_manager.load(app.canvas_scene, path)
        assert app._project_manager.georeference is None


def test_a_non_finite_stored_number_rejects_the_whole_file(app: Any, tmp_path: Path) -> None:
    from open_garden_planner.core.plan_io import PlanValidationError

    path = tmp_path / "nan.ogp"
    data = _save_and_read(app, path)
    data["metadata"]["georeference"] = {**BERLIN, "origin_lat": 1e999}  # -> Infinity in JSON
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises((PlanValidationError, ValueError)):
        app._project_manager.load(app.canvas_scene, path)


def test_new_plan_forgets_the_georeference(app: Any) -> None:
    app._do_agent_set_georeference(dict(BERLIN))
    app._project_manager.new_project()
    assert app._project_manager.georeference is None


# --- satellite import auto-binding --------------------------------------------------


def _picture() -> Any:
    return SimpleNamespace(geo_metadata={"center": [48.137, 11.575], "meters_per_pixel": 0.3})


def test_a_satellite_import_binds_the_plan_automatically(app: Any) -> None:
    centre = app.canvas_scene.canvas_rect.center()
    depth = _depth(app)

    app._bind_satellite_georeference(_picture())

    georef = app._project_manager.georeference
    assert georef is not None and _depth(app) == depth + 1
    assert gps_to_scene(48.137, 11.575, georef) == pytest.approx((centre.x(), centre.y()))
    app.canvas_view.command_manager.undo()
    assert app._project_manager.georeference is None


def test_a_satellite_without_a_centre_binds_nothing(app: Any) -> None:
    app._bind_satellite_georeference(SimpleNamespace(geo_metadata=None))
    app._bind_satellite_georeference(SimpleNamespace(geo_metadata={"center": ["a", "b"]}))
    assert app._project_manager.georeference is None


def test_an_existing_georeference_is_only_replaced_when_the_user_agrees(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app._do_agent_set_georeference(dict(BERLIN))
    kept = app._project_manager.georeference
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: QMessageBox.StandardButton.No)
    app._bind_satellite_georeference(_picture())
    assert app._project_manager.georeference == kept
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: QMessageBox.StandardButton.Yes)
    app._bind_satellite_georeference(_picture())
    assert app._project_manager.georeference != kept
    assert app._project_manager.georeference.origin_lat == 48.137  # type: ignore[union-attr]


# --- the MCP surface over the real transport ----------------------------------------


def _call(server: Any, qtbot: Any, calls: list[tuple[str, dict[str, Any]]],
          token: str | None = TOKEN) -> list[Any]:
    out: list[Any] = []
    state: dict[str, Any] = {}

    async def body() -> None:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with (
            streamablehttp_client(server.url, headers=headers) as (r, w, _),
            ClientSession(r, w) as session,
        ):
            await session.initialize()
            for name, args in calls:
                call = await session.call_tool(name, args)
                out.append(call)

    def run() -> None:
        try:
            asyncio.run(body())
        except Exception as exc:
            state["error"] = exc
        finally:
            state["done"] = True

    threading.Thread(target=run, name="mcp-georef-test-client").start()
    qtbot.waitUntil(lambda: state.get("done", False), timeout=20000)
    assert state.get("error") is None, state.get("error")
    return out


def test_georeference_tools_over_the_real_transport(app: Any, qtbot: Any) -> None:
    server, _ = start_server(app._build_agent_providers(), write_token=TOKEN, writes_enabled=True)
    try:
        depth = _depth(app)
        first, setter, after = _call(server, qtbot, [
            ("get_georeference", {}),
            ("set_georeference", {"origin_lat": 52.52, "origin_lon": 13.405,
                                  "origin_x_cm": 100, "origin_y_cm": 200.0}),
            ("get_georeference", {}),
        ])
        assert first.structuredContent["configured"] is False
        assert setter.isError is False and setter.structuredContent["action"] == "set_georeference"
        assert after.structuredContent["configured"] is True
        assert after.structuredContent["georeference"]["cm_per_meter_y"] == 100.0  # defaults to x
        assert _depth(app) == depth + 1

        lat, lon = scene_to_gps(1000.0, 600.0, app._project_manager.georeference)
        (cal,) = _call(server, qtbot, [("calibrate_georeference", {"control_points": [
            {"lat": lat, "lon": lon, "x_cm": 1005.0, "y_cm": 600.0, "label": "corner"}]})])
        assert cal.isError is False
        report = cal.structuredContent["calibration"]
        assert report["method"] == "translation" and report["residuals_cm"][0]["label"] == "corner"
        assert _depth(app) == depth + 2

        (cleared,) = _call(server, qtbot, [("set_georeference", {"clear": True})])
        assert cleared.structuredContent["action"] == "clear_georeference"
        assert app._project_manager.georeference is None
    finally:
        server.stop()


def test_bad_arguments_are_refused_before_any_change(app: Any, qtbot: Any) -> None:
    server, _ = start_server(app._build_agent_providers(), write_token=TOKEN, writes_enabled=True)
    try:
        depth = _depth(app)
        calls = _call(server, qtbot, [
            ("set_georeference", {"origin_lat": True, "origin_lon": 13.0}),
            ("set_georeference", {"origin_lat": 91.0, "origin_lon": 13.0}),
            ("set_georeference", {"origin_lat": 1.0, "origin_lon": 1.0, "cm_per_meter_x": 0}),
            ("set_georeference", {"origin_lat": 1.0}),
            ("set_georeference", {"clear": True, "origin_lat": 1.0, "origin_lon": 1.0}),
            ("calibrate_georeference", {"control_points": []}),
            ("calibrate_georeference", {"control_points": [{"lat": 1, "lon": 1, "x_cm": "a", "y_cm": 0}]}),
        ])
        assert all(c.isError for c in calls)
        assert _depth(app) == depth and app._project_manager.georeference is None
    finally:
        server.stop()


def test_the_write_tools_refuse_a_missing_or_wrong_token(app: Any, qtbot: Any) -> None:
    server, _ = start_server(app._build_agent_providers(), write_token=TOKEN, writes_enabled=True)
    try:
        calls = _call(server, qtbot, [("set_georeference", {"origin_lat": 1.0, "origin_lon": 1.0})],
                      token="not-the-token")
        assert calls[0].isError
        assert app._project_manager.georeference is None
    finally:
        server.stop()
