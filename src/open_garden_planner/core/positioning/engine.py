"""PositioningEngine: the one object the Field Remote server drives (design section 2).

Pipeline per GNSS fix: validate -> georef ENU -> anchor -> Kalman predict ->
gate -> update (+ ZUPT) -> estimate. State is session-only; time comes from
the samples. VIO fusion is WP-H: :meth:`add_vio` only records the latest pose,
so ``fix_mode`` never reports ``vio_fused`` from this module.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, replace

import numpy as np

from ..georef import GeoReference, GeorefError
from . import calibration as calib
from .breadcrumbs import Point, select_breadcrumbs, simplify_track
from .closure import ClosureResult, bowditch, path_length
from .gating import RejectTracker, gate_measurement, validate_gnss
from .kalman import KalmanCV, Step, rts_smooth
from .survey import SurveyResult, survey_in
from .types import (
    ACC_JUMP_RATIO,
    ANCHOR_FIT_OK_RMS_M,
    ANCHOR_STALE_S,
    R_INFLATE,
    R_MIN_M,
    RESEED_P_SCALE,
    RTK_FIXED_ACC_M,
    RTK_FLOAT_ACC_M,
    RTK_SUSTAIN_SAMPLES,
    SIGMA_A_STILL,
    SIGMA_A_WALK,
    ZUPT_R_V,
    ZUPT_WINDOW_S,
    FixMode,
    GnssSample,
    ImuSample,
    PositionEstimate,
    VioSample,
)
from .zupt import StationaryDetector

MAX_TRACK_STEPS = 20000
MOVING_SPEED_MPS = 0.5


@dataclass(frozen=True)
class TrackResult:
    points_enu: tuple[Point, ...]
    points_cm: tuple[Point, ...]
    length_m: float
    epsilon_m: float
    n_raw: int  # filter steps recorded
    smoothed: bool
    closure: ClosureResult | None
    truncated: bool


class PositioningEngine:
    def __init__(self, georef: GeoReference) -> None:
        self.georef = georef
        self.last_reject_reason: str | None = None
        self.reseed_count = 0
        self.latest_vio: VioSample | None = None
        self._kf = KalmanCV()
        self._zupt = StationaryDetector()
        self._rejects = RejectTracker()
        self._anchor: calib.Anchor | None = None
        self._reseed_pending = False
        self._last_t: float | None = None
        self._last_acc: tuple[float, float] | None = None  # (t, reported accuracy)
        self._recent_acc: deque[float] = deque(maxlen=RTK_SUSTAIN_SAMPLES)
        self._last_sample: GnssSample | None = None
        self._last_estimate: PositionEstimate | None = None
        self._compass: tuple[float, float] | None = None  # (t, heading) from an absolute source
        self._recording = False
        self._steps: list[Step] = []
        self._rec_meta: list[tuple[float, bool]] = []  # (sigma, stationary) per step
        self._truncated = False

    # ---------------------------------------------------------------- inputs

    def add_gnss(self, s: GnssSample) -> PositionEstimate | None:
        """Process one fix. Returns the estimate (``gated=True`` if the fix was refused),
        or ``None`` while no position has ever been accepted. See ``last_reject_reason``.
        """
        self.last_reject_reason = validate_gnss(s, self._last_t)
        if self.last_reject_reason is not None:
            return self._gated_estimate(s.t if math.isfinite(s.t) else None)
        try:
            e0, n0 = self.georef.gps_to_enu(s.lat, s.lon)
            e, n = self._anchor.apply(e0, n0) if self._anchor else (e0, n0)
        except GeorefError:
            self.last_reject_reason = "bad_coordinates"
            return self._gated_estimate(None)
        if not (math.isfinite(e) and math.isfinite(n)):
            self.last_reject_reason = "non_finite"
            return self._gated_estimate(None)

        self._last_t = s.t
        r = max(s.accuracy_m, R_MIN_M)
        if self._last_acc and s.t - self._last_acc[0] <= 1.0 and s.accuracy_m > ACC_JUMP_RATIO * self._last_acc[1]:
            r *= R_INFLATE  # accuracy collapsed in a second: believe the fix less
        self._last_acc = (s.t, s.accuracy_m)
        self._last_sample = s

        kf = self._kf
        if not kf.initialized or self._reseed_pending:
            self._reseed_pending = False
            self._seed(s, e, n, r)
            return self._estimate(s, gated=False)

        prev_pos = kf.x[:2].copy()
        # Leaving the stationary state (review P1-1). The ZUPT pinned the velocity, and the
        # window-based evidence lags a walk that began by up to ZUPT_WINDOW_S, so without the
        # two measures below the first walking fixes were gated and only the three-reject
        # reseed recovered (a position jump). 1) On the exit, re-open the velocity, before
        # predict so the prediction re-correlates it with the position. 2) Within the window
        # of the last stationary evidence, the implied-speed gate counts from the earliest
        # moment the walk could have begun, not from the last fix.
        if self._zupt.exited:
            kf.release_velocity()
            self._zupt.exited = False  # once, even if this fix is then gated
        sigma_a = SIGMA_A_STILL if self._zupt.stationary else SIGMA_A_WALK
        f, dt = kf.predict(s.t, sigma_a)
        gate_dt = dt
        still_t = self._zupt.last_still_t
        if still_t is not None and s.t - still_t <= ZUPT_WINDOW_S:
            gate_dt = max(dt, s.t - (still_t - ZUPT_WINDOW_S))  # motion may predate the exit
        x_pred, p_pred = kf.x.copy(), kf.p.copy()
        y, cov = kf.innovation(e, n, r)
        reason = gate_measurement(
            y, cov, jump_m=math.dist((e, n), (prev_pos[0], prev_pos[1])), r=r, dt=gate_dt
        )
        if reason is not None:
            self.last_reject_reason = reason
            if self._rejects.reject(s.t):  # the filter, not the fix, is wrong
                self._seed(s, e, n, RESEED_P_SCALE * r)
                self.reseed_count += 1
                return self._estimate(s, gated=False)
            self._record(Step(s.t, x_pred, p_pred, x_pred, p_pred, f, kf.chain_break))
            return self._estimate(s, gated=True)

        self._rejects.accept()
        kf.update(e, n, r)
        if self._zupt.update(s.t, e, n, s.accuracy_m, s.speed_mps):
            kf.update_zero_velocity(ZUPT_R_V)
        self._recent_acc.append(s.accuracy_m)
        self._record(Step(s.t, x_pred, p_pred, kf.x.copy(), kf.p.copy(), f, kf.chain_break))
        return self._estimate(s, gated=False)

    def add_vio(self, v: VioSample) -> None:
        """Hook for WP-H: remembers the latest pose only. No fusion happens here."""
        self.latest_vio = v

    def add_imu(self, m: ImuSample) -> None:
        """Feeds the ZUPT acceleration test and the compass heading (absolute sources only)."""
        self._zupt.add_accel(m.t, m.ax, m.ay, m.az)
        if m.absolute and m.heading_deg is not None and math.isfinite(m.heading_deg):
            self._compass = (m.t, m.heading_deg % 360.0)

    # ----------------------------------------------------- calibration/survey

    def survey_in(self, samples: Sequence[GnssSample]) -> SurveyResult:
        """Stand-still average in the RAW device frame (use it for calibration points)."""
        return survey_in(samples, self.georef)

    def set_anchor(self, plan_xy_cm: tuple[float, float], survey: SurveyResult, t: float) -> None:
        """1-point "calibrate here": translation only, session state."""
        self._install(calib.calibrate_one_point(plan_xy_cm, survey, self.georef, t))

    def set_anchor_points(
        self,
        pairs: Sequence[tuple[tuple[float, float], SurveyResult]],
        t: float,
        *,
        allow_affine: bool = False,
    ) -> calib.FitResult:
        """2-4 point calibration; raises :class:`calibration.CalibrationError` on a bad set."""
        anchor, fit = calib.calibrate_points(pairs, self.georef, t, allow_affine=allow_affine)
        self._install(anchor)
        return fit

    def clear_anchor(self) -> None:
        self._anchor = None
        self._reseed_pending = True

    def _install(self, anchor: calib.Anchor) -> None:
        self._anchor = anchor
        self._reseed_pending = True  # the frame changed: the next fix re-seeds, never gates

    # ------------------------------------------------------------- recording

    def start_track(self) -> None:
        self._recording = True
        self._steps = []
        self._rec_meta = []
        self._truncated = False

    def track_preview(self) -> list[Point]:
        """Unsimplified filtered breadcrumbs (live phone preview)."""
        pts = [(float(s.x_post[0]), float(s.x_post[1])) for s in self._steps]
        keep = select_breadcrumbs(pts, [m[0] for m in self._rec_meta], [m[1] for m in self._rec_meta])
        return [pts[i] for i in keep]

    def finish_track(
        self,
        *,
        smooth: bool = True,
        close: str | None = None,
        target_plan_cm: tuple[float, float] | None = None,
    ) -> TrackResult:
        """Smooth, optionally Bowditch-close (``"start"`` or ``"point"``), simplify, return.

        Raises :class:`closure.ClosureError` when closure is requested but refused.
        """
        steps = self._steps
        self._recording = False
        if not steps:
            return TrackResult((), (), 0.0, 0.0, 0, smooth, None, self._truncated)
        states = rts_smooth(steps) if smooth else [s.x_post for s in steps]
        pts = [(float(x[0]), float(x[1])) for x in states]
        sig = [m[0] for m in self._rec_meta]
        keep = select_breadcrumbs(pts, sig, [m[1] for m in self._rec_meta])
        crumbs = [pts[i] for i in keep]
        crumb_sig = [sig[i] for i in keep]
        closure: ClosureResult | None = None
        if close is not None:
            target: Point | None = None
            if close == "point":
                if target_plan_cm is None:
                    raise ValueError("close='point' needs target_plan_cm")
                target = calib._plan_enu(self.georef, *target_plan_cm)
            elif close != "start":
                raise ValueError("close must be 'start', 'point' or None")
            closure = bowditch(crumbs, target)
            crumbs = list(closure.points)
        simple, eps = simplify_track(crumbs, crumb_sig)
        cm = tuple(self._to_cm(e, n) for e, n in simple)
        return TrackResult(tuple(simple), cm, path_length(simple), eps, len(steps), smooth, closure, self._truncated)

    # ---------------------------------------------------------------- helpers

    @property
    def anchor(self) -> calib.Anchor | None:
        return self._anchor

    def _seed(self, s: GnssSample, e: float, n: float, r: float) -> None:
        self._kf.seed(s.t, e, n, r)
        self._zupt.reset()
        self._rejects.accept()
        x = self._kf.x.copy()
        self._record(Step(s.t, x, self._kf.p.copy(), x, self._kf.p.copy(), np.eye(4), True))

    def _record(self, step: Step) -> None:
        if not self._recording:
            return
        if len(self._steps) >= MAX_TRACK_STEPS:
            self._truncated = True
            return
        self._steps.append(step)
        self._rec_meta.append((math.sqrt(float(step.p_post[0, 0] + step.p_post[1, 1]) / 2.0), self._zupt.stationary))

    def _to_cm(self, e: float, n: float) -> Point:
        return self.georef.gps_to_canvas(*self.georef.enu_to_gps(e, n))

    def _fix_mode(self, t: float) -> FixMode:
        acc = self._recent_acc
        if len(acc) == RTK_SUSTAIN_SAMPLES:
            if max(acc) < RTK_FIXED_ACC_M:
                return FixMode.RTK_FIXED
            if max(acc) < RTK_FLOAT_ACC_M:
                return FixMode.RTK_FLOAT
        a = self._anchor
        if a is not None and (
            t - a.t < ANCHOR_STALE_S or (a.fit_rms_m is not None and a.fit_rms_m < ANCHOR_FIT_OK_RMS_M)
        ):
            return FixMode.ANCHOR_CALIBRATED
        return FixMode.STANDARD

    def _estimate(self, s: GnssSample, *, gated: bool) -> PositionEstimate:
        kf = self._kf
        e, n = float(kf.x[0]), float(kf.x[1])
        x_cm, y_cm = self._to_cm(e, n)
        lat, lon = self.georef.enu_to_gps(e, n)
        stationary = self._zupt.stationary
        speed = 0.0 if stationary else kf.speed_mps
        heading: float | None = None
        source = "none"
        if speed > MOVING_SPEED_MPS:
            if s.heading_deg is not None and math.isfinite(s.heading_deg):
                heading = s.heading_deg % 360.0
            else:
                heading = math.degrees(math.atan2(float(kf.x[2]), float(kf.x[3]))) % 360.0
            source = "gnss"
        elif self._compass is not None and s.t - self._compass[0] <= 2.0:
            heading, source = self._compass[1], "compass"
        sigma = kf.sigma_m
        a = self._anchor
        est = PositionEstimate(
            t=s.t,
            e_m=e,
            n_m=n,
            x_cm=x_cm,
            y_cm=y_cm,
            lat=lat,
            lon=lon,
            sigma_m=sigma,
            heading_deg=heading,
            speed_mps=speed,
            fix_mode=self._fix_mode(s.t),
            stationary=stationary,
            quality="good" if sigma <= 2.0 else "fair" if sigma <= 5.0 else "poor",
            gated=gated,
            heading_source=source,
            anchor_source=a.kind if a else "none",
            anchor_age_s=(s.t - a.t) if a else None,
            horizontal_accuracy_m=s.accuracy_m,
        )
        self._last_estimate = est
        return est

    def _gated_estimate(self, t: float | None) -> PositionEstimate | None:
        """The last estimate re-flagged ``gated`` (input never reached the filter)."""
        est = self._last_estimate
        if est is None:
            return None
        return replace(est, gated=True, t=est.t if t is None else t)

