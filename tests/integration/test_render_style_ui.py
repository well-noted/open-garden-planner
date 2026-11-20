"""Render style switching in the real main window (Package 4, WP7; section 8.10).

A real ``GardenPlannerApp`` with the 3D engine replaced at the documented seam
(``Workspace.engine_factory``). The user picks a style from the View menu's
"Render Style" submenu, the toolbar combo and the 3D pane's button; the running
canvas, the 3D look, the stored preference and every other surface follow, and
the plan is untouched: not dirty, no undo entry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMessageBox

from open_garden_planner.app.application import GardenPlannerApp
from open_garden_planner.app.settings import get_settings
from open_garden_planner.core.render_style import RenderStyle
from open_garden_planner.core.scene3d import RecordingSink
from open_garden_planner.ui.dialogs.export_dialog import ExportPngDialog, ExportSvgDialog
from open_garden_planner.ui.view3d.init_guard import Init3DGuard
from open_garden_planner.ui.view3d.workspace import Workspace, WorkspaceMode
from tests.integration.test_view3d_workspace_widget import FakeHost


@pytest.fixture()
def window(qtbot: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    settings = get_settings()
    saved = (settings.render_style, settings.cad_callouts,
             settings.export_render_style_override)
    settings.render_style = "illustrated"
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *_a, **_k: QMessageBox.StandardButton.Discard)
    win = GardenPlannerApp()
    qtbot.addWidget(win)
    workspace = win._workspace
    assert isinstance(workspace, Workspace)
    hosts: list[FakeHost] = []

    def factory(prefer_software: bool):
        hosts.append(FakeHost(prefer_software))
        return hosts[-1], RecordingSink()

    workspace.engine_factory = factory
    workspace._guard = Init3DGuard(tmp_path / "log", is_running=lambda _pid: False)
    win.show()
    qtbot.waitExposed(win)
    win._test_hosts = hosts  # type: ignore[attr-defined]
    yield win
    (settings.render_style, settings.cad_callouts,
     settings.export_render_style_override) = saved


def _style_action(win: GardenPlannerApp, style: RenderStyle) -> QAction:
    for action in win._render_style_menu.actions():
        if action.data() == style.value:
            return action
    raise AssertionError(f"no menu action for {style}")


def _open_3d(win: GardenPlannerApp, qtbot: Any) -> None:
    win._workspace.set_mode(WorkspaceMode.VIEW3D)
    qtbot.waitUntil(lambda: win._workspace.look is not None, timeout=5000)
    qtbot.waitUntil(lambda: bool(win._test_hosts[-1].looks), timeout=8000)  # the sun arrived


def test_the_view_menu_holds_the_render_style_submenu(window: GardenPlannerApp) -> None:
    titles = [a.text().replace("&", "") for a in window._view_menu.actions()]
    assert "Render Style" in titles
    labels = [a.text() for a in window._render_style_menu.actions() if a.text()]
    assert labels == ["Illustrated", "Architectural CAD", "Line && Wash", "Leader callouts"]
    assert window.render_style_toolbar.combo.accessibleName() == "Render Style"


def test_menu_switch_reaches_canvas_3d_setting_and_every_surface(
    window: GardenPlannerApp, qtbot: Any
) -> None:
    _open_3d(window, qtbot)
    workspace = window._workspace
    scene = window.canvas_scene
    manager = window._project_manager
    commands = window.canvas_view.command_manager
    depth, dirty, revision = commands.undo_depth, manager.is_dirty, scene.paint_revision
    host = window._test_hosts[-1]
    looks_before = len(host.looks)

    _style_action(window, RenderStyle.LINE_AND_WASH).trigger()

    assert scene.render_style is RenderStyle.LINE_AND_WASH
    assert scene.paint_revision > revision                      # the paint machinery moved
    assert workspace.look.render_style == "line_and_wash"
    assert len(host.looks) > looks_before                       # the host was handed the look
    assert host.looks[-1].npr is not None
    assert get_settings().render_style == "line_and_wash"
    assert window.render_style_toolbar.combo.currentData() == "line_and_wash"
    assert _style_action(window, RenderStyle.LINE_AND_WASH).isChecked()
    assert workspace.style_button is not None
    pane_actions = [a for a in workspace.style_button.menu().actions() if a.isCheckable()]
    assert [a.isChecked() for a in pane_actions][:3] == [False, False, True]
    # a view preference: not dirty, no undo entry
    assert commands.undo_depth == depth and manager.is_dirty == dirty

    # CAD in 3D stays plain
    _style_action(window, RenderStyle.CAD).trigger()
    assert scene.render_style is RenderStyle.CAD
    assert workspace.look.render_style == "cad"
    assert host.looks[-1].npr is None
    assert commands.undo_depth == depth and manager.is_dirty == dirty


def test_toolbar_combo_and_3d_button_switch_the_same_state(
    window: GardenPlannerApp, qtbot: Any
) -> None:
    _open_3d(window, qtbot)
    combo = window.render_style_toolbar.combo
    combo.setCurrentIndex(1)
    combo.activated.emit(1)
    assert window.canvas_scene.render_style is RenderStyle.CAD
    assert _style_action(window, RenderStyle.CAD).isChecked()
    # the 3D pane's button holds the very same actions
    pane_actions = [a for a in window._workspace.style_button.menu().actions() if a.isCheckable()]
    pane_actions[2].trigger()
    assert window.canvas_scene.render_style is RenderStyle.LINE_AND_WASH
    assert combo.currentData() == "line_and_wash"
    assert get_settings().render_style == "line_and_wash"


def test_a_style_chosen_before_3d_opens_is_used_when_it_does(
    window: GardenPlannerApp, qtbot: Any
) -> None:
    _style_action(window, RenderStyle.LINE_AND_WASH).trigger()
    _open_3d(window, qtbot)
    assert window._workspace.look.render_style == "line_and_wash"


def test_leader_callouts_toggle_follows_cad(window: GardenPlannerApp) -> None:
    callouts = window._render_style_controller.callouts_action
    assert not callouts.isEnabled()
    _style_action(window, RenderStyle.CAD).trigger()
    assert callouts.isEnabled()
    callouts.setChecked(True)
    assert get_settings().cad_callouts is True
    _style_action(window, RenderStyle.ILLUSTRATED).trigger()
    assert not callouts.isEnabled()


def test_the_stored_style_is_restored_by_a_new_window(
    window: GardenPlannerApp, qtbot: Any
) -> None:
    _style_action(window, RenderStyle.CAD).trigger()
    second = GardenPlannerApp()
    qtbot.addWidget(second)
    assert second.canvas_scene.render_style is RenderStyle.CAD
    assert _style_action(second, RenderStyle.CAD).isChecked()
    assert second.render_style_toolbar.combo.currentData() == "cad"


def test_export_dialogs_default_to_follow_the_canvas(window: GardenPlannerApp,
                                                     qtbot: Any) -> None:
    _style_action(window, RenderStyle.LINE_AND_WASH).trigger()
    png = ExportPngDialog(500, 400, None, canvas_style=window.canvas_scene.render_style)
    qtbot.addWidget(png)
    assert png.render_style_override is None
    assert "Line & Wash" in png.style_group.combo.itemText(0)
    assert png.style_group.note_label.text() == ""          # a raster format keeps everything
    svg = ExportSvgDialog(window.canvas_scene.render_style)
    qtbot.addWidget(svg)
    assert svg.render_style_override is None
    assert svg.style_group.note_label.text() == "SVG/PDF: washes without paper texture or glazing"
    assert svg.paper_texture is False
    svg.style_group.combo.setCurrentIndex(svg.style_group.combo.findData("cad"))
    assert svg.render_style_override is RenderStyle.CAD
    svg.accept()
    assert get_settings().export_render_style_override == "cad"
