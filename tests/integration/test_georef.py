"""End-to-end georeferencing workflow: dialog, menu, satellite import, save/load, undo (ADR-079)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from PyQt6.QtWidgets import QDialog, QMessageBox

from open_garden_planner.app.application import GardenPlannerApp
from open_garden_planner.app.settings import get_settings
from open_garden_planner.core.georef import GeoReference
from open_garden_planner.core.project import ProjectManager
from open_garden_planner.ui.canvas.canvas_scene import CanvasScene
from open_garden_planner.ui.canvas.items import BackgroundImageItem
from open_garden_planner.ui.dialogs import georeference_dialog as gd
from open_garden_planner.ui.dialogs.georeference_dialog import GeoreferenceDialog


@pytest.fixture
def window(qtbot, monkeypatch, tmp_path):
    get_settings().show_welcome_on_startup = False
    monkeypatch.setattr(
        "open_garden_planner.services.example_library.user_templates_dir", lambda: tmp_path
    )
    win = GardenPlannerApp()
    monkeypatch.setattr(win, "_confirm_discard_changes", lambda: True)
    qtbot.addWidget(win)
    return win


def _png(w: int = 400, h: int = 300) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (20, 90, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _satellite_item(window: GardenPlannerApp) -> BackgroundImageItem:
    """A satellite background placed the way the import flow places it."""
    ground = GeoReference(48.1374, 11.5755)  # a 100 m x 75 m image centred here
    nw = ground.enu_to_gps(-50.0, 37.5)
    se = ground.enu_to_gps(50.0, -37.5)
    item = BackgroundImageItem.from_fetch_result(
        "sat.png",
        _png(),
        meters_per_pixel=0.25,
        bbox_nw=nw,
        bbox_se=se,
        zoom=19,
    )
    scene = window.canvas_scene
    scene.addItem(item)
    w_cm = item.pixmap().width() / item.scale_factor
    h_cm = item.pixmap().height() / item.scale_factor
    scene.resize_canvas(w_cm, h_cm)
    item.setPos(w_cm / 2 - item.pixmap().width() / 2, h_cm / 2 - item.pixmap().height() / 2)
    return item


class TestDialog:
    def test_fit_button_fills_fields_and_residuals(self, qtbot):
        truth = GeoReference(48.0, 11.0, 0.01, 25.0)
        dialog = GeoreferenceDialog(None, georef=None, north_angle_deg=0.0)
        qtbot.addWidget(dialog)
        assert dialog.georef is None  # unticked: not georeferenced
        dialog._enabled.setChecked(True)
        for i, (x, y) in enumerate([(0, 0), (4000, 0), (4000, 3000), (0, 3000)]):
            lat, lon = truth.canvas_to_gps(x, y)
            dialog._add_row(f"P{i}", lat, lon, x, y)
        dialog._on_fit()
        assert "RMS" in dialog._status.text()
        geo = dialog.georef
        assert geo is not None and geo.source == "fit"
        assert geo.origin_lat == pytest.approx(48.0, abs=1e-6)
        assert dialog.north_angle_deg == pytest.approx(25.0, abs=1e-2)
        assert all(float(dialog._table.item(r, gd._COL_RES).text()) < 0.01 for r in range(4))
        assert len(geo.reference_points) == 4

    def test_a_bad_fit_reports_instead_of_crashing(self, qtbot):
        dialog = GeoreferenceDialog(None, georef=None)
        qtbot.addWidget(dialog)
        dialog._enabled.setChecked(True)
        dialog._on_fit()  # no points
        assert "Cannot fit" in dialog._status.text()

    def test_prefills_from_existing_georef_and_location(self, qtbot):
        geo = GeoReference(10.0, 20.0, 0.02, 90.0, source="manual")
        d = GeoreferenceDialog(None, georef=geo, north_angle_deg=90.0)
        qtbot.addWidget(d)
        assert d.georef == geo
        d2 = GeoreferenceDialog(None, georef=None, location={"latitude": 51.5, "longitude": -0.12})
        qtbot.addWidget(d2)
        d2._enabled.setChecked(True)
        assert d2.georef.origin_lat == pytest.approx(51.5)


class TestMenuWorkflow:
    def test_dialog_to_undo_to_save_and_reload(self, window, qtbot, monkeypatch, tmp_path: Path):
        pm: ProjectManager = window._project_manager

        def fake_exec(self_: GeoreferenceDialog) -> int:
            self_._enabled.setChecked(True)
            self_._lat.setValue(48.1374)
            self_._lon.setValue(11.5755)
            self_._rotation.setValue(30.0)
            return int(QDialog.DialogCode.Accepted)

        monkeypatch.setattr(GeoreferenceDialog, "exec", fake_exec)
        manager = window.canvas_view.command_manager
        before = len(manager._undo_stack)
        window._on_set_georeference()
        assert len(manager._undo_stack) == before + 1
        assert pm.georef is not None and pm.north_angle_deg == 30.0
        assert pm.georef.rotation_deg == 30.0

        target = tmp_path / "g.ogp"
        pm.save(window.canvas_scene, target)
        raw = json.loads(target.read_text(encoding="utf-8"))
        assert raw["georef"]["origin_lat"] == pytest.approx(48.1374)
        assert "rotation_deg" not in raw["georef"] and raw["north_angle_deg"] == 30.0
        reopened = ProjectManager()
        reopened.load(CanvasScene(width_cm=500, height_cm=500), target)
        assert reopened.georef == pm.georef

        manager.undo()
        assert pm.georef is None and pm.north_angle_deg == 0.0
        manager.redo()
        assert pm.georef is not None and pm.north_angle_deg == 30.0

    def test_unchanged_accept_adds_no_undo_step(self, window, monkeypatch):
        window._project_manager.set_georef(GeoReference(10.0, 20.0))
        monkeypatch.setattr(
            GeoreferenceDialog, "exec", lambda _s: int(QDialog.DialogCode.Accepted)
        )
        before = len(window.canvas_view.command_manager._undo_stack)
        window._on_set_georeference()
        assert len(window.canvas_view.command_manager._undo_stack) == before

    def test_unticking_clears_it_undoably(self, window, monkeypatch):
        pm = window._project_manager
        pm.set_georef(GeoReference(10.0, 20.0))

        def fake_exec(self_: GeoreferenceDialog) -> int:
            self_._enabled.setChecked(False)
            return int(QDialog.DialogCode.Accepted)

        monkeypatch.setattr(GeoreferenceDialog, "exec", fake_exec)
        window._on_set_georeference()
        assert pm.georef is None
        window.canvas_view.command_manager.undo()
        assert pm.georef is not None

    def test_menu_has_the_entry(self, window):
        texts = [a.text().replace("&", "") for m in window.menuBar().findChildren(type(window.menuBar().addMenu("x"))) for a in m.actions()]
        assert any("Georeference" in t for t in texts)


class TestSatelliteAutoPopulate:
    def test_a_fresh_import_sets_the_georef_in_one_undo_step(self, window):
        pm = window._project_manager
        item = _satellite_item(window)
        before = len(window.canvas_view.command_manager._undo_stack)
        assert window._apply_satellite_georef(item, "google_static_maps") is True
        assert len(window.canvas_view.command_manager._undo_stack) == before + 1
        geo = pm.georef
        assert geo is not None and geo.source == "google_static_maps"
        scene = window.canvas_scene
        lat, lon = geo.canvas_to_gps(scene.width_cm / 2, scene.height_cm / 2)
        assert (lat, lon) == pytest.approx((48.1374, 11.5755), abs=1e-7)
        # North-west corner of the bbox is the top-left of the image: x=0, y=top.
        nw = GeoReference(48.1374, 11.5755).enu_to_gps(-50.0, 37.5)
        x, y = geo.gps_to_canvas(*nw)
        assert x == pytest.approx(0.0, abs=1.0) and y == pytest.approx(scene.height_cm, abs=1.0)
        window.canvas_view.command_manager.undo()
        assert pm.georef is None

    def test_a_user_calibrated_georef_is_kept_unless_confirmed(self, window, monkeypatch):
        pm = window._project_manager
        mine = GeoReference(1.0, 2.0, source="manual")
        pm.set_georef(mine)
        item = _satellite_item(window)
        asked: list[str] = []

        def no(*args: Any, **_k: Any) -> Any:
            asked.append(args[1])
            return QMessageBox.StandardButton.No

        monkeypatch.setattr(QMessageBox, "question", staticmethod(no))
        assert window._apply_satellite_georef(item, "google_static_maps") is False
        assert asked and pm.georef == mine.with_rotation(pm.north_angle_deg)

        monkeypatch.setattr(
            QMessageBox, "question", staticmethod(lambda *_a, **_k: QMessageBox.StandardButton.Yes)
        )
        assert window._apply_satellite_georef(item, "google_static_maps") is True
        assert pm.georef.source == "google_static_maps"

    def test_a_satellite_georef_is_replaced_silently_by_the_next_import(self, window, monkeypatch):
        monkeypatch.setattr(
            QMessageBox, "question", staticmethod(lambda *_a, **_k: pytest.fail("must not ask"))
        )
        item = _satellite_item(window)
        assert window._apply_satellite_georef(item, "google_static_maps")
        assert window._apply_satellite_georef(item, "google_js_view_capture")
        assert window._project_manager.georef.source == "google_js_view_capture"

    def test_a_plan_without_geo_metadata_is_left_alone(self, window):
        plain = BackgroundImageItem("x.png", _pixmap_data=_png())
        assert window._apply_satellite_georef(plain, "x") is False
        assert window._project_manager.georef is None


class TestEntryPointsAgree:
    """The satellite picker and the agent's set_initial_map share one builder (ADR-079)."""

    @staticmethod
    def _result() -> Any:
        from open_garden_planner.services import google_maps_service as gms

        ground = GeoReference(48.1374, 11.5755)
        nw = ground.enu_to_gps(-50.0, 37.5)
        se = ground.enu_to_gps(50.0, -37.5)
        return gms.FetchResult(
            image=Image.new("RGB", (400, 300), (20, 90, 30)),
            meters_per_pixel=0.25,
            zoom=19,
            bbox=gms.BoundingBox(nw[0], nw[1], se[0], se[1]),
            tile_grid=(1, 1),
        )

    @staticmethod
    def _state(win: GardenPlannerApp) -> dict[str, Any]:
        scene, pm = win.canvas_scene, win._project_manager
        (item,) = [i for i in scene.items() if isinstance(i, BackgroundImageItem)]
        meta = dict(item.geo_metadata or {})
        meta.pop("fetched_at")
        return {
            "canvas": (scene.width_cm, scene.height_cm),
            "pos": (item.pos().x(), item.pos().y()),
            "scale": item.scale_factor,
            "pixmap": (item.pixmap().width(), item.pixmap().height()),
            "meta": meta,
            "georef": pm.georef,
            "north": pm.north_angle_deg,
        }

    def test_picker_and_agent_produce_identical_plan_state(self, window, monkeypatch, qtbot):
        result = self._result()

        class FakePicker:
            DialogCode = QDialog.DialogCode
            fetch_result = result

            def __init__(self, *_a: Any, **_k: Any) -> None:
                pass

            @staticmethod
            def is_available(_key: str) -> bool:
                return True

            def exec(self) -> int:
                return int(QDialog.DialogCode.Accepted)

        import sys
        import types

        fake_module = types.ModuleType("open_garden_planner.ui.dialogs.map_picker_dialog")
        fake_module.MapPickerDialog = FakePicker  # type: ignore[attr-defined]
        monkeypatch.setitem(
            sys.modules, "open_garden_planner.ui.dialogs.map_picker_dialog", fake_module
        )
        monkeypatch.setattr(window, "_resolved_google_maps_api_key", lambda: "k")
        window._on_load_satellite_background()
        via_picker = self._state(window)

        # A second, fresh app takes the agent route with the very same fetch result.
        other = GardenPlannerApp()
        monkeypatch.setattr(other, "_confirm_discard_changes", lambda: True)
        qtbot.addWidget(other)
        other._agent_host._do_agent_set_initial_map(result, None)
        via_agent = self._state(other)

        assert via_picker == via_agent
        assert via_agent["georef"].source == "google_static_maps"
        assert via_agent["canvas"] == pytest.approx((10000.0, 7500.0))
        # The agent additionally sets the garden location; the picker never did.
        assert other._project_manager.location["latitude"] == pytest.approx(48.1374)


class TestDialogProvenance:
    def test_a_hand_edit_makes_a_satellite_georef_manual_and_drops_the_stale_rms(self, qtbot):
        geo = GeoReference(48.123456789, 11.987654321, source="google_static_maps", fit_rms_m=0.2)
        dialog = GeoreferenceDialog(None, georef=geo)
        qtbot.addWidget(dialog)
        dialog._lat.setValue(48.2)
        out = dialog.georef
        assert out.source == "manual" and out.fit_rms_m is None

    def test_an_untouched_ok_returns_the_stored_georef_unrounded(self, qtbot):
        geo = GeoReference(48.123456789, 11.987654321, 0.0123456789, source="google_static_maps")
        dialog = GeoreferenceDialog(None, georef=geo, north_angle_deg=12.3456)
        qtbot.addWidget(dialog)
        assert dialog.georef.origin_lat == 48.123456789
        assert dialog.georef.scale_meters_per_unit == 0.0123456789
        assert dialog.georef.source == "google_static_maps"

    def test_a_fit_keeps_its_own_source_and_rms(self, qtbot):
        truth = GeoReference(48.0, 11.0, 0.01, 25.0)
        dialog = GeoreferenceDialog(None, georef=GeoReference(1.0, 1.0, source="google_static_maps"))
        qtbot.addWidget(dialog)
        for i, (x, y) in enumerate([(0, 0), (4000, 0), (4000, 3000)]):
            lat, lon = truth.canvas_to_gps(x, y)
            dialog._add_row(f"P{i}", lat, lon, x, y)
        dialog._on_fit()
        out = dialog.georef
        assert out.source == "fit" and out.fit_rms_m is not None and out.fit_rms_m < 0.01
