"""Survey-In, calibration and track recording over the real routes (WP-F)."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest

from open_garden_planner.field_remote import server, tracking_routes
from open_garden_planner.services import field_records as fr
from tests.unit.test_field_remote_projection import make_snapshot
from tests.unit.test_field_remote_read_routes import AUTH, Env

ORIGIN = (48.0, 11.0)
M_LAT = 111194.93  # metres per degree of latitude on the desktop's sphere
M_LON = M_LAT * math.cos(math.radians(ORIGIN[0]))


def gps(east_m: float, north_m: float) -> tuple[float, float]:
    return ORIGIN[0] + north_m / M_LAT, ORIGIN[1] + east_m / M_LON


def fix(t: float, east: float, north: float, acc: float = 2.0) -> dict[str, Any]:
    lat, lon = gps(east, north)
    return {"t": 1_700_000_000.0 + t, "lat": lat, "lon": lon, "accuracy_m": acc}


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    return Env(tmp_path, records=True)


def test_session_paths_match_the_guards_exemptions() -> None:
    assert tracking_routes.SESSION_PATHS <= server.WRITES_ALLOWED_WITHOUT_RECORDS
    assert {"/api/position"} == server.WRITES_ALLOWED_WITHOUT_RECORDS - tracking_routes.SESSION_PATHS


def test_survey_in_returns_the_average_and_an_honest_note(env: Env) -> None:
    samples = [fix(i, 10.0 + 0.3 * (i % 3), 5.0 - 0.2 * (i % 2)) for i in range(8)]
    r = env.post("/api/survey", {"samples": samples})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["n_used"] + body["n_discarded"] == 8 and body["survey_id"].startswith("s")
    assert "not a validated accuracy" in body["note"]
    assert abs(body["x_cm"] - 1010) < 60 and abs(body["y_cm"] - 490) < 60  # 1 cm per unit, no rotation
    assert env.post("/api/survey", {"samples": samples[:2]}).status_code == 422


def test_one_point_calibration_moves_the_puck(env: Env) -> None:
    survey = env.post("/api/survey", {"samples": [fix(i, 10.0, 5.0) for i in range(6)]}).json()
    before = env.post("/api/position", fix(100, 10.0, 5.0)).json()["telemetry"]
    point = {"survey_id": survey["survey_id"], "plan_x_cm": 2000, "plan_y_cm": 1000}
    cal = env.post("/api/calibrate", {"points": [point]})
    assert cal.status_code == 200 and cal.json()["kind"] == "gnss_point"
    after = env.post("/api/position", fix(101, 10.0, 5.0)).json()["telemetry"]
    assert after["anchor_source"] == "gnss_point" and after["fix_mode"] == "anchor_calibrated"
    assert abs(after["x_cm"] - 2000) < 150 and abs(after["y_cm"] - 1000) < 150
    assert abs(before["x_cm"] - 2000) > 500
    assert env.client.delete("/api/calibrate", headers=AUTH).json() == {"cleared": True}
    ghost = {"survey_id": "s999", "plan_x_cm": 0, "plan_y_cm": 0}
    assert env.post("/api/calibrate", {"points": [ghost]}).status_code == 404


def test_a_recorded_walk_becomes_a_pending_irrigation_record(env: Env) -> None:
    assert env.post("/api/track/start", {}).status_code == 200
    # an L-shaped walk, 20 m east then 10 m north, one fix a second, light jitter
    true_len = 30.0
    pts = [(float(i), 0.0) for i in range(21)] + [(20.0, float(j)) for j in range(1, 11)]
    for i, (e, n) in enumerate(pts):
        jitter = 0.15 * math.sin(i * 1.7)
        assert env.post("/api/position", fix(i, e + jitter, n - jitter)).status_code == 200
    prev = env.get("/api/track/preview").json()
    assert prev["recording"] is True and len(prev["points_cm"]) >= 3
    done = env.post("/api/track/finish", {}).json()
    assert done["saveable"] is True and done["n_vertices"] <= fr.MAX_POLYLINE_POINTS
    assert abs(done["length_m"] - true_len) / true_len < 0.05
    body = {
        "kind": "irrigation_path", "idempotency_key": "walk-0000001",
        "geometry": {"type": "polyline", "points": done["points"]}, "diameter_mm": 16, "closes_on": False,
    }
    plan_id = env.get("/api/field-records").json()["plan_id"]
    saved = env.client.post("/api/records", json=body, headers={**AUTH, "X-Plan-Id": plan_id})
    assert saved.status_code == 201, saved.text
    rec = fr.get_record(env.plan, saved.json()["record"]["id"])
    assert rec is not None and rec.status == "pending" and rec.kind == "irrigation_path"


def test_a_short_recording_is_flagged_not_saveable(env: Env) -> None:
    env.post("/api/track/start", {})
    for i in range(6):
        env.post("/api/position", fix(i, 0.2 * i, 0.0))
    done = env.post("/api/track/finish", {}).json()
    assert done["saveable"] is False and "too_short" in done["problems"]


def test_bowditch_close_reports_the_misclosure(env: Env) -> None:
    env.post("/api/track/start", {})
    walk = [(0, 0)] * 3 + [(i, 0) for i in range(1, 11)] + [(10, j) for j in range(1, 11)]
    walk += [(10 - i, 10) for i in range(1, 11)] + [(0, 10 - j + 0.8) for j in range(1, 11)]
    for i, (e, n) in enumerate(walk):
        env.post("/api/position", fix(i, e, n))
    done = env.post("/api/track/finish", {"close": "start"}).json()
    assert done["closure"] is not None and done["closure"]["closure_m"] > 0
    assert env.post("/api/track/finish", {"close": "point"}).status_code == 422  # target missing


def test_tracking_works_with_records_off_but_saving_does_not(tmp_path: Path) -> None:
    off = Env(tmp_path, records=False)
    assert off.post("/api/track/start", {}).status_code == 200
    assert off.post("/api/survey", {"samples": [fix(i, 1.0, 1.0) for i in range(4)]}).status_code == 200
    body = {"kind": "note", "idempotency_key": "note-00000001", "notes": "x"}
    assert off.post("/api/records", body).status_code == 403


def test_not_georeferenced_plan_is_refused(tmp_path: Path) -> None:
    bare = Env(tmp_path, records=True, snapshot=make_snapshot(georef=None))
    assert bare.post("/api/track/start", {}).status_code == 409
    assert bare.post("/api/survey", {"samples": [fix(i, 1.0, 1.0) for i in range(4)]}).status_code == 409


def test_bad_bodies_are_refused(env: Env) -> None:
    assert env.post("/api/survey", {"samples": [{"t": 1, "lat": 99, "lon": 0, "accuracy_m": 1}]}).status_code == 422
    assert env.post("/api/calibrate", {"points": []}).status_code == 422
    assert env.post("/api/track/finish", {"close": "never"}).status_code == 422
    plain = {**AUTH, "content-type": "text/plain"}
    assert env.client.post("/api/survey", content=b"x", headers=plain).status_code == 415


def test_calibrating_during_a_recording_is_refused(env: Env) -> None:
    survey = env.post("/api/survey", {"samples": [fix(i, 10.0, 5.0) for i in range(6)]}).json()
    point = {"survey_id": survey["survey_id"], "plan_x_cm": 2000, "plan_y_cm": 1000}
    env.post("/api/track/start", {})
    assert env.post("/api/calibrate", {"points": [point]}).status_code == 409
    assert env.client.delete("/api/calibrate", headers=AUTH).status_code == 409
    env.post("/api/track/cancel", {})
    assert env.post("/api/calibrate", {"points": [point]}).status_code == 200


def test_one_absurd_client_clock_does_not_starve_later_fixes(env: Env) -> None:
    assert env.post("/api/position", fix(0, 1.0, 1.0)).json()["telemetry"]["gated"] is False
    poisoned = {**fix(1, 1.0, 1.0), "t": 1e15}
    env.post("/api/position", poisoned)
    after = env.post("/api/position", fix(2, 1.0, 1.0)).json()["telemetry"]
    assert after["gated"] is False and after["x_cm"] is not None
