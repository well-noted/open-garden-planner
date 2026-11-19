"""Integration tests for the desktop Field records review panel (WP-R, design 1.7).

Real ``GardenPlannerApp``, real panel and approver, real command stack. Pins: approving is
ONE undo step through the plan's own commands, a candidate approval supersedes its siblings
(and Reopen restores them), the Irrigation layer is created with the polyline in one macro
and a locked one is refused, and a reference point reaches the georeference fit dialog.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QMessageBox

from open_garden_planner.app.application import GardenPlannerApp
from open_garden_planner.app.settings import get_settings
from open_garden_planner.core.georef import GeoReference
from open_garden_planner.services import field_records as fr
from open_garden_planner.ui.dialogs import georeference_dialog as gd
from open_garden_planner.ui.panels.field_records_panel import FieldRecordsPanel

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
    win._project_manager._current_file = tmp_path / "garden.ogp"
    rect = win.canvas_scene.canvas_rect
    centred = GeoReference(*ORIGIN)
    lat0, lon0 = centred.canvas_to_gps(-rect.width() / 2.0, -rect.height() / 2.0)
    win._project_manager.set_georef(GeoReference(lat0, lon0))
    win._project_manager.set_north_angle(0.0)
    return win


@pytest.fixture()
def panel(app: Any, qtbot: Any) -> FieldRecordsPanel:
    widget = FieldRecordsPanel(app._field_record_approver())
    qtbot.addWidget(widget)
    return widget


def _plan(app: Any) -> str:
    return str(app._project_manager.current_file)


def _depth(app: Any) -> int:
    return len(app.canvas_view.command_manager._undo_stack)


def _add(app: Any, **fields: Any) -> fr.FieldRecord:
    fields.setdefault("lat", ORIGIN[0])
    fields.setdefault("lon", ORIGIN[1])
    return fr.add_record(_plan(app), fields)


def _status(app: Any, record_id: str) -> str:
    return fr.get_record(_plan(app), record_id).status


def _line(app: Any) -> list[list[float]]:
    rect = app.canvas_scene.canvas_rect
    cx, cy = rect.width() / 2.0, rect.height() / 2.0
    return [
        [ORIGIN[0], ORIGIN[1], cx, cy],
        [ORIGIN[0], ORIGIN[1], cx + 200.0, cy],
        [ORIGIN[0], ORIGIN[1], cx + 200.0, cy + 150.0],
    ]


def _all_items(app: Any) -> list[str]:
    return [str(i.item_id) for i in app.canvas_scene.items() if hasattr(i, "item_id")]


def _layer_names(app: Any) -> list[str]:
    return [lyr.name for lyr in app.canvas_scene.layers]


class TestApprove:
    def test_plant_is_one_undo_step(self, app, panel):
        rec = _add(app, plant_name="Apple", species_key="Malus domestica")
        before, items = _depth(app), set(_all_items(app))
        result = panel.approve_record(rec.id)
        assert result is not None and result.item_id
        assert _depth(app) == before + 1
        got = fr.get_record(_plan(app), rec.id)
        assert got.status == "placed" and got.placed_object_id == result.item_id
        app.canvas_view.command_manager.undo()
        assert set(_all_items(app)) == items  # one undo removes the plant
        assert _depth(app) == before

    def test_reject_keeps_the_record_and_changes_no_plan(self, app, panel):
        rec = _add(app, plant_name="Apple", species_key="Malus domestica")
        before = _depth(app)
        panel.reject_record(rec.id)
        assert _status(app, rec.id) == "rejected" and _depth(app) == before
        assert rec.id not in panel.visible_record_ids()  # default filter: pending
        panel.status_filter.setCurrentIndex(0)  # all statuses: a hidden record stays reachable
        assert rec.id in panel.visible_record_ids()

    def test_unknown_species_is_refused_with_a_message(self, app, panel):
        rec = _add(app, plant_name="Zzzzz not a plant")
        before = _depth(app)
        assert panel.approve_record(rec.id) is None
        assert panel.message.text() and _depth(app) == before
        assert _status(app, rec.id) == "pending"

    def test_button_click_approves(self, app, panel, qtbot):
        rec = _add(app, kind="note", notes="wet corner")
        panel.select_record(rec.id)
        qtbot.mouseClick(panel.cards[0].approve_button, Qt.MouseButton.LeftButton)
        assert _status(app, rec.id) == "placed"

    def test_unsaved_plan_shows_a_message(self, app, panel):
        app._project_manager._current_file = None
        panel.refresh()
        assert "Save the plan" in panel.message.text()


class TestPlacementRequestGroup:
    def _three(self, app: Any) -> list[fr.FieldRecord]:
        return [
            _add(
                app,
                kind="placement_request",
                what="apple",
                group_id="grp1",
                plant_name="Apple",
                species_key="Malus domestica",
                lat=ORIGIN[0] + i * 1e-6,
            )
            for i in range(3)
        ]

    def test_approving_one_supersedes_the_others_and_reopen_restores(self, app, panel):
        a, b, c = self._three(app)
        panel.select_record(a.id)
        assert len(panel.cards) == 3  # side-by-side comparison
        before = _depth(app)
        panel.approve_record(b.id)
        assert (_status(app, a.id), _status(app, b.id), _status(app, c.id)) == (
            "superseded",
            "placed",
            "superseded",
        )
        assert _depth(app) == before + 1
        app.canvas_view.command_manager.undo()  # the placement is undone, the sidecar is not
        assert _status(app, b.id) == "placed"  # dangling placed_object_id (ADR-080)
        panel.status_filter.setCurrentIndex(0)
        panel.select_record(b.id)
        assert panel.reopen_group_button.isEnabled()  # dangling object: reopen is offered
        panel.reopen_record(b.id)
        assert {_status(app, r.id) for r in (a, b, c)} == {"pending"}
        assert fr.get_record(_plan(app), b.id).placed_object_id is None

    def test_reject_all(self, app, panel):
        recs = self._three(app)
        before = _depth(app)
        panel.reject_group("grp1")
        assert {_status(app, r.id) for r in recs} == {"rejected"}
        assert _depth(app) == before


class TestIrrigation:
    def test_creates_layer_and_polyline_in_one_undo(self, app, panel):
        rec = _add(
            app,
            kind="irrigation_path",
            geometry={"type": "polyline", "points": _line(app)},
            name="Drip line",
            diameter_mm=16,
        )
        assert "Irrigation" not in _layer_names(app)
        before, items = _depth(app), set(_all_items(app))
        result = panel.approve_record(rec.id)
        assert result is not None and result.item_id
        assert _depth(app) == before + 1  # layer + line: ONE step
        irrigation = next(lyr for lyr in app.canvas_scene.layers if lyr.name == "Irrigation")
        item = next(i for i in app.canvas_scene.items() if str(getattr(i, "item_id", "")) == result.item_id)
        assert str(item.layer_id) == str(irrigation.id)
        assert item.metadata["field_record"]["diameter_mm"] == 16
        assert _status(app, rec.id) == "placed"
        app.canvas_view.command_manager.undo()
        assert "Irrigation" not in _layer_names(app)
        assert set(_all_items(app)) == items
        app.canvas_view.command_manager.redo()
        assert "Irrigation" in _layer_names(app)

    def test_existing_layer_is_reused_not_duplicated(self, app, panel):
        first = _add(app, kind="irrigation_path", geometry={"type": "polyline", "points": _line(app)})
        second = _add(app, kind="irrigation_path", geometry={"type": "polyline", "points": _line(app)})
        panel.approve_record(first.id)
        panel.approve_record(second.id)
        assert _layer_names(app).count("Irrigation") == 1

    def test_locked_irrigation_layer_is_refused_and_never_unlocked(self, app, panel):
        from open_garden_planner.models.layer import Layer

        layer = Layer(name="Irrigation", locked=True)
        app.canvas_scene.layers.append(layer)
        rec = _add(app, kind="irrigation_path", geometry={"type": "polyline", "points": _line(app)})
        before = _depth(app)
        assert panel.approve_record(rec.id) is None
        assert "locked" in panel.message.text()
        assert layer.locked and _depth(app) == before and _status(app, rec.id) == "pending"

    def test_locked_active_layer_refuses_a_plant(self, app, panel):
        app.canvas_scene.active_layer.locked = True
        rec = _add(app, plant_name="Apple", species_key="Malus domestica")
        before = _depth(app)
        assert panel.approve_record(rec.id) is None
        assert "locked" in panel.message.text() and _depth(app) == before


class TestReferencePoint:
    def test_reaches_the_fit_dialog_and_is_marked_after_accept(self, app, panel, monkeypatch):
        seen: dict[str, Any] = {}

        def fake_exec(dialog: Any) -> QDialog.DialogCode:
            table = dialog._table
            seen["names"] = [table.cellWidget(r, 0).text() for r in range(table.rowCount())]
            seen["status"] = dialog._status.text()
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(gd.GeoreferenceDialog, "exec", fake_exec)
        a = _add(
            app, kind="reference_point", name="Corner A", lat=48.0, lon=11.0,
            geometry={"type": "point", "lat": 48.0, "lon": 11.0, "x_cm": 100.0, "y_cm": 100.0},
        )
        b = _add(
            app, kind="reference_point", name="Corner B", lat=48.0, lon=11.001,
            geometry={"type": "point", "lat": 48.0, "lon": 11.001, "x_cm": 8300.0, "y_cm": 100.0},
        )
        panel.approve_record(a.id)
        assert seen["names"][-1] == "Corner A"
        result = panel.approve_record(b.id)
        assert {"Corner A", "Corner B"} <= set(seen["names"])
        assert "RMS" in seen["status"]  # the fit rms is shown before the user confirms
        assert result.georef_updated and _status(app, b.id) == "placed"

    def test_cancelled_dialog_leaves_the_record_pending(self, app, panel, monkeypatch):
        monkeypatch.setattr(gd.GeoreferenceDialog, "exec", lambda _d: QDialog.DialogCode.Rejected)
        rec = _add(
            app, kind="reference_point", name="X", lat=48.0, lon=11.0,
            geometry={"type": "point", "lat": 48.0, "lon": 11.0, "x_cm": 5.0, "y_cm": 5.0},
        )
        before = _depth(app)
        result = panel.approve_record(rec.id)
        assert result.cancelled and _depth(app) == before and _status(app, rec.id) == "pending"
