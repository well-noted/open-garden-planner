"""WP2 of the render styles (Package 4): StyleState, its LRU, the scene hooks,
the bounding pad and ``pen_for_painter``."""

from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QColor, QImage, QPainter

from open_garden_planner.core.render_style import RenderStyle
from open_garden_planner.services.scene_rendering import render_scene_region
from open_garden_planner.ui.canvas.canvas_scene import CanvasScene
from open_garden_planner.ui.canvas.items import RectangleItem
from open_garden_planner.ui.canvas.style import base
from open_garden_planner.ui.canvas.style.base import (
    bounding_pad_cm,
    pen_for_painter,
    styled_bounding_rect,
)
from open_garden_planner.ui.canvas.style.state import StyleState, StyleStateCache


class FakePainter:
    """A style painter that records what it was asked and builds a marker state."""

    def __init__(self, pad: float = 0.0, draws: bool = False) -> None:
        self.pad = pad
        self.draws = draws
        self.builds: list[int] = []
        self.shapes = 0
        self.paths = 0

    def paint_shape(self, item: Any, painter: QPainter, option: Any) -> bool:
        self.shapes += 1
        return self.draws

    def paint_path(self, path_paint: Any, painter: QPainter, style: Any) -> bool:
        self.paths += 1
        return self.draws

    def paint_canopy(self, item: Any, painter: QPainter, spec: Any) -> bool:
        return False

    def paint_label_decor(self, item: Any, painter: QPainter) -> None:
        return None

    def bounding_pad_cm(self, item: Any, band: int) -> float:
        return self.pad

    def build_state(self, scene: Any, band: int) -> dict[str, Any]:
        self.builds.append(band)
        return {"band": band}


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch) -> FakePainter:
    painter = FakePainter()
    monkeypatch.setitem(base.STYLE_PAINTERS, RenderStyle.CAD, painter)
    return painter


@pytest.fixture()
def scene(qtbot: object) -> CanvasScene:
    return CanvasScene(width_cm=1000, height_cm=800)


class TestCache:
    def test_lookup_never_builds(self) -> None:
        cache = StyleStateCache()
        assert cache.lookup(RenderStyle.CAD, 1, 0) is None
        assert cache.builds == 0

    def test_ensure_builds_once_per_key(self, fake: FakePainter) -> None:
        cache = StyleStateCache()
        first = cache.ensure(None, RenderStyle.CAD, 1, 3)
        assert cache.ensure(None, RenderStyle.CAD, 1, 3) is first
        assert first.data == {"band": 3}
        assert first.key == (RenderStyle.CAD, 1, 3)
        assert cache.builds == 1 and fake.builds == [3]

    def test_a_style_without_a_painter_has_an_empty_state(self) -> None:
        state = StyleStateCache().ensure(None, RenderStyle.LINE_AND_WASH, 0, 0)
        assert state == StyleState(RenderStyle.LINE_AND_WASH, 0, 0, {})

    def test_lru_keeps_two_and_drops_the_least_recent(self, fake: FakePainter) -> None:
        cache = StyleStateCache()
        cache.ensure(None, RenderStyle.CAD, 1, 0)
        cache.ensure(None, RenderStyle.CAD, 1, 1)
        assert cache.lookup(RenderStyle.CAD, 1, 0) is not None  # 0 is now the freshest
        cache.ensure(None, RenderStyle.CAD, 1, 2)
        assert len(cache) == 2
        assert cache.lookup(RenderStyle.CAD, 1, 1) is None
        assert cache.lookup(RenderStyle.CAD, 1, 0) is not None


class TestSceneHooks:
    def test_illustrated_has_no_state(self, scene: CanvasScene) -> None:
        assert scene.style_state() is None
        assert scene.ensure_style_state() is None

    def test_switch_builds_the_state_before_any_paint(
        self, scene: CanvasScene, fake: FakePainter
    ) -> None:
        scene.set_render_style(RenderStyle.CAD)
        assert fake.builds == [0]
        state = scene.style_state()
        assert state is not None and state.key == (RenderStyle.CAD, scene.paint_revision, 0)

    def test_paint_revision_bump_misses_then_rebuilds_deferred(
        self, scene: CanvasScene, fake: FakePainter, qtbot: object
    ) -> None:
        scene.set_render_style(RenderStyle.CAD)
        scene.note_paint_changed()
        builds = len(fake.builds)
        assert scene.style_state() is None  # a paint reads, never builds
        assert len(fake.builds) == builds
        qtbot.waitUntil(lambda: scene.style_state() is not None, timeout=2000)  # type: ignore[attr-defined]
        assert len(fake.builds) == builds + 1

    def test_many_bumps_coalesce_to_one_rebuild(
        self, scene: CanvasScene, fake: FakePainter, qtbot: object
    ) -> None:
        scene.set_render_style(RenderStyle.CAD)
        before = len(fake.builds)
        for _ in range(5):
            scene.note_paint_changed()
        qtbot.waitUntil(lambda: scene.style_state() is not None, timeout=2000)  # type: ignore[attr-defined]
        assert len(fake.builds) == before + 1

    def test_band_change_builds_that_band(self, scene: CanvasScene, fake: FakePainter) -> None:
        scene.set_render_style(RenderStyle.CAD)
        scene.set_style_band(4)
        assert fake.builds == [0, 4]
        assert scene.style_band == 4
        scene.set_style_band(4)
        assert fake.builds == [0, 4]

    def test_band_change_under_illustrated_builds_nothing(
        self, scene: CanvasScene, fake: FakePainter
    ) -> None:
        scene.set_style_band(5)
        assert fake.builds == [] and scene.style_band == 5

    def test_render_prewarms_its_band_and_keeps_the_screens(
        self, scene: CanvasScene, fake: FakePainter
    ) -> None:
        scene.set_render_style(RenderStyle.CAD)
        scene.set_style_band(1)
        img = QImage(64, 64, QImage.Format.Format_ARGB32)
        painter = QPainter(img)
        try:
            # 64 px over 1000 cm is ~0.064 px/cm: band -4
            render_scene_region(
                scene, painter, QRectF(0, 0, 64, 64), QRectF(0, 0, 1000, 1000),
                render_style=RenderStyle.CAD,
            )
        finally:
            painter.end()
        assert -4 in fake.builds
        assert scene.style_band == 1
        state = scene.style_state()
        assert state is not None and state.band == 1


class TestBoundingPad:
    def test_zero_without_a_painter_and_for_illustrated(self, scene: CanvasScene) -> None:
        item = RectangleItem(0, 0, 100, 50)
        scene.addItem(item)
        rect = QRectF(0, 0, 100, 50)
        assert bounding_pad_cm(item, RenderStyle.CAD, 0) == 0.0
        assert styled_bounding_rect(item, rect) == rect

    def test_styled_rect_grows_by_the_pad_and_shape_does_not(
        self, scene: CanvasScene, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(base.STYLE_PAINTERS, RenderStyle.CAD, FakePainter(pad=5.0))
        item = RectangleItem(0, 0, 100, 50)
        scene.addItem(item)
        rect = QRectF(0, 0, 100, 50)
        assert styled_bounding_rect(item, rect) == rect  # illustrated
        scene.set_render_style(RenderStyle.CAD)
        assert styled_bounding_rect(item, rect) == QRectF(-5, -5, 110, 60)

    def test_style_switch_notifies_items_that_carry_the_pad(self, scene: CanvasScene) -> None:
        item = RectangleItem(0, 0, 10, 10)
        calls: list[int] = []
        item.style_geometry_changed = lambda: calls.append(1)  # type: ignore[attr-defined]
        scene.addItem(item)
        scene.set_render_style(RenderStyle.CAD)
        assert calls == [1]


class TestPen:
    @pytest.fixture()
    def image(self) -> QImage:
        return QImage(10, 10, QImage.Format.Format_ARGB32)

    def test_screen_weight_is_96_dpi_and_cosmetic(self, image: QImage) -> None:
        painter = QPainter(image)
        try:
            pen = pen_for_painter(painter, 0.5, QColor("black"))
        finally:
            painter.end()
        assert pen.isCosmetic()
        assert pen.widthF() == pytest.approx(0.5 * 96.0 / 25.4)

    def test_never_thinner_than_one_pixel(self, image: QImage) -> None:
        painter = QPainter(image)
        try:
            assert pen_for_painter(painter, 0.01, QColor("black")).widthF() == 1.0
        finally:
            painter.end()

    def test_export_dpi(self, image: QImage) -> None:
        painter = QPainter(image)
        try:
            pen = pen_for_painter(painter, 0.25, QColor("red"), dpi=300)
        finally:
            painter.end()
        assert pen.widthF() == pytest.approx(0.25 * 300 / 25.4)
        assert pen.color() == QColor("red")


class _CountingRect(RectangleItem):
    """A canvas item that counts its repaint requests and geometry announcements."""

    def __init__(self) -> None:
        super().__init__(0, 0, 10, 10)
        self.updates = 0
        self.geometry_calls = 0

    def update(self, *args: Any) -> None:
        self.updates += 1
        super().update(*args)

    def style_geometry_changed(self) -> None:
        self.geometry_calls += 1


class TestRenderOverride:
    def test_override_keeps_the_screen_state_and_the_revision(
        self, scene: CanvasScene, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        screen, other = FakePainter(), FakePainter()
        monkeypatch.setitem(base.STYLE_PAINTERS, RenderStyle.CAD, screen)
        monkeypatch.setitem(base.STYLE_PAINTERS, RenderStyle.LINE_AND_WASH, other)
        scene.set_render_style(RenderStyle.CAD)
        kept = scene.ensure_style_state()
        revision = scene.paint_revision
        screen_builds = len(screen.builds)
        with scene.style_for_render(RenderStyle.LINE_AND_WASH, 0.5):
            assert scene.render_style is RenderStyle.LINE_AND_WASH
        assert scene.paint_revision == revision
        assert scene.render_style is RenderStyle.CAD
        assert scene.style_state() is kept
        assert len(screen.builds) == screen_builds
        # The override built exactly one state: at the output's band.
        assert other.builds == [-1]

    def test_band_and_style_changes_announce_geometry_on_entry_and_exit(
        self, scene: CanvasScene, fake: FakePainter
    ) -> None:
        item = _CountingRect()
        scene.addItem(item)
        scene.set_render_style(RenderStyle.CAD)
        item.geometry_calls = 0
        with scene.style_for_render(None, 0.001):
            assert item.geometry_calls == 1
        assert item.geometry_calls == 2
        item.geometry_calls = 0
        with scene.style_for_render(None, None):  # nothing differs
            pass
        assert item.geometry_calls == 0

    def test_pad_follows_the_band_inside_a_render(
        self, scene: CanvasScene, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Banded(FakePainter):
            def bounding_pad_cm(self, item: Any, band: int) -> float:
                return 10.0 if band < 0 else 1.0

        monkeypatch.setitem(base.STYLE_PAINTERS, RenderStyle.CAD, Banded())
        item = RectangleItem(0, 0, 10, 10)
        scene.addItem(item)
        scene.set_render_style(RenderStyle.CAD)
        screen = styled_bounding_rect(item, QRectF(0, 0, 10, 10))
        with scene.style_for_render(None, 0.001):
            wide = styled_bounding_rect(item, QRectF(0, 0, 10, 10))
        assert wide.width() > screen.width()
        assert styled_bounding_rect(item, QRectF(0, 0, 10, 10)) == screen

    def test_a_deferred_rebuild_repaints_every_item(
        self, scene: CanvasScene, fake: FakePainter
    ) -> None:
        item = _CountingRect()
        scene.addItem(item)
        scene.set_render_style(RenderStyle.CAD)
        item.updates = 0
        scene._rebuild_style_state()
        assert item.updates >= 1


class TestOutputDpi:
    def test_a_300_dpi_output_gets_300_dpi_weights_and_screen_is_unchanged(
        self, scene: CanvasScene
    ) -> None:
        image = QImage(10, 10, QImage.Format.Format_ARGB32)  # device ratio 1
        painter = QPainter(image)
        try:
            with scene.style_for_render(None, 1.0, 300.0):
                inside = pen_for_painter(painter, 0.5, QColor("black")).widthF()
                assert scene.style_output_dpi == 300.0
            after = pen_for_painter(painter, 0.5, QColor("black")).widthF()
        finally:
            painter.end()
        assert inside == pytest.approx(0.5 * 300 / 25.4)
        assert after == pytest.approx(0.5 * 96 / 25.4)
        assert scene.style_output_dpi is None

    def test_png_export_passes_its_dpi(
        self, scene: CanvasScene, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from open_garden_planner.services.export_service import ExportService

        seen: list[float | None] = []

        class Probe(RectangleItem):
            def paint(self, painter: QPainter, option: Any, widget: Any = None) -> None:
                seen.append(pen_for_painter(painter, 1.0, QColor("black")).widthF())

        scene.addItem(Probe(0, 0, 100, 100))
        ExportService.export_to_png(scene, tmp_path / "a.png", dpi=300, output_width_cm=10)
        assert seen and all(w == pytest.approx(300 / 25.4) for w in seen)
