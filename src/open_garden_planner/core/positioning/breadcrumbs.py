"""Breadcrumb selection and track simplification (design section 2.4)."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence

import numpy as np

from .types import (
    CRUMB_MAX_VERTICES,
    CRUMB_MIN_M,
    CRUMB_MIN_SPACING_M,
    RDP_MIN_EPS_M,
    RDP_SIGMA_FACTOR,
)

Point = tuple[float, float]


def select_breadcrumbs(
    points: Sequence[Point], sigmas: Sequence[float], stationary: Sequence[bool]
) -> list[int]:
    """Indices to keep: emit after moving >= max(0.5 m, sigma); frozen while stationary.

    First and last points are always kept (a recording's endpoints are meaningful).
    """
    if not points:
        return []
    keep = [0]
    last = points[0]
    for i in range(1, len(points)):
        if stationary[i]:
            continue
        if math.dist(points[i], last) >= max(CRUMB_MIN_M, sigmas[i]):
            keep.append(i)
            last = points[i]
    if keep[-1] != len(points) - 1:
        keep.append(len(points) - 1)
    return keep


def rdp(points: Sequence[Point], epsilon: float) -> list[Point]:
    """Ramer-Douglas-Peucker (iterative, so a long track cannot hit the recursion limit)."""
    n = len(points)
    if n < 3:
        return list(points)
    pts = np.asarray(points, dtype=float)
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        seg = pts[b] - pts[a]
        rel = pts[a + 1 : b] - pts[a]
        length = float(np.hypot(seg[0], seg[1]))
        if length == 0.0:  # closed loop: distance to the shared point
            dists = np.hypot(rel[:, 0], rel[:, 1])
        else:
            dists = np.abs(seg[0] * rel[:, 1] - seg[1] * rel[:, 0]) / length
        k = int(np.argmax(dists))
        if dists[k] > epsilon:
            idx = a + 1 + k
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [tuple(p) for p in pts[keep].tolist()]  # type: ignore[misc]


def _drop_close(points: list[Point], min_gap: float) -> list[Point]:
    """Drop interior vertices closer than ``min_gap`` to the previous kept one."""
    if len(points) < 3:
        return points
    out = [points[0]]
    for p in points[1:-1]:
        if math.dist(p, out[-1]) >= min_gap and math.dist(p, points[-1]) >= min_gap:
            out.append(p)
    out.append(points[-1])
    return out


def simplify_track(
    points: Sequence[Point], sigmas: Sequence[float]
) -> tuple[list[Point], float]:
    """RDP with ``epsilon = max(0.15 m, 0.5 x median sigma)``, drop vertices < 0.3 m apart,
    cap at 2000 vertices by raising epsilon. Returns ``(vertices, epsilon_used)``.
    """
    if len(points) < 3:
        return list(points), 0.0
    eps = max(RDP_MIN_EPS_M, RDP_SIGMA_FACTOR * statistics.median(sigmas)) if sigmas else RDP_MIN_EPS_M
    out = _drop_close(rdp(points, eps), CRUMB_MIN_SPACING_M)
    while len(out) > CRUMB_MAX_VERTICES:
        eps *= 1.5
        out = _drop_close(rdp(points, eps), CRUMB_MIN_SPACING_M)
    return out, eps
