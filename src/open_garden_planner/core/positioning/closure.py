"""Bowditch loop closure (design section 2.7).

A path that should end at a known point (or close on its start) has closure
vector ``c = p_end - p_target``. It is spread by cumulative path length:
``p_i' = p_i - (s_i / s_total) c``. Refused when ``|c| / s_total`` exceeds 5 %,
which is a mistake rather than drift. The control-network adjustment the design
mentions is deferred to v2 (it would exceed the ~150-line budget).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

Point = tuple[float, float]
MAX_CLOSURE_RATIO = 0.05


class ClosureError(ValueError):
    """The closure was refused (bad input, no path length, or too large to be drift)."""


@dataclass(frozen=True)
class ClosureResult:
    points: tuple[Point, ...]
    closure_vector: Point  # p_end - p_target, before correction
    closure_m: float
    path_length_m: float
    ratio: float  # closure_m / path_length_m


def bowditch(points: Sequence[Point], target: Point | None = None) -> ClosureResult:
    """Distribute the end-point misclosure along the path. ``target=None`` closes on the start."""
    pts = np.asarray(points, dtype=float)
    if pts.ndim != 2 or pts.shape[0] < 2 or pts.shape[1] != 2 or not np.all(np.isfinite(pts)):
        raise ClosureError("need at least two finite points")
    tgt = pts[0] if target is None else np.asarray(target, dtype=float)
    if tgt.shape != (2,) or not np.all(np.isfinite(tgt)):
        raise ClosureError("target must be a finite point")
    seg = np.hypot(*np.diff(pts, axis=0).T)
    total = float(seg.sum())
    if total <= 0.0:
        raise ClosureError("the path has no length")
    c = pts[-1] - tgt
    closure = float(np.hypot(c[0], c[1]))
    ratio = closure / total
    if ratio > MAX_CLOSURE_RATIO:
        raise ClosureError(f"closure {closure:.1f} m is {ratio:.0%} of the path: not drift")
    frac = np.concatenate(([0.0], np.cumsum(seg))) / total
    fixed = pts - frac[:, None] * c[None, :]
    return ClosureResult(
        points=tuple((float(x), float(y)) for x, y in fixed),
        closure_vector=(float(c[0]), float(c[1])),
        closure_m=closure,
        path_length_m=total,
        ratio=ratio,
    )


def path_length(points: Sequence[Point]) -> float:
    return sum(math.dist(a, b) for a, b in zip(points, points[1:], strict=False))
