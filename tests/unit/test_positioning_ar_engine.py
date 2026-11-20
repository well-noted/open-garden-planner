"""PositioningEngine with VIO: plan-locked track, loss rules, fallback, recording, step prior.

Synthetic frames only: a known rigid transform from the WebXR ground plane to plan ENU, so the
expected positions are exact. Nothing here says how accurate a real phone would be.
"""

from __future__ import annotations

import math

import pytest

from open_garden_planner.core.geo_frame import make_frame
from open_garden_planner.core.positioning import (
    FixMode,
    GnssSample,
    ImuSample,
    PositioningEngine,
    VioSample,
)
from open_garden_planner.core.positioning.fusion import FusionError, Transform2D

G = make_frame(origin_lat=33.43, origin_lon=-111.74)
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


# -- review fixes: arrival order, freshness, labels, thinning, reset -----------------------------


def test_recording_drops_gnss_covered_by_a_vio_span_that_arrives_later() -> None:
    eng = locked_engine()
    eng.start_track()
    walk = [(TL + 0.1 * (k + 1), 20.0 + 0.1 * (k + 1)) for k in range(60)]
    for t, x in walk[::10]:  # the GNSS request lands first, 80 m off the locked track
        e, n = TRUE.apply(x, 0.0)
        eng.add_gnss(gnss(t + 0.01, e + 80.0, n))
    for t, x in walk:  # then the VIO batch for the same seconds
        eng.add_vio(vio(t, x, 0.0))
    result = eng.finish_track(smooth=True)
    e0, n0 = TRUE.apply(23.0, 0.0)
    assert result.points_enu and all(math.dist(p, (e0, n0)) < 7.0 for p in result.points_enu)
    assert all(math.dist(p, (e0, n0)) < 7.0 for p in eng.track_preview())


def test_vio_steps_are_thinned_into_the_recording() -> None:
    eng = locked_engine()
    eng.start_track()
    for k in range(100):  # 10 s at 10 Hz
        eng.add_vio(vio(TL + 0.1 * (k + 1), 20.0 + 0.1 * (k + 1), 0.0))
    n = len(eng._steps)
    assert 15 <= n <= 25  # about 2 per second, not 10


def test_ar_status_ages_out_without_new_poses() -> None:
    eng = locked_engine()
    eng.add_vio(vio(TL + 0.4, *P))
    assert eng.ar_status(TL + 0.5)["source"] == "plan_locked"
    late = eng.ar_status(TL + 5.0)  # e.g. a GNSS request seconds after the last pose
    assert late["source"] != "plan_locked" and late["fix_mode"] is None and late["sigma_m"] is None
    stale = eng.ar_status(stream_stale=True)  # the server has heard no pose for a while
    assert stale["source"] != "plan_locked" and stale["fix_mode"] is None


def test_a_vio_sourced_estimate_is_never_labelled_rtk_and_claims_no_quality() -> None:
    eng = locked_engine()
    e, n = TRUE.apply(20.0, 0.0)
    for i in range(6):  # RTK-grade receiver accuracy sustained
        eng.add_gnss(gnss(TL + 0.05 * (i + 1), e, n, acc=0.01))
    est = eng.add_vio(vio(TL + 0.4, *P))
    assert est is not None and est.fix_mode is FixMode.VIO_FUSED
    assert est.quality == "unknown"


def test_a_reference_space_reset_also_resets_the_fallback_aligner() -> None:
    from open_garden_planner.field_remote.session import RecordingSession

    session = RecordingSession()
    eng = session.engine_for(G)
    for k in range(120):
        x, z = _circle(k)
        eng.add_vio(vio(T0 + k, x, z))
        eng.add_gnss(gnss(T0 + k, *TRUE.apply(x, z)))
    assert eng.ar_status()["source"] == "gnss_aligned"
    x, z = _circle(120)
    session._feed_ar(eng, [(vio(T0 + 120, x + 0.5, z), True)], [])  # small reset: under the jump bound
    assert eng._fallback.transform is None
    assert eng.ar_status()["source"] == "gnss"


def test_the_session_reports_a_lock_stale_when_no_pose_was_heard() -> None:
    from open_garden_planner.field_remote.session import RecordingSession

    session = RecordingSession()
    now = [100.0]
    session._clock = lambda: now[0]
    eng = session.engine_for(G)
    eng.add_vio(vio(T0, 0.0, 0.0))
    a_xz, b_xz = (0.0, 0.0), (20.0, 0.0)
    eng.ar_capture_a(a_xz, TRUE.apply(*a_xz))
    vios = [(vio(T0 + 0.5 * k, float(k), 0.0), False) for k in range(1, 21)]
    session._feed_ar(eng, vios, [])
    eng.ar_capture_b(b_xz, TRUE.apply(*b_xz), TL)
    session._feed_ar(eng, [(vio(TL + 0.4, *P), False)], [])
    assert session.ar_status(G)["source"] == "plan_locked"
    now[0] += 10.0  # the phone went to the background: nothing arrives
    assert session.ar_status(G)["source"] != "plan_locked"


def _locked_session(now: list[float]):  # type: ignore[no-untyped-def]
    from open_garden_planner.field_remote.session import RecordingSession

    session = RecordingSession()
    session._clock = lambda: now[0]
    eng = session.engine_for(G)
    session._feed_ar(eng, [(vio(T0, 0.0, 0.0), False)], [])
    eng.ar_capture_a((0.0, 0.0), TRUE.apply(0.0, 0.0))
    session._feed_ar(eng, [(vio(T0 + 0.5 * k, float(k), 0.0), False) for k in range(1, 21)], [])
    eng.ar_capture_b((20.0, 0.0), TRUE.apply(20.0, 0.0), TL)
    session._feed_ar(eng, [(vio(TL + 0.4, *P), False)], [])
    return session


def _fix(t: float, e: float, n: float):  # type: ignore[no-untyped-def]
    from open_garden_planner.field_remote import projection

    lat, lon = G.enu_to_gps(e, n)
    return projection.Fix(t=t, lat=lat, lon=lon, accuracy_m=3.0)


def test_an_engine_reset_after_a_clock_jump_says_why_the_lock_was_dropped() -> None:
    now = [100.0]
    session = _locked_session(now)
    session.solver([_fix(TL + 0.5, *TRUE.apply(*P))], G)
    assert session.ar_status(G)["state"] == "locked"
    session.solver([_fix(TL + 7200.0, *TRUE.apply(*P))], G)  # the phone clock jumped two hours
    status = session.ar_status(G)
    assert status["state"] == "unlocked" and status["unlock_reason"] == "clock_jump"


def test_a_stale_pose_stream_never_publishes_the_locked_track() -> None:
    now = [100.0]
    session = _locked_session(now)
    fresh = session.solver([_fix(TL + 0.5, *TRUE.apply(*P))], G)
    assert fresh["track_source"] == "vio" and fresh["fused"]["source"] == "plan_locked"
    now[0] += 10.0  # no pose batch for 10 s, but the GNSS fix is still within the fused window
    tele = session.solver([_fix(TL + 0.6, *TRUE.apply(*P))], G)
    assert tele["track_source"] == "gnss" and tele["fused"]["source"] != "plan_locked"


def test_vio_arriving_after_truncation_leaves_no_hole_in_the_kept_gnss(monkeypatch: pytest.MonkeyPatch) -> None:
    from open_garden_planner.core.positioning import engine as engine_mod

    monkeypatch.setattr(engine_mod, "MAX_TRACK_STEPS", 5)
    eng = locked_engine()
    eng.start_track()
    for k in range(1, 7):  # the GNSS request lands first and fills the recording
        eng.add_gnss(gnss(TL + k, *TRUE.apply(20.0 + 0.5 * k, 0.0)))
    kept = len(eng._segmented()[0])
    assert eng._truncated and kept == 5
    for k in range(1, 61):  # then the VIO batch for the same seconds, after the cut
        eng.add_vio(vio(TL + 0.1 * k, 20.0 + 0.05 * k, 0.0))
    assert len(eng._segmented()[0]) == kept and eng._vio_spans == []  # nothing dropped, no span grew


def test_the_vio_span_list_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from open_garden_planner.core.positioning import engine as engine_mod

    monkeypatch.setattr(engine_mod, "MAX_VIO_SPANS", 3)
    eng = PositioningEngine(G)
    eng.start_track()
    for k in range(10):  # ten runs of VIO, each separated by more than VIO_FRESH_S
        eng._record_vio(SimpleNamespace(t=T0 + 5.0 * k, e_m=0.0, n_m=0.0, sigma_m=0.25))  # type: ignore[arg-type]
    assert len(eng._vio_spans) == 3 and eng._truncated and len(eng._steps) == 3
