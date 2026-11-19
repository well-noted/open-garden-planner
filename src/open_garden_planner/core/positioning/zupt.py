"""Stationary detection for zero-velocity updates (design section 2.4)."""

from __future__ import annotations

import math
from collections import deque

import numpy as np

from .types import (
    ZUPT_ACCEL_VAR,
    ZUPT_DISP_ACC_FACTOR,
    ZUPT_DISP_WINDOW_S,
    ZUPT_EXIT_SAMPLES,
    ZUPT_MIN_DISP_M,
    ZUPT_SPEED_MPS,
    ZUPT_WINDOW_S,
)


class StationaryDetector:
    """Stationary when, over >= 3 s, every reported speed is < 0.3 m/s, or the net
    displacement over a 6 s window is small (see :meth:`_evaluate`), and (when there are
    >= 5 accelerometer samples) the acceleration-magnitude variance is < 0.05.

    Entering is immediate; leaving needs ``ZUPT_EXIT_SAMPLES`` (2) consecutive
    non-stationary evaluations (hysteresis). ``exited`` is set on the evaluation that drops
    the flag, and ``last_still_t`` keeps the time of the last stationary evaluation: the
    evidence is window-based, so a walk can have begun up to ``ZUPT_WINDOW_S`` before it
    is lost, and the engine uses both to leave the stationary state without gating the
    first walking fixes.
    """

    def __init__(self) -> None:
        self._fixes: deque[tuple[float, float, float, float, float | None]] = deque()
        self._accel: deque[tuple[float, float]] = deque()
        self.stationary = False
        self.exited = False
        self.last_still_t: float | None = None
        self._exit_count = 0

    def add_accel(self, t: float, ax: float, ay: float, az: float) -> None:
        if all(math.isfinite(v) for v in (t, ax, ay, az)):
            self._accel.append((t, math.sqrt(ax * ax + ay * ay + az * az)))
            while self._accel and t - self._accel[0][0] > ZUPT_WINDOW_S:
                self._accel.popleft()

    def update(self, t: float, e: float, n: float, accuracy_m: float, speed: float | None) -> bool:
        """Feed an accepted fix; returns the (hysteresis-filtered) stationary flag."""
        sp = speed if speed is not None and math.isfinite(speed) else None
        self._fixes.append((t, e, n, accuracy_m, sp))
        while self._fixes and t - self._fixes[0][0] > ZUPT_DISP_WINDOW_S:
            self._fixes.popleft()
        self.exited = False
        if self._evaluate(t):
            self.stationary = True
            self.last_still_t = t
            self._exit_count = 0
        elif self.stationary:
            self._exit_count += 1
            if self._exit_count >= ZUPT_EXIT_SAMPLES:
                self.stationary = False
                self.exited = True
                self._exit_count = 0
        return self.stationary

    def reset(self) -> None:
        self._fixes.clear()
        self.stationary = False
        self.exited = False
        self.last_still_t = None
        self._exit_count = 0

    def _evaluate(self, t: float) -> bool:
        """Stationary evidence: (a) reported speeds all slow over 3 s, or (b) no net displacement.

        Deviation from the design, found in the replay harness: a raw bounding-box extent over
        3 s below ``1.5 x accuracy`` is satisfied by a walker (1.3 m/s x 3 s = 3.9 m < 6 m at
        4 m accuracy), which froze the filter mid-walk. (b) therefore compares the MEAN of the
        first and second half of a 6 s window (noise averages down by sqrt(n_half)) against
        ``max(1 m, accuracy / sqrt(n_half))``. A reported speed >= 0.3 m/s vetoes both.
        """
        fx = self._fixes
        recent = [f for f in fx if t - f[0] <= ZUPT_WINDOW_S]
        if len(recent) < 2 or t - recent[0][0] < ZUPT_WINDOW_S * 0.9:
            return False  # not enough history yet
        speeds = [f[4] for f in recent if f[4] is not None]
        if any(sp >= ZUPT_SPEED_MPS for sp in speeds):
            return False  # a reported speed is direct evidence of motion
        slow = len(speeds) == len(recent)
        small = False
        if len(fx) >= 6 and t - fx[0][0] >= ZUPT_DISP_WINDOW_S * 0.9:
            pts = np.array([(f[1], f[2]) for f in fx])
            half = len(pts) // 2
            net = float(np.linalg.norm(pts[half:].mean(axis=0) - pts[:half].mean(axis=0)))
            med_acc = float(np.median([f[3] for f in fx]))
            small = net < max(ZUPT_MIN_DISP_M, ZUPT_DISP_ACC_FACTOR * med_acc / math.sqrt(half))
        if not (slow or small):
            return False
        # IMU veto only when there is enough accelerometer history to judge it.
        return not (len(self._accel) >= 5 and float(np.var([a[1] for a in self._accel])) >= ZUPT_ACCEL_VAR)
