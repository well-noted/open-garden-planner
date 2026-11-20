"""Engine behaviour: gating, ZUPT, hostile input, anchors, fix mode, recorded tracks."""

from __future__ import annotations

import math

import numpy as np
import pytest

from open_garden_planner.core.geo_frame import make_frame
from open_garden_planner.core.positioning import (
    FixMode,
    GnssSample,
    ImuSample,
    PositioningEngine,
)
from open_garden_planner.core.positioning.closure import ClosureError
from open_garden_planner.core.positioning.kalman import KalmanCV

G = make_frame(origin_lat=33.43, origin_lon=-111.74)


def fix(t: float, e: float, n: float, acc: float = 3.0, **kw) -> GnssSample:
    lat, lon = G.enu_to_gps(e, n)
    return GnssSample(t=t, lat=lat, lon=lon, accuracy_m=acc, **kw)


def walk(eng: PositioningEngine, seconds: int, acc: float, v: float = 1.5, t0: float = 0.0):
    est = None
    for i in range(seconds):
        est = eng.add_gnss(fix(t0 + i, v * i, 0.0, acc))
    return est


# --------------------------------------------------------------------- gating


def test_spike_near_wall_rejected_by_speed_even_when_r_is_large() -> None:
    """acc 25 m (under the house wall): S is so large that Mahalanobis passes a 45 m jump."""
    eng = PositioningEngine(G)
    walk(eng, 20, acc=25.0)
    before = eng.add_gnss(fix(20.0, 30.0, 0.0, 25.0))
    spike = eng.add_gnss(fix(21.0, 31.5, 45.0, 25.0))
    assert spike is not None and spike.gated
    assert eng.last_reject_reason == "velocity_jump"
    assert abs(spike.n_m) < 5.0  # the estimate did not follow the spike
    ok = eng.add_gnss(fix(22.0, 33.0, 0.0, 25.0))
    assert ok is not None and not ok.gated
    assert before is not None and ok.e_m > before.e_m


def test_spike_rejected_by_mahalanobis_when_accuracy_is_good() -> None:
    eng = PositioningEngine(G)
    walk(eng, 20, acc=2.0)
    spike = eng.add_gnss(fix(20.0, 20.0, 25.0, 2.0))
    assert spike is not None and spike.gated
    assert eng.last_reject_reason == "mahalanobis"


def test_three_consecutive_rejects_reseed_the_filter() -> None:
    eng = PositioningEngine(G)
    walk(eng, 15, acc=2.0)
    far = [fix(15.0 + i, 500.0, 500.0, 2.0) for i in range(3)]  # a real, sustained relocation
    outs = [eng.add_gnss(s) for s in far]
    assert outs[0] is not None and outs[0].gated and outs[1] is not None and outs[1].gated
    assert outs[2] is not None and not outs[2].gated
    assert eng.reseed_count == 1
    assert math.dist((outs[2].e_m, outs[2].n_m), (500.0, 500.0)) < 1.0
    assert outs[2].sigma_m > 4.0  # re-seeded with a large P


def test_isolated_rejects_do_not_reseed() -> None:
    eng = PositioningEngine(G)
    walk(eng, 10, acc=2.0)
    eng.add_gnss(fix(10.0, 10.0, 60.0, 2.0))
    eng.add_gnss(fix(11.0, 11.0, 0.0, 2.0))  # good fix clears the run
    eng.add_gnss(fix(12.0, 12.0, 60.0, 2.0))
    assert eng.reseed_count == 0


# ---------------------------------------------------------------------- ZUPT


def test_zupt_stationary_freezes_speed_and_breadcrumbs() -> None:
    eng = PositioningEngine(G)
    eng.start_track()
    rng = np.random.default_rng(3)
    est = None
    crumbs_when_declared = 0
    for i in range(15):
        est = eng.add_gnss(fix(float(i), 5 + rng.normal(0, 0.8), 5 + rng.normal(0, 0.8), 3.0, speed_mps=0.05))
        if i == 5:
            crumbs_when_declared = len(eng.track_preview())
    assert est is not None and est.stationary and est.speed_mps == 0.0
    assert len(eng.track_preview()) == crumbs_when_declared  # standing jitter adds no breadcrumbs


def test_zupt_exits_with_hysteresis_when_walking() -> None:
    eng = PositioningEngine(G)
    for i in range(8):
        eng.add_gnss(fix(float(i), 0.0, 0.0, 3.0, speed_mps=0.0))
    assert eng._zupt.stationary
    flags = []
    for i in range(8, 20):
        e = eng.add_gnss(fix(float(i), 10.0 * (i - 7), 0.0, 3.0, speed_mps=1.4))  # strides of 10 m
        flags.append(e.stationary if e else None)
    assert flags[0] is True  # first non-stationary evaluation is absorbed by hysteresis
    assert flags[-1] is False


def test_imu_variance_vetoes_stationary() -> None:
    eng = PositioningEngine(G)
    for i in range(8):
        for k in range(5):
            eng.add_imu(ImuSample(t=i + k * 0.2, ax=0.0, ay=0.0, az=9.8 + 3.0 * ((-1) ** k)))
        eng.add_gnss(fix(float(i), 0.0, 0.0, 3.0, speed_mps=0.0))
    assert not eng._zupt.stationary  # shaking phone: not still, whatever the fixes say


# ------------------------------------------------------------- hostile input


@pytest.mark.parametrize(
    "kw,reason",
    [
        ({"t": float("nan")}, "non_finite"),
        ({"lat": float("inf")}, "non_finite"),
        ({"lon": float("nan")}, "non_finite"),
        ({"accuracy_m": float("nan")}, "non_finite"),
        ({"accuracy_m": 0.0}, "accuracy_out_of_range"),
        ({"accuracy_m": -3.0}, "accuracy_out_of_range"),
        ({"accuracy_m": 51.0}, "accuracy_out_of_range"),
        ({"lat": 91.0}, "bad_coordinates"),
        ({"lon": 181.0}, "bad_coordinates"),
    ],
)
def test_invalid_samples_are_gated_and_never_corrupt_state(kw, reason) -> None:
    eng = PositioningEngine(G)
    first = eng.add_gnss(fix(0.0, 0.0, 0.0))
    assert first is not None
    base = {"t": 1.0, "lat": 33.43, "lon": -111.74, "accuracy_m": 3.0}
    base.update(kw)
    out = eng.add_gnss(GnssSample(**base))
    assert out is not None and out.gated and eng.last_reject_reason == reason
    good = eng.add_gnss(fix(1.0, 1.0, 0.0))
    assert good is not None and not good.gated
    assert all(math.isfinite(v) for v in (good.e_m, good.n_m, good.sigma_m))


def test_invalid_first_sample_returns_none() -> None:
    eng = PositioningEngine(G)
    assert eng.add_gnss(GnssSample(0.0, float("nan"), 0.0, 3.0)) is None
    assert eng.add_gnss(GnssSample(0.0, 33.0, -111.0, 0.0)) is None


def test_backwards_and_duplicate_time_rejected() -> None:
    eng = PositioningEngine(G)
    eng.add_gnss(fix(10.0, 0.0, 0.0))
    for t in (9.0, 10.0):
        out = eng.add_gnss(fix(t, 0.0, 0.0))
        assert out is not None and out.gated and eng.last_reject_reason == "time_not_increasing"


def test_long_gap_keeps_position_and_reseeds_velocity() -> None:
    kf = KalmanCV()
    kf.seed(0.0, 0.0, 0.0, 2.0)
    kf.x[2] = 3.0
    kf.predict(100.0, 0.8)
    assert kf.chain_break and kf.x[2] == 0.0 and kf.x[0] == 0.0


def test_accuracy_floor_blunts_optimistic_reports() -> None:
    eng = PositioningEngine(G)
    est = eng.add_gnss(fix(0.0, 0.0, 0.0, acc=0.2))
    assert est is not None and est.sigma_m >= 1.5  # r_min, not the reported 0.2 m


# --------------------------------------------------- anchors and fix mode


def test_anchor_shifts_position_and_sets_fix_mode_and_goes_stale() -> None:
    eng = PositioningEngine(G)
    plan = (1000.0, 500.0)
    pe, pn = G.gps_to_enu(*G.canvas_to_gps(*plan))
    survey = eng.survey_in([fix(float(i), pe + 4.0, pn - 3.0, 3.0) for i in range(5)])
    eng.set_anchor(plan, survey, t=100.0)
    est = eng.add_gnss(fix(101.0, pe + 4.0, pn - 3.0, 3.0))
    assert est is not None
    assert (est.e_m, est.n_m) == pytest.approx((pe, pn), abs=0.01)
    assert est.x_cm == pytest.approx(plan[0], abs=2.0) and est.y_cm == pytest.approx(plan[1], abs=2.0)
    assert est.fix_mode is FixMode.ANCHOR_CALIBRATED and est.anchor_source == "gnss_point"
    late = eng.add_gnss(fix(100.0 + 601.0, pe + 4.0, pn - 3.0, 3.0))
    assert late is not None and late.fix_mode is FixMode.STANDARD and late.anchor_age_s == pytest.approx(601.0)


def test_new_anchor_reseeds_instead_of_gating_the_jump() -> None:
    eng = PositioningEngine(G)
    walk(eng, 10, acc=2.0)
    sv = eng.survey_in([fix(float(i), 0.0, 0.0, 3.0) for i in range(5)])
    eng.set_anchor((5000.0, 5000.0), sv, t=11.0)  # a ~70 m shift of the frame
    est = eng.add_gnss(fix(12.0, 12.0, 0.0, 2.0))
    assert est is not None and not est.gated


def test_rtk_modes_are_inferred_from_sustained_accuracy() -> None:
    eng = PositioningEngine(G)
    est = None
    for i in range(6):
        est = eng.add_gnss(fix(float(i), 0.0, 0.0, 0.3))
    assert est is not None and est.fix_mode is FixMode.RTK_FLOAT
    for i in range(6, 12):
        est = eng.add_gnss(fix(float(i), 0.0, 0.0, 0.02))
    assert est is not None and est.fix_mode is FixMode.RTK_FIXED


def test_heading_from_motion_then_compass_when_still() -> None:
    eng = PositioningEngine(G)
    est = walk(eng, 15, acc=1.5, v=1.2)  # due east
    assert est is not None and est.heading_source == "gnss"
    assert est.heading_deg == pytest.approx(90.0, abs=5.0)
    eng2 = PositioningEngine(G)
    eng2.add_imu(ImuSample(t=0.0, ax=0, ay=0, az=9.8, heading_deg=270.0, absolute=True))
    e2 = eng2.add_gnss(fix(1.0, 0.0, 0.0))
    assert e2 is not None and (e2.heading_deg, e2.heading_source) == (270.0, "compass")
    eng2.add_imu(ImuSample(t=1.5, ax=0, ay=0, az=9.8, heading_deg=10.0, absolute=False))
    e3 = eng2.add_gnss(fix(30.0, 0.0, 0.0))  # compass is 29 s old and the new one is relative
    assert e3 is not None and e3.heading_deg is None


# ---------------------------------------------------------- recorded tracks


def _noisy_line(eng: PositioningEngine, seed: int, seconds: int = 60) -> None:
    rng = np.random.default_rng(seed)
    for i in range(seconds):
        eng.add_gnss(fix(float(i), 1.2 * i + rng.normal(0, 1.5), rng.normal(0, 1.5), 3.0))


def test_finish_track_straight_line_length_within_five_percent() -> None:
    eng = PositioningEngine(G)
    eng.start_track()
    _noisy_line(eng, 11)
    tr = eng.finish_track()
    truth = 1.2 * 59
    assert abs(tr.length_m - truth) / truth < 0.05
    assert len(tr.points_enu) < 10 and tr.smoothed
    assert tr.points_cm[0] != tr.points_cm[-1]


def test_finish_track_closure_to_start_and_refusal() -> None:
    eng = PositioningEngine(G)
    eng.start_track()
    pts = [(0, 0), (10, 0), (20, 0), (30, 0), (30, 10), (30, 20), (20, 20), (10, 20), (0, 20), (0, 10), (0.8, 0.5)]
    for i, (e, n) in enumerate(pts):
        eng.add_gnss(fix(float(i * 8), float(e), float(n), 1.5))
        eng.add_gnss(fix(float(i * 8 + 4), float(e), float(n), 1.5))
    tr = eng.finish_track(smooth=False, close="start")
    assert tr.closure is not None and tr.closure.ratio < 0.05
    assert math.dist(tr.points_enu[0], tr.points_enu[-1]) < 1e-6

    eng2 = PositioningEngine(G)
    eng2.start_track()
    for i in range(6):
        eng2.add_gnss(fix(float(i), 5.0 * i, 0.0, 1.5))
    with pytest.raises(ClosureError):
        eng2.finish_track(close="start")  # an open line is not a loop


def test_finish_track_empty_and_bad_close_arg() -> None:
    eng = PositioningEngine(G)
    eng.start_track()
    assert eng.finish_track().points_enu == ()
    eng.start_track()
    _noisy_line(eng, 1, 8)
    with pytest.raises(ValueError):
        eng.finish_track(close="point")  # needs a target
