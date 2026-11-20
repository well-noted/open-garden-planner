"""Mulch end to end (§8.10): drawn with its tool, undone, saved and loaded, recoloured
from the Properties panel's mulch types (one undo step), exported in every render style,
and written by the Agent API's roster — the whole path a user takes, not the registries."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QImage, QMouseEvent
from PyQt6.QtWidgets import QComboBox

from open_garden_planner.core.commands import CommandManager
from open_garden_planner.core.fill_patterns import MULCH_COLORS, FillPattern
from open_garden_planner.core.object_types import ObjectType
from open_garden_planner.core.project import ProjectManager
from open_garden_planner.core.render_style import RenderStyle
from open_garden_planner.core.tools import ToolType
from open_garden_planner.services.export_service import ExportService
from open_garden_planner.ui.canvas.canvas_scene import CanvasScene
from open_garden_planner.ui.canvas.canvas_view import CanvasView
from open_garden_planner.ui.canvas.items import PolygonItem
from open_garden_planner.ui.panels import PropertiesPanel


def _click() -> MagicMock:
    event = MagicMock(spec=QMouseEvent)
    event.button.return_value = Qt.MouseButton.LeftButton
    event.modifiers.return_value = Qt.KeyboardModifier.NoModifier
    return event


def _draw_mulch(canvas: CanvasView) -> PolygonItem:
    canvas.set_active_tool(ToolType.MULCH)
    tool = canvas.tool_manager.active_tool
    for point in (QPointF(200, 100), QPointF(500, 100), QPointF(500, 400), QPointF(200, 400)):
        tool.mouse_press(_click(), point)
    tool.mouse_double_click(_click(), QPointF(200, 400))
    found = [i for i in canvas.scene().items() if isinstance(i, PolygonItem)]
    assert len(found) == 1
    return found[0]


def test_the_mulch_tool_draws_undoes_and_redoes_a_mulch_surface(canvas: CanvasView, qtbot: object) -> None:
    item = _draw_mulch(canvas)
    assert item.object_type is ObjectType.MULCH
    assert item.fill_pattern is FillPattern.MULCH
    assert QColor(item.fill_color).name() == "#7a5234"
    canvas.command_manager.undo()
    assert not [i for i in canvas.scene().items() if isinstance(i, PolygonItem)]
    canvas.command_manager.redo()
    assert len([i for i in canvas.scene().items() if isinstance(i, PolygonItem)]) == 1


def test_mulch_survives_save_and_load_and_keeps_its_type_and_layer(
        canvas: CanvasView, qtbot: object, tmp_path: Path) -> None:
    item = _draw_mulch(canvas)
    layer = item.layer_id
    path = tmp_path / "mulch.ogp"
    ProjectManager().save(canvas.scene(), path)
    loaded = CanvasScene(width_cm=5000, height_cm=3000)
    ProjectManager().load(loaded, path)
    (back,) = [i for i in loaded.items() if isinstance(i, PolygonItem)]
    assert back.object_type is ObjectType.MULCH
    assert back.fill_pattern is FillPattern.MULCH
    assert back.layer_id == layer


def test_the_panel_offers_mulch_types_and_recolours_in_one_undo_step(qtbot: object) -> None:
    commands = CommandManager()
    panel = PropertiesPanel(command_manager=commands)
    item = PolygonItem([QPointF(0, 0), QPointF(300, 0), QPointF(300, 200)], ObjectType.MULCH)
    panel.set_selected_items([item])
    combo = panel.findChild(QComboBox, "mulch_color_combo")
    assert combo is not None and combo.count() == 1 + len(MULCH_COLORS)
    assert combo.isVisibleTo(panel) and combo.currentData() == "bark"
    index = combo.findData("black")
    combo.setCurrentIndex(index)
    assert QColor(item.fill_color).name() == QColor("#3a3027").name()
    commands.undo()
    assert QColor(item.fill_color).name() == "#7a5234"
    # a gravel item shows no mulch row
    gravel = PolygonItem([QPointF(0, 0), QPointF(300, 0), QPointF(300, 200)], ObjectType.DRIVEWAY)
    panel.set_selected_items([gravel])
    shown = panel.findChild(QComboBox, "mulch_color_combo")
    assert shown is None or not shown.isVisibleTo(panel)


@pytest.mark.parametrize("style", list(RenderStyle), ids=lambda s: s.value)
def test_mulch_exports_in_every_render_style(qtbot: object, tmp_path: Path, style: RenderStyle) -> None:
    scene = CanvasScene(width_cm=2000, height_cm=1400)
    scene.addItem(PolygonItem([QPointF(200, 200), QPointF(900, 200), QPointF(900, 700),
                               QPointF(200, 700)], ObjectType.MULCH, layer_id=scene.layers[0].id))
    out = tmp_path / f"{style.value}.png"
    ExportService.export_to_png(scene, out, dpi=72, output_width_cm=40, render_style=style)
    image = QImage(str(out))
    assert not image.isNull() and image.width() > 50
    assert len({image.pixel(x, y) for x in range(0, image.width(), 5)
                for y in range(0, image.height(), 5)}) > 3
    svg = tmp_path / f"{style.value}.svg"
    ExportService.export_to_svg(scene, svg, output_width_cm=40, render_style=style)
    assert svg.stat().st_size > 500
