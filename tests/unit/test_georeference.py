"""Qt-free plan georeference: projection, calibration, validation, persistence shape."""

from __future__ import annotations

import math
from typing import Any

import pytest

from open_garden_planner.core.georeference import (
    ControlPoint,
    GeoreferenceError,
    PlanGeoreference,
    fit_affine_calibration,
    georeference_bounds,
    georeference_from_plan,
    georeference_from_satellite,
    gps_to_scene,
    meters_per_degree,
    scene_to_gps,
)

BERLIN = PlanGeoreference(origin_lat=52.5200, origin_lon=13.4050, origin_x_cm=1000.0, origin_y_cm=500.0)


def test_origin_maps_to_its_scene_anchor() -> None:
    assert gps_to_scene(52.5200, 13.4050, BERLIN) == pytest.approx((1000.0, 500.0))
    lat, lon = scene_to_gps(1000.0, 500.0, BERLIN)
    assert (lat, lon) == pytest.approx((52.5200, 13.4050))


def test_plus_y_is_north_and_plus_x_is_east_on_a_north_up_plan() -> None:
    m_lat, m_lon = meters_per_degree(52.52)
    x, y = gps_to_scene(52.5200 + 10.0 / m_lat, 13.4050, BERLIN)  # 10 m north
    assert (x, y) == pytest.approx((1000.0, 500.0 + 1000.0), abs=1e-6)
    x, y = gps_to_scene(52.5200, 13.4050 + 10.0 / m_lon, BERLIN)  # 10 m east
    assert (x, y) == pytest.approx((1000.0 + 1000.0, 500.0), abs=1e-6)


def test_the_plans_north_angle_turns_the_mapping() -> None:
    m_lat, _ = meters_per_degree(52.52)
    north = 52.5200 + 10.0 / m_lat
    # North turned 90 degrees clockwise: true north points to plan-right (+x).
    x, y = gps_to_scene(north, 13.4050, BERLIN, north_angle_deg=90.0)
    assert (x, y) == pytest.approx((1000.0 + 1000.0, 500.0), abs=1e-6)
    # ... and 180: north points plan-down.
    x, y = gps_to_scene(north, 13.4050, BERLIN, north_angle_deg=180.0)
    assert (x, y) == pytest.approx((1000.0, 500.0 - 1000.0), abs=1e-6)


@pytest.mark.parametrize("north", [0.0, 37.5, 135.0, 290.0])
@pytest.mark.parametrize("rotation", [0.0, -12.0, 90.0])
def test_round_trip_is_exact_to_a_tenth_of_a_millimetre(north: float, rotation: float) -> None:
    georef = PlanGeoreference(
        52.52, 13.405, 120.0, -80.0, cm_per_meter_x=100.0, cm_per_meter_y=100.0, rotation_deg=rotation
    )
    for x, y in ((0.0, 0.0), (5000.0, 3000.0), (-2500.0, 4100.0)):
        lat, lon = scene_to_gps(x, y, georef, north)
        bx, by = gps_to_scene(lat, lon, georef, north)
        assert math.hypot(bx - x, by - y) < 0.01  # cm: 0.1 mm


def test_meters_per_degree_matches_known_wgs84_values() -> None:
    m_lat, m_lon = meters_per_degree(0.0)
    assert m_lat == pytest.approx(110574.3, abs=1.0)
    assert m_lon == pytest.approx(111319.5, abs=1.0)
    m_lat, m_lon = meters_per_degree(60.0)
    assert m_lon == pytest.approx(55800.0, abs=100.0)


def _truth(x: float, y: float) -> tuple[float, float]:
    """A survey-grade stand-in: the real scene position of a point, with a skewed plan."""
    return 1.02 * x + 0.03 * y + 40.0, -0.02 * x + 0.98 * y - 25.0


def _points(base: PlanGeoreference, spots: list[tuple[float, float]]) -> list[ControlPoint]:
    out = []
    for x, y in spots:
        lat, lon = scene_to_gps(x, y, base)
        tx, ty = _truth(x, y)
        out.append(ControlPoint(lat, lon, tx, ty))
    return out


def test_one_point_locks_the_translation() -> None:
    base = PlanGeoreference(52.52, 13.405)
    lat, lon = scene_to_gps(300.0, 200.0, base)
    result = fit_affine_calibration(base, [ControlPoint(lat, lon, 330.0, 190.0)])
    assert result.method == "translation"
    assert result.residuals_cm == pytest.approx((0.0,), abs=1e-6)
    assert gps_to_scene(lat, lon, result.georeference) == pytest.approx((330.0, 190.0))
    # A different point moves by the same offset.
    lat2, lon2 = scene_to_gps(900.0, 900.0, base)
    assert gps_to_scene(lat2, lon2, result.georeference) == pytest.approx((930.0, 890.0), abs=1e-6)


def test_two_points_fit_a_similarity_with_zero_residual() -> None:
    base = PlanGeoreference(52.52, 13.405)
    pts = []
    for (x, y) in ((0.0, 0.0), (4000.0, 1000.0)):
        lat, lon = scene_to_gps(x, y, base)
        # truth: rotate 3 degrees and scale 1.01, then shift
        a, b = 1.01 * math.cos(math.radians(3)), 1.01 * math.sin(math.radians(3))
        pts.append(ControlPoint(lat, lon, a * x - b * y + 25.0, b * x + a * y - 10.0))
    result = fit_affine_calibration(base, pts)
    assert result.method == "similarity"
    assert max(result.residuals_cm) < 1e-6
    assert result.rms_error_cm < 1e-6


def test_three_points_fit_an_exact_affine_and_four_report_least_squares_residuals() -> None:
    base = PlanGeoreference(52.52, 13.405)
    pts = _points(base, [(0.0, 0.0), (5000.0, 0.0), (0.0, 3000.0)])
    exact = fit_affine_calibration(base, pts)
    assert exact.method == "affine"
    assert max(exact.residuals_cm) < 1e-4
    # An interior point predicted by the fit lands on the truth.
    lat, lon = scene_to_gps(2500.0, 1500.0, base)
    assert gps_to_scene(lat, lon, exact.georeference) == pytest.approx(_truth(2500.0, 1500.0), abs=1e-3)

    noisy = _points(base, [(0.0, 0.0), (5000.0, 0.0), (0.0, 3000.0), (5000.0, 3000.0)])
    noisy[3] = ControlPoint(noisy[3].lat, noisy[3].lon, noisy[3].x_cm + 20.0, noisy[3].y_cm)
    result = fit_affine_calibration(base, noisy)
    assert len(result.residuals_cm) == 4
    assert 0.0 < result.rms_error_cm < 20.0  # least squares spreads the 20 cm error
    assert result.rms_error_cm == pytest.approx(
        math.sqrt(sum(r * r for r in result.residuals_cm) / 4)
    )


def test_calibration_is_idempotent_and_replaces_a_previous_affine() -> None:
    base = PlanGeoreference(52.52, 13.405)
    pts = _points(base, [(0.0, 0.0), (5000.0, 0.0), (0.0, 3000.0)])
    once = fit_affine_calibration(base, pts)
    twice = fit_affine_calibration(once.georeference, pts)
    assert twice.georeference.affine_matrix == pytest.approx(once.georeference.affine_matrix)


def test_inverse_with_an_affine_still_round_trips() -> None:
    base = PlanGeoreference(52.52, 13.405)
    cal = fit_affine_calibration(base, _points(base, [(0.0, 0.0), (5000.0, 0.0), (0.0, 3000.0)]))
    for x, y in ((100.0, 200.0), (4000.0, 2900.0)):
        lat, lon = scene_to_gps(x, y, cal.georeference, 20.0)
        assert gps_to_scene(lat, lon, cal.georeference, 20.0) == pytest.approx((x, y), abs=0.01)


def test_degenerate_calibrations_are_refused() -> None:
    base = PlanGeoreference(52.52, 13.405)
    with pytest.raises(GeoreferenceError):
        fit_affine_calibration(base, [])
    same = ControlPoint(52.52, 13.405, 0.0, 0.0)
    with pytest.raises(GeoreferenceError, match="same GPS"):
        fit_affine_calibration(base, [same, ControlPoint(52.52, 13.405, 500.0, 0.0)])
    collinear = [ControlPoint(*scene_to_gps(x, 0.0, base), x, 0.0) for x in (0.0, 1000.0, 2000.0)]
    with pytest.raises(GeoreferenceError, match="collinear"):
        fit_affine_calibration(base, collinear)
    with pytest.raises(GeoreferenceError, match="at most"):
        fit_affine_calibration(base, [same] * 65)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"origin_lat": float("nan"), "origin_lon": 0.0},
        {"origin_lat": 0.0, "origin_lon": float("inf")},
        {"origin_lat": True, "origin_lon": 0.0},
        {"origin_lat": 91.0, "origin_lon": 0.0},
        {"origin_lat": 0.0, "origin_lon": 181.0},
        {"origin_lat": 90.0, "origin_lon": 0.0},  # the pole: a degree of longitude is zero
        {"origin_lat": 0.0, "origin_lon": 0.0, "cm_per_meter_x": 0.0},
        {"origin_lat": 0.0, "origin_lon": 0.0, "cm_per_meter_y": -1.0},
        {"origin_lat": 0.0, "origin_lon": 0.0, "origin_x_cm": float("nan")},
        {"origin_lat": 0.0, "origin_lon": 0.0, "rotation_deg": "10"},
        {"origin_lat": 0.0, "origin_lon": 0.0, "affine_matrix": (1.0, 0.0, 0.0, 0.0, 0.0, 0.0)},
        {"origin_lat": 0.0, "origin_lon": 0.0, "affine_matrix": (1.0, 0.0, 0.0)},
    ],
)
def test_invalid_georeferences_are_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(GeoreferenceError):
        PlanGeoreference(**kwargs)


def test_conversions_reject_non_finite_and_boolean_inputs() -> None:
    for bad in (float("nan"), float("inf"), True):
        with pytest.raises(GeoreferenceError):
            scene_to_gps(bad, 0.0, BERLIN)
        with pytest.raises(GeoreferenceError):
            gps_to_scene(52.5, bad, BERLIN)
    with pytest.raises(GeoreferenceError):
        gps_to_scene(52.5, 13.4, BERLIN, north_angle_deg=float("nan"))


def test_dict_round_trip_is_lossless_including_calibration() -> None:
    base = PlanGeoreference(52.52, 13.405, 10.0, 20.0, 100.0, 100.0, 4.0)
    cal = fit_affine_calibration(base, _points(base, [(0.0, 0.0), (5000.0, 0.0), (0.0, 3000.0)]))
    again = PlanGeoreference.from_dict(cal.georeference.to_dict())
    assert again == cal.georeference
    assert PlanGeoreference.from_dict(base.to_dict()) == base
    assert "control_points" not in base.to_dict() and "affine_matrix" not in base.to_dict()


@pytest.mark.parametrize(
    "raw",
    [
        None, [], "x", 3,
        {"origin_lat": 1.0},
        {"origin_lat": 1.0, "origin_lon": 1.0, "control_points": "nope"},
        {"origin_lat": 1.0, "origin_lon": 1.0, "control_points": [{"lat": 1.0}]},
        {"origin_lat": 1.0, "origin_lon": 1.0, "affine_matrix": [1, 2, 3]},
        {"origin_lat": 1.0, "origin_lon": 1.0, "control_points": [{}] * 65},
    ],
)
def test_malformed_stored_values_fail_closed(raw: Any) -> None:
    with pytest.raises(GeoreferenceError):
        PlanGeoreference.from_dict(raw)
    assert georeference_from_plan(raw) is None  # a loaded plan still opens


def test_loader_keeps_a_valid_value() -> None:
    assert georeference_from_plan(BERLIN.to_dict()) == BERLIN


def test_bounds_report_the_corners_and_compass_extent() -> None:
    info = georeference_bounds(BERLIN, 2000.0, 1000.0)
    sw, ne = info["corners"]["south_west"], info["corners"]["north_east"]
    assert ne["lat"] > sw["lat"] and ne["lon"] > sw["lon"]
    assert info["min_lat"] == pytest.approx(sw["lat"])
    assert info["max_lon"] == pytest.approx(ne["lon"])
    assert info["corners"]["center"]["lat"] == pytest.approx(
        scene_to_gps(1000.0, 500.0, BERLIN)[0]
    )


def test_satellite_binding_anchors_the_picture_centre_at_the_canvas_centre() -> None:
    meta = {"center": [48.137, 11.575], "meters_per_pixel": 0.3}
    georef = georeference_from_satellite(meta, 2500.0, 1500.0)
    assert gps_to_scene(48.137, 11.575, georef) == pytest.approx((2500.0, 1500.0))
    # With the plan's north turned, the binding cancels it so the picture stays north-up.
    turned = georeference_from_satellite(meta, 2500.0, 1500.0, north_angle_deg=40.0)
    m_lat, _ = meters_per_degree(48.137)
    x, y = gps_to_scene(48.137 + 10.0 / m_lat, 11.575, turned, north_angle_deg=40.0)
    assert (x, y) == pytest.approx((2500.0, 1500.0 + 1000.0), abs=1e-6)
    for bad in ({}, {"center": [1.0]}, {"center": ["a", "b"]}, {"center": [float("nan"), 1.0]}):
        with pytest.raises(GeoreferenceError):
            georeference_from_satellite(bad, 0.0, 0.0)


# --- review fixes: fit-time north lock, finite outputs (ADR-079) -----------------------


def _calibrated_at(north: float) -> PlanGeoreference:
    base = PlanGeoreference(52.52, 13.405, 100.0, 50.0)
    pts = _points(base, [(0.0, 0.0), (5000.0, 0.0), (0.0, 3000.0)])
    return fit_affine_calibration(base, pts, north).georeference


def test_a_calibration_stores_and_locks_the_fit_time_north() -> None:
    cal = _calibrated_at(30.0)
    assert cal.fit_north_angle_deg == 30.0
    lat, lon = scene_to_gps(700.0, 400.0, cal, 30.0)
    # The plan's north later turns: the calibrated conversion must not move.
    assert gps_to_scene(lat, lon, cal, 75.0) == pytest.approx((700.0, 400.0), abs=1e-6)
    assert scene_to_gps(700.0, 400.0, cal, 75.0) == pytest.approx((lat, lon), abs=1e-12)


def test_an_uncalibrated_georeference_still_follows_the_plans_north() -> None:
    base = PlanGeoreference(52.52, 13.405)
    assert base.fit_north_angle_deg is None
    assert gps_to_scene(52.5201, 13.4052, base, 0.0) != pytest.approx(
        gps_to_scene(52.5201, 13.4052, base, 90.0)
    )


def test_a_legacy_affine_without_a_fit_north_uses_the_passed_north() -> None:
    cal = _calibrated_at(10.0)
    legacy = PlanGeoreference.from_dict(
        {k: v for k, v in cal.to_dict().items() if k != "fit_north_angle_deg"}
    )
    assert legacy.fit_north_angle_deg is None
    assert gps_to_scene(52.5201, 13.4052, legacy, 0.0) != pytest.approx(
        gps_to_scene(52.5201, 13.4052, legacy, 40.0)
    )


def test_the_fit_north_round_trips_and_is_validated() -> None:
    cal = _calibrated_at(30.0)
    assert cal.to_dict()["fit_north_angle_deg"] == 30.0
    assert PlanGeoreference.from_dict(cal.to_dict()).fit_north_angle_deg == 30.0
    assert "fit_north_angle_deg" not in BERLIN.to_dict()
    for bad in (float("nan"), float("inf"), True, "30"):
        with pytest.raises(GeoreferenceError):
            PlanGeoreference.from_dict({**BERLIN.to_dict(), "fit_north_angle_deg": bad})
        with pytest.raises(GeoreferenceError):
            PlanGeoreference(52.52, 13.405, fit_north_angle_deg=bad)  # type: ignore[arg-type]


def test_huge_finite_inputs_that_overflow_are_refused() -> None:
    tiny_scale = PlanGeoreference(52.52, 13.405, cm_per_meter_x=1e-300, cm_per_meter_y=1e-300)
    with pytest.raises(GeoreferenceError, match="not finite"):
        scene_to_gps(1e308, 1e308, tiny_scale)
    huge_scale = PlanGeoreference(52.52, 13.405, cm_per_meter_x=1e308, cm_per_meter_y=1e308)
    with pytest.raises(GeoreferenceError, match="not finite"):
        gps_to_scene(52.6, 13.5, huge_scale)
    big_affine = PlanGeoreference(
        52.52, 13.405, cm_per_meter_x=1e308, cm_per_meter_y=1e308,
        affine_matrix=(1e308, 0.0, 0.0, 0.0, 1e308, 0.0),
    )
    with pytest.raises(GeoreferenceError, match="not finite"):
        gps_to_scene(52.6, 13.4, big_affine)
    # an ordinary conversion still passes
    assert all(math.isfinite(v) for v in gps_to_scene(52.6, 13.5, BERLIN))


def test_a_legacy_calibration_without_a_stored_north_says_it_follows_the_plans_north() -> None:
    from dataclasses import replace

    from open_garden_planner.agent_api.geo_recipes import georeference_view

    cal = _calibrated_at(30.0)
    locked = georeference_view(cal, 30.0, 1000.0, 1000.0)
    assert locked["north_locked"] is True and "locked" in locked["calibration_note"]

    legacy = replace(cal, fit_north_angle_deg=None)
    view = georeference_view(legacy, 30.0, 1000.0, 1000.0)
    assert view["calibrated"] is True and view["north_locked"] is False
    assert "older build" in view["calibration_note"]

    plain = georeference_view(replace(cal, affine_matrix=None, fit_north_angle_deg=None), 30.0, 1000.0, 1000.0)
    assert plain["calibration_note"] is None
