"""WP5 of the render styles (Package 4): the CAD leader-callout layer."""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QTransform
from PyQt6.QtWidgets import QGraphicsItem, QGraphicsSimpleTextItem

from open_garden_planner.core.object_types import ObjectType
from open_garden_planner.core.render_style import RenderStyle
from open_garden_planner.ui.canvas.canvas_scene import CanvasScene
from open_garden_planner.ui.canvas.items import CircleItem, PolygonItem
from open_garden_planner.ui.canvas.items.callout_item import CalloutItem
from open_garden_planner.ui.canvas.style.callouts import (
    StyleCalloutLayer,
    compute_callouts,
    overlapping_pairs,
)

P = QPointF


def _plant(scene: CanvasScene, x: float, y: float, kind: ObjectType, common: str | None) -> CircleItem:
    item = CircleItem(x, y, 60, kind, layer_id=scene.layers[0].id)
    if common is not None:
        item.metadata["plant_species"] = {"common_name": common}
    scene.addItem(item)
    return item


@pytest.fixture()
def scene(qtbot: object) -> CanvasScene:
    s = CanvasScene(width_cm=2000, height_cm=1400)
    lay = s.layers[0].id
    s.addItem(PolygonItem([P(200, 800), P(800, 800), P(800, 1300), P(200, 1300)],
                          ObjectType.HOUSE, layer_id=lay))
    # A tight cluster: every callout contends for the same free slots.
    for i in range(9):
        _plant(s, 1000 + (i % 3) * 90, 500 + (i // 3) * 90, ObjectType.SHRUB, f"Blueberry {i}")
    _plant(s, 400, 400, ObjectType.TREE, "Apple tree")
    _plant(s, 1500, 1000, ObjectType.PERENNIAL, None)
    return s


def _layer(scene: CanvasScene) -> StyleCalloutLayer | None:
    layers = [i for i in scene.items() if isinstance(i, StyleCalloutLayer)]
    assert len(layers) <= 1
    return layers[0] if layers else None


def test_off_by_default_and_no_layer(scene: CanvasScene) -> None:
    assert scene.cad_callouts is False
    scene.set_render_style(RenderStyle.CAD)
    assert _layer(scene) is None
    assert "callouts" not in scene.ensure_style_state().data  # type: ignore[union-attr]


def test_layer_only_in_cad_with_flag(scene: CanvasScene) -> None:
    scene.set_cad_callouts(True)
    assert _layer(scene) is None  # illustrated
    scene.set_render_style(RenderStyle.CAD)
    layer = _layer(scene)
    assert layer is not None and len(layer.callouts) == 11
    scene.set_render_style(RenderStyle.ILLUSTRATED)
    assert _layer(scene) is None
    scene.set_render_style(RenderStyle.CAD)
    scene.set_cad_callouts(False)
    assert _layer(scene) is None


def test_layer_is_view_decoration(scene: CanvasScene) -> None:
    scene.set_cad_callouts(True)
    scene.set_render_style(RenderStyle.CAD)
    layer = _layer(scene)
    assert layer is not None
    assert not isinstance(layer, CalloutItem)
    assert not layer.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
    assert layer.shape().isEmpty()
    assert layer.zValue() > max(
        i.zValue() for i in scene.items() if i is not layer and i.parentItem() is None)
    assert scene.itemAt(P(1000, 500), QTransform()) is not layer
    texts = [c for c in layer.childItems() if isinstance(c, QGraphicsSimpleTextItem)]
    assert len(texts) == 11
    for t in texts:
        assert t.flags() & QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations


def test_toggle_is_no_plan_change(scene: CanvasScene) -> None:
    seen: list[int] = []
    scene.diagnostics_changed.connect(lambda: seen.append(1))
    scene.set_render_style(RenderStyle.CAD)
    scene.set_cad_callouts(True)
    scene.set_cad_callouts(False)
    assert not seen


def test_text_is_common_name_then_object_name_then_fallback(scene: CanvasScene) -> None:
    scene.set_cad_callouts(True)
    scene.set_render_style(RenderStyle.CAD)
    texts = {c.text for c in _layer(scene).callouts}  # type: ignore[union-attr]
    assert "Apple tree" in texts and "Blueberry 3" in texts
    assert "Plant" in texts  # perennial with no name


def test_layout_is_deterministic_and_collision_free(scene: CanvasScene) -> None:
    a = compute_callouts(scene, 0)
    b = compute_callouts(scene, 0)
    assert a == b and len(a) == 11
    assert overlapping_pairs(a) == []
    # no box covers another plant's anchor or the house
    house = next(i for i in scene.items() if isinstance(i, PolygonItem)).sceneBoundingRect()
    for c in a:
        assert not c.box.intersects(house)
        for o in a:
            if o is not c:
                assert not c.box.contains(o.anchor)


@pytest.mark.parametrize("band", [-1, 0, 1])
def test_collision_free_at_every_band(scene: CanvasScene, band: int) -> None:
    layout = compute_callouts(scene, band)
    assert len(layout) == 11
    assert overlapping_pairs(layout) == []


def test_layout_is_cached_and_follows_the_paint_revision(scene: CanvasScene) -> None:
    scene.set_cad_callouts(True)
    scene.set_render_style(RenderStyle.CAD)
    state = scene.ensure_style_state()
    assert scene.ensure_style_state() is state
    builds = scene._style_states.builds
    scene.ensure_style_state()
    assert scene._style_states.builds == builds
    _plant(scene, 1800, 200, ObjectType.TREE, "Pear tree")
    scene.note_paint_changed()
    scene.ensure_style_state()
    layer = _layer(scene)
    assert layer is not None and len(layer.callouts) == 12


def test_clear_drops_the_layer(scene: CanvasScene) -> None:
    scene.set_cad_callouts(True)
    scene.set_render_style(RenderStyle.CAD)
    scene.clear()
    assert _layer(scene) is None
    scene.ensure_style_state()  # must not touch a dead wrapper
