"""Render style is a view preference (Package 4, WP1/WP2; section 8.10).

A style switch must not touch the plan: not the .ogp bytes, not the undo stack,
not the dirty flag; and the default output of every exporter stays what it was.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QColor, QImage, QPainter

from open_garden_planner.core.commands import CommandManager, CreateItemCommand
from open_garden_planner.core.project import ProjectManager
from open_garden_planner.core.render_style import RenderStyle
from open_garden_planner.services.export_service import ExportService
from open_garden_planner.ui.canvas.canvas_scene import CanvasScene
from open_garden_planner.ui.canvas.canvas_view import CanvasView
from open_garden_planner.ui.canvas.items import RectangleItem


def _plan(qtbot: object) -> tuple[CanvasScene, CommandManager, ProjectManager]:
    scene = CanvasScene(width_cm=1000, height_cm=800)
    commands = CommandManager()
    commands.execute(CreateItemCommand(scene, RectangleItem(100, 100, 200, 100)))
    project = ProjectManager()
    # What the application wires (application.py: stack_changed -> mark_dirty); without
    # it a "the style switch left the plan clean" assertion could never fail.
    commands.stack_changed.connect(project.mark_dirty)
    return scene, commands, project


def test_the_dirty_wiring_of_the_fixture_can_fail(qtbot: object, tmp_path: Path) -> None:
    scene, commands, project = _plan(qtbot)
    project.save(scene, tmp_path / "a.ogp")
    assert not project.is_dirty
    commands.execute(CreateItemCommand(scene, RectangleItem(0, 0, 10, 10)))
    assert project.is_dirty


def _stable(path: Path) -> dict:
    """The saved plan without its save time: the only field that differs
    between two saves of the same plan."""
    data = json.loads(path.read_text(encoding="utf-8"))
    data["metadata"].pop("modified", None)
    return data


def test_switching_style_leaves_the_plan_untouched(qtbot: object, tmp_path: Path) -> None:
    scene, commands, project = _plan(qtbot)
    first = tmp_path / "a.ogp"
    project.save(scene, first)
    undo_before = commands.can_undo
    dirty_before = project.is_dirty

    scene.set_render_style(RenderStyle.CAD)
    scene.set_render_style(RenderStyle.LINE_AND_WASH)

    assert commands.can_undo == undo_before
    assert project.is_dirty == dirty_before
    second = tmp_path / "b.ogp"
    project.save(scene, second)
    assert b"render_style" not in second.read_bytes()
    assert _stable(first) == _stable(second)


def test_default_png_is_a_bare_scene_render(qtbot: object, tmp_path: Path) -> None:
    """The default export is what the code painted before the seam existed: the
    same pixels as a plain ``scene.render`` with no style machinery."""
    scene, _commands, _project = _plan(qtbot)
    out = tmp_path / "plain.png"
    ExportService.export_to_png(scene, out, dpi=72, output_width_cm=10)
    exported = QImage(str(out))

    rect = scene.canvas_rect if hasattr(scene, "canvas_rect") else scene.sceneRect()
    bare = QImage(exported.width(), exported.height(), QImage.Format.Format_ARGB32)
    bare.fill(QColor(scene.CANVAS_COLOR))
    painter = QPainter(bare)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    painter.translate(0, bare.height())
    painter.scale(1.0, -1.0)
    # The export has always rendered uncached; that is not the seam's doing.
    cached = ExportService._disable_item_caches(scene)
    scene.render(painter, QRectF(0, 0, bare.width(), bare.height()), rect)
    ExportService._restore_item_caches(cached)
    painter.end()
    assert exported.convertToFormat(QImage.Format.Format_ARGB32) == bare
    assert scene.render_style is RenderStyle.ILLUSTRATED


def test_export_style_override_does_not_change_the_canvas_style(
    qtbot: object, tmp_path: Path
) -> None:
    scene, _commands, _project = _plan(qtbot)
    scene.set_render_style(RenderStyle.LINE_AND_WASH)
    ExportService.export_to_png(
        scene, tmp_path / "cad.png", dpi=72, output_width_cm=10, render_style=RenderStyle.CAD
    )
    ExportService.export_to_svg(
        scene, tmp_path / "cad.svg", output_width_cm=10, render_style=RenderStyle.CAD
    )
    assert scene.render_style is RenderStyle.LINE_AND_WASH
    assert (tmp_path / "cad.png").stat().st_size > 0
    assert (tmp_path / "cad.svg").stat().st_size > 0


class _Painter:
    """A style painter that claims every shape and path it is asked about."""

    def __init__(self) -> None:
        self.shapes = 0
        self.paths = 0
        self.builds: list[int] = []

    def paint_shape(self, item, painter, option) -> bool:
        self.shapes += 1
        painter.fillRect(item.rect(), QColor("black"))
        return True

    def paint_path(self, path_paint, painter, style) -> bool:
        self.paths += 1
        return True

    def paint_canopy(self, item, painter, spec) -> bool:
        return False

    def paint_label_decor(self, item, painter) -> None:
        return None

    def bounding_pad_cm(self, item, band) -> float:
        return 0.0

    def build_state(self, scene, band) -> dict:
        self.builds.append(band)
        return {}


def _paint_view(view: CanvasView) -> QImage:
    image = QImage(200, 160, QImage.Format.Format_ARGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    try:
        view.scene().render(painter)
    finally:
        painter.end()
    return image


def test_style_painter_is_asked_by_shapes_and_paths(
    qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_garden_planner.core.object_types import ObjectType, PathFenceStyle
    from open_garden_planner.ui.canvas.items import PolylineItem
    from open_garden_planner.ui.canvas.style import base

    fake = _Painter()
    monkeypatch.setitem(base.STYLE_PAINTERS, RenderStyle.CAD, fake)
    scene = CanvasScene(width_cm=400, height_cm=300)
    scene.addItem(RectangleItem(50, 50, 100, 80))
    scene.addItem(PolylineItem(
        [QPointF(20, 200), QPointF(300, 200)],
        object_type=ObjectType.PATH, path_fence_style=PathFenceStyle.PAVED_PATH,
    ))
    view = CanvasView(scene)
    qtbot.addWidget(view)  # type: ignore[attr-defined]
    view.resize(420, 320)

    _paint_view(view)
    assert fake.shapes == 0 and fake.paths == 0  # illustrated: the painter is not asked

    scene.set_render_style(RenderStyle.CAD)
    _paint_view(view)
    assert fake.shapes >= 1 and fake.paths >= 1


def test_zoom_into_a_new_band_builds_its_state_before_the_paint(
    qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_garden_planner.ui.canvas.style import base

    fake = _Painter()
    monkeypatch.setitem(base.STYLE_PAINTERS, RenderStyle.CAD, fake)
    scene = CanvasScene(width_cm=400, height_cm=300)
    view = CanvasView(scene)
    qtbot.addWidget(view)  # type: ignore[attr-defined]
    scene.set_render_style(RenderStyle.CAD)
    band = scene.style_band
    builds = len(fake.builds)

    view.set_zoom(view.zoom_factor * 1.2)  # inside the band
    if scene.style_band == band:
        assert len(fake.builds) == builds
    view.set_zoom(view.zoom_factor * 8)  # three octaves up
    assert scene.style_band > band
    assert fake.builds[-1] == scene.style_band
    state = scene.style_state()
    assert state is not None and state.band == scene.style_band
