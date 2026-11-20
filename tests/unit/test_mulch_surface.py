"""Mulch as an object type: its style, its roles in every render style, its panel colours,
its place in each registry that lists the ground surfaces, and that it is a ground surface
(lies on the ground, no height) like a driveway."""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QColor, QImage, QPainter

from open_garden_planner.agent_api.creates import _POLYGON_TYPE_NAMES
from open_garden_planner.core.fill_patterns import MULCH_COLORS, FillPattern, mulch_color_key
from open_garden_planner.core.object_height import GROUND_SURFACE_TYPE_NAMES, lies_on_ground
from open_garden_planner.core.object_types import (
    OBJECT_STYLES,
    ObjectType,
    get_style,
    get_valid_types_for_shape,
)
from open_garden_planner.core.render_style import RenderStyle
from open_garden_planner.core.render_style_roles import StyleRole, role_for_type_name
from open_garden_planner.core.tools import ToolType
from open_garden_planner.services.scene_rendering import render_scene_region, style_background
from open_garden_planner.ui.canvas.canvas_scene import CanvasScene
from open_garden_planner.ui.canvas.items import PolygonItem
from open_garden_planner.ui.widgets.gallery_data import _bed_and_surface_items


def test_mulch_is_a_polygon_surface_with_the_mulch_texture() -> None:
    style = OBJECT_STYLES[ObjectType.MULCH]
    assert style.fill_pattern is FillPattern.MULCH
    assert style.display_name == "Mulch"
    assert ObjectType.MULCH in get_valid_types_for_shape("polygon")


def test_mulch_lies_on_the_ground_until_given_a_height() -> None:
    assert "MULCH" in GROUND_SURFACE_TYPE_NAMES
    assert lies_on_ground(ObjectType.MULCH, {})
    assert not lies_on_ground(ObjectType.MULCH, {"object_height_cm": 20.0})


def test_the_agent_can_create_mulch_as_a_polygon() -> None:
    assert "MULCH" in _POLYGON_TYPE_NAMES


def test_mulch_has_a_style_role_and_a_tool_and_a_gallery_entry(qtbot: object) -> None:
    assert role_for_type_name("MULCH") is StyleRole.MULCH
    assert ToolType.MULCH.name == "MULCH"
    items = {item.object_type: item for item in _bed_and_surface_items()}
    assert items[ObjectType.MULCH].tool_type is ToolType.MULCH


def test_mulch_presets_are_distinct_and_recognised() -> None:
    keys = [key for key, _name, _hex in MULCH_COLORS]
    assert len(set(keys)) == len(keys) == 4
    for key, _name, hex_rgb in MULCH_COLORS:
        assert mulch_color_key(QColor(hex_rgb)) == key
    assert mulch_color_key(QColor("#33aa33")) is None
    # the type's own default fill is a preset, so a fresh mulch reads "Bark", not "Custom"
    assert mulch_color_key(get_style(ObjectType.MULCH).fill_color) == "bark"


def _render(style: RenderStyle, item_type: ObjectType) -> QImage:
    scene = CanvasScene(width_cm=2000, height_cm=1400)
    scene.addItem(PolygonItem([QPointF(200, 200), QPointF(900, 200), QPointF(900, 700),
                               QPointF(200, 700)], item_type, layer_id=scene.layers[0].id))
    scene.set_render_style(style)
    img = QImage(400, 280, QImage.Format.Format_ARGB32)
    img.fill(style_background(style, scene))
    painter = QPainter(img)
    try:
        render_scene_region(scene, painter, QRectF(0, 0, 400, 280), QRectF(0, 0, 2000, 1400),
                            render_style=style)
    finally:
        painter.end()
    return img


@pytest.mark.parametrize("style", list(RenderStyle), ids=lambda s: s.value)
def test_mulch_is_drawn_in_every_render_style(qtbot: object, style: RenderStyle) -> None:
    blank = _render(style, ObjectType.GENERIC_POLYGON)
    mulch = _render(style, ObjectType.MULCH)
    assert mulch != blank or style is RenderStyle.ILLUSTRATED  # CAD/wash differ by role
    inside = mulch.copy(100, 100, 150, 100)
    background = QColor(style_background(style, CanvasScene(width_cm=2000, height_cm=1400)))
    differing = sum(1 for y in range(0, inside.height(), 3) for x in range(0, inside.width(), 3)
                    if QColor(inside.pixel(x, y)).rgb() != background.rgb())
    assert differing > 50, f"{style.value}: the mulch polygon left the paper bare"


def test_cad_mulch_is_told_from_a_lawn_and_a_bed(qtbot: object) -> None:
    mulch = _render(RenderStyle.CAD, ObjectType.MULCH)
    assert mulch != _render(RenderStyle.CAD, ObjectType.LAWN)
    assert mulch != _render(RenderStyle.CAD, ObjectType.GARDEN_BED)
