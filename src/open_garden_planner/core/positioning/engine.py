"""PositioningEngine: the one object the Field Remote server drives (design section 2).

Pipeline per GNSS fix: validate -> georef ENU -> anchor -> Kalman predict ->
gate -> update (+ ZUPT) -> estimate. State is session-only; time comes from
the samples. VIO fusion (WP-H, ``fusion.py``): :meth:`add_vio` feeds a plan-locked
aligner whose positions replace the Kalman track only while locked, tracking and the
scale check passed (``fix_mode`` ``vio_fused``). VIO never modifies the GNSS filter;
without a lock a GNSS-aligned fallback is reported as telemetry only.
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
from .fusion import (
    FusedFix,
    FusionAligner,
    FusionState,
    PdrStepper,
    PlanLockedAligner,
    Transform2D,
)
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
VIO_FRESH_S = 1.0  # a VIO fix older than this no longer drives the track
PDR_VIO_QUIET_S = 2.0  # steps feed the prediction only when VIO has been silent this long
COMPASS_FRESH_S = 2.0
PDR_SIGMA_A = 1.5
VIO_RECORD_MIN_DT_S = 0.5  # VIO steps enter a recording at most 2 per second (MAX_TRACK_STEPS lasts hours)


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
        self._locked = PlanLockedAligner()
        self._fallback = FusionAligner()
        self._pdr = PdrStepper()
        self._vio_fix: FusedFix | None = None
        self._vio_estimate_last: PositionEstimate | None = None
        self._last_vio_t: float | None = None
        self._fallback_fix: FusedFix | None = None
        self._pending_disp: tuple[float, float] | None = None
        self._motion_model: str | None = None
        self._rec_vio: list[bool] = []  # per recorded step: from VIO (True) or the GNSS filter
        self._rec_seq: list[int] = []  # per GNSS step: its index in the filter sequence (-1 for VIO)
        self._gnss_seq = 0
        self._vio_spans: list[list[float]] = []  # [first t, last t] of each run of fresh VIO fixes
        self._rec_last_vio_t: float | None = None

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
        disp = self._take_pdr_displacement(s.t)
        if disp is not None:
            f, dt = kf.predict_displacement(s.t, disp[0], disp[1], PDR_SIGMA_A)
        else:
            f, dt = kf.predict(s.t, sigma_a)
        self._motion_model = "motion-smoothed" if disp is not None else None
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

    def add_vio(self, v: VioSample) -> PositionEstimate | None:
        """Feed one VIO pose to both aligners.

        Returns the plan-locked estimate (``track_source="vio"``) while locked, tracking
        and the scale check passed; ``None`` otherwise (the Kalman track then stays the
        source). After a loss the lock is gone until re-locked. A brief break is bridged
        only when a walker could have made the jump: across a pose gap (> ``POSE_GAP_S``
        between tracking poses) or a recovery from limited/lost (measured from the last
        tracking pose), a jump above ``WALK_SPEED_MPS * gap + WALK_SLACK_M`` unlocks with
        ``vio_pose_jump``; so does any implied speed above ``MAX_VIO_SPEED_MPS``.
        """
        self.latest_vio = v
        self._last_vio_t = v.t
        fix = self._locked.on_sample(v)
        self._fallback.on_vio(v)
        if fix is not None and fix.fix_mode is FixMode.VIO_FUSED:
            prev = self._vio_estimate_last if self._vio_fix is not None else None
            self._vio_fix = fix
            est = self._vio_estimate(fix, prev)
            self._vio_estimate_last = est
            if self._recording:
                self._record_vio(fix)
            return est
        self._vio_fix = None
        self._vio_estimate_last = None
        return None

    def add_imu(self, m: ImuSample) -> None:
        """Feeds the ZUPT acceleration test, the compass heading (absolute sources only)
        and the step detector (a displacement prior only while VIO is silent)."""
        self._zupt.add_accel(m.t, m.ax, m.ay, m.az)
        if m.absolute and m.heading_deg is not None and math.isfinite(m.heading_deg):
            self._compass = (m.t, m.heading_deg % 360.0)
        comp = self._compass
        heading = comp[1] if comp is not None and m.t - comp[0] <= COMPASS_FRESH_S else None
        step = self._pdr.update(m, heading)
        if (
            step is not None
            and step.heading_deg is not None
            and not self._zupt.stationary
            and (self._last_vio_t is None or m.t - self._last_vio_t > PDR_VIO_QUIET_S)
        ):
            de, dn = self._pending_disp or (0.0, 0.0)
            self._pending_disp = (de + step.de, dn + step.dn)

    # ------------------------------------------------------------ AR (WP-H)

    def ar_capture_a(
        self, hit_xz: tuple[float, float], plan_en: tuple[float, float], vio_t: float | None = None
    ) -> None:
        """Lock point A: the reticle hit (x, z) in the VIO frame and its plan ENU position.

        ``vio_t`` is the last pose the phone had sent when it took the hit: refused when the
        pose stream has not reached it or the frame broke after it.
        """
        self._locked.capture_a(hit_xz, plan_en, vio_t)
        self._vio_fix = None
        self._vio_estimate_last = None

    def ar_capture_b(
        self, hit_xz: tuple[float, float], plan_en: tuple[float, float], vio_t: float | None = None
    ) -> Transform2D:
        """Lock point B and fit the frame. ``FusionError`` when the points are unusable.

        The lock time is the last VIO sample's time, from the stream, never a client clock."""
        return self._locked.capture_b(hit_xz, plan_en, vio_t)

    def ar_unlock(self, reason: str = "user") -> None:
        self._locked.unlock(reason)
        self._vio_fix = None
        self._vio_estimate_last = None

    def ar_frame_reset(self, t: float) -> None:
        """The phone reset its reference space at ``t``: both aligners forget the old frame."""
        self._locked.frame_reset(t)
        self._fallback.reset()
        self._fallback_fix = None
        self._vio_fix = None
        self._vio_estimate_last = None

    def ar_closure(
        self, hit_xz: tuple[float, float], plan_en: tuple[float, float], vio_t: float | None = None
    ) -> float:
        """Free residual at a third known point, metres ("closure at C")."""
        return self._locked.third_point_residual(hit_xz, plan_en, vio_t)

    def fused_estimate(self, t: float) -> PositionEstimate | None:
        """The plan-locked VIO estimate when it is no older than ``VIO_FRESH_S`` at ``t``."""
        est, vf = self._vio_estimate_last, self._vio_fix
        if vf is not None and est is not None and abs(t - vf.t) <= VIO_FRESH_S:
            return est
        return None

    def ar_status(self, t: float | None = None, *, stream_stale: bool = False) -> dict[str, object]:
        """The lock state for telemetry. ``t`` is the newest sample time of the request (the
        latest VIO pose when omitted); ``stream_stale`` says the server has heard no pose for a
        while (a backgrounded phone sends nothing, so the sample clock alone cannot tell)."""
        lk = self._locked
        sc = lk.scale
        ref_t = t if t is not None else self._last_vio_t
        fresh = not stream_stale and ref_t is not None and self.fused_estimate(ref_t) is not None
        if fresh:
            source = "plan_locked"
        elif not stream_stale and self._fallback_fix is not None and self._fallback.transform is not None:
            source = "gnss_aligned"
        else:
            source = "gnss"
        return {
            "state": str(lk.state),
            "source": source,
            "fix_mode": str(FixMode.VIO_FUSED) if fresh else None,
            "reasons": list(lk.last_fix.reasons) if lk.last_fix is not None else [],
            "scale": None
            if sc is None
            else {
                "ratio": round(sc.ratio, 4) if math.isfinite(sc.ratio) else None,
                "ok": sc.ok,
                "message": sc.message,
            },
            "closure_m": None if lk.closure_m is None else round(lk.closure_m, 3),
            "gnss_alert": lk.gnss_alert,
            "unlock_reason": lk.unlock_reason,
            "lock_t": lk.lock_t,
            "sigma_m": lk.anchor_sigma_m if fresh else None,
            "relative_sigma_m_per_10m": None,
            "fallback_reasons": list(self._fallback.reasons),
        }

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
        self._rec_vio = []
        self._rec_seq = []
        self._vio_spans = []
        self._rec_last_vio_t = None
        self._truncated = False

    def track_preview(self) -> list[Point]:
        """Unsimplified filtered breadcrumbs (live phone preview)."""
        steps, meta = self._segmented()
        pts = [(float(s.x_post[0]), float(s.x_post[1])) for s in steps]
        keep = select_breadcrumbs(pts, [m[0] for m in meta], [m[1] for m in meta])
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
        steps, meta = self._segmented()
        self._recording = False
        if not steps:
            return TrackResult((), (), 0.0, 0.0, 0, smooth, None, self._truncated)
        states = rts_smooth(steps) if smooth else [s.x_post for s in steps]
        pts = [(float(x[0]), float(x[1])) for x in states]
        sig = [m[0] for m in meta]
        keep = select_breadcrumbs(pts, sig, [m[1] for m in meta])
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

    def _in_vio_span(self, t: float) -> bool:
        return any(a <= t <= b + VIO_FRESH_S for a, b in self._vio_spans)

    def _record_vio(self, fix: FusedFix) -> None:
        """Note the VIO coverage on every fix; keep a step at most every ``VIO_RECORD_MIN_DT_S``."""
        spans = self._vio_spans
        new_span = not spans or fix.t - spans[-1][1] > VIO_FRESH_S or fix.t < spans[-1][0]
        if new_span:
            spans.append([fix.t, fix.t])
        else:
            spans[-1][1] = max(spans[-1][1], fix.t)
        last = self._rec_last_vio_t
        if not new_span and last is not None and 0.0 <= fix.t - last < VIO_RECORD_MIN_DT_S:
            return
        self._rec_last_vio_t = fix.t
        x = np.array([fix.e_m, fix.n_m, 0.0, 0.0])
        p = np.eye(4) * (fix.sigma_m**2)
        self._record(Step(fix.t, x, p, x.copy(), p.copy(), np.eye(4), True), vio=True)

    def _record(self, step: Step, *, vio: bool = False) -> None:
        if not self._recording:
            return
        seq = -1
        if not vio:
            seq = self._gnss_seq
            self._gnss_seq += 1
            if self._in_vio_span(step.t):
                return  # the VIO track is the source there; finish re-checks late-arriving VIO
        if len(self._steps) >= MAX_TRACK_STEPS:
            self._truncated = True
            return
        self._steps.append(step)
        self._rec_vio.append(vio)
        self._rec_seq.append(seq)
        self._rec_meta.append((math.sqrt(float(step.p_post[0, 0] + step.p_post[1, 1]) / 2.0), self._zupt.stationary))

    def _segmented(self) -> tuple[list[Step], list[tuple[float, bool]]]:
        """The recording as one source at a time, whatever order the requests arrived in.

        GNSS and VIO come in separate requests, so a VIO span can arrive after the GNSS steps
        it covers: those GNSS steps are dropped here, the rest is ordered by time, and nothing
        is smoothed across a change of source or across a dropped GNSS step.
        """
        rows = [
            (s, m, v, q)
            for s, m, v, q in zip(self._steps, self._rec_meta, self._rec_vio, self._rec_seq, strict=True)
            if v or not self._in_vio_span(s.t)
        ]
        rows.sort(key=lambda r: r[0].t)
        steps: list[Step] = []
        prev_vio: bool | None = None
        prev_seq: int | None = None
        for s, _m, v, q in rows:
            gap = not v and prev_seq is not None and q != prev_seq + 1
            brk = prev_vio is None or v != prev_vio or gap
            steps.append(replace(s, chain_start=True) if brk and not s.chain_start else s)
            prev_vio = v
            prev_seq = None if v else q
        return steps, [r[1] for r in rows]

    def _to_cm(self, e: float, n: float) -> Point:
        return self.georef.gps_to_canvas(*self.georef.enu_to_gps(e, n))

    def _take_pdr_displacement(self, t: float) -> tuple[float, float] | None:
        disp, self._pending_disp = self._pending_disp, None
        if disp is None or (self._last_vio_t is not None and t - self._last_vio_t <= PDR_VIO_QUIET_S):
            return None
        return disp

    def _vio_estimate(self, fix: FusedFix, prev: PositionEstimate | None) -> PositionEstimate:
        e, n = fix.e_m, fix.n_m
        x_cm, y_cm = self._to_cm(e, n)
        lat, lon = self.georef.enu_to_gps(e, n)
        speed = 0.0
        if prev is not None and fix.t > prev.t:
            speed = min(math.dist((e, n), (prev.e_m, prev.n_m)) / (fix.t - prev.t), 10.0)
        return PositionEstimate(
            t=fix.t,
            e_m=e,
            n_m=n,
            x_cm=x_cm,
            y_cm=y_cm,
            lat=lat,
            lon=lon,
            sigma_m=fix.sigma_m,
            heading_deg=None,
            speed_mps=speed,
            fix_mode=FixMode.VIO_FUSED,  # never an RTK label: the position is not from GNSS
            stationary=speed < 0.1,
            quality="unknown",  # the plan-lock sigma is a placeholder until WP-M: no quality is implied
            gated=False,
            heading_source="none",
            anchor_source="plan_points",
            anchor_age_s=None if fix.anchor_t is None else fix.t - fix.anchor_t,
            track_source="vio",
        )

    def _on_filtered(self, s: GnssSample) -> None:
        """After the filter accepted a fix: the gross-error check (locked) or the fallback fit."""
        kf = self._kf
        e, n = float(kf.x[0]), float(kf.x[1])
        if self._locked.state in (FusionState.LOCKED, FusionState.LOCKED_LIMITED):
            self._locked.gnss_disagreement((e, n), kf.sigma_m)
        else:
            self._fallback_fix = self._fallback.add_gnss(s.t, e, n, kf.sigma_m)

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
        if not gated:
            self._on_filtered(s)
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
            motion_model=self._motion_model,
        )
        self._last_estimate = est
        return est

    def _gated_estimate(self, t: float | None) -> PositionEstimate | None:
        """The last estimate re-flagged ``gated`` (input never reached the filter)."""
        est = self._last_estimate
        if est is None:
            return None
        return replace(est, gated=True, t=est.t if t is None else t)

