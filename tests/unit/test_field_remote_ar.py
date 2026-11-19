"""The AR plan lock over the real routes (WP-H): lock A and B, locked telemetry, loss and reset.

Synthetic VIO only: the poses are generated from a known transform, so the expected plan
position is exact. Whether WebXR delivers anything like it on a phone is not tested here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_field_remote_read_routes import AUTH, Env

T0 = 1_700_000_000.0
A_CM = (1000.0, 500.0)
B_CM = (3000.0, 500.0)  # 20 m east of A at 1 cm per unit
ORIGIN_LAT, ORIGIN_LON = 48.0, 11.0


def vio(t: float, x: float, z: float, tracking: str = "tracking", **extra: Any) -> dict[str, Any]:
    return {"t": t, "x": x, "y": 1.4, "z": z, "qx": 0.0, "qy": 0.0, "qz": 0.0, "qw": 1.0, "tracking": tracking, **extra}


def lock_body(plan: tuple[float, float], hit: tuple[float, float], t: float) -> dict[str, Any]:
    return {"plan": {"x_cm": plan[0], "y_cm": plan[1]}, "hit": {"x": hit[0], "y": 0.0, "z": hit[1]}, "vio_t": t}


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    return Env(tmp_path, records=False)  # session-only state: allowed with the records toggle off


def post_vio(env: Env, samples: list[dict[str, Any]]) -> Any:
    return env.post("/api/position", {"vio_batch": samples})


def lock_both(env: Env) -> None:
    assert post_vio(env, [vio(T0, 0.0, 0.0)]).status_code == 200
    assert env.post("/api/ar/lock/a", lock_body(A_CM, (0.0, 0.0), T0)).status_code == 200
    assert post_vio(env, [vio(T0 + 0.5 * k, float(k), 0.0) for k in range(1, 21)]).status_code == 200
    res = env.post("/api/ar/lock/b", lock_body(B_CM, (20.0, 0.0), T0 + 10.0))
    assert res.status_code == 200, res.text
    assert res.json()["state"] == "locked" and res.json()["scale"]["ok"] is True


def test_the_ar_routes_need_the_token(env: Env) -> None:
    for path in ("/api/ar/lock/a", "/api/ar/lock/b", "/api/ar/check", "/api/ar/unlock"):
        assert env.client.post(path, json={}).status_code == 401, path
    assert env.client.get("/api/ar/status").status_code == 401


def test_locked_vio_is_the_published_position_and_reports_vio_fused(env: Env) -> None:
    lock_both(env)
    tele = post_vio(env, [vio(T0 + 10.4, 21.0, -1.0)]).json()["telemetry"]
    assert tele["fix_mode"] == "vio_fused" and tele["track_source"] == "vio"
    assert tele["x_cm"] == pytest.approx(A_CM[0] + 2100.0, abs=1.0) and tele["y_cm"] == pytest.approx(A_CM[1] + 100.0, abs=1.0)
    assert tele["sigma_m"] is None  # the plan-lock sigma is a placeholder, never shown as a spread
    assert tele["fused"]["state"] == "locked" and tele["fused"]["source"] == "plan_locked"
    assert tele["fused"]["relative_sigma_m_per_10m"] is None
    assert env.source.positions[-1]["fix_mode"] == "vio_fused"  # the desktop puck got it


def test_a_gnss_batch_while_locked_still_answers_with_the_locked_track(env: Env) -> None:
    lock_both(env)
    far = {"t": T0 + 10.4, "lat": ORIGIN_LAT + 0.01, "lon": ORIGIN_LON, "accuracy_m": 3.0}  # about 1.1 km away
    body = {"positions": [far], "vio_batch": [vio(T0 + 10.4, 21.0, -1.0)]}
    tele = env.post("/api/position", body).json()["telemetry"]
    assert tele["track_source"] == "vio" and tele["x_cm"] == pytest.approx(A_CM[0] + 2100.0, abs=1.0)
    assert tele["fused"]["gnss_alert"] is not None or tele["fused"]["source"] == "plan_locked"


def test_vio_on_the_single_position_form(env: Env) -> None:
    pos = {"t": T0, "lat": ORIGIN_LAT, "lon": ORIGIN_LON, "accuracy_m": 5, "vio": vio(T0, 0.0, 0.0),
           "imu": {"t": T0, "ax": 0.0, "ay": 0.0, "az": 9.8}}
    res = env.post("/api/position", pos)
    assert res.status_code == 200 and res.json()["telemetry"]["track_source"] == "gnss"


def test_lock_b_without_a_is_refused_and_leaves_state_unlocked(env: Env) -> None:
    post_vio(env, [vio(T0, 0.0, 0.0)])
    res = env.post("/api/ar/lock/b", lock_body(B_CM, (20.0, 0.0), T0))
    assert res.status_code == 409 and res.json()["error"] == "ar_lock_refused"
    assert env.get("/api/ar/status").json()["state"] == "unlocked"


def test_lock_a_needs_good_tracking(env: Env) -> None:
    res = env.post("/api/ar/lock/a", lock_body(A_CM, (0.0, 0.0), T0))  # no VIO sample yet
    assert res.status_code == 409
    post_vio(env, [vio(T0, 0.0, 0.0, "limited")])
    assert env.post("/api/ar/lock/a", lock_body(A_CM, (0.0, 0.0), T0)).status_code == 409


def test_a_failed_scale_check_locks_but_is_not_vio_fused(env: Env) -> None:
    post_vio(env, [vio(T0, 0.0, 0.0)])
    env.post("/api/ar/lock/a", lock_body(A_CM, (0.0, 0.0), T0))
    post_vio(env, [vio(T0 + 0.5 * k, float(k), 0.0) for k in range(1, 21)])
    res = env.post("/api/ar/lock/b", lock_body((A_CM[0] + 2300.0, A_CM[1]), (20.0, 0.0), T0 + 10.0))
    assert res.status_code == 200 and res.json()["scale"]["ok"] is False
    assert "disagree" in res.json()["scale"]["message"]
    tele = post_vio(env, [vio(T0 + 10.4, 21.0, -1.0)]).json()["telemetry"]
    assert tele.get("fix_mode") != "vio_fused" and tele["x_cm"] is None


def test_closure_at_c_and_unlock(env: Env) -> None:
    lock_both(env)
    post_vio(env, [vio(T0 + 10.4, 21.0, -1.0)])
    res = env.post("/api/ar/check", lock_body((A_CM[0] + 1000.0 + 30.0, A_CM[1] + 500.0), (10.0, -5.0), T0 + 10.4))
    assert res.status_code == 200 and res.json()["label"] == "closure at C"
    assert res.json()["closure_m"] == pytest.approx(0.3, abs=0.01)
    assert env.post("/api/ar/unlock", {}).json()["state"] == "unlocked"
    assert env.post("/api/ar/check", lock_body(B_CM, (10.0, -5.0), T0)).status_code == 409


def test_tracking_loss_unlocks_and_is_not_resumed(env: Env) -> None:
    lock_both(env)
    post_vio(env, [vio(T0 + 10.4, 21.0, -1.0)])
    post_vio(env, [vio(T0 + 10.8, 21.0, -1.0, "limited"), vio(T0 + 11.2, 21.0, -1.0, "limited"),
                   vio(T0 + 11.6, 21.0, -1.0, "limited"),
                   vio(T0 + 11.9, 21.0, -1.0, "limited")])  # limited for over 1 s
    tele = post_vio(env, [vio(T0 + 12.3, 22.0, -1.0)]).json()["telemetry"]
    assert tele["fused"]["state"] == "unlocked" and tele["track_source"] == "gnss" and tele["x_cm"] is None
    assert tele["fused"]["unlock_reason"] is not None


def test_a_reference_space_reset_drops_the_lock(env: Env) -> None:
    lock_both(env)
    tele = post_vio(env, [vio(T0 + 10.4, 21.0, -1.0, reset=True)]).json()["telemetry"]
    assert tele["fused"]["state"] == "unlocked" and tele["x_cm"] is None
    assert env.get("/api/ar/status").json()["unlock_reason"] == "reference_space_reset"


@pytest.mark.parametrize(
    "sample",
    [
        vio(T0, float("nan"), 0.0),
        vio(T0, 0.0, 0.0, "found"),
        {**vio(T0, 0.0, 0.0), "extra": 1},
        {"t": T0, "x": 0.0},
    ],
)
def test_bad_vio_is_refused(env: Env, sample: dict[str, Any]) -> None:
    body = json.dumps({"vio_batch": [sample]}, allow_nan=True)
    res = env.client.post("/api/position", content=body, headers={**AUTH, "Content-Type": "application/json"})
    assert res.status_code == 422 and res.json()["error"] == "invalid_position"


def test_batch_limits_and_empty_bodies(env: Env) -> None:
    assert post_vio(env, [vio(T0 + i * 0.1, 0.0, 0.0) for i in range(51)]).status_code == 422
    assert env.post("/api/position", {"positions": []}).status_code == 422
    nan_imu = json.dumps({"imu_batch": [{"t": T0, "ax": float("nan"), "ay": 0.0, "az": 9.8}]}, allow_nan=True)
    res = env.client.post("/api/position", content=nan_imu, headers={**AUTH, "Content-Type": "application/json"})
    assert res.status_code == 422


def test_ar_samples_without_a_georeference_are_refused(tmp_path: Path) -> None:
    from tests.unit.test_field_remote_projection import make_snapshot

    bare = Env(tmp_path, snapshot=make_snapshot(georef=None))
    assert bare.post("/api/position", {"vio_batch": [vio(T0, 0.0, 0.0)]}).status_code == 409
    assert bare.post("/api/ar/lock/a", lock_body(A_CM, (0.0, 0.0), T0)).status_code == 409


def test_lock_bodies_reject_non_finite_and_unknown_fields(env: Env) -> None:
    body = json.dumps(lock_body(A_CM, (float("inf"), 0.0), T0), allow_nan=True)
    res = env.client.post("/api/ar/lock/a", content=body, headers={**AUTH, "Content-Type": "application/json"})
    assert res.status_code == 422
    assert env.post("/api/ar/lock/a", {**lock_body(A_CM, (0.0, 0.0), T0), "plan_point_id": "x"}).status_code == 422


def test_the_page_carries_no_accuracy_claim_and_sends_no_image_data() -> None:
    import re

    from open_garden_planner.field_remote import static

    text = static.ASSETS["/static/survey.js"].body.decode("utf-8")
    ar = text[text.index("// ---- AR lock"):]
    assert not re.search(r"toBlob|toDataURL|getImageData|readPixels|captureStream", ar)
    assert "Uses the camera to track movement; no images leave the phone" in static.ASSETS["/"].body.decode("utf-8")
    for url, asset in static.ASSETS.items():
        body = asset.body.decode("utf-8")
        assert not re.search(r"centimet|sub-?(?:cm|decimeter)|survey[- ]grade|guaranteed", body, re.I), url


# -- review fixes: hostile numbers, server lock time, frame changes, labels -----------------------


def _raw_post(env: Env, path: str, text: str) -> Any:
    return env.client.post(path, content=text, headers={**AUTH, "Content-Type": "application/json"})


@pytest.mark.parametrize("bad", ["Infinity", "-Infinity", "NaN", "1e400", "4200000000", "-1"])
def test_a_lock_time_that_is_not_a_finite_epoch_is_refused_and_position_keeps_working(env: Env, bad: str) -> None:
    assert post_vio(env, [vio(T0, 0.0, 0.0)]).status_code == 200
    text = '{"plan":{"x_cm":1000,"y_cm":500},"hit":{"x":0,"y":0,"z":0},"vio_t":' + bad + "}"
    assert _raw_post(env, "/api/ar/lock/a", text).status_code == 422
    assert env.get("/api/ar/status").status_code == 200
    res = post_vio(env, [vio(T0 + 0.1, 0.0, 0.0)])
    assert res.status_code == 200 and res.json()["telemetry"]["fused"]["lock_t"] is None


def test_the_client_lock_time_field_is_gone(env: Env) -> None:
    post_vio(env, [vio(T0, 0.0, 0.0)])
    body = {**lock_body(A_CM, (0.0, 0.0), T0), "t": T0}
    assert env.post("/api/ar/lock/a", body).status_code == 422


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("/api/position", '{"vio_batch":[{"t":1e400,"x":0,"y":0,"z":0,"qx":0,"qy":0,"qz":0,"qw":1,"tracking":"tracking"}]}'),
        ("/api/position", '{"vio_batch":[{"t":1,"x":1e300,"y":0,"z":0,"qx":0,"qy":0,"qz":0,"qw":1,"tracking":"tracking"}]}'),
        ("/api/position", '{"vio_batch":[{"t":1,"x":0,"y":0,"z":0,"qx":9,"qy":0,"qz":0,"qw":1,"tracking":"tracking"}]}'),
        ("/api/position", '{"imu_batch":[{"t":1,"ax":1e300,"ay":0,"az":9.8}]}'),
        ("/api/position", '{"imu_batch":[{"t":Infinity,"ax":0,"ay":0,"az":9.8}]}'),
        ("/api/position", '{"imu_batch":[{"t":1,"ax":0,"ay":0,"az":9.8,"heading_deg":1e300}]}'),
        ("/api/position", '{"t":1e400,"lat":48,"lon":11,"accuracy_m":3}'),
        ("/api/ar/lock/a", '{"plan":{"x_cm":1e300,"y_cm":0},"hit":{"x":0,"y":0,"z":0},"vio_t":1}'),
        ("/api/ar/lock/a", '{"plan":{"x_cm":0,"y_cm":0},"hit":{"x":1e300,"y":0,"z":0},"vio_t":1}'),
    ],
)
def test_unbounded_ar_numbers_are_refused(env: Env, path: str, text: str) -> None:
    assert _raw_post(env, path, text).status_code == 422


def test_the_lock_time_comes_from_the_pose_stream(env: Env) -> None:
    lock_both(env)
    assert env.get("/api/ar/status").json()["lock_t"] == T0 + 10.0  # the last pose, not a phone clock


def test_a_lock_the_pose_stream_has_not_reached_is_refused(env: Env) -> None:
    post_vio(env, [vio(T0, 0.0, 0.0)])
    res = env.post("/api/ar/lock/a", lock_body(A_CM, (0.0, 0.0), T0 + 0.5))  # poses still queued on the phone
    assert res.status_code == 409 and res.json()["error"] == "ar_lock_refused"
    assert env.get("/api/ar/status").json()["state"] == "unlocked"


def test_a_lock_taken_before_a_queued_reset_is_refused(env: Env) -> None:
    post_vio(env, [vio(T0, 0.0, 0.0)])
    # the hit was taken at T0; the reset that followed it reaches the server first
    post_vio(env, [vio(T0 + 0.3, 0.0, 0.0, "limited", reset=True), vio(T0 + 0.4, 0.0, 0.0)])
    res = env.post("/api/ar/lock/a", lock_body(A_CM, (0.0, 0.0), T0))
    assert res.status_code == 409 and "reset" in res.json()["detail"]


def test_an_rtk_receiver_label_never_reaches_a_vio_sourced_position(env: Env) -> None:
    lock_both(env)
    fix = {"t": T0 + 10.4, "lat": ORIGIN_LAT, "lon": ORIGIN_LON, "accuracy_m": 0.02, "fix_quality": "rtk_fixed"}
    tele = env.post("/api/position", {"positions": [fix], "vio_batch": [vio(T0 + 10.4, 21.0, -1.0)]}).json()["telemetry"]
    assert tele["track_source"] == "vio" and tele["fix_mode"] == "vio_fused" and tele["fix_mode_reported"] is False
    assert tele["quality"] == "unknown"


@pytest.mark.parametrize("bad", ["Infinity", "1e400"])
def test_a_hostile_lock_time_can_never_break_the_position_answers(env: Env, bad: str) -> None:
    """The reviewer's probe: a lock B carrying ``t: Infinity`` used to be stored and 500 every answer."""
    assert post_vio(env, [vio(T0, 0.0, 0.0)]).status_code == 200
    for body in (lock_body(A_CM, (0.0, 0.0), T0), {**lock_body(A_CM, (0.0, 0.0), T0), "t": T0}):
        env.post("/api/ar/lock/a", body)  # today's body, then the old client-time body
        body.pop("vio_t")
        env.post("/api/ar/lock/a", body)
    post_vio(env, [vio(T0 + 0.5 * k, float(k), 0.0) for k in range(1, 21)])
    for field in ("t", "vio_t"):
        text = '{"plan":{"x_cm":3000,"y_cm":500},"hit":{"x":20,"y":0,"z":0},"' + field + '":' + bad + "}"
        assert _raw_post(env, "/api/ar/lock/b", text).status_code == 422
    res = post_vio(env, [vio(T0 + 10.4, 21.0, -1.0)])
    assert res.status_code == 200
    assert env.get("/api/ar/status").status_code == 200


@pytest.mark.parametrize("bad", ["Infinity", "1e400"])
def test_survey_samples_refuse_non_finite_times(env: Env, bad: str) -> None:
    text = '{"samples":[{"t":' + bad + ',"lat":48,"lon":11,"accuracy_m":3}]}'
    assert _raw_post(env, "/api/survey", text).status_code == 422
