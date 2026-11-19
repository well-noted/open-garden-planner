"""WP1 of the render styles (Package 4): the scene property, the settings, the
`styled_scene` seam, the background helpers and the call-site rule."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import QGraphicsRectItem, QGraphicsScene

import open_garden_planner
from open_garden_planner.app.settings import AppSettings
from open_garden_planner.core.render_style import RenderStyle
from open_garden_planner.services.scene_rendering import (
    THUMBNAIL_STYLE,
    effective_style,
    render_scene_region,
    render_thumbnail_region,
    style_background,
    styled_scene,
)
from open_garden_planner.ui.canvas.canvas_scene import CanvasScene
from open_garden_planner.ui.canvas.items import CircleItem

SRC = Path(open_garden_planner.__file__).parent


class _Probe(QGraphicsRectItem):
    """Records the render style its scene had at each paint."""

    def __init__(self) -> None:
        super().__init__(0, 0, 50, 50)
        self.seen: list[RenderStyle] = []

    def paint(self, painter, option, widget=None) -> None:
        self.seen.append(self.scene().render_style)


@pytest.fixture()
def scene(qtbot: object) -> CanvasScene:
    return CanvasScene(width_cm=1000, height_cm=800)


def _render(scene: CanvasScene, **kwargs: object) -> QImage:
    img = QImage(80, 60, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    painter = QPainter(img)
    try:
        render_scene_region(
            scene, painter, QRectF(0, 0, 80, 60), scene.sceneRect(), **kwargs
        )
    finally:
        painter.end()
    return img


class TestSettings:
    def test_defaults(self) -> None:
        settings = AppSettings()
        assert settings.render_style == "illustrated"
        assert settings.cad_callouts is False  # owner decision Q4
        assert settings.export_render_style_override == "follow"

    def test_round_trip_and_junk(self) -> None:
        settings = AppSettings()
        settings.render_style = "cad"
        assert AppSettings().render_style == "cad"
        settings.render_style = "watercolour"
        assert AppSettings().render_style == "illustrated"
        settings.export_render_style_override = "line_and_wash"
        assert AppSettings().export_render_style_override == "line_and_wash"
        settings.export_render_style_override = "nonsense"
        assert AppSettings().export_render_style_override == "follow"
        settings.cad_callouts = True
        assert AppSettings().cad_callouts is True

    def test_keys(self) -> None:
        assert AppSettings.KEY_RENDER_STYLE == "appearance/render_style"
        assert AppSettings.KEY_CAD_CALLOUTS == "appearance/cad_callouts"
        assert AppSettings.KEY_EXPORT_RENDER_STYLE_OVERRIDE == "export/render_style_override"


class TestSceneProperty:
    def test_default_is_illustrated(self, scene: CanvasScene) -> None:
        assert scene.render_style is RenderStyle.ILLUSTRATED

    def test_switch_moves_paint_revision_only(self, scene: CanvasScene) -> None:
        plan_changes: list[object] = []
        scene.plan_changed.connect(lambda *a: plan_changes.append(a))
        before = scene.paint_revision
        scene.set_render_style(RenderStyle.CAD)
        assert scene.render_style is RenderStyle.CAD
        assert scene.paint_revision > before
        assert plan_changes == []

    def test_same_style_is_a_noop(self, scene: CanvasScene) -> None:
        before = scene.paint_revision
        scene.set_render_style(RenderStyle.ILLUSTRATED)
        assert scene.paint_revision == before

    def test_unknown_style_keeps_the_current(self, scene: CanvasScene) -> None:
        scene.set_render_style("cad")
        scene.set_render_style("nonsense")
        assert scene.render_style is RenderStyle.CAD


class TestStyledScene:
    def test_sets_and_restores(self, scene: CanvasScene) -> None:
        with styled_scene(scene, RenderStyle.CAD):
            assert scene.render_style is RenderStyle.CAD
        assert scene.render_style is RenderStyle.ILLUSTRATED

    def test_restores_on_error(self, scene: CanvasScene) -> None:
        scene.set_render_style(RenderStyle.LINE_AND_WASH)
        with pytest.raises(RuntimeError), styled_scene(scene, RenderStyle.CAD):
            raise RuntimeError("boom")
        assert scene.render_style is RenderStyle.LINE_AND_WASH

    def test_none_keeps_the_scene_style(self, scene: CanvasScene) -> None:
        scene.set_render_style(RenderStyle.CAD)
        with styled_scene(scene, None):
            assert scene.render_style is RenderStyle.CAD

    def test_plain_scene_is_rendered_as_it_is(self, qtbot: object) -> None:
        plain = QGraphicsScene()
        with styled_scene(plain, RenderStyle.CAD):
            pass
        assert effective_style(plain, None) is RenderStyle.ILLUSTRATED


class TestRenderRegion:
    def test_default_is_pixel_identical_to_illustrated(self, scene: CanvasScene) -> None:
        scene.addItem(CircleItem(500, 400, 100))
        assert _render(scene) == _render(scene, render_style=RenderStyle.ILLUSTRATED)

    def test_item_sees_the_requested_style_and_scene_is_restored(
        self, scene: CanvasScene
    ) -> None:
        probe = _Probe()
        scene.addItem(probe)
        _render(scene, render_style=RenderStyle.CAD)
        assert probe.seen and set(probe.seen) == {RenderStyle.CAD}
        assert scene.render_style is RenderStyle.ILLUSTRATED

    def test_thumbnail_is_always_illustrated(self, scene: CanvasScene) -> None:
        assert THUMBNAIL_STYLE is RenderStyle.ILLUSTRATED
        scene.set_render_style(RenderStyle.LINE_AND_WASH)
        probe = _Probe()
        scene.addItem(probe)
        img = QImage(40, 30, QImage.Format.Format_ARGB32)
        painter = QPainter(img)
        try:
            render_thumbnail_region(scene, painter, QRectF(0, 0, 40, 30), scene.sceneRect())
        finally:
            painter.end()
        assert probe.seen and set(probe.seen) == {RenderStyle.ILLUSTRATED}
        assert scene.render_style is RenderStyle.LINE_AND_WASH


class TestBackground:
    def test_illustrated_uses_the_scene_canvas_colour(self, scene: CanvasScene) -> None:
        assert style_background(RenderStyle.ILLUSTRATED, scene) == scene.CANVAS_COLOR
        assert style_background(RenderStyle.ILLUSTRATED) == QColor("white")

    def test_cad_and_wash_have_their_own_paper(self, scene: CanvasScene) -> None:
        assert style_background(RenderStyle.CAD, scene) == QColor("#ffffff")
        assert style_background(RenderStyle.LINE_AND_WASH, scene) == QColor("#f4eedf")


def _is_styled(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.With):
            for item in node.items:
                expr = item.context_expr
                if isinstance(expr, ast.Call) and getattr(expr.func, "id", "") == "styled_scene":
                    return True
                if isinstance(expr, ast.Name) and expr.id == "style_ctx":
                    return True
    return False


def test_every_scene_render_call_site_is_styled() -> None:
    """A scene.render call that bypasses styled_scene would paint in the wrong
    style in one output (design 1.2): the rule is enforced here."""
    offenders: list[str] = []
    seen = 0
    files = [*(SRC / "services").glob("*.py"), SRC / "ui" / "dialogs" / "print_dialog.py"]
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "render"
            ):
                continue
            owner = ast.unparse(node.func.value)
            if "scene" not in owner:
                continue
            seen += 1
            if not _is_styled(node, parents):
                offenders.append(f"{path.name}:{node.lineno} {owner}.render")
    assert seen >= 5
    assert offenders == []
