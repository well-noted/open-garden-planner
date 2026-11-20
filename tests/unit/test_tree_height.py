"""Two-tap tree height: the pure maths with synthetic poses (known tree, known viewer)."""

from __future__ import annotations

import math

import pytest

from open_garden_planner.core.positioning import tree_height as th

V3 = tuple[float, float, float]
Q4 = tuple[float, float, float, float]


def _axis(ax: str, deg: float) -> Q4:
    h = math.radians(deg) / 2.0
    s, c = math.sin(h), math.cos(h)
    return {"x": (s, 0.0, 0.0, c), "y": (0.0, s, 0.0, c), "z": (0.0, 0.0, s, c)}[ax]


def _mul(a: Q4, b: Q4) -> Q4:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def quat(yaw_deg: float, pitch_deg: float, roll_deg: float = 0.0) -> Q4:
    """WebXR orientation: yaw about +y (left positive), then pitch about +x, then roll about +z."""
    return _mul(_mul(_axis("y", yaw_deg), _axis("x", pitch_deg)), _axis("z", roll_deg))


def aim(viewer: V3, target: V3) -> Q4:
    """The orientation that looks from ``viewer`` at ``target`` (no roll)."""
    dx, dy, dz = (target[i] - viewer[i] for i in range(3))
    yaw = math.degrees(math.atan2(-dx, -dz))  # forward is -z at yaw 0; +yaw turns left (towards -x)
    pitch = math.degrees(math.atan2(dy, math.hypot(dx, dz)))
    return quat(yaw, pitch)


def tap(viewer: V3, target: V3, t: float, tracking: str = "tracking") -> th.TapSample:
    return th.TapSample(viewer, aim(viewer, target), tracking, t)


TREE = 12.0  # known height, metres


def scene(dist: float, yaw: float, eye: float = 1.5, floor: float = -0.2) -> tuple[V3, V3, V3]:
    """A trunk ``dist`` metres from the viewer along heading ``yaw`` (deg): viewer, base, top."""
    a = math.radians(yaw)
    ex, ez = -math.sin(a) * dist, -math.cos(a) * dist
    return (0.0, floor + eye, 0.0), (ex, floor, ez), (ex, floor + TREE, ez)


@pytest.mark.parametrize("dist", [5.0, 10.0, 25.0])
@pytest.mark.parametrize("yaw", [0.0, 90.0, 217.0, -45.0])
def test_hit_test_mode_recovers_the_known_height(dist: float, yaw: float) -> None:
    viewer, base, top = scene(dist, yaw)
    out = th.compute_tree_height(tap(viewer, base, 10.0), tap(viewer, top, 12.0), ground=base)
    assert out["mode"] == "hit_test"
    assert out["height_m"] == pytest.approx(TREE, abs=0.01)
    assert out["d_m"] == pytest.approx(dist, abs=0.01)
    assert out["eye_height_m"] == pytest.approx(1.5, abs=1e-3)
    if dist < 20:  # a base tap steeper than 5 degrees down; further away it is gated (next test)
        assert out["d_alt_m"] == pytest.approx(dist, abs=0.01) and out["warnings"] == []
    else:
        assert out["warnings"] == ["base_pitch_not_down"]


def test_taps_from_different_spots_use_the_second_pose() -> None:
    viewer, base, top = scene(10.0, 30.0)
    moved = (viewer[0] + 1.0, viewer[1] + 0.1, viewer[2] + 1.5)  # a step, and a little higher
    out = th.compute_tree_height(tap(viewer, base, 1.0), tap(moved, top, 4.0), ground=base)
    assert out["height_m"] == pytest.approx(TREE, abs=0.01)
    assert out["eye_height_m"] == pytest.approx(1.6, abs=1e-3)


@pytest.mark.parametrize("dist", [5.0, 15.0, 30.0])
def test_manual_distance_does_not_need_the_eye_height(dist: float) -> None:
    viewer, base, top = scene(dist, 140.0, eye=1.2)
    out = th.compute_tree_height(tap(viewer, base, 1.0), tap(viewer, top, 3.0), manual_distance_m=dist)
    assert out["mode"] == "manual" and out["eye_height_m"] is None
    assert out["height_m"] == pytest.approx(TREE, abs=0.01)


def test_both_inputs_report_both_and_the_typed_distance_wins() -> None:
    viewer, base, top = scene(10.0, 0.0)
    out = th.compute_tree_height(tap(viewer, base, 1.0), tap(viewer, top, 3.0), ground=base, manual_distance_m=12.0)
    assert out["mode"] == "both"
    assert out["d_m"] == 12.0 and out["height_m"] > TREE  # the (wrong) typed distance is used
    assert out["hit_test"]["height_m"] == pytest.approx(TREE, abs=0.01)  # type: ignore[index]
    assert out["distance_disagreement_rel"] == pytest.approx(2.0 / 12.0, abs=1e-3)
    assert "distance_disagreement" in out["warnings"]  # type: ignore[operator]
    assert out["eye_height_m"] == pytest.approx(1.5, abs=1e-3)


def test_quaternion_conventions_including_a_90_degree_yaw() -> None:
    # yaw +90 about +y turns the forward axis -z into -x; a pure yaw has no pitch and no roll
    q = quat(90.0, 0.0)
    assert th.forward_ray(q) == pytest.approx((-1.0, 0.0, 0.0), abs=1e-9)
    assert th.pitch_deg(q) == pytest.approx(0.0, abs=1e-9) and th.roll_deg(q) == pytest.approx(0.0, abs=1e-9)
    assert th.pitch_deg(quat(0.0, 30.0)) == pytest.approx(30.0, abs=1e-9)  # positive pitch looks up
    assert th.pitch_deg(quat(90.0, -20.0)) == pytest.approx(-20.0, abs=1e-9)
    assert th.pitch_deg((0.0, 0.0, 0.0, 1.0)) == pytest.approx(0.0, abs=1e-12)  # identity looks at -z


def test_a_rolled_phone_warns_but_the_pitch_is_unchanged() -> None:
    viewer, base, top = scene(15.0, 10.0)
    yaw = math.degrees(math.atan2(-base[0], -base[2]))
    pitch = math.degrees(math.atan2(-1.5, 15.0))
    rolled = th.TapSample(viewer, quat(yaw, pitch, 30.0), "tracking", 1.0)
    out = th.compute_tree_height(rolled, tap(viewer, top, 3.0), ground=base)
    assert "roll_high" in out["warnings"] and out["roll_deg"] == pytest.approx(30.0, abs=1e-6)  # type: ignore[operator]
    assert out["height_m"] == pytest.approx(TREE, abs=0.05)


def test_a_steep_crown_warns_and_straight_up_is_refused() -> None:
    viewer, base, top = scene(3.0, 0.0)  # 12 m tree from 3 m: crown pitch about 74 degrees
    out = th.compute_tree_height(tap(viewer, base, 1.0), tap(viewer, top, 3.0), ground=base)
    assert out["pitch_crown_deg"] > 70 and "crown_pitch_steep" in out["warnings"]  # type: ignore[operator]
    straight_up = th.TapSample(viewer, quat(0.0, 89.5), "tracking", 3.0)
    with pytest.raises(th.HeightError) as err:
        th.compute_tree_height(tap(viewer, base, 1.0), straight_up, ground=base)
    assert err.value.code == "pitch_unusable"


def test_distance_and_base_pitch_warnings() -> None:
    viewer, base, top = scene(1.5, 0.0)
    out = th.compute_tree_height(tap(viewer, base, 1.0), tap(viewer, top, 3.0), ground=base)
    assert "distance_too_short" in out["warnings"]  # type: ignore[operator]
    viewer, base, top = scene(60.0, 0.0)
    out = th.compute_tree_height(tap(viewer, base, 1.0), tap(viewer, top, 3.0), ground=base)
    assert "distance_too_long" in out["warnings"]  # type: ignore[operator]
    viewer, base, top = scene(10.0, 0.0)
    level = th.TapSample(viewer, quat(0.0, -2.0), "tracking", 1.0)  # looks level, not down
    out = th.compute_tree_height(level, tap(viewer, top, 3.0), ground=base)
    assert "base_pitch_not_down" in out["warnings"]  # type: ignore[operator]


def test_close_taps_and_poor_tracking_warn() -> None:
    viewer, base, top = scene(10.0, 0.0)
    out = th.compute_tree_height(tap(viewer, base, 5.0), tap(viewer, top, 5.2, "limited"), ground=base)
    assert {"taps_too_close_in_time", "tracking_not_good"} <= set(out["warnings"])  # type: ignore[arg-type]


def test_a_hit_that_disagrees_with_the_ray_and_an_absurd_ground_warn() -> None:
    viewer, base, top = scene(10.0, 0.0)
    shifted = (base[0], base[1], base[2] * 1.3)  # the ray looked at 10 m; the hit says 13 m
    out = th.compute_tree_height(tap(viewer, base, 1.0), tap(viewer, top, 3.0), ground=shifted)
    assert "ground_geometry_mismatch" in out["warnings"]  # type: ignore[operator]
    sunk = (base[0], base[1] - 4.0, base[2])  # a "ground" 4 m below the phone's plane
    out = th.compute_tree_height(tap(viewer, base, 1.0), tap(viewer, top, 3.0), ground=sunk)
    assert "ground_off_plane" in out["warnings"]  # type: ignore[operator]


def test_uncertainty_is_the_labelled_assumption_and_grows_with_distance() -> None:
    nv, nb, nt = scene(8.0, 0.0)
    near = th.compute_tree_height(tap(nv, nb, 1.0), tap(nv, nt, 3.0), ground=nb)
    fv, fb, ft = scene(30.0, 0.0)
    far = th.compute_tree_height(tap(fv, fb, 1.0), tap(fv, ft, 3.0), ground=fb)
    assert 0 < near["uncertainty_m"] < far["uncertainty_m"]  # type: ignore[operator]
    lo, hi = far["range_m"]  # type: ignore[misc]
    assert lo == pytest.approx(far["height_m"] - far["uncertainty_m"], abs=2e-3)  # type: ignore[operator]
    assert hi == pytest.approx(far["height_m"] + far["uncertainty_m"], abs=2e-3)  # type: ignore[operator]
    assert far["assumed"]["angle_error_deg"] == th.ASSUMED_ANGLE_ERROR_DEG  # type: ignore[index]
    assert "NOT MEASURED" in far["assumed"]["note"]  # type: ignore[index]


def test_the_orientation_fallback_uses_the_phone_tilt_and_says_it_is_coarse() -> None:
    # upright in portrait is beta 90 (camera on the horizon); beta 140 aims 50 degrees up
    assert th.tilt_pitch_deg(th.TiltSample(90.0, 0.0)) == pytest.approx(0.0, abs=1e-9)
    assert th.tilt_pitch_deg(th.TiltSample(140.0, 0.0)) == pytest.approx(50.0, abs=1e-9)
    dist, eye = 15.0, 1.5
    base = -math.degrees(math.atan2(eye, dist))
    crown = math.degrees(math.atan2(TREE - eye, dist))
    out = th.compute_tree_height(
        tilt1=th.TiltSample(90.0 + base, 0.0), tilt2=th.TiltSample(90.0 + crown, 0.0), manual_distance_m=dist
    )
    assert out["mode"] == "manual_orientation" and "orientation_only_coarse" in out["warnings"]  # type: ignore[operator]
    assert out["height_m"] == pytest.approx(TREE, abs=0.01)
    with pytest.raises(th.HeightError):  # no distance
        th.compute_tree_height(tilt1=th.TiltSample(90.0, 0.0), tilt2=th.TiltSample(120.0, 0.0))


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_inputs_are_a_hard_error(bad: float) -> None:
    viewer, base, top = scene(10.0, 0.0)
    good = tap(viewer, top, 3.0)
    with pytest.raises(th.HeightError):
        th.compute_tree_height(th.TapSample((bad, 1.0, 0.0), good.quaternion, "tracking", 1.0), good, ground=base)
    with pytest.raises(th.HeightError):
        th.compute_tree_height(tap(viewer, base, 1.0), good, ground=(0.0, bad, -10.0))
    with pytest.raises(th.HeightError):
        th.compute_tree_height(tap(viewer, base, 1.0), good, manual_distance_m=bad)
    with pytest.raises(th.HeightError):
        th.compute_tree_height(th.TapSample(viewer, (0.0, bad, 0.0, 1.0), "tracking", 1.0), good, ground=base)


def test_a_crown_below_the_base_and_missing_inputs_are_refused() -> None:
    viewer, base, _ = scene(10.0, 0.0)
    low = tap(viewer, (base[0], base[1] - 0.5, base[2]), 3.0)
    with pytest.raises(th.HeightError) as err:
        th.compute_tree_height(tap(viewer, base, 1.0), low, ground=base)
    assert err.value.code == "crown_not_above_base"
    with pytest.raises(th.HeightError) as err:
        th.compute_tree_height(tap(viewer, base, 1.0), low)
    assert err.value.code == "distance_required"
    with pytest.raises(th.HeightError):  # not a unit quaternion
        th.compute_tree_height(th.TapSample(viewer, (0.0, 0.0, 0.0, 3.0), "tracking", 1.0), low, ground=base)
