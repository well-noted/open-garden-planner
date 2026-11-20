"""End-to-end test of the read-only site-measurement and height tools (real MCP).

Loads a copy of the owner's Encanto plan into a real scene, serves it through the
real Agent API server and calls every new tool over streamable HTTP. Expected
values are hand-computed from the polygon points in the file (see the comments)
or taken from ``core.object_height`` directly, so the tools are pinned to the
geometry the file holds and to the ladder the 3D view reads.
"""

from __future__ import annotations

import dataclasses
import math
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest

from open_garden_planner.core import ProjectManager
from open_garden_planner.core import object_height as oh
from tests.agent_api_helpers import start_server
from tests.integration.test_agent_api_server import _drive, _providers

ENCANTO = Path(r"C:\Users\tetuo\Documents\OpenGardenPlanner\3830-E-Encanto-St.ogp")

pytestmark = pytest.mark.skipif(not ENCANTO.exists(), reason="Encanto plan copy not available")


def _call(server: Any, qtbot: Any, body: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    threading.Thread(target=_drive, args=(server, body, result), name="mcp-test-client").start()
    qtbot.waitUntil(lambda: result.get("done", False), timeout=20000)
    assert result.get("error") is None, result.get("error")
    return result


def test_site_measure_tools_on_the_encanto_plan(canvas: Any, qtbot: Any, tmp_path: Path) -> None:
    plan = tmp_path / "encanto.ogp"
    shutil.copy(ENCANTO, plan)
    scene = canvas.scene()
    manager = ProjectManager()
    manager.load(scene, plan)
    base = _providers(scene)
    providers = dataclasses.replace(base, snapshot=lambda: manager.snapshot_dict(scene))

    snapshot = manager.snapshot_dict(scene)
    by_name = {str(o.get("name")): o for o in snapshot["objects"] if o.get("name")}
    driveway = next(o for o in snapshot["objects"] if o.get("object_type") == "DRIVEWAY")
    house = next(o for o in snapshot["objects"] if o.get("object_type") == "HOUSE")
    wall = next(o for o in snapshot["objects"] if o.get("object_type") == "WALL")
    tree = by_name["Tipu tree (Tipuana tipu)"]
    # The loaded plan is a rotated-free plan; guard the hand computation below.
    assert not any(o.get("rotation_angle") for o in (driveway, house, wall))
    dirty_before = manager.is_dirty
    undo_before = getattr(scene, "undo_stack", None)

    server, _ = start_server(providers)
    out: dict[str, Any] = {}

    async def body(session: Any) -> None:
        class _Reply:
            def __init__(self, res: Any) -> None:
                self.raw = res
                sc = res.structuredContent
                self.structuredContent = sc["result"] if sc and set(sc) == {"result"} else sc
                self.isError = res.isError
                self.content = res.content

        async def call(name: str, args: dict[str, Any]) -> Any:
            return _Reply(await session.call_tool(name, args))

        out["tools"] = [t.name for t in (await session.list_tools()).tools]
        out["area"] = (await call("measure_area", {"item_id": driveway["item_id"]})).structuredContent
        out["length"] = (await call("measure_length", {"item_id": wall["item_id"]})).structuredContent
        out["clear"] = (await call(
            "measure_clearance", {"id_a": house["item_id"], "id_b": wall["item_id"]})).structuredContent
        out["clear_alias"] = (await call(
            "measure_clearance", {"id_a": house["item_id"], "id_b": "boundary"})).structuredContent
        out["points"] = (await call("measure_points", {
            "a": {"x_cm": 1237.0, "y_cm": 84.0}, "b": {"x_cm": 2423.0, "y_cm": 1671.0}})).structuredContent
        out["to_obj"] = (await call("measure_to_object", {
            "point": {"x_cm": 0.0, "y_cm": 3426.0}, "item_id": house["item_id"]})).structuredContent
        out["path"] = (await call("measure_path", {"points": [
            {"x_cm": 0.0, "y_cm": 0.0}, {"x_cm": 300.0, "y_cm": 0.0}, {"x_cm": 300.0, "y_cm": 400.0}],
            "closed": True})).structuredContent
        out["extents"] = (await call("site_extents", {})).structuredContent
        out["h_house"] = (await call("get_object_height", {"item_id": house["item_id"]})).structuredContent
        out["h_tree"] = (await call("get_object_height", {"item_id": tree["item_id"]})).structuredContent
        out["rows"] = (await call("list_object_heights", {"limit": 3})).structuredContent
        out["bad_nan"] = await call("measure_points", {
            "a": {"x_cm": 0.0, "y_cm": 0.0}, "b": {"x_cm": 1e12, "y_cm": 0.0}})
        out["bad_geo"] = await call("measure_points", {
            "a": {"lat": 33.0, "lon": -111.0}, "b": {"x_cm": 0.0, "y_cm": 0.0}})
        out["unknown"] = (await call("measure_clearance", {"id_a": "nope", "id_b": "boundary"})).structuredContent

    try:
        result = _call(server, qtbot, body)
        assert result.get("error") is None
    finally:
        server.stop()

    for name in ("measure_area", "measure_length", "measure_clearance", "measure_points",
                 "measure_path", "measure_to_object", "site_extents", "get_object_height",
                 "list_object_heights"):
        assert name in out["tools"], name

    # Driveway: axis-aligned 1186 x 1587 cm rectangle (x 1237..2423, y 84..1671).
    assert out["area"]["area_cm2"] == pytest.approx(1186 * 1587)  # 1 882 182
    assert out["area"]["perimeter_cm"] == pytest.approx(2 * (1186 + 1587))
    # Boundary wall: open 4-point line, legs 4699 + 3799 + 4699.
    assert out["length"]["length_cm"] == pytest.approx(4699 + 3799 + 4699)
    assert out["length"]["vertex_count"] == 4 and out["length"]["closed"] is False
    # House west face x=306 against the wall's west run x=52: 254 cm, the smallest gap
    # (east 3851-3315=536, north 4708-3426=1282, south 919-9=910).
    assert out["clear"]["distance_cm"] == pytest.approx(254.0)
    assert out["clear"]["relation"] == "disjoint" and out["clear"]["overlaps"] is False
    assert out["clear"]["point_b"]["x_cm"] == pytest.approx(52.0)
    assert out["clear"]["point_a"]["x_cm"] == pytest.approx(306.0)
    assert out["clear_alias"]["distance_cm"] == pytest.approx(254.0)
    # Driveway SW corner to NE corner: sqrt(1186^2 + 1587^2).
    assert out["points"]["distance_cm"] == pytest.approx(math.hypot(1186, 1587))
    assert out["points"]["dx_cm"] == pytest.approx(1186.0) and out["points"]["dy_cm"] == pytest.approx(1587.0)
    assert out["points"]["compass_bearing_deg"] == pytest.approx(math.degrees(math.atan2(1186, 1587)))
    # (0, 3426) is 306 cm west of the house's NW corner (306, 3426).
    assert out["to_obj"]["distance_cm"] == pytest.approx(306.0) and out["to_obj"]["inside"] is False
    assert out["path"]["leg_lengths_cm"] == pytest.approx([300.0, 400.0, 500.0])
    # Boundary: 3799 x 4699 rectangle once the open line is closed.
    ext = out["extents"]
    assert ext["boundary"]["area_cm2"] == pytest.approx(3799 * 4699)
    assert ext["boundary"]["closed_implicitly"] is True
    assert ext["min_x_cm"] == pytest.approx(52.0) and ext["min_y_cm"] == pytest.approx(9.0)
    assert sum(ext["layer_counts"].values()) == ext["object_count"] == 23 - 0
    assert ext["tallest"]["item_id"] == house["item_id"]
    # Heights equal what core.object_height computes from the loaded metadata.
    plan_date = None
    iso = snapshot["agent_meta"].get("as_of_date")
    if iso:
        from datetime import date

        plan_date = date.fromisoformat(iso)
    for key, obj in (("h_house", house), ("h_tree", tree)):
        expected, source = oh.resolve_height(obj["object_type"], obj.get("metadata"), plan_date)
        assert out[key]["height_cm"] == expected and out[key]["source"] == source
    assert out["h_house"]["height_cm"] == 450.0 and out["h_house"]["source"] == "default"
    assert [r["height_cm"] for r in out["rows"]] == sorted(
        (r["height_cm"] for r in out["rows"]), reverse=True)
    # Untrusted input is an MCP tool error, not a crash; unknown ids are null.
    assert out["bad_nan"].isError and "beyond" in out["bad_nan"].content[0].text
    assert out["bad_geo"].isError
    assert out["unknown"] is None
    # Read-only: nothing dirtied, no undo entry.
    assert manager.is_dirty == dirty_before
    if undo_before is not None:
        assert scene.undo_stack is undo_before
