"""2D constant-velocity Kalman filter and RTS smoother (design section 2.3).

State ``[e, n, ve, vn]`` in metres and m/s. Measurement noise comes from the
reported accuracy, floored at ``R_MIN_M`` by the caller. Small and explicit on
purpose: this is security-adjacent (it decides which fixes are believed).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .types import DT_MAX_S, DT_MIN_S, V0_SIGMA

_H_POS = np.array([[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]])
_H_VEL = np.array([[0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]])


def _transition(dt: float) -> np.ndarray:
    f = np.eye(4)
    f[0, 2] = f[1, 3] = dt
    return f


def _process_noise(dt: float, sigma_a: float) -> np.ndarray:
    """White-noise-acceleration blocks (dt^4/4, dt^3/2, dt^2) x sigma_a^2, per axis."""
    q = np.zeros((4, 4))
    s2 = sigma_a * sigma_a
    for i in (0, 1):
        q[i, i] = s2 * dt**4 / 4.0
        q[i, i + 2] = q[i + 2, i] = s2 * dt**3 / 2.0
        q[i + 2, i + 2] = s2 * dt**2
    return q


@dataclass
class Step:
    """One filter step, kept so a finished recording can be RTS-smoothed."""

    t: float
    x_pred: np.ndarray
    p_pred: np.ndarray
    x_post: np.ndarray
    p_post: np.ndarray
    f: np.ndarray
    chain_start: bool  # seed, re-seed or long gap: nothing is smoothed across it


class KalmanCV:
    """Forward filter. ``predict`` -> optional gate on ``innovation`` -> ``update``."""

    def __init__(self) -> None:
        self.x = np.zeros(4)
        self.p = np.eye(4)
        self.t: float | None = None
        self.chain_break = False  # set by predict() after a long gap

    @property
    def initialized(self) -> bool:
        return self.t is not None

    @property
    def sigma_m(self) -> float:
        return math.sqrt(float(self.p[0, 0] + self.p[1, 1]) / 2.0)

    @property
    def speed_mps(self) -> float:
        return math.hypot(float(self.x[2]), float(self.x[3]))

    def seed(self, t: float, e: float, n: float, r: float) -> None:
        """(Re)start at a measurement: ``P0 = diag(r^2, r^2, 2^2, 2^2)``, zero velocity."""
        self.x = np.array([e, n, 0.0, 0.0])
        self.p = np.diag([r * r, r * r, V0_SIGMA**2, V0_SIGMA**2])
        self.t = t

    def predict(self, t: float, sigma_a: float) -> tuple[np.ndarray, float]:
        """Advance to ``t``. Returns ``(F, dt_used)``; dt is clamped to [0.05, 5] s.

        A gap longer than the clamp keeps the position and re-seeds the velocity.
        """
        assert self.t is not None
        raw_dt = t - self.t
        self.chain_break = raw_dt > DT_MAX_S
        if self.chain_break:
            self.x[2:] = 0.0
            self.p[:2, 2:] = 0.0
            self.p[2:, :2] = 0.0
            self.p[2:, 2:] = np.eye(2) * V0_SIGMA**2
        dt = min(max(raw_dt, DT_MIN_S), DT_MAX_S)
        f = _transition(dt)
        self.x = f @ self.x
        self.p = f @ self.p @ f.T + _process_noise(dt, sigma_a)
        self.t = t
        return f, dt

    def predict_displacement(self, t: float, de: float, dn: float, sigma_a: float = 1.5) -> tuple[np.ndarray, float]:
        """Like :meth:`predict`, but the mean position moves by a step-counted displacement
        (PDR, design 2.8) instead of ``v * dt``. Covariance propagates as usual with the
        given acceleration noise. Returns ``(F, dt_used)``.
        """
        assert self.t is not None
        raw_dt = t - self.t
        self.chain_break = raw_dt > DT_MAX_S
        if self.chain_break:
            self.x[2:] = 0.0
            self.p[:2, 2:] = 0.0
            self.p[2:, :2] = 0.0
            self.p[2:, 2:] = np.eye(2) * V0_SIGMA**2
        dt = min(max(raw_dt, DT_MIN_S), DT_MAX_S)
        f = _transition(dt)
        self.x[0] += de
        self.x[1] += dn
        self.p = f @ self.p @ f.T + _process_noise(dt, sigma_a)
        self.t = t
        return f, dt

    def release_velocity(self) -> None:
        """Undo zero-velocity pinning when motion starts: velocity sigma back to ``V0_SIGMA``,
        decorrelated from the position (the mean is kept). Called before ``predict`` it acts
        as extra process noise on that step, so the RTS smoother stays valid.
        """
        self.p[:2, 2:] = 0.0
        self.p[2:, :2] = 0.0
        self.p[2:, 2:] = np.eye(2) * V0_SIGMA**2

    def innovation(self, e: float, n: float, r: float) -> tuple[np.ndarray, np.ndarray]:
        """Residual ``y`` and its covariance ``S`` for a position measurement."""
        y = np.array([e, n]) - _H_POS @ self.x
        s = _H_POS @ self.p @ _H_POS.T + np.eye(2) * (r * r)
        return y, s

    def update(self, e: float, n: float, r: float) -> None:
        self._joseph(_H_POS, np.array([e, n]), r)

    def update_zero_velocity(self, r_v: float) -> None:
        """ZUPT pseudo-measurement ``v = 0``."""
        self._joseph(_H_VEL, np.zeros(2), r_v)

    def _joseph(self, h: np.ndarray, z: np.ndarray, r: float) -> None:
        rm = np.eye(2) * (r * r)
        s = h @ self.p @ h.T + rm
        k = self.p @ h.T @ np.linalg.inv(s)
        i_kh = np.eye(4) - k @ h
        self.x = self.x + k @ (z - h @ self.x)
        self.p = i_kh @ self.p @ i_kh.T + k @ rm @ k.T  # Joseph form
        self.p = (self.p + self.p.T) / 2.0


def rts_smooth(steps: list[Step]) -> list[np.ndarray]:
    """Rauch-Tung-Striebel smoothed states for a finished recording (never used live)."""
    n = len(steps)
    out = [s.x_post.copy() for s in steps]
    for k in range(n - 2, -1, -1):
        nxt = steps[k + 1]
        if nxt.chain_start:
            continue
        gain = np.linalg.solve(nxt.p_pred.T, (steps[k].p_post @ nxt.f.T).T).T
        out[k] = steps[k].x_post + gain @ (out[k + 1] - nxt.x_pred)
    return out
