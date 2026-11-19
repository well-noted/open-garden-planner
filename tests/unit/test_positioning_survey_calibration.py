"""Survey-In (corrected N_eff) and anchor calibration recovering a known transform."""

from __future__ import annotations

import math

import pytest

from open_garden_planner.core.georef import GeoReference
from open_garden_planner.core.positioning.calibration import (
    CalibrationError,
    calibrate_one_point,
    calibrate_points,
)
from open_garden_planner.core.positioning.survey import SurveyError, SurveyResult, n_eff, survey_in
from open_garden_planner.core.positioning.types import GnssSample

G = GeoReference(origin_lat=33.43, origin_lon=-111.74)


def fix(t: float, e: float, n: float, acc: float) -> GnssSample:
    lat, lon = G.enu_to_gps(e, n)
    return GnssSample(t=t, lat=lat, lon=lon, accuracy_m=acc)


def sv(e: float, n: float) -> SurveyResult:
    lat, lon = G.enu_to_gps(e, n)
    return SurveyResult(e, n, lat, lon, 2.0, 5, 0, 4.0)


def test_n_eff_is_corrected_not_n_over_three() -> None:
    assert n_eff(4) == pytest.approx(1.2)
    assert n_eff(6) == pytest.approx(1.35)
    assert n_eff(8) == pytest.approx(1.5)
    assert n_eff(100) == pytest.approx(1.5)
    assert n_eff(2) == pytest.approx(1.2)


def test_survey_sigma_uses_n_eff_and_floor() -> None:
    samples = [fix(float(i), 5.0, 5.0, 4.0) for i in range(5)]  # N=5 -> N_eff 1.275
    r = survey_in(samples, G)
    assert r.sigma_m == pytest.approx(4.0 / math.sqrt(n_eff(5)))
    assert r.sigma_m > 3.4  # nowhere near the old 4/sqrt(5/3)=3.1 or N/3 optimism
    good = survey_in([fix(float(i), 0.0, 0.0, 0.3) for i in range(5)], G)
    assert good.sigma_m == 0.5  # floor


def test_survey_discards_bad_samples() -> None:
    samples = [fix(0, 0.0, 0.0, 3.0), fix(1, 0.1, 0.0, 3.0), fix(2, 0.0, 0.1, 3.0),
               fix(3, 0.0, 0.0, 3.0), fix(4, 1.0, 1.0, 12.0)]  # 12 m > 2 x median(3)
    r = survey_in(samples, G)
    assert (r.n_used, r.n_discarded) == (4, 1)
    assert abs(r.e_m) < 0.1 and abs(r.n_m) < 0.1  # the outlier did not pull the mean


def test_survey_weights_favour_accurate_fixes() -> None:
    samples = [fix(0, 0.0, 0.0, 1.0), fix(1, 0.0, 0.0, 1.0), fix(2, 0.0, 0.0, 1.0),
               fix(3, 0.0, 0.0, 1.0), fix(4, 4.0, 0.0, 5.0)]
    r = survey_in(samples, G)
    assert r.e_m < 0.2


def test_survey_refuses_short_few_or_moving() -> None:
    with pytest.raises(SurveyError, match="at least"):
        survey_in([fix(i, 0, 0, 3) for i in range(3)], G)
    with pytest.raises(SurveyError, match="3 seconds"):
        survey_in([fix(i * 0.5, 0, 0, 3) for i in range(5)], G)
    walking = [fix(float(i), 3.0 * i, 0.0, 3.0) for i in range(5)]
    with pytest.raises(SurveyError, match="moved"):
        survey_in(walking, G)


def test_survey_hard_stop_at_five_seconds() -> None:
    samples = [fix(float(i), 0.0, 0.0, 3.0) for i in range(5)] + [fix(60.0, 500.0, 0.0, 3.0)]
    r = survey_in(samples, G)  # the late, far sample is beyond the hard stop
    assert abs(r.e_m) < 0.1


def test_survey_ignores_nan_and_zero_accuracy() -> None:
    samples = [fix(0, 0, 0, 3), GnssSample(0.5, float("nan"), 0.0, 3.0), fix(1, 0, 0, 3),
               fix(1.5, 0, 0, 0.0), fix(2, 0, 0, 3), fix(3, 0, 0, 3)]
    r = survey_in(samples, G)
    assert r.n_used == 4


def test_one_point_anchor_translation() -> None:
    plan = (1000.0, 500.0)  # cm
    pe, pn = G.gps_to_enu(*G.canvas_to_gps(*plan))
    a = calibrate_one_point(plan, sv(pe + 3.0, pn - 2.0), G, t=100.0)  # device reads 3 m E, 2 m S off
    assert a.apply(pe + 3.0, pn - 2.0) == pytest.approx((pe, pn), abs=1e-9)
    assert a.kind == "gnss_point" and a.t == 100.0


def _device_to_plan_truth(theta_deg: float, scale: float, t: tuple[float, float]):
    th = math.radians(theta_deg)
    c, s = math.cos(th), math.sin(th)

    def fwd(e: float, n: float) -> tuple[float, float]:  # device -> plan
        return scale * (c * e - s * n) + t[0], scale * (s * e + c * n) + t[1]

    def inv(e: float, n: float) -> tuple[float, float]:  # plan -> device
        x, y = (e - t[0]) / scale, (n - t[1]) / scale
        return c * x + s * y, -s * x + c * y

    return fwd, inv


def test_multi_point_calibration_recovers_known_transform() -> None:
    fwd, inv = _device_to_plan_truth(2.0, 1.01, (3.0, -2.0))
    plan_enu = [(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (0.0, 20.0)]
    pairs = []
    for pe, pn in plan_enu:
        lat, lon = G.enu_to_gps(pe, pn)
        pairs.append((G.gps_to_canvas(lat, lon), sv(*inv(pe, pn))))
    for k in (2, 3, 4):
        anchor, fit = calibrate_points(pairs[:k], G, t=0.0)
        assert fit.rms_m < 0.02
        for dev in [(5.0, 5.0), (25.0, 12.0)]:
            expect = fwd(*dev)
            assert anchor.apply(*dev) == pytest.approx(expect, abs=0.02)


def test_calibration_refuses_mistapped_point() -> None:
    plan_enu = [(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (0.0, 20.0)]
    pairs = []
    for i, (pe, pn) in enumerate(plan_enu):
        lat, lon = G.enu_to_gps(pe, pn)
        off = 25.0 if i == 3 else 0.0  # fourth point tapped 25 m away
        pairs.append((G.gps_to_canvas(lat, lon), sv(pe + off, pn)))
    with pytest.raises(CalibrationError):
        calibrate_points(pairs, G, t=0.0)


def test_calibration_point_count_and_collinear_refused() -> None:
    with pytest.raises(CalibrationError):
        calibrate_points([((0.0, 0.0), sv(0, 0))], G, t=0.0)
    line = []
    for e in (0.0, 10.0, 20.0):
        lat, lon = G.enu_to_gps(e, 0.0)
        line.append((G.gps_to_canvas(lat, lon), sv(e, 0.0)))
    with pytest.raises(CalibrationError):
        calibrate_points(line, G, t=0.0)
