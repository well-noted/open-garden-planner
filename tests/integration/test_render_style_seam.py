"""Render style is a view preference (Package 4, WP1/WP2; section 8.10).

A style switch must not touch the plan: not the .ogp bytes, not the undo stack,
not the dirty flag; and the default output of every exporter stays what it was.
"""

from __future__ import annotations

import json
from pathlib import Path

from open_garden_planner.core.commands import CommandManager, CreateItemCommand
from open_garden_planner.core.project import ProjectManager
from open_garden_planner.core.render_style import RenderStyle
from open_garden_planner.services.export_service import ExportService
from open_garden_planner.ui.canvas.canvas_scene import CanvasScene
from open_garden_planner.ui.canvas.items import RectangleItem


def _plan(qtbot: object) -> tuple[CanvasScene, CommandManager, ProjectManager]:
    scene = CanvasScene(width_cm=1000, height_cm=800)
    commands = CommandManager()
    commands.execute(CreateItemCommand(scene, RectangleItem(100, 100, 200, 100)))
    return scene, commands, ProjectManager()


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


def test_default_png_is_the_illustrated_png(qtbot: object, tmp_path: Path) -> None:
    scene, _commands, _project = _plan(qtbot)
    plain, explicit = tmp_path / "plain.png", tmp_path / "explicit.png"
    ExportService.export_to_png(scene, plain, dpi=72, output_width_cm=10)
    ExportService.export_to_png(
        scene, explicit, dpi=72, output_width_cm=10, render_style=RenderStyle.ILLUSTRATED
    )
    assert plain.read_bytes() == explicit.read_bytes()
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
