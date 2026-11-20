"""Field Remote tree height from the running app (docs 8.10).

Real uvicorn + TLS on loopback and the real canvas: the phone locks A and B, posts synthetic VIO,
asks for a two-tap height (the trunk's plan spot comes back because the lock holds), and saves it
as a pending measurement record that the desktop-side read path returns. Synthetic poses only:
nothing here says how a real phone's WebXR hit-test or orientation behave.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from open_garden_planner.app.application import GardenPlannerApp
from open_garden_planner.app.settings import get_settings
from open_garden_planner.core.georef import GeoReference
from open_garden_planner.field_remote.routes import plan_id
from open_garden_planner.services import field_records as fr
from tests.integration.test_field_remote_ar import A_CM, B_CM, T0, _lock, _vio
from tests.integration.test_field_remote_position import Phone, _free_port
from tests.unit.test_tree_height import aim


@pytest.fixture(autouse=True)
def _no_welcome_dialog(_reset_app_settings: Any) -> None:
    get_settings().show_welcome_on_startup = False


@pytest.fixture
def phone(qtbot: Any, tmp_path: Path):
    win = GardenPlannerApp()
    qtbot.addWidget(win)
    host = win.field_remote_host
    host._data_dir = tmp_path / "tls"
    get_settings().field_remote_port = _free_port()
    win._project_manager.set_georef(GeoReference(origin_lat=48.0, origin_lon=11.0, scale_meters_per_unit=0.01))
    win._project_manager.mark_clean()
    host.start()
    assert host.is_running
    client = Phone(qtbot, get_settings().field_remote_port, get_settings().field_remote_token)
    yield win, host, client
    client.close()
    host.stop()


def test_a_tree_height_is_computed_placed_on_the_plan_and_saved_for_review(phone: Any, qtbot: Any, tmp_path: Path) -> None:
    win, host, client = phone
    undo_before = len(win.canvas_view.command_manager._undo_stack)

    def call(method: str, path: str, **kw: Any) -> Any:
        qtbot.wait(250)  # the write limit is 5 per second
        return client.call(method, path, **kw)

    # the AR lock first (same session, same frame): A at plan (1000, 500) cm, B 20 m east
    call("POST", "/api/position", json={"vio_batch": [_vio(T0, 0.0, 0.0)]})
    assert call("POST", "/api/ar/lock/a", json=_lock(A_CM, (0.0, 0.0), T0)).status_code == 200
    call("POST", "/api/position", json={"vio_batch": [_vio(T0 + 0.5 * k, float(k), 0.0) for k in range(1, 21)]})
    assert call("POST", "/api/ar/lock/b", json=_lock(B_CM, (20.0, 0.0), T0 + 10.0)).status_code == 200

    # a 12 m tree 10 m north of the spot the phone stands on (x=10, z=0); the trunk foot is on the ground
    viewer, base, top = (10.0, 1.3, 0.0), (10.0, -0.2, -10.0), (10.0, 11.8, -10.0)

    def tap(target: tuple[float, float, float], t: float) -> dict[str, Any]:
        return {"position": list(viewer), "quaternion": list(aim(viewer, target)), "tracking": "tracking", "t": t}

    body = {
        "tap1": tap(base, T0 + 11.0), "tap2": tap(top, T0 + 13.0),
        "ground": {"x": base[0], "y": base[1], "z": base[2]},
    }
    res = call("POST", "/api/ar/height", json=body)
    assert res.status_code == 200, res.text
    answer = res.json()
    assert answer["mode"] == "hit_test" and answer["height_m"] == pytest.approx(12.0, abs=0.01)
    assert answer["trunk_plan"]["x_cm"] == pytest.approx(A_CM[0] + 1000.0, abs=1.0)  # 10 m east of A
    assert answer["trunk_plan"]["y_cm"] == pytest.approx(A_CM[1] + 1000.0, abs=1.0)  # 10 m north of A

    # saving obeys the toggle and the saved plan; the record lands in the sidecar of this process
    plan = tmp_path / "garden.ogp"
    win._project_manager._current_file = plan
    client._client.headers["X-Plan-Id"] = plan_id(str(plan))
    record = {
        "kind": "measurement", "idempotency_key": "int-tree-height-01", "plant_name": "Oak", "height_m": answer["height_m"],
        "height_data": {"mode": answer["mode"], "warnings": answer["warnings"], "uncertainty_m": answer["uncertainty_m"]},
        "geometry": {"type": "point", "lat": answer["trunk_plan"]["lat"], "lon": answer["trunk_plan"]["lon"],
                     "x_cm": answer["trunk_plan"]["x_cm"], "y_cm": answer["trunk_plan"]["y_cm"]},
        "fix_mode": "vio_fused", "precision_hint": "plan_locked", "linked_object_id": "obj-42",
    }
    assert call("POST", "/api/records", json=record).status_code == 403  # the toggle is off by default
    host.set_records_enabled(True)
    saved = call("POST", "/api/records", json=record)
    assert saved.status_code == 201, saved.text
    stored = fr.load_records(plan).records
    assert len(stored) == 1 and stored[0].kind == "measurement" and stored[0].status == "pending"
    assert stored[0].height_m == answer["height_m"] and stored[0].linked_object_id == "obj-42"

    listed = call("GET", "/api/field-records").json()  # the existing read path
    assert listed["records"][0]["height_m"] == answer["height_m"]
    assert listed["records"][0]["height_data"]["mode"] == "hit_test"

    # a measurement only: nothing on the plan changed
    assert len(win.canvas_view.command_manager._undo_stack) == undo_before
    assert win._project_manager.is_dirty is False


def test_the_height_answer_needs_no_records_permission_and_no_lock(phone: Any, qtbot: Any) -> None:
    _win, _host, client = phone
    viewer, base, top = (0.0, 1.3, 0.0), (0.0, -0.2, -8.0), (0.0, 11.8, -8.0)
    body = {
        "tap1": {"position": list(viewer), "quaternion": list(aim(viewer, base)), "tracking": "tracking", "t": T0},
        "tap2": {"position": list(viewer), "quaternion": list(aim(viewer, top)), "tracking": "tracking", "t": T0 + 2},
        "ground": {"x": base[0], "y": base[1], "z": base[2]},
    }
    res = client.call("POST", "/api/ar/height", json=body)
    assert res.status_code == 200 and res.json()["trunk_plan"] is None  # records off, no lock: still an answer
    raw = json.dumps({**body, "tap2": {**body["tap2"], "t": float("nan")}}, allow_nan=True)  # a hostile client's NaN
    refused = client.call("POST", "/api/ar/height", content=raw, headers={"Content-Type": "application/json"})
    assert refused.status_code == 422
