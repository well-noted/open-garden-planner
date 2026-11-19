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


#: ``.render(`` calls that are not a scene's, or that deliberately paint the screen
#: style: ``(file, owner expression)``. A new entry needs a reason.
_RENDER_ALLOW = {
    ("core/furniture_renderer.py", "renderer"),  # QSvgRenderer
    ("core/plant_renderer.py", "renderer"),  # QSvgRenderer
    ("ui/icons.py", "renderer"),  # QSvgRenderer
    ("ui/widgets/gallery_data.py", "renderer"),  # QSvgRenderer
    ("ui/panels/constraints_panel.py", "descriptor"),  # a text descriptor
    ("agent_api/server.py", "providers"),  # the provider reaches render_scene_region
    # The minimap is a screen overview that follows the canvas style as it is.
    ("ui/widgets/minimap_widget.py", "self._canvas_scene"),
}


def _styled_names(tree: ast.AST) -> set[str]:
    """Variables bound to a ``styled_scene(...)`` call."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _calls_styled_scene(node.value):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _calls_styled_scene(expr: ast.AST) -> bool:
    if not isinstance(expr, ast.Call):
        return False
    func = expr.func
    return (isinstance(func, ast.Name) and func.id == "styled_scene") or (
        isinstance(func, ast.Attribute) and func.attr == "styled_scene"
    )


def _is_styled(node: ast.AST, parents: dict[ast.AST, ast.AST], names: set[str]) -> bool:
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.With):
            for item in node.items:
                expr = item.context_expr
                if _calls_styled_scene(expr):
                    return True
                if isinstance(expr, ast.Name) and expr.id in names:
                    return True
    return False


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}


def test_every_scene_render_call_site_is_styled() -> None:
    """A scene.render call that bypasses styled_scene would paint in the wrong
    style in one output (design 1.2). Every ``.render(`` in src/ is either inside
    a ``with`` that really is ``styled_scene`` (or a variable bound to one), or
    on the explicit allow-list; the receiver's name is not trusted."""
    offenders: list[str] = []
    seen = 0
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents, names = _parents(tree), _styled_names(tree)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "render"
            ):
                continue
            owner = ast.unparse(node.func.value)
            if (rel, owner) in _RENDER_ALLOW:
                continue
            seen += 1
            if not _is_styled(node, parents, names):
                offenders.append(f"{rel}:{node.lineno} {owner}.render")
    assert seen >= 5
    assert offenders == []


def test_the_allow_list_has_no_stale_entries() -> None:
    present: set[tuple[str, str]] = set()
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "render"
            ):
                present.add((rel, ast.unparse(node.func.value)))
    assert present >= _RENDER_ALLOW


def test_thumbnail_writers_use_the_thumbnail_helper() -> None:
    """A function that writes a thumbnail goes through ``render_thumbnail_region``
    (always illustrated), never ``render_scene_region`` with a style."""
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if "thumbnail" not in fn.name.lower() or rel == "services/scene_rendering.py":
                continue
            for call in ast.walk(fn):
                if isinstance(call, ast.Call) and getattr(
                    call.func, "id", getattr(call.func, "attr", "")
                ) in {"render_scene_region", "styled_scene"}:
                    offenders.append(f"{rel}:{call.lineno} in {fn.name}")
    assert offenders == []


class TestSeamDefaults:
    def test_none_style_still_paints_at_the_output_band(self, scene: CanvasScene) -> None:
        from open_garden_planner.ui.canvas.style.geometry import band_for_scale

        scene.set_style_band(3)
        with styled_scene(scene, None, 0.1):
            assert scene.style_band == band_for_scale(0.1)
        assert scene.style_band == 3

    def test_default_output_is_a_bare_scene_render(self, scene: CanvasScene) -> None:
        scene.addItem(CircleItem(500, 400, 100))
        bare = QImage(80, 60, QImage.Format.Format_ARGB32)
        bare.fill(QColor("white"))
        painter = QPainter(bare)
        painter.translate(0, 60)
        painter.scale(1.0, -1.0)
        scene.render(painter, QRectF(0, 0, 80, 60), scene.sceneRect())
        painter.end()
        assert _render(scene, hide_overlays=False, hide_construction=False) == bare
