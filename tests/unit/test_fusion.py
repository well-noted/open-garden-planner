"""WP-H fusion: synthetic VIO frames, deterministic, no sleeps."""

from __future__ import annotations

import math

import numpy as np
import pytest

from open_garden_planner.core.positioning.fusion import (
    FusionAligner,
    FusionError,
    FusionState,
    PdrStepper,
    PlanLockedAligner,
    Transform2D,
    vio_ground,
)
from open_garden_planner.core.positioning.types import FixMode, ImuSample, VioSample


def _vio(t: float, x: float, z: float, tracking: str = "tracking") -> VioSample:
    return VioSample(t, x, 1.4, z, 0.0, 0.0, 0.0, 1.0, tracking)  # type: ignore[arg-type]


def _ang(a: float, b: float) -> float:
    return abs(math.remainder(a - b, 2.0 * math.pi))


def _identity_locked() -> PlanLockedAligner:
    al = PlanLockedAligner()
    al.lock((0.0, 0.0), (0.0, 0.0), (0.0, -10.0), (0.0, 10.0))
    return al


def test_webxr_axis_mapping() -> None:
    ident = Transform2D(0.0, 0.0, 0.0)
    assert ident.apply(*vio_ground(_vio(0, 0.0, -1.0))) == pytest.approx((0.0, 1.0))  # forward -> north
    assert ident.apply(*vio_ground(_vio(0, 1.0, 0.0))) == pytest.approx((1.0, 0.0))  # right -> east
    assert Transform2D(math.pi / 2, 0.0, 0.0).apply(0.0, -1.0) == pytest.approx((-1.0, 0.0))  # CCW
    assert vio_ground(VioSample(0.0, 2.0, 5.0, 3.0, 0.0, 0.0, 0.0, 1.0)) == (2.0, 3.0)  # y ignored


def test_exact_recovery_and_inverse() -> None:
    rng = np.random.default_rng(1)
    true = Transform2D(math.radians(37.0), 12.5, -4.0)
    a, b = (1.0, -2.0), (7.0, 3.0)
    got = PlanLockedAligner().lock(a, true.apply(*a), b, true.apply(*b))
    assert _ang(got.yaw_rad, true.yaw_rad) < 1e-12
    for x, z in rng.uniform(-50.0, 50.0, (20, 2)):
        assert got.apply(x, z) == pytest.approx(true.apply(x, z), abs=1e-9)
        assert got.inverse(*got.apply(x, z)) == pytest.approx((x, z), abs=1e-9)


def test_degenerate_lock() -> None:
    al = PlanLockedAligner()
    with pytest.raises(FusionError):
        al.lock((0.0, 0.0), (0.0, 0.0), (1.5, 0.0), (10.0, 0.0))
    with pytest.raises(FusionError):
        al.lock((0.0, 0.0), (0.0, 0.0), (10.0, 0.0), (1.0, 0.0))
    with pytest.raises(FusionError):
        al.lock((0.0, float("nan")), (0.0, 0.0), (10.0, 0.0), (10.0, 0.0))
    assert al.state is FusionState.UNLOCKED and al.transform is None


def test_bad_scale_warns_and_withholds_vio_fused() -> None:
    bad = PlanLockedAligner.scale_check((0.0, 0.0), (0.0, 0.0), (10.25, 0.0), (10.0, 0.0))
    assert not bad.ok and bad.ratio == pytest.approx(1.025)
    assert "plan points disagree with the measured distance" in bad.message
    assert PlanLockedAligner.scale_check((0.0, 0.0), (0.0, 0.0), (10.15, 0.0), (10.0, 0.0)).ok
    al = PlanLockedAligner()
    al.lock((0.0, 0.0), (0.0, 0.0), (10.25, 0.0), (10.0, 0.0))
    fix = al.on_sample(_vio(0.0, 1.0, 0.0))
    assert fix is not None and fix.fix_mode is None and "scale_check_failed" in fix.reasons


def test_third_point_residual() -> None:
    al = PlanLockedAligner()
    with pytest.raises(FusionError):
        al.third_point_residual((5.0, 5.0), (0.0, 0.0))
    true = Transform2D(math.radians(-20.0), 3.0, 8.0)
    a, b = (0.0, 0.0), (6.0, -4.0)
    al.lock(a, true.apply(*a), b, true.apply(*b))
    ce, cn = true.apply(5.0, 5.0)
    assert al.third_point_residual((5.0, 5.0), (ce + 0.3, cn)) == pytest.approx(0.3, abs=1e-9)


def test_tracking_loss_unlocks_and_requires_relock() -> None:
    al = _identity_locked()
    for k in range(5):
        fix = al.on_sample(_vio(k * 0.25, 0.0, -0.1 * k))
        assert fix is not None and fix.fix_mode is FixMode.VIO_FUSED
    fix = al.on_sample(_vio(1.25, 0.0, -0.5, "limited"))  # short limited: still locked, not vio_fused
    assert fix is not None and fix.fix_mode is None and al.state is FusionState.LOCKED_LIMITED
    fix = al.on_sample(_vio(1.5, 0.0, -0.5))
    assert fix is not None and fix.fix_mode is FixMode.VIO_FUSED
    for k in range(5):  # limited 1.75 .. 2.75: exactly 1.0 s is not > 1 s
        assert al.on_sample(_vio(1.75 + 0.25 * k, 0.0, -0.5, "limited")) is not None
    assert al.on_sample(_vio(3.0, 0.0, -0.5, "lost")) is None  # 1.25 s
    assert al.state is FusionState.UNLOCKED and al.unlock_reason == "tracking_lost"
    assert al.on_sample(_vio(3.25, 0.0, -0.5)) is None  # never spliced
    al.lock((0.0, 0.0), (0.0, 0.0), (0.0, -10.0), (0.0, 10.0))
    assert al.on_sample(_vio(3.5, 0.0, -0.5)) is not None


def test_gap_and_pending_a_dropped() -> None:
    al = _identity_locked()
    al.on_sample(_vio(0.0, 0.0, 0.0))
    assert al.on_sample(_vio(1.5, 0.0, 0.0)) is None and al.unlock_reason == "vio_gap"
    al.on_sample(_vio(1.75, 0.0, 0.0))
    al.capture_a((0.0, 0.0), (0.0, 0.0))
    assert al.state is FusionState.PENDING_B
    for k in range(6):
        al.on_sample(_vio(2.0 + 0.25 * k, 0.0, 0.0, "lost"))
    assert al.state is FusionState.UNLOCKED
    al.on_sample(_vio(3.5, 0.0, 0.0))
    with pytest.raises(FusionError):
        al.capture_b((0.0, -10.0), (0.0, 10.0))


def test_drift_not_pulled_to_gnss_and_gross_error_alert() -> None:
    al = _identity_locked()
    fix = None
    for k in range(201):  # true walk north at 1 m/s, VIO over-reads 1 %
        true_n = 0.5 * k
        fix = al.on_sample(_vio(0.5 * k, 0.0, -1.01 * true_n))
        if k == 40:
            assert fix is not None and fix.n_m == pytest.approx(20.2)
            assert al.gnss_disagreement((0.0, 20.0), sigma=1.0) is None
    assert fix is not None and fix.n_m == pytest.approx(101.0)  # GNSS never moved it
    assert al.gnss_disagreement((0.0, 100.0), sigma=0.3) is not None


def _circle(k: int, r: float = 6.0, speed: float = 1.2) -> tuple[float, float]:
    ang = speed * k / r
    return r * math.sin(ang), -r * (1.0 - math.cos(ang))


def _feed(al: FusionAligner, true: Transform2D, ks: range, rng: np.random.Generator) -> None:
    for k in ks:
        x, z = _circle(k)
        al.on_vio(_vio(float(k), x, z))
        e, n = true.apply(x, z)
        al.add_gnss(float(k), e + rng.normal(0.0, 1.0), n + rng.normal(0.0, 1.0), 1.0)


def test_fallback_translation_only_then_converges() -> None:
    rng = np.random.default_rng(7)
    true = Transform2D(math.radians(30.0), 5.0, -3.0)
    al = FusionAligner()
    _feed(al, true, range(9), rng)
    assert not al.yaw_observed and "yaw_unobserved_translation_only" in al.reasons
    _feed(al, true, range(9, 120), rng)
    assert al.yaw_observed and al.yaw is not None
    assert _ang(al.yaw, true.yaw_rad) < math.radians(3.0)
    x, z = _circle(119)
    fix = al.position(_vio(119.0, x, z))
    assert fix is not None and fix.fix_mode is None
    assert math.dist((fix.e_m, fix.n_m), true.apply(x, z)) < 1.0


def test_fallback_yaw_jump_rejected_unless_tracking_loss() -> None:
    rng = np.random.default_rng(11)
    true = Transform2D(math.radians(30.0), 5.0, -3.0)
    al = FusionAligner()
    _feed(al, true, range(120), rng)
    al.clear_window()
    jumped = Transform2D(math.radians(70.0), 5.0, -3.0)
    _feed(al, jumped, range(120, 133), rng)  # 4 rejected fits (< 5 resets)
    assert al.yaw is not None and _ang(al.yaw, true.yaw_rad) < math.radians(3.0)
    assert "yaw_jump_rejected" in al.reasons
    x, z = _circle(133)
    al.on_vio(_vio(132.5, x, z, "limited"))  # brief loss explains the jump
    _feed(al, jumped, range(133, 134), rng)
    assert _ang(al.yaw, jumped.yaw_rad) < math.radians(3.0)


def _gait(freq_hz: float, seconds: float = 10.0, rate_hz: float = 50.0, seed: int = 3) -> list[ImuSample]:
    rng = np.random.default_rng(seed)
    out = []
    for i in range(int(seconds * rate_hz)):
        t = i / rate_hz
        az = 9.81 + 2.5 * math.sin(2.0 * math.pi * freq_hz * t) + rng.normal(0.0, 0.3)
        out.append(ImuSample(t, 0.0, 0.0, az, heading_deg=90.0, absolute=True))
    return out


def test_pdr_step_count_and_heading() -> None:
    pdr = PdrStepper()
    steps = [s for m in _gait(1.8) if (s := pdr.update(m)) is not None]
    assert 17 <= len(steps) <= 18
    assert all(s.label == "motion-smoothed" for s in steps)
    assert sum(s.de for s in steps) == pytest.approx(0.7 * len(steps))
    assert sum(abs(s.dn) for s in steps) == pytest.approx(0.0, abs=1e-9)


def test_pdr_refractory_limits_cadence() -> None:
    pdr = PdrStepper()
    times = [s.t for m in _gait(5.0) if (s := pdr.update(m)) is not None]
    assert 0 < len(times) < 50
    assert all(b - a >= 0.3 - 1e-9 for a, b in zip(times, times[1:], strict=False))
