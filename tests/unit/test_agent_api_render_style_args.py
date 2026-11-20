"""The Agent API's render-style argument handling that needs no server (WP11)."""

from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtWidgets import QGraphicsScene

from open_garden_planner.agent_api.render import render_canvas_image
from open_garden_planner.core.render_style import (
    RENDER_STYLE_INFO,
    RENDER_STYLES,
    render_style_catalogue,
)


def test_the_catalogue_covers_every_style_in_order() -> None:
    catalogue = render_style_catalogue()
    assert [entry["id"] for entry in catalogue] == [style.value for style in RENDER_STYLES]
    assert set(RENDER_STYLE_INFO) == set(RENDER_STYLES)
    for entry in catalogue:
        assert set(entry) == {"id", "label_en", "svg_vector", "supports_3d", "options"}
        assert entry["svg_vector"] in ("full", "approximate")


def test_an_unknown_style_is_refused_before_anything_is_drawn(qtbot: Any) -> None:
    scene = QGraphicsScene(0, 0, 100, 100)
    with pytest.raises(ValueError, match="valid values: illustrated, cad, line_and_wash"):
        render_canvas_image(scene, None, None, 128, render_style="watercolour")


def test_a_plain_scene_without_a_style_renders_illustrated(qtbot: Any) -> None:
    scene = QGraphicsScene(0, 0, 100, 100)
    result = render_canvas_image(scene, None, None, 128)
    assert result["render_style"] == "illustrated" and result["warnings"] == []
    assert result["callouts"] is False
