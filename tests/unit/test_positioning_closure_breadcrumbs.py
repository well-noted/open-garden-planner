"""Bowditch closure golden and breadcrumb/RDP behaviour."""

from __future__ import annotations

import math

import pytest

from open_garden_planner.core.positioning.breadcrumbs import rdp, select_breadcrumbs, simplify_track
from open_garden_planner.core.positioning.closure import ClosureError, bowditch


def test_bowditch_golden_distributes_by_path_length() -> None:
    # A 40 x 30 m loop that should close on its start but ends 0.6 m east, 0.8 m north of it.
    pts = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0), (0.6, 0.8)]
    lens = [40.0, 30.0, 40.0, math.hypot(0.6, 29.2)]
    total = sum(lens)  # ~139.2 m
    res = bowditch(pts)
    assert res.closure_vector == pytest.approx((0.6, 0.8))
    assert res.closure_m == pytest.approx(1.0)
    assert res.path_length_m == pytest.approx(total)
    assert res.ratio == pytest.approx(1.0 / total)
    assert res.points[0] == (0.0, 0.0)  # start never moves
    assert res.points[-1] == pytest.approx((0.0, 0.0), abs=1e-12)  # end lands on the start
    cum = 0.0
    for i in range(1, 4):  # interior vertices take the share of drift run up to them
        cum += lens[i - 1]
        f = cum / total
        assert res.points[i] == pytest.approx((pts[i][0] - f * 0.6, pts[i][1] - f * 0.8))


def test_bowditch_to_plan_point_target() -> None:
    pts = [(0.0, 0.0), (10.0, 0.0), (20.0, 0.4)]
    res = bowditch(pts, target=(20.0, 0.0))
    assert res.points[-1] == pytest.approx((20.0, 0.0))
    assert res.points[1] == pytest.approx((10.0, -0.2 + 0.0), abs=0.01)  # half the drift at half the length
    assert res.ratio == pytest.approx(0.4 / res.path_length_m)


def test_bowditch_refuses_over_five_percent() -> None:
    with pytest.raises(ClosureError, match="not drift"):
        bowditch([(0.0, 0.0), (10.0, 0.0), (10.0, 5.0)])  # end 11 m from start on a 15 m path


@pytest.mark.parametrize(
    "pts", [[(0.0, 0.0)], [(0.0, 0.0), (0.0, 0.0)], [(0.0, 0.0), (float("nan"), 1.0)]]
)
def test_bowditch_bad_paths(pts) -> None:
    with pytest.raises(ClosureError):
        bowditch(pts)


def test_rdp_collinear_collapses_to_endpoints() -> None:
    pts = [(float(i), 0.001 * (i % 2)) for i in range(50)]
    assert rdp(pts, 0.15) == [pts[0], pts[-1]]


def test_rdp_keeps_a_corner() -> None:
    pts = [(float(i), 0.0) for i in range(11)] + [(10.0, float(j)) for j in range(1, 11)]
    out = rdp(pts, 0.15)
    assert out == [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]


def test_rdp_closed_loop_does_not_collapse() -> None:
    sq = [(0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (0.0, 5.0), (0.0, 0.0)]
    assert len(rdp(sq, 0.15)) == 5


def test_simplify_drops_close_vertices_and_caps() -> None:
    zig = [(i * 0.1, 0.4 * (i % 2)) for i in range(5000)]
    out, eps = simplify_track(zig, [0.2] * len(zig))
    assert len(out) <= 2000
    assert out[0] == zig[0] and out[-1] == zig[-1]  # endpoints always kept
    assert eps >= 0.15


def test_breadcrumbs_min_distance_and_stationary_freeze() -> None:
    pts = [(0.0, 0.0), (0.2, 0.0), (0.7, 0.0), (0.9, 0.0), (1.5, 0.0), (1.5, 0.0)]
    sig = [0.3] * 6
    keep = select_breadcrumbs(pts, sig, [False] * 6)
    assert keep == [0, 2, 4, 5]  # 0.5 m rule; last point kept as the endpoint
    keep_frozen = select_breadcrumbs(pts, sig, [False, False, True, True, True, True])
    assert keep_frozen == [0, 5]  # frozen while stationary, endpoint still kept


def test_breadcrumb_spacing_scales_with_sigma() -> None:
    pts = [(float(i) * 0.6, 0.0) for i in range(6)]
    keep = select_breadcrumbs(pts, [2.0] * 6, [False] * 6)
    assert keep == [0, 4, 5]  # 0.6 m steps but sigma 2 m: next crumb after >= 2 m
