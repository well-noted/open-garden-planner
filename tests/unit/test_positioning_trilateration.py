"""Trilateration: 2 circles, over-determined noise, degenerate and hostile input."""

from __future__ import annotations

import math

import numpy as np
import pytest

from open_garden_planner.core.positioning.trilateration import TrilaterationError, solve

A = (0.0, 0.0)
B = (10.0, 0.0)
C = (0.0, 8.0)
TRUTH = (4.0, 3.0)


def _d(p, q):
    return math.dist(p, q)


def test_two_circles_pick_side_by_hint() -> None:
    d = [_d(TRUTH, A), _d(TRUTH, B)]
    up = solve([A, B], d, side_hint=(4.0, 1.0))
    down = solve([A, B], d, side_hint=(4.0, -1.0))
    assert up.pos == pytest.approx(TRUTH, abs=1e-9)
    assert down.pos == pytest.approx((4.0, -3.0), abs=1e-9)


def test_two_circles_without_hint_is_ambiguous_and_lists_both() -> None:
    with pytest.raises(TrilaterationError) as exc:
        solve([A, B], [_d(TRUTH, A), _d(TRUTH, B)])
    assert len(exc.value.solutions) == 2


def test_three_exact_distances_zero_residual() -> None:
    r = solve([A, B, C], [_d(TRUTH, p) for p in (A, B, C)])
    assert r.pos == pytest.approx(TRUTH, abs=1e-8)
    assert r.rms < 1e-8
    assert r.sigma < 0.05  # computed spread of well-conditioned 2 cm tape readings


def test_noisy_overdetermined_is_close_and_reports_residuals() -> None:
    rng = np.random.default_rng(7)
    pts = [A, B, C, (10.0, 8.0)]
    d = [_d(TRUTH, p) + rng.normal(0, 0.02) for p in pts]
    r = solve(pts, d)
    assert math.dist(r.pos, TRUTH) < 0.05
    assert len(r.residuals) == 4
    assert r.rms == pytest.approx(math.sqrt(np.mean(np.square(r.residuals))))
    assert r.cov[0][1] == pytest.approx(r.cov[1][0])


def test_collinear_known_points_refused() -> None:
    pts = [(0.0, 0.0), (5.0, 0.0), (10.0, 0.0)]
    # truth far out on the line's axis: lines of sight nearly parallel
    t = (30.0, 1.0)
    with pytest.raises(TrilaterationError):
        solve(pts, [_d(t, p) for p in pts], side_hint=t)


def test_near_collinear_two_circles_refused_below_20_degrees() -> None:
    t = (60.0, 2.0)  # almost on the A-B axis, far away: crossing angle is tiny
    with pytest.raises(TrilaterationError, match="degrees"):
        solve([A, B], [_d(t, A), _d(t, B)], side_hint=t)


def test_inconsistent_distance_refused() -> None:
    d = [_d(TRUTH, A), _d(TRUTH, B), _d(TRUTH, C) + 1.0]  # one tape misread by a metre
    with pytest.raises(TrilaterationError, match="disagree"):
        solve([A, B, C], d)


def test_circles_that_do_not_meet_refused() -> None:
    with pytest.raises(TrilaterationError):
        solve([A, B], [1.0, 1.0], side_hint=(5.0, 1.0))


@pytest.mark.parametrize(
    "points,dists",
    [
        ([A], [1.0]),
        ([A, B], [1.0]),
        ([A, B], [float("nan"), 5.0]),
        ([A, B], [float("inf"), 5.0]),
        ([A, B], [0.0, 5.0]),
        ([A, B], [-1.0, 5.0]),
        ([A, (float("nan"), 0.0)], [5.0, 5.0]),
        ([A, A], [5.0, 5.0]),  # coincident known points
    ],
)
def test_bad_input_refused(points, dists) -> None:
    with pytest.raises(TrilaterationError):
        solve(points, dists, side_hint=(1.0, 1.0))


def test_sigma_d_must_be_positive() -> None:
    with pytest.raises(TrilaterationError):
        solve([A, B, C], [5.0, 8.0, 5.0], sigma_d=[0.0, 0.02, 0.02])


def test_bad_side_hint_refused() -> None:
    with pytest.raises(TrilaterationError):
        solve([A, B], [5.0, 8.0], side_hint=(float("nan"), 0.0))
