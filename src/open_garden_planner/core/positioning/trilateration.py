"""Tape/laser trilateration (design section 2.6): no GPS, no camera.

Planar and unit-agnostic (metres in practice). 2 points: circle intersection;
3+: 2D Gauss-Newton on ``r_i = |p - a_i| - d_i`` with weights ``1/sigma_d^2``.
The covariance ``(J^T W J)^-1`` and the residuals are computed, so the sigma
shown is an honest readout of the geometry and the measurements.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

Point = tuple[float, float]

SIGMA_BASE_M = 0.02  # 2 cm + 0.2 % of the distance, user-editable
SIGMA_REL = 0.002
MAX_ITER = 20
MIN_ANGLE_DEG = 20.0
RMS_REFUSE_FACTOR = 3.0


class TrilaterationError(ValueError):
    """Refused: bad input, degenerate geometry, inconsistent distances or an ambiguous side."""

    def __init__(self, message: str, solutions: tuple[Point, ...] = ()) -> None:
        super().__init__(message)
        self.solutions = solutions


@dataclass(frozen=True)
class TriResult:
    pos: Point
    residuals: tuple[float, ...]  # |p - a_i| - d_i, input order
    rms: float
    cov: tuple[tuple[float, float], tuple[float, float]]  # (J^T W J)^-1, units^2
    sigma: float  # sqrt(trace(cov) / 2)
    dop: float  # sqrt(trace((J^T J)^-1)): geometric dilution, ~1.4 is ideal for 2 circles
    angle_deg: float  # widest angle between two lines of sight (0..90)
    iterations: int


def _angle_between_lines(u: np.ndarray, v: np.ndarray) -> float:
    c = abs(float(u @ v)) / (float(np.linalg.norm(u)) * float(np.linalg.norm(v)))
    return math.degrees(math.acos(min(1.0, c)))


def _intersections(a1: np.ndarray, r1: float, a2: np.ndarray, r2: float) -> list[np.ndarray]:
    d = float(np.linalg.norm(a2 - a1))
    if d < 1e-9:
        raise TrilaterationError("known points coincide")
    if d > r1 + r2 or d < abs(r1 - r2):
        raise TrilaterationError("distances are inconsistent with the point spacing")
    a = (r1 * r1 - r2 * r2 + d * d) / (2.0 * d)
    h = math.sqrt(max(r1 * r1 - a * a, 0.0))
    ux = (a2 - a1) / d
    base = a1 + a * ux
    perp = np.array([-ux[1], ux[0]])
    return [base + h * perp, base - h * perp]


def solve(
    points: Sequence[Point],
    distances: Sequence[float],
    *,
    side_hint: Point | None = None,
    sigma_d: Sequence[float] | None = None,
) -> TriResult:
    """Position from >= 2 ``(known point, measured distance)`` pairs.

    ``side_hint`` is any point on the correct side (e.g. the current estimate); it picks
    between the two 2-circle solutions and seeds the 3+ case. Without it, 2 circles raise
    :class:`TrilaterationError` carrying both candidates in ``.solutions``.
    """
    n = len(points)
    if n < 2 or len(distances) != n:
        raise TrilaterationError("need at least 2 points and one distance per point")
    pts = np.asarray(points, dtype=float)
    d = np.asarray(distances, dtype=float)
    if pts.shape != (n, 2) or not np.all(np.isfinite(pts)) or not np.all(np.isfinite(d)):
        raise TrilaterationError("points and distances must be finite")
    if np.any(d <= 0):
        raise TrilaterationError("distances must be positive")
    if sigma_d is None:
        sig = SIGMA_BASE_M + SIGMA_REL * d
    else:
        sig = np.asarray(sigma_d, dtype=float)
        if sig.shape != (n,) or not np.all(np.isfinite(sig)) or np.any(sig <= 0):
            raise TrilaterationError("sigma_d must be positive, one per point")
    hint = None
    if side_hint is not None:
        hint = np.asarray(side_hint, dtype=float)
        if hint.shape != (2,) or not np.all(np.isfinite(hint)):
            raise TrilaterationError("side_hint must be a finite point")

    # Seed: intersect the best-conditioned pair (closest to 90 degrees apart as seen from the
    # other circle centres is not known yet, so rank pairs by their chord geometry: the pair
    # with the largest sin of the crossing angle at the intersection).
    best: tuple[float, list[np.ndarray], tuple[int, int]] | None = None
    for i in range(n):
        for j in range(i + 1, n):
            try:
                cand = _intersections(pts[i], d[i], pts[j], d[j])
            except TrilaterationError:
                continue
            p = cand[0]
            ang = _angle_between_lines(p - pts[i], p - pts[j])
            if best is None or ang > best[0]:
                best = (ang, cand, (i, j))
    if best is None:
        raise TrilaterationError("no two circles intersect: check the distances")
    _, cand, _ = best

    if n == 2:
        if hint is None:
            raise TrilaterationError(
                "two circles meet in two places: give a side hint",
                tuple((float(c[0]), float(c[1])) for c in cand),
            )
        p = min(cand, key=lambda c: float(np.linalg.norm(c - hint)))
        return _finish(pts, d, sig, p, 0)

    if hint is not None:
        start = min(cand, key=lambda c: float(np.linalg.norm(c - hint)))
    else:  # choose the candidate that best satisfies the remaining circles
        start = min(cand, key=lambda c: float(np.sum((np.linalg.norm(pts - c, axis=1) - d) ** 2)))
    p = start.copy()
    w = 1.0 / sig**2
    iters = 0
    for _ in range(MAX_ITER):
        iters += 1
        diff = p - pts
        rng = np.linalg.norm(diff, axis=1)
        if np.any(rng < 1e-9):
            raise TrilaterationError("position coincides with a known point")
        jac = diff / rng[:, None]
        res = rng - d
        jtw = jac.T * w
        try:
            step = np.linalg.solve(jtw @ jac, -(jtw @ res))
        except np.linalg.LinAlgError as exc:
            raise TrilaterationError("degenerate geometry (collinear points)") from exc
        p = p + step
        if float(np.linalg.norm(step)) < 1e-9:
            break
    return _finish(pts, d, sig, p, iters)


def _finish(pts: np.ndarray, d: np.ndarray, sig: np.ndarray, p: np.ndarray, iters: int) -> TriResult:
    diff = p - pts
    rng = np.linalg.norm(diff, axis=1)
    if np.any(rng < 1e-9):
        raise TrilaterationError("position coincides with a known point")
    jac = diff / rng[:, None]
    # Widest angle between two lines of sight; collinear / near-collinear geometry fails here.
    n = len(pts)
    angle = max(
        _angle_between_lines(jac[i], jac[j]) for i in range(n) for j in range(i + 1, n)
    )
    if angle < MIN_ANGLE_DEG:
        raise TrilaterationError(f"degenerate geometry: lines of sight only {angle:.0f} degrees apart")
    res = rng - d
    rms = float(math.sqrt(float(np.mean(res**2))))
    if n >= 3 and rms > RMS_REFUSE_FACTOR * float(np.mean(sig)):
        raise TrilaterationError(f"distances disagree: residual rms {rms:.3f} (mis-measured?)")
    try:
        cov = np.linalg.inv((jac.T / sig**2) @ jac)
        dop = math.sqrt(float(np.trace(np.linalg.inv(jac.T @ jac))))
    except np.linalg.LinAlgError as exc:
        raise TrilaterationError("degenerate geometry (collinear points)") from exc
    return TriResult(
        pos=(float(p[0]), float(p[1])),
        residuals=tuple(float(r) for r in res),
        rms=rms,
        cov=((float(cov[0, 0]), float(cov[0, 1])), (float(cov[1, 0]), float(cov[1, 1]))),
        sigma=math.sqrt(float(np.trace(cov)) / 2.0),
        dop=dop,
        angle_deg=angle,
        iterations=iters,
    )
