"""``POST /api/ar/height`` and the saved tree-height record, through the real routes.

Synthetic poses only (the same scene as ``test_tree_height``); nothing here says how a real
phone's WebXR behaves.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from open_garden_planner.field_remote import server, tracking_routes
from open_garden_planner.field_remote.routes import plan_id
from open_garden_planner.services import field_records as fr
from tests.unit.test_field_remote_ar import T0, lock_both
from tests.unit.test_field_remote_read_routes import Env
from tests.unit.test_tree_height import aim, scene


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    return Env(tmp_path, records=False)  # the height answer writes nothing, so the toggle does not apply


def tap_body(viewer: tuple[float, float, float], target: tuple[float, float, float], t: float, tracking: str = "tracking") -> dict[str, Any]:
    return {"position": list(viewer), "quaternion": list(aim(viewer, target)), "tracking": tracking, "t": t}


def body(dist: float = 10.0, **extra: Any) -> dict[str, Any]:
    viewer, base, top = scene(dist, 20.0)
    return {
        "tap1": tap_body(viewer, base, T0),
        "tap2": tap_body(viewer, top, T0 + 2),
        "ground": {"x": base[0], "y": base[1], "z": base[2]},
        **extra,
    }


def test_the_height_route_is_session_only_and_needs_the_token(env: Env) -> None:
    assert "/api/ar/height" in tracking_routes.SESSION_PATHS <= server.WRITES_ALLOWED_WITHOUT_RECORDS
    assert env.client.post("/api/ar/height", json=body()).status_code == 401


def test_a_hit_test_answer_has_the_height_the_range_and_no_accuracy_claim(env: Env, tmp_path: Path) -> None:
    res = env.post("/api/ar/height", body())
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["mode"] == "hit_test" and out["height_m"] == pytest.approx(12.0, abs=0.01)
    assert out["range_m"][0] < out["height_m"] < out["range_m"][1]
    assert out["warnings"] == [] and out["trunk_plan"] is None  # no AR lock yet
    assert "NOT MEASURED" in out["assumed"]["note"]
    assert not any(k in out for k in ("accuracy", "accuracy_m", "sigma_m"))
    assert list(tmp_path.iterdir()) == []  # nothing on disk


def test_a_typed_distance_and_both_modes(env: Env) -> None:
    only = body()
    del only["ground"]
    only["manual_distance_m"] = 10.0
    assert env.post("/api/ar/height", only).json()["mode"] == "manual"
    both = env.post("/api/ar/height", body(manual_distance_m=10.0)).json()
    assert both["mode"] == "both" and both["hit_test"]["height_m"] == pytest.approx(12.0, abs=0.01)


def test_the_orientation_fallback(env: Env) -> None:
    dist, eye = 15.0, 1.5
    base = -math.degrees(math.atan2(eye, dist))
    crown = math.degrees(math.atan2(12.0 - eye, dist))
    res = env.post("/api/ar/height", {
        "tilt1": {"beta_deg": 90 + base, "gamma_deg": 0}, "tilt2": {"beta_deg": 90 + crown, "gamma_deg": 0},
        "manual_distance_m": dist,
    })
    out = res.json()
    assert res.status_code == 200 and out["mode"] == "manual_orientation" and "orientation_only_coarse" in out["warnings"]
    assert out["height_m"] == pytest.approx(12.0, abs=0.01)


def test_trunk_plan_position_follows_the_ar_lock(env: Env) -> None:
    lock_both(env)  # A at plan (1000, 500) cm = xz (0, 0); east is +x, north is -z
    payload = body()
    payload["ground"] = {"x": 10.0, "y": -0.2, "z": -5.0}
    viewer = (10.0, 1.3, -5.0 + 10.0)
    payload["tap1"] = tap_body(viewer, (10.0, -0.2, -5.0), T0 + 11)
    payload["tap2"] = tap_body(viewer, (10.0, 11.8, -5.0), T0 + 13)
    out = env.post("/api/ar/height", payload).json()
    assert out["trunk_plan"]["x_cm"] == pytest.approx(2000.0, abs=1.0)
    assert out["trunk_plan"]["y_cm"] == pytest.approx(1000.0, abs=1.0)
    assert -90 < out["trunk_plan"]["lat"] < 90 and -180 < out["trunk_plan"]["lon"] < 180


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["tap1"].update(position=[float("nan"), 0, 0]),
        lambda b: b["tap1"].update(position=[1e9, 0, 0]),
        lambda b: b["tap1"].update(position=[0, 0]),
        lambda b: b["tap2"].update(quaternion=[0, 0, 0, 9]),
        lambda b: b["tap2"].update(tracking="great"),
        lambda b: b["tap2"].update(t=-1),
        lambda b: b["tap2"].update(t=5e9),
        lambda b: b["tap2"].update(extra=1),
        lambda b: b["ground"].update(y=float("inf")),
        lambda b: b.update(manual_distance_m=-3),
        lambda b: b.update(manual_distance_m=5000),
        lambda b: b.update(manual_distance_m="10"),
        lambda b: b.update(client_time=1),
    ],
)
def test_invalid_bodies_are_422(env: Env, mutate: Any) -> None:
    payload = body()
    mutate(payload)
    # NaN/inf cannot travel as strict JSON: send them the way a hostile client would
    raw = json.dumps(payload, allow_nan=True)
    res = env.client.post("/api/ar/height", content=raw, headers={"Authorization": f"Bearer {'t' * 43}", "Content-Type": "application/json"})
    assert res.status_code == 422, res.text


def test_the_maths_refusals_are_422_with_a_code(env: Env) -> None:
    viewer, base, _ = scene(10.0, 0.0)
    straight_up = {"position": list(viewer), "quaternion": [0.7071067811865476, 0, 0, 0.7071067811865476], "tracking": "tracking", "t": T0 + 2}
    res = env.post("/api/ar/height", {"tap1": tap_body(viewer, base, T0), "tap2": straight_up, "ground": {"x": base[0], "y": base[1], "z": base[2]}})
    assert res.status_code == 422 and res.json()["error"] == "pitch_unusable"
    only = body()
    del only["ground"]
    res = env.post("/api/ar/height", only)
    assert res.status_code == 422 and res.json()["error"] == "distance_required"
    res = env.post("/api/ar/height", {"tap1": only["tap1"], "ground": body()["ground"]})
    assert res.status_code == 422 and res.json()["error"] == "invalid_inputs"


def test_the_height_can_be_saved_as_a_measurement_record_and_read_back(tmp_path: Path) -> None:
    env = Env(tmp_path, records=True)
    answer = env.post("/api/ar/height", body(manual_distance_m=10.0)).json()
    rec = {
        "kind": "measurement", "idempotency_key": "tree-height-0001", "plant_name": "Oak by the gate",
        "notes": "two-tap", "height_m": answer["height_m"],
        "height_data": {"mode": answer["mode"], "warnings": answer["warnings"], "uncertainty_m": answer["uncertainty_m"],
                        "inputs": {"tap1": body()["tap1"]["position"], "q1": body()["tap1"]["quaternion"], "manual_distance_m": 10.0}},
        "linked_object_id": "obj_12",
        "geometry": {"type": "point", "lat": 48.0, "lon": 11.0, "x_cm": 2000.0, "y_cm": 1000.0},
        "fix_mode": "vio_fused", "precision_hint": "plan_locked",
    }
    headers = {"Authorization": f"Bearer {'t' * 43}", "X-Plan-Id": plan_id(env.plan or "")}
    res = env.client.post("/api/records", json=rec, headers=headers)
    assert res.status_code == 201, res.text
    stored = fr.load_records(env.plan).records[0]
    assert stored.kind == "measurement" and stored.height_m == answer["height_m"] and stored.linked_object_id == "obj_12"
    assert stored.height_data and stored.height_data["inputs"]["manual_distance_m"] == 10.0  # type: ignore[index]
    listed = env.client.get("/api/field-records", headers=headers).json()["records"][0]  # the existing read path
    assert listed["height_m"] == answer["height_m"] and listed["height_data"]["mode"] == "both"


def test_the_saving_still_obeys_the_records_toggle_and_the_schema_limits(tmp_path: Path) -> None:
    off = Env(tmp_path, records=False)
    rec = {"kind": "measurement", "idempotency_key": "tree-height-0002", "height_m": 9.0}
    assert off.post("/api/records", rec).status_code == 403
    on = Env(tmp_path / "on", records=True)
    (tmp_path / "on").mkdir()
    headers = {"Authorization": f"Bearer {'t' * 43}", "X-Plan-Id": plan_id(on.plan or "")}
    for bad in (
        {"kind": "note", "height_m": 9.0},  # only a measurement carries a height
        {"kind": "measurement", "height_m": -1.0},
        {"kind": "measurement", "height_m": 5000.0},
        {"kind": "measurement", "height_data": {"a": {"b": {"c": {"d": 1}}}}},  # too deep
        {"kind": "measurement", "height_data": {"k": "x" * 500}},
        {"kind": "measurement", "linked_object_id": "bad id!"},
    ):
        res = on.client.post("/api/records", json={"idempotency_key": "tree-height-0003", **bad}, headers=headers)
        assert res.status_code == 422, (bad, res.text)
