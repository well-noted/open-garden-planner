"""CAD leader callouts end to end (Package 4, WP5; section 8.10): a CAD scene with
callouts on is exported to PNG and SVG; the layout is deterministic and
collision-free; the plan, the undo stack and the default output are untouched."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QColor, QImage

from open_garden_planner.core.commands import CommandManager, CreateItemCommand
from open_garden_planner.core.object_types import ObjectType
from open_garden_planner.core.render_style import RenderStyle
from open_garden_planner.services.export_service import ExportService
from open_garden_planner.ui.canvas.canvas_scene import CanvasScene
from open_garden_planner.ui.canvas.items import CircleItem, PolygonItem
from open_garden_planner.ui.canvas.items.callout_item import CalloutItem
from open_garden_planner.ui.canvas.style.callouts import (
    StyleCalloutLayer,
    compute_callouts,
    overlapping_pairs,
)

P = QPointF


def _plan(qtbot: object) -> tuple[CanvasScene, CommandManager]:
    scene = CanvasScene(width_cm=2000, height_cm=1400)
    lay = scene.layers[0].id
    commands = CommandManager()
    items: list[object] = [
        PolygonItem([P(200, 800), P(800, 800), P(800, 1300), P(200, 1300)],
                    ObjectType.HOUSE, layer_id=lay),
    ]
    names = ["Apple tree", "Pear tree", "Blueberry", "Currant", "Lavender", "Rosemary"]
    for i, name in enumerate(names):
        c = CircleItem(1000 + (i % 3) * 110, 500 + (i // 3) * 110, 50,
                       ObjectType.SHRUB if i else ObjectType.TREE, layer_id=lay)
        c.metadata["plant_species"] = {"common_name": name}
        items.append(c)
    for item in items:
        commands.execute(CreateItemCommand(scene, item))  # type: ignore[arg-type]
    return scene, commands


def _ink(path: Path) -> int:
    img = QImage(str(path))
    assert not img.isNull()
    return sum(1 for y in range(0, img.height(), 2) for x in range(0, img.width(), 2)
               if QColor(img.pixel(x, y)).lightness() < 100)


def test_png_with_callouts_differs_is_deterministic_and_plan_is_untouched(
    qtbot: object, tmp_path: Path
) -> None:
    scene, commands = _plan(qtbot)
    scene.set_render_style(RenderStyle.CAD)
    ExportService.export_to_png(scene, tmp_path / "off.png", dpi=96, output_width_cm=40)
    undo = commands.can_undo
    scene.set_cad_callouts(True)
    ExportService.export_to_png(scene, tmp_path / "a.png", dpi=96, output_width_cm=40)
    ExportService.export_to_png(scene, tmp_path / "b.png", dpi=96, output_width_cm=40)
    assert (tmp_path / "a.png").read_bytes() == (tmp_path / "b.png").read_bytes()
    assert (tmp_path / "a.png").read_bytes() != (tmp_path / "off.png").read_bytes()
    assert _ink(tmp_path / "a.png") > _ink(tmp_path / "off.png")
    assert commands.can_undo == undo
    assert scene.render_style is RenderStyle.CAD


def test_svg_export_carries_the_callouts(qtbot: object, tmp_path: Path) -> None:
    scene, _commands = _plan(qtbot)
    scene.set_cad_callouts(True)
    ExportService.export_to_svg(scene, tmp_path / "on.svg", output_width_cm=40,
                                render_style=RenderStyle.CAD)
    scene.set_cad_callouts(False)
    ExportService.export_to_svg(scene, tmp_path / "off.svg", output_width_cm=40,
                                render_style=RenderStyle.CAD)
    on = (tmp_path / "on.svg").read_text(encoding="utf-8")
    off = (tmp_path / "off.svg").read_text(encoding="utf-8")
    assert "<image" not in on
    assert len(on) > len(off)
    assert scene.render_style is RenderStyle.ILLUSTRATED
    assert not [i for i in scene.items() if isinstance(i, StyleCalloutLayer)]


def test_callouts_never_reach_the_saved_plan(qtbot: object) -> None:
    scene, _commands = _plan(qtbot)
    scene.set_cad_callouts(True)
    scene.set_render_style(RenderStyle.CAD)
    layers = [i for i in scene.items() if isinstance(i, StyleCalloutLayer)]
    assert layers
    # Nothing that serialises a plan object matches the layer or its text.
    assert not hasattr(layers[0], "to_dict") and not hasattr(layers[0], "item_id")
    assert not any(isinstance(i, CalloutItem) for i in scene.items())


def test_fixture_layout_has_no_overlaps(qtbot: object) -> None:
    scene, _commands = _plan(qtbot)
    for band in (-1, 0, 1):
        layout = compute_callouts(scene, band)
        assert len(layout) == 6
        assert overlapping_pairs(layout) == []
