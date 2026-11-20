"""The CAD render style end to end (Package 4, WP4; section 8.10): a small plan
is switched to CAD and rendered through the PNG and SVG exporters; the plan, the
undo stack and the default output are untouched."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QColor, QImage

from open_garden_planner.core.commands import CommandManager, CreateItemCommand
from open_garden_planner.core.object_types import ObjectType, PathFenceStyle
from open_garden_planner.core.render_style import RenderStyle
from open_garden_planner.core.render_style_roles import IRRIGATION_LAYER_KEY
from open_garden_planner.models.layer import Layer
from open_garden_planner.services.export_service import ExportService
from open_garden_planner.ui.canvas.canvas_scene import CanvasScene
from open_garden_planner.ui.canvas.items import CircleItem, PolygonItem, PolylineItem

P = QPointF


def _plan(qtbot: object) -> tuple[CanvasScene, CommandManager]:
    scene = CanvasScene(width_cm=2000, height_cm=1400)
    irrigation = Layer(name="x", permaculture_layer=IRRIGATION_LAYER_KEY, z_order=1)
    scene.set_layers([*scene.layers, irrigation])
    lay = scene.layers[0].id
    commands = CommandManager()
    for item in (
        PolygonItem([P(200, 800), P(800, 800), P(800, 1300), P(200, 1300)], ObjectType.HOUSE, layer_id=lay),
        PolygonItem([P(1100, 300), P(1600, 300), P(1600, 600)], ObjectType.POND_POOL, layer_id=lay),
        PolylineItem([P(900, 700), P(1300, 900)], ObjectType.PATH, layer_id=lay,
                     path_fence_style=PathFenceStyle.PAVED_PATH),
        PolylineItem([P(100, 100), P(1900, 700)], ObjectType.PATH, layer_id=irrigation.id),
        CircleItem(500, 400, 150, ObjectType.TREE, layer_id=lay),
        CircleItem(900, 400, 60, ObjectType.SHRUB, layer_id=lay),
    ):
        commands.execute(CreateItemCommand(scene, item))
    return scene, commands


def test_cad_export_is_white_paper_with_ink_and_leaves_the_plan_alone(
    qtbot: object, tmp_path: Path
) -> None:
    scene, commands = _plan(qtbot)
    scene.set_render_style(RenderStyle.CAD)
    undo = commands.can_undo
    path = tmp_path / "cad.png"
    ExportService.export_to_png(scene, path, dpi=72, output_width_cm=40)
    img = QImage(str(path))
    assert not img.isNull()
    dark = sum(1 for y in range(0, img.height(), 2) for x in range(0, img.width(), 2)
               if QColor(img.pixel(x, y)).lightness() < 100)
    white = sum(1 for y in range(0, img.height(), 4) for x in range(0, img.width(), 4)
                if QColor(img.pixel(x, y)).lightness() > 245)
    assert dark > 50 and white > (img.width() // 4) * (img.height() // 4) * 0.5
    assert scene.render_style is RenderStyle.CAD
    assert commands.can_undo == undo


def test_cad_svg_is_vector_only(qtbot: object, tmp_path: Path) -> None:
    scene, _commands = _plan(qtbot)
    out = tmp_path / "cad.svg"
    ExportService.export_to_svg(scene, out, output_width_cm=40, render_style=RenderStyle.CAD)
    text = out.read_text(encoding="utf-8")
    assert "<image" not in text
    assert "stroke-dasharray" in text  # irrigation
    assert scene.render_style is RenderStyle.ILLUSTRATED


def test_default_export_unchanged_after_a_cad_round_trip(qtbot: object, tmp_path: Path) -> None:
    scene, _commands = _plan(qtbot)
    ExportService.export_to_png(scene, tmp_path / "a.png", dpi=72, output_width_cm=40)
    scene.set_render_style(RenderStyle.CAD)
    scene.set_render_style(RenderStyle.ILLUSTRATED)
    ExportService.export_to_png(scene, tmp_path / "b.png", dpi=72, output_width_cm=40)
    assert (tmp_path / "a.png").read_bytes() == (tmp_path / "b.png").read_bytes()
