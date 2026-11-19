"""PositioningEngine with VIO: plan-locked track, loss rules, fallback, recording, step prior.

Synthetic frames only: a known rigid transform from the WebXR ground plane to plan ENU, so the
expected positions are exact. Nothing here says how accurate a real phone would be.
"""

from __future__ import annotations

import math

import pytest

from open_garden_planner.core.georef import GeoReference
from open_garden_planner.core.positioning import (
    FixMode,
    GnssSample,
    ImuSample,
    PositioningEngine,
    VioSample,
)
from open_garden_planner.core.positioning.fusion import FusionError, Transform2D

G = GeoReference(origin_lat=33.43, origin_lon=-111.74)
TRUE = Transform2D(math.radians(30.0), 5.0, -3.0)
T0 = 1_000.0


def vio(t: float, x: float, z: float, tracking: str = "tracking") -> VioSample:
    return VioSample(t=t, x=x, y=1.4, z=z, qx=0.0, qy=0.0, qz=0.0, qw=1.0, tracking=tracking)  # type: ignore[arg-type]


def gnss(t: float, e: float, n: float, acc: float = 3.0) -> GnssSample:
    lat, lon = G.enu_to_gps(e, n)
    return GnssSample(t=t, lat=lat, lon=lon, accuracy_m=acc)


TL = T0 + 10.0  # the lock completes here
P = (21.0, -1.0)  # a VIO position shortly after B


def locked_engine() -> PositioningEngine:
    """A and B 20 m apart in the VIO frame (walked at 2 m/s), consistent with TRUE."""
    eng = PositioningEngine(G)
    eng.add_vio(vio(T0, 0.0, 0.0))
    a_xz, b_xz = (0.0, 0.0), (20.0, 0.0)
    eng.ar_capture_a(a_xz, TRUE.apply(*a_xz))
    for k in range(1, 21):
        eng.add_vio(vio(T0 + 0.5 * k, float(k), 0.0))
    eng.ar_capture_b(b_xz, TRUE.apply(*b_xz), TL)
    return eng


def test_locked_vio_replaces_the_kalman_track_and_reports_vio_fused() -> None:
    eng = locked_engine()
    est = eng.add_vio(vio(TL + 0.4, *P))
    assert est is not None and est.track_source == "vio" and est.fix_mode is FixMode.VIO_FUSED
    e, n = TRUE.apply(*P)
    assert (est.e_m, est.n_m) == pytest.approx((e, n))
    assert est.anchor_source == "plan_points" and est.gated is False
    assert eng.fused_estimate(TL + 0.5) is est
    assert eng.ar_status()["source"] == "plan_locked"


def test_gnss_while_locked_runs_the_filter_unmodified_and_only_raises_an_alert() -> None:
    eng = locked_engine()
    eng.add_vio(vio(TL + 0.4, *P))
    e, n = TRUE.apply(*P)
    # GNSS 80 m away from the locked frame: the filter takes it as it always would
    est = eng.add_gnss(gnss(TL + 0.5, e + 80.0, n, acc=3.0))
    assert est is not None and est.track_source == "gnss"
    assert abs(est.e_m - (e + 80.0)) < 1.0  # the Kalman track is the GNSS one, untouched by VIO
    assert eng.fused_estimate(TL + 0.5) is not None  # the locked track is still what is published
    assert eng.ar_status()["gnss_alert"] is not None
    fused = eng.add_vio(vio(TL + 0.6, *P))
    assert fused is not None and (fused.e_m, fused.n_m) == pytest.approx((e, n))  # no pull toward GNSS


def test_tracking_loss_unlocks_and_is_never_spliced() -> None:
    eng = locked_engine()
    assert eng.add_vio(vio(TL + 0.4, *P)) is not None
    assert eng.add_vio(vio(TL + 0.6, *P, "limited")) is None  # limited: no vio_fused
    assert eng.fused_estimate(TL + 0.6) is None
    assert eng.add_vio(vio(TL + 1.0, *P, "limited")) is None
    assert eng.add_vio(vio(TL + 1.8, *P, "limited")) is None  # limited for over 1 s: unlocked
    status = eng.ar_status()
    assert status["state"] == "unlocked" and status["unlock_reason"] == "tracking_limited"
    assert eng.add_vio(vio(TL + 2.2, *P)) is None  # tracking again, still not locked
    assert eng.fused_estimate(TL + 2.2) is None


def test_a_failed_scale_check_is_not_labelled_vio_fused() -> None:
    eng = PositioningEngine(G)
    eng.add_vio(vio(T0, 0.0, 0.0))
    eng.ar_capture_a((0.0, 0.0), TRUE.apply(0.0, 0.0))
    for k in range(1, 21):
        eng.add_vio(vio(T0 + 0.5 * k, float(k), 0.0))
    eng.ar_capture_b((20.0, 0.0), TRUE.apply(23.0, 0.0), TL)  # plan says 23 m, VIO measured 20 m
    assert eng.add_vio(vio(TL + 0.4, *P)) is None
    scale = eng.ar_status()["scale"]
    assert isinstance(scale, dict) and scale["ok"] is False and "disagree" in str(scale["message"])
    assert "scale_check_failed" in eng.ar_status()["reasons"]


def test_lock_refuses_degenerate_points_and_untracked_capture() -> None:
    eng = PositioningEngine(G)
    with pytest.raises(FusionError):
        eng.ar_capture_a((0.0, 0.0), (0.0, 0.0))  # no VIO sample yet: tracking is not good
    eng.add_vio(vio(T0, 0.0, 0.0))
    eng.ar_capture_a((0.0, 0.0), (0.0, 0.0))
    eng.add_vio(vio(T0 + 0.5, 0.5, 0.0))
    with pytest.raises(FusionError):
        eng.ar_capture_b((0.5, 0.0), (10.0, 0.0))  # 0.5 m baseline


def test_closure_at_a_third_point_and_unlock() -> None:
    eng = locked_engine()
    eng.add_vio(vio(TL + 0.4, *P))
    e, n = TRUE.apply(10.0, 10.0)
    assert eng.ar_closure((10.0, 10.0), (e + 0.3, n)) == pytest.approx(0.3)
    assert eng.ar_status()["closure_m"] == pytest.approx(0.3)
    eng.ar_unlock()
    assert eng.fused_estimate(TL + 0.5) is None and eng.ar_status()["state"] == "unlocked"
    with pytest.raises(FusionError):
        eng.ar_closure((10.0, 10.0), (e, n))


def test_a_reference_space_reset_unlock_is_visible_in_the_status() -> None:
    eng = locked_engine()
    eng.ar_unlock("reference_space_reset")
    assert eng.ar_status()["unlock_reason"] == "reference_space_reset"


def _circle(k: int, r: float = 6.0, speed: float = 1.2) -> tuple[float, float]:
    ang = speed * k / r
    return r * math.sin(ang), -r * (1.0 - math.cos(ang))


def test_without_a_lock_the_fallback_is_telemetry_only() -> None:
    eng = PositioningEngine(G)
    modes = set()
    for k in range(120):
        x, z = _circle(k)
        assert eng.add_vio(vio(T0 + k, x, z)) is None  # no lock: never the track source
        e, n = TRUE.apply(x, z)
        est = eng.add_gnss(gnss(T0 + k, e, n))
        assert est is not None and est.track_source == "gnss"
        modes.add(est.fix_mode)
    assert FixMode.VIO_FUSED not in modes
    status = eng.ar_status()
    assert status["source"] == "gnss_aligned" and status["state"] == "unlocked"


def test_recording_uses_the_vio_track_while_fresh_and_no_mixed_sources() -> None:
    eng = locked_engine()
    eng.start_track()
    for k in range(60):  # 6 s at 10 Hz, walking 1 m/s along VIO +x, with GNSS fixes 80 m off
        t = TL + 0.1 * (k + 1)
        x = 20.0 + 0.1 * (k + 1)
        eng.add_vio(vio(t, x, 0.0))
        if k % 10 == 0:
            e, n = TRUE.apply(x, 0.0)
            eng.add_gnss(gnss(t + 0.01, e + 80.0, n))
    result = eng.finish_track(smooth=True)
    assert result.length_m == pytest.approx(5.9, abs=0.5)
    e0, n0 = TRUE.apply(23.0, 0.0)
    for e, n in result.points_enu:
        assert math.dist((e, n), (e0, n0)) < 7.0  # nothing from the GNSS track 80 m away


def test_steps_with_a_heading_feed_the_prediction_when_there_is_no_vio() -> None:
    eng = PositioningEngine(G)
    for i in range(12):
        eng.add_gnss(gnss(T0 + i, 1.5 * i, 0.0))
    t = T0 + 11.2
    while t < T0 + 12.6:  # 50 Hz accelerometer: a 0.1 s bump every 0.5 s, heading due east
        phase = (t - T0) % 0.5
        mag = 9.8 + (4.0 if phase < 0.1 else 0.0)
        eng.add_imu(ImuSample(t=t, ax=0.0, ay=0.0, az=mag, heading_deg=90.0, absolute=True))
        t += 0.02
    est = eng.add_gnss(gnss(T0 + 12.0, 18.0, 0.0))
    assert est is not None and est.motion_model == "motion-smoothed"


def test_steps_are_ignored_while_vio_is_running() -> None:
    eng = PositioningEngine(G)
    for i in range(12):
        eng.add_gnss(gnss(T0 + i, 1.5 * i, 0.0))
    eng.add_vio(vio(T0 + 12.0, 0.0, 0.0))
    t = T0 + 11.2
    while t < T0 + 12.6:
        mag = 9.8 + (4.0 if (t - T0) % 0.5 < 0.1 else 0.0)
        eng.add_imu(ImuSample(t=t, ax=0.0, ay=0.0, az=mag, heading_deg=90.0, absolute=True))
        t += 0.02
    est = eng.add_gnss(gnss(T0 + 12.5, 18.7, 0.0))
    assert est is not None and est.motion_model is None
