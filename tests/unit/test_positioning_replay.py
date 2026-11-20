"""Replay harness: seeded synthetic walks through the engine.

Assertions are RATIOS (filtered error / raw error) and flags, never absolute metres:
absolute accuracy is a hypothesis until on-device measurement (design WP-M).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from open_garden_planner.core.georef import GeoReference
from open_garden_planner.core.positioning import GnssSample, PositioningEngine

G = GeoReference(origin_lat=33.43, origin_lon=-111.74)
V = 1.3  # m/s


def _sample(t: float, e: float, n: float, acc: float) -> GnssSample:
    lat, lon = G.enu_to_gps(e, n)
    return GnssSample(t=t, lat=lat, lon=lon, accuracy_m=acc)


def truth_straight(t: float) -> tuple[float, float]:
    return V * t, 0.0


def truth_l(t: float) -> tuple[float, float]:
    d = V * t
    return (d, 0.0) if d <= 40.0 else (40.0, d - 40.0)


def truth_still(_t: float) -> tuple[float, float]:
    return 12.0, -7.0


def replay(truth, seconds: int, seed: int, acc_of=lambda _t: 4.0, noise_of=lambda _t: 3.0, spikes=()):
    """Returns (filtered_err, raw_err, gated_flags) per sample."""
    rng = np.random.default_rng(seed)
    eng = PositioningEngine(G)
    f_err, r_err, gated = [], [], []
    for i in range(seconds):
        t = float(i)
        te, tn = truth(t)
        sg = noise_of(t)
        e, n = te + rng.normal(0, sg), tn + rng.normal(0, sg)
        if i in spikes:
            e, n = e + 35.0, n - 35.0
        est = eng.add_gnss(_sample(t, e, n, acc_of(t)))
        assert est is not None
        f_err.append(math.dist((est.e_m, est.n_m), (te, tn)))
        r_err.append(math.dist((e, n), (te, tn)))
        gated.append(est.gated)
    return np.array(f_err), np.array(r_err), gated


def rms(a: np.ndarray, skip: int = 10) -> float:
    return float(np.sqrt(np.mean(a[skip:] ** 2)))


SEEDS = range(1, 9)


def pooled_ratio(truth, **kw) -> float:
    """sqrt(sum filtered err^2 / sum raw err^2) over several seeds (one seed is too noisy)."""
    fs, rs = [], []
    for seed in SEEDS:
        f, r, _ = replay(truth, 90, seed, **kw)
        fs.append(f[10:] ** 2)
        rs.append(r[10:] ** 2)
    return float(math.sqrt(np.concatenate(fs).sum() / np.concatenate(rs).sum()))


def test_straight_walk_filtered_is_no_worse_than_raw() -> None:
    # Honest expectation: at 1 Hz, 1.3 m/s and 3 m noise a CV filter buys ~10 %, not more.
    assert pooled_ratio(truth_straight) < 0.97


def test_l_walk_filtered_is_no_worse_than_raw() -> None:
    assert pooled_ratio(truth_l) < 1.0  # the corner costs lag; no gain is claimed here


def test_stand_still_error_is_much_smaller_than_raw() -> None:
    assert pooled_ratio(truth_still) < 0.5


@pytest.mark.parametrize("seed", [4, 5])
def test_multipath_spike_does_not_drag_the_track(seed: int) -> None:
    spikes = (30, 31, 60)  # two in a row, then a lone one
    f, r, gated = replay(truth_straight, 90, seed, spikes=spikes)
    assert all(gated[i] for i in spikes)
    assert f.max() < 0.3 * r[list(spikes)].min()  # nowhere near the 35 m-offset fixes
    assert rms(f) / rms(r) < 0.5


def test_accuracy_ramp_reported_and_true_noise_grow_together() -> None:
    f, r, _ = replay(
        truth_straight, 90, 6, acc_of=lambda t: 3.0 + 0.25 * t, noise_of=lambda t: 2.0 + 0.2 * t
    )
    assert rms(f) / rms(r) < 0.85


def test_replay_is_deterministic() -> None:
    a, _, _ = replay(truth_l, 60, 9)
    b, _, _ = replay(truth_l, 60, 9)
    assert np.array_equal(a, b)


def test_a_walk_that_starts_after_standing_still_is_not_pinned_by_the_stationary_filter() -> None:
    """Review finding: stationary updates held velocity at zero, the first walking fixes were
    gated, and the filter only recovered through the three-reject reseed (a position jump)."""
    rng = np.random.default_rng(11)
    eng = PositioningEngine(G)
    last = None
    gated_after_start = 0
    for t in range(0, 40):
        e, n = (0.0, 0.0) if t < 15 else (V * (t - 15), 0.0)
        got = eng.add_gnss(_sample(float(t), e + rng.normal(0, 1.5), n + rng.normal(0, 1.5), 3.0))
        last = got
        if t >= 15 and got is not None and got.gated:
            gated_after_start += 1
    assert last is not None
    assert gated_after_start <= 2
    assert eng.reseed_count == 0
    true_e = V * 24
    assert abs(last.e_m - true_e) < 6.0
