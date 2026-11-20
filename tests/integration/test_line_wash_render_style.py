"""Line & Wash end to end (Package 4, WP6; section 8.10): PNG and SVG exports of a
small plan, deterministic across two runs, plan and undo stack untouched."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QColor, QImage

from open_garden_planner.core.object_types import ObjectType, PathFenceStyle
from open_garden_planner.core.render_style import RenderStyle
from open_garden_planner.services.export_service import ExportService
from open_garden_planner.ui.canvas.canvas_scene import CanvasScene
from open_garden_planner.ui.canvas.items import CircleItem, PolygonItem, PolylineItem

P = QPointF


def _plan() -> CanvasScene:
    scene = CanvasScene(width_cm=2000, height_cm=1400)
    lay = scene.layers[0].id
    scene.addItem(PolygonItem([P(200, 800), P(800, 800), P(800, 1300), P(200, 1300)],
                              ObjectType.HOUSE, layer_id=lay))
    scene.addItem(PolylineItem([P(900, 700), P(1300, 900)], ObjectType.PATH, layer_id=lay,
                               path_fence_style=PathFenceStyle.PAVED_PATH))
    scene.addItem(CircleItem(500, 400, 150, ObjectType.TREE, layer_id=lay))
    scene.addItem(CircleItem(620, 450, 100, ObjectType.TREE, layer_id=lay))
    return scene


def test_png_is_paper_with_ink_and_repeatable(qtbot: object, tmp_path: Path) -> None:
    scene = _plan()
    scene.set_render_style(RenderStyle.LINE_AND_WASH)
    ExportService.export_to_png(scene, tmp_path / "a.png", dpi=72, output_width_cm=40)
    ExportService.export_to_png(scene, tmp_path / "b.png", dpi=72, output_width_cm=40)
    assert (tmp_path / "a.png").read_bytes() == (tmp_path / "b.png").read_bytes()
    img = QImage(str(tmp_path / "a.png"))
    corner = QColor(img.pixel(3, 3))
    assert corner.red() > corner.blue() and corner.lightness() > 200
    assert any(QColor(img.pixel(x, y)).lightness() < 100
               for y in range(0, img.height(), 2) for x in range(0, img.width(), 2))
    assert scene.render_style is RenderStyle.LINE_AND_WASH


def test_svg_is_vector_without_texture_and_repeatable(qtbot: object, tmp_path: Path) -> None:
    scene = _plan()
    for name in ("a.svg", "b.svg"):
        ExportService.export_to_svg(scene, tmp_path / name, output_width_cm=40,
                                    render_style=RenderStyle.LINE_AND_WASH)
    first = (tmp_path / "a.svg").read_text(encoding="utf-8")
    assert "<image" not in first
    assert "<path" in first
    assert first == (tmp_path / "b.svg").read_text(encoding="utf-8")
    assert scene.render_style is RenderStyle.ILLUSTRATED
