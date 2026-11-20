"""MCP tests for the WP-R agent approver: ``place_field_record`` / ``reject_field_record``.

Same harness as ``test_agent_api_field_photos`` (real providers, real MCP client). Pins the
edit toggle (writes disabled: the tools do not exist), token auth, locked-layer refusal, ONE
undo step per approval, polylines with the Irrigation layer, reference points, candidate
supersede and the ``status``/``kind``/``group_id`` filters of ``list_field_records``.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtWidgets import QMessageBox

from open_garden_planner.app.application import GardenPlannerApp
from open_garden_planner.app.settings import get_settings
from open_garden_planner.core.geo_frame import make_frame
from open_garden_planner.services import field_records as fr
from tests.agent_api_helpers import start_server

TOKEN = "test-write-token-12345"
ORIGIN = (48.0, 11.0)


@pytest.fixture()
def app(qtbot: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    get_settings().show_welcome_on_startup = False
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_a, **_k: QMessageBox.StandardButton.Discard
    )
    win = GardenPlannerApp()
    qtbot.addWidget(win)
    win.canvas_view.set_snap_enabled(False)
    win._project_manager._current_file = tmp_path / "garden.ogp"
    rect = win.canvas_scene.canvas_rect
    centred = make_frame(*ORIGIN)
    lat0, lon0 = centred.canvas_to_gps(-rect.width() / 2.0, -rect.height() / 2.0)
    win._project_manager.set_georeference(make_frame(lat0, lon0).georeference)
    win._project_manager.set_north_angle(0.0)
    try:
        yield win
    finally:
        win._stop_agent_api()


def _call(
    app: Any,
    qtbot: Any,
    calls: list[tuple[str, dict[str, Any]]],
    *,
    token: str | None = TOKEN,
    writes: bool = True,
) -> list[tuple[bool, Any, str]]:
    server, _ = start_server(app._build_agent_providers(), write_token=TOKEN, writes_enabled=writes)
    out: list[tuple[bool, Any, str]] = []
    state: dict[str, Any] = {}

    async def run() -> None:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with (
            streamablehttp_client(server.url, headers=headers) as (r, w, _),
            ClientSession(r, w) as session,
        ):
            await session.initialize()
            for name, args in calls:
                res = await session.call_tool(name, args)
                text = " ".join(getattr(c, "text", "") for c in res.content)
                out.append((bool(res.isError), res.structuredContent, text))

    def drive() -> None:
        try:
            asyncio.run(run())
        except Exception as exc:
            state["error"] = exc
        finally:
            state["done"] = True

    try:
        threading.Thread(target=drive, name="mcp-field-records-client").start()
        qtbot.waitUntil(lambda: state.get("done", False), timeout=30000)
    finally:
        server.stop()
    assert state.get("error") is None, state.get("error")
    return out


def _plan(app: Any) -> str:
    return str(app._project_manager.current_file)


def _depth(app: Any) -> int:
    return len(app.canvas_view.command_manager._undo_stack)


def _add(app: Any, **fields: Any) -> str:
    fields.setdefault("lat", ORIGIN[0])
    fields.setdefault("lon", ORIGIN[1])
    return fr.add_record(_plan(app), fields).id


def _status(app: Any, rid: str) -> str:
    return fr.get_record(_plan(app), rid).status


def _line(app: Any) -> list[list[float]]:
    rect = app.canvas_scene.canvas_rect
    cx, cy = rect.width() / 2.0, rect.height() / 2.0
    return [[ORIGIN[0], ORIGIN[1], cx, cy], [ORIGIN[0], ORIGIN[1], cx + 200.0, cy]]


def _layer_names(app: Any) -> list[str]:
    return [lyr.name for lyr in app.canvas_scene.layers]


class TestToggleAndToken:
    def test_edit_toggle_off_removes_the_write_tools(self, app, qtbot):
        rid = _add(app, plant_name="Apple", species_key="Malus domestica")
        before = _depth(app)
        res = _call(
            app,
            qtbot,
            [("place_field_record", {"record_id": rid}), ("reject_field_record", {"record_id": rid})],
            writes=False,
        )
        assert [r[0] for r in res] == [True, True]
        assert _depth(app) == before and _status(app, rid) == "pending"

    def test_missing_token_is_refused(self, app, qtbot):
        rid = _add(app, plant_name="Apple", species_key="Malus domestica")
        before = _depth(app)
        (res,) = _call(app, qtbot, [("place_field_record", {"record_id": rid})], token=None)
        assert res[0] is True
        assert _depth(app) == before and _status(app, rid) == "pending"


class TestPlace:
    def test_plant_is_one_undo_step_and_marked_placed(self, app, qtbot):
        rid = _add(app, plant_name="Apple", species_key="Malus domestica")
        before = _depth(app)
        (res,) = _call(app, qtbot, [("place_field_record", {"record_id": rid})])
        assert res[0] is False, res[2]
        assert res[1]["kind"] == "plant" and res[1]["item_id"]
        assert _depth(app) == before + 1
        assert fr.get_record(_plan(app), rid).placed_object_id == res[1]["item_id"]
        app.canvas_view.command_manager.undo()
        assert _depth(app) == before

    def test_note_becomes_a_callout_carrying_its_record(self, app, qtbot):
        rid = _add(app, kind="note", notes="wet corner")
        (res,) = _call(app, qtbot, [("place_field_record", {"record_id": rid})])
        assert res[0] is False, res[2]
        item = next(i for i in app.canvas_scene.items() if str(getattr(i, "item_id", "")) == res[1]["item_id"])
        assert item.metadata["field_record"]["record_id"] == rid

    def test_already_placed_and_rejected_records_are_refused(self, app, qtbot):
        placed = _add(app, kind="note", notes="a")
        rejected = _add(app, kind="note", notes="b", status="rejected")
        res = _call(
            app,
            qtbot,
            [
                ("place_field_record", {"record_id": placed}),
                ("place_field_record", {"record_id": placed}),
                ("place_field_record", {"record_id": rejected}),
                ("place_field_record", {"record_id": "does-not-exist"}),
            ],
        )
        assert [r[0] for r in res] == [False, True, True, True]
        assert "already placed" in res[1][2]

    def test_locked_active_layer_refuses_and_leaves_everything(self, app, qtbot):
        app.canvas_scene.active_layer.locked = True
        rid = _add(app, plant_name="Apple", species_key="Malus domestica")
        before = _depth(app)
        (res,) = _call(app, qtbot, [("place_field_record", {"record_id": rid})])
        assert res[0] is True and "locked" in res[2]
        assert _depth(app) == before and _status(app, rid) == "pending"

    def test_irrigation_creates_layer_and_line_in_one_undo(self, app, qtbot):
        rid = _add(app, kind="irrigation_path", geometry={"type": "polyline", "points": _line(app)})
        before = _depth(app)
        (res,) = _call(app, qtbot, [("place_field_record", {"record_id": rid})])
        assert res[0] is False, res[2]
        assert _depth(app) == before + 1 and "Irrigation" in _layer_names(app)
        app.canvas_view.command_manager.undo()
        assert "Irrigation" not in _layer_names(app) and _depth(app) == before

    def test_locked_irrigation_layer_is_refused(self, app, qtbot):
        from open_garden_planner.models.layer import Layer

        layer = Layer(name="Irrigation", locked=True)
        app.canvas_scene.layers.append(layer)
        rid = _add(app, kind="irrigation_path", geometry={"type": "polyline", "points": _line(app)})
        before = _depth(app)
        (res,) = _call(app, qtbot, [("place_field_record", {"record_id": rid})])
        assert res[0] is True and "locked" in res[2]
        assert layer.locked and _depth(app) == before

    def test_reference_points_are_fitted_without_a_dialog(self, app, qtbot):
        a = _add(
            app, kind="reference_point", name="A", lon=11.0,
            geometry={"type": "point", "lat": 48.0, "lon": 11.0, "x_cm": 100.0, "y_cm": 100.0},
        )
        b = _add(
            app, kind="reference_point", name="B", lon=11.001,
            geometry={"type": "point", "lat": 48.0, "lon": 11.001, "x_cm": 8300.0, "y_cm": 100.0},
        )
        before = _depth(app)
        res = _call(app, qtbot, [("place_field_record", {"record_id": a}), ("place_field_record", {"record_id": b})])
        assert [r[0] for r in res] == [False, False], [r[2] for r in res]
        assert _depth(app) == before + 2  # one undo step each
        assert res[1][1]["georef_updated"] and res[1][1]["fit_rms_m"] is not None
        assert {p.label for p in app._project_manager.georeference.control_points} == {"A", "B"}
        assert _status(app, a) == "approved" and _status(app, b) == "approved"


class TestGroupsAndList:
    def _three(self, app: Any) -> list[str]:
        return [
            _add(app, kind="placement_request", what="apple", group_id="grp1", plant_name="Apple",
                 species_key="Malus domestica", lat=ORIGIN[0] + i * 1e-6,
                 geometry={"type": "point", "lat": ORIGIN[0] + i * 1e-6, "lon": ORIGIN[1], "x_cm": 1000.0 + i * 50, "y_cm": 1000.0})
            for i in range(3)
        ]

    def test_approving_a_candidate_supersedes_siblings(self, app, qtbot):
        a, b, c = self._three(app)
        res = _call(
            app,
            qtbot,
            [
                ("list_field_records", {"group_id": "grp1"}),
                ("place_field_record", {"record_id": b}),
                ("list_field_records", {"group_id": "grp1", "status": "superseded"}),
            ],
        )
        assert res[0][1]["groups"][0]["group_id"] == "grp1" and len(res[0][1]["groups"][0]["open_ids"]) == 3
        assert sorted(res[1][1]["superseded"]) == sorted([a, c])
        assert res[2][1]["returned"] == 2
        assert (_status(app, a), _status(app, b), _status(app, c)) == ("superseded", "placed", "superseded")

    def test_reject_group_and_filters(self, app, qtbot):
        a, b, c = self._three(app)
        note = _add(app, kind="note", notes="n")
        res = _call(
            app,
            qtbot,
            [
                ("reject_field_record", {"group_id": "grp1"}),
                ("list_field_records", {"status": "rejected"}),
                ("list_field_records", {"kind": "note"}),
                ("list_field_records", {"status": "bogus"}),
                ("reject_field_record", {}),
            ],
        )
        assert sorted(res[0][1]["rejected"]) == sorted([a, b, c])
        assert res[1][1]["returned"] == 3 and res[2][1]["returned"] == 1
        assert res[2][1]["records"][0]["id"] == note
        assert res[3][0] is True and res[4][0] is True

    def test_reject_single_changes_no_plan(self, app, qtbot):
        rid = _add(app, plant_name="Apple", species_key="Malus domestica")
        before = _depth(app)
        (res,) = _call(app, qtbot, [("reject_field_record", {"record_id": rid})])
        assert res[0] is False and _status(app, rid) == "rejected" and _depth(app) == before
