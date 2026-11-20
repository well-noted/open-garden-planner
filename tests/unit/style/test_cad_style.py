"""WP4 of the render styles (Package 4): the CAD painter, its roles and its
pixel statistics (robust properties, no per-pixel goldens)."""

from __future__ import annotations

import colorsys

import pytest
from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QColor, QImage, QPainter

from open_garden_planner.core.object_types import ObjectType, PathFenceStyle
from open_garden_planner.core.render_style import RenderStyle
from open_garden_planner.core.render_style_roles import (
    IRRIGATION_LAYER_KEY,
    StyleRole,
    role_for_item,
    role_for_type_name,
)
from open_garden_planner.models.layer import Layer
from open_garden_planner.services.scene_rendering import render_scene_region, style_background
from open_garden_planner.ui.canvas.canvas_scene import CanvasScene
from open_garden_planner.ui.canvas.items import CircleItem, PolygonItem, PolylineItem
from open_garden_planner.ui.canvas.style.base import STYLE_PAINTERS, styled_bounding_rect

P = QPointF


def _render(scene: CanvasScene, style: RenderStyle, w: int = 400, h: int = 280) -> QImage:
    scene.set_render_style(style)
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(style_background(style, scene))
    painter = QPainter(img)
    try:
        render_scene_region(scene, painter, QRectF(0, 0, w, h),
                            QRectF(0, 0, 2000, 1400), render_style=style)
    finally:
        painter.end()
    return img


def _saturation(img: QImage, x0: int, y0: int, x1: int, y1: int) -> float:
    total, count = 0.0, 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            c = QColor(img.pixel(x, y))
            total += colorsys.rgb_to_hsv(c.redF(), c.greenF(), c.blueF())[1]
            count += 1
    return total / count


@pytest.fixture()
def scene(qtbot: object) -> CanvasScene:
    s = CanvasScene(width_cm=2000, height_cm=1400)
    irr = Layer(name="Anything translated", permaculture_layer=IRRIGATION_LAYER_KEY, z_order=1)
    s.set_layers([*s.layers, irr])
    s._irr = irr  # type: ignore[attr-defined]
    return s


def test_registered() -> None:
    assert RenderStyle.CAD in STYLE_PAINTERS


def test_roles_by_type_and_layer_key(scene: CanvasScene) -> None:
    assert role_for_type_name("HOUSE") is StyleRole.BUILDING
    assert role_for_type_name("TREE") is StyleRole.PLANT_TREE
    assert role_for_type_name("SOMETHING_NEW") is StyleRole.OTHER
    line = PolylineItem([P(0, 0), P(100, 0)], ObjectType.FENCE, layer_id=scene._irr.id)
    scene.addItem(line)
    ids = frozenset({scene._irr.id})
    assert role_for_item(line, ids) is StyleRole.IRRIGATION
    assert role_for_item(line) is StyleRole.FENCE_WALL


def test_irrigation_is_found_by_key_not_name(scene: CanvasScene) -> None:
    scene._irr.name = "Bewaesserung"
    line = PolylineItem([P(100, 700), P(1900, 700)], ObjectType.PATH, layer_id=scene._irr.id)
    scene.addItem(line)
    img = _render(scene, RenderStyle.CAD)
    blue = sum(
        1 for y in range(img.height()) for x in range(img.width())
        if QColor(img.pixel(x, y)).blue() > QColor(img.pixel(x, y)).red() + 60
    )
    assert blue > 20


def test_empty_region_is_paper_and_building_is_hatched(scene: CanvasScene) -> None:
    lay = scene.layers[0].id
    scene.addItem(PolygonItem([P(200, 800), P(800, 800), P(800, 1300), P(200, 1300)],
                              ObjectType.HOUSE, layer_id=lay))
    img = _render(scene, RenderStyle.CAD, 800, 560)
    assert QColor(img.pixel(600, 100)).red() >= 250  # empty paper
    assert _saturation(img, 0, 0, 800, 560) < 0.08
    inside = {img.pixel(x, 150) for x in range(100, 300)}
    assert len(inside) > 1  # hatch lines cross the interior


def test_canopy_radius_is_current_size(scene: CanvasScene) -> None:
    lay = scene.layers[0].id
    scene.addItem(CircleItem(1000, 700, 200, ObjectType.TREE, layer_id=lay))
    img = _render(scene, RenderStyle.CAD, 800, 560)
    # row through the centre (y 280 px): first dark pixel left of centre is the circle
    cx = 400
    dark = [x for x in range(cx) if QColor(img.pixel(x, 280)).red() < 200]
    assert dark, "no canopy drawn"
    assert abs((cx - min(dark)) - 200 * 0.4) <= 2  # 0.4 px per cm, radius 200 cm


def test_default_output_unchanged_by_registration(scene: CanvasScene) -> None:
    lay = scene.layers[0].id
    scene.addItem(PolygonItem([P(200, 800), P(800, 800), P(800, 1300)],
                              ObjectType.HOUSE, layer_id=lay))
    a = _render(scene, RenderStyle.ILLUSTRATED)
    _render(scene, RenderStyle.CAD)
    b = _render(scene, RenderStyle.ILLUSTRATED)
    assert a == b


def test_render_is_deterministic(scene: CanvasScene) -> None:
    scene.addItem(PolylineItem([P(100, 100), P(900, 500)], ObjectType.PATH,
                               layer_id=scene.layers[0].id,
                               path_fence_style=PathFenceStyle.STEPPING_STONES))
    assert _render(scene, RenderStyle.CAD) == _render(scene, RenderStyle.CAD)


def test_hatch_phase_anchored_to_scene(scene: CanvasScene) -> None:
    from open_garden_planner.ui.canvas.style.cad import _hatch_for
    item = PolygonItem([P(0, 0), P(300, 0), P(300, 300), P(0, 300)], ObjectType.HOUSE,
                       layer_id=scene.layers[0].id)
    scene.addItem(item)
    ring = [item.polygon().at(i) for i in range(4)]
    spacing = 2.0 * 96 / 25.4  # band 0, 96 dpi
    for pos in (P(0, 0), P(37, 13)):
        item.setPos(pos)
        segments = _hatch_for(item, ring, (45.0, 0.18, 2.0), 0)
        assert segments
        for a, _b in segments:
            sa = item.mapToScene(a)
            k = ((sa.y() - sa.x()) / 2 ** 0.5) / spacing  # 45-degree family offset
            assert abs(k - round(k)) < 1e-3


def test_pad_only_in_cad(scene: CanvasScene) -> None:
    item = PolygonItem([P(0, 0), P(300, 0), P(300, 300)], ObjectType.HOUSE,
                       layer_id=scene.layers[0].id)
    scene.addItem(item)
    plain = item.boundingRect()
    scene.set_render_style(RenderStyle.CAD)
    assert item.boundingRect().width() > plain.width()
    assert styled_bounding_rect(item, QRectF(0, 0, 10, 10)).width() > 10
    scene.set_render_style(RenderStyle.ILLUSTRATED)
    assert item.boundingRect() == plain
