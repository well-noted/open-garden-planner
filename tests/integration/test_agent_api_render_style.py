"""``render_style`` on the Agent API's render tools (render styles WP11).

A real server and client against a real scene (the ``test_agent_api_render`` rig). The
contract pinned here: an explicit style draws that one picture and leaves the scene exactly
as it was (its style, its paint revision: nothing repaints), no style follows the user's
current one, the answer echoes what was drawn, callouts belong to CAD, and an unknown id is
an error that lists the valid ones. The 3D half (stand-in provider, real coordinator) is in
``test_agent_api_render_3d.py`` and ``test_directors_cut.py``.
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from typing import Any

import pytest

from open_garden_planner.core.render_style import (
    RENDER_STYLES,
    RenderStyle,
    render_style_catalogue,
)
from tests.agent_api_helpers import start_server
from tests.integration.test_agent_api_render import _decode_png, _drive, _providers


def _call(canvas: Any, qtbot: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    scene = canvas.scene()
    providers = replace(_providers(scene), render_style=lambda: scene.render_style.value)
    server, _ = start_server(providers)
    result: dict[str, Any] = {}

    async def body(session: Any) -> None:
        tools = await session.list_tools()
        result["tools"] = {tool.name: tool for tool in tools.tools}
        result["call"] = await session.call_tool(name, arguments)

    threading.Thread(target=_drive, args=(server, body, result), name="mcp-test-client").start()
    try:
        qtbot.waitUntil(lambda: result.get("done", False), timeout=20000)
        assert result.get("error") is None, result.get("error")
    finally:
        server.stop()
    return result


def _render(canvas: Any, qtbot: Any, **arguments: Any) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    result = _call(canvas, qtbot, "render_canvas_image", {"image_width_px": 256, **arguments})
    call = result["call"]
    assert call.isError is not True, call.content
    image_block, text_block = call.content
    return _decode_png(image_block), json.loads(text_block.text), result


def _scene_state(scene: Any) -> tuple[Any, ...]:
    return (scene.render_style, scene._paint_revision, scene.style_output_dpi)


def test_an_explicit_style_draws_one_picture_and_leaves_the_scene_as_it_was(
        canvas: Any, qtbot: Any) -> None:
    scene = canvas.scene()
    assert scene.render_style is RenderStyle.ILLUSTRATED
    before = _scene_state(scene)
    pixels = {}
    for style in ("cad", "line_and_wash"):
        image, meta, _ = _render(canvas, qtbot, render_style=style)
        assert meta["render_style"] == style
        assert meta["warnings"] == []
        pixels[style] = image.pixelColor(2, 2)
        assert _scene_state(scene) == before, f"{style}: the scene is as it was"
    assert pixels["cad"].name() == "#ffffff", "CAD is drawn on white paper"
    assert pixels["line_and_wash"].name() != "#ffffff", "Line & Wash on its own paper"
    assert scene.render_style is RenderStyle.ILLUSTRATED


def test_no_style_follows_the_users_current_one_and_says_which(canvas: Any, qtbot: Any) -> None:
    scene = canvas.scene()
    _image, meta, _ = _render(canvas, qtbot)
    assert meta["render_style"] == "illustrated"
    scene.set_render_style(RenderStyle.CAD)
    image, meta, _ = _render(canvas, qtbot)
    assert meta["render_style"] == "cad" and image.pixelColor(2, 2).name() == "#ffffff"
    # Naming the user's own style is the same picture as naming none.
    again, meta2, _ = _render(canvas, qtbot, render_style="cad")
    assert meta2["render_style"] == "cad" and again == image
    # An explicit style for one call does not move the user's choice.
    _render(canvas, qtbot, render_style="line_and_wash")
    assert scene.render_style is RenderStyle.CAD


def test_callouts_belong_to_cad_and_are_reported_otherwise(canvas: Any, qtbot: Any) -> None:
    _image, meta, _ = _render(canvas, qtbot, render_style="line_and_wash", callouts=True)
    assert meta["callouts"] is False
    assert len(meta["warnings"]) == 1 and "callouts ignored" in meta["warnings"][0]
    _image, meta, _ = _render(canvas, qtbot, render_style="cad", callouts=True)
    assert len(meta["warnings"]) == 1 and "draws none" in meta["warnings"][0]
    _image, meta, _ = _render(canvas, qtbot, render_style="cad")
    assert meta["warnings"] == [], "callouts not asked for: nothing to report"


def test_an_unknown_style_is_an_error_that_lists_the_valid_ids(canvas: Any, qtbot: Any) -> None:
    scene = canvas.scene()
    before = _scene_state(scene)
    result = _call(canvas, qtbot, "render_canvas_image", {"render_style": "watercolour"})
    assert result["call"].isError is True
    text = result["call"].content[0].text
    assert all(style.value in text for style in RENDER_STYLES)
    assert _scene_state(scene) == before


def test_the_schema_enum_is_the_shared_constant_and_the_description_says_omit(
        canvas: Any, qtbot: Any) -> None:
    result = _call(canvas, qtbot, "list_render_styles", {})
    expected = [style.value for style in RENDER_STYLES]
    for name in ("render_canvas_image", "render_3d_view"):
        schema = result["tools"][name].inputSchema["properties"]["render_style"]
        enums = [branch["enum"] for branch in schema["anyOf"] if "enum" in branch]
        assert enums == [expected], name
    assert "callouts" in result["tools"]["render_canvas_image"].inputSchema["properties"]


def test_list_render_styles_describes_each_style_and_the_current_one(
        canvas: Any, qtbot: Any) -> None:
    canvas.scene().set_render_style(RenderStyle.LINE_AND_WASH)
    result = _call(canvas, qtbot, "list_render_styles", {})
    call = result["call"]
    assert call.isError is not True
    payload = call.structuredContent
    assert payload["current"] == "line_and_wash"
    assert payload["styles"] == render_style_catalogue()
    by_id = {entry["id"]: entry for entry in payload["styles"]}
    assert by_id["cad"]["options"] == ["callouts"] and by_id["illustrated"]["options"] == []
    assert by_id["line_and_wash"]["svg_vector"] == "approximate"
    assert all(entry["supports_3d"] is True for entry in payload["styles"])


@pytest.mark.parametrize("style", [s.value for s in RENDER_STYLES])
def test_the_echoed_style_reproduces_the_picture(canvas: Any, qtbot: Any, style: str) -> None:
    first, meta, _ = _render(canvas, qtbot, render_style=style)
    second, _meta, _ = _render(canvas, qtbot, render_style=meta["render_style"])
    assert first == second
