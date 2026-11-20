"""VIO and PDR fusion (design section 2.8, WP-H).

Pure numpy, no Qt, no I/O. No accuracy is claimed by anything in this module: every sigma is
either a filter spread or a labelled placeholder until WP-M measures it.

Frames: WebXR is right-handed (x right, y up, -z forward). The ground plane is (x, z); it is
mapped to the right-handed 2D frame (u, v) = (x, -z) so that a yaw-only rotation reaches plan
ENU (east, north) without a reflection.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from open_garden_planner.core.units import format_length

from .types import FixMode, ImuSample, VioSample

Point2 = tuple[float, float]


def _metres(m: float) -> str:
    """Diagnostic text for a distance held in metres, through the shared length formatter."""
    return format_length(m * 100.0)

# --- tuning (keep every number here) ---
LOCK_MIN_BASELINE_M = 2.0
SCALE_TOL = 0.02
TRACKING_LOSS_S = 1.0  # limited/lost longer than this (strictly) unlocks
MAX_VIO_SPEED_MPS = 5.0  # implied VIO speed above this is a reference-space jump
POSE_GAP_S = 0.5  # two tracking poses further apart than this (strictly) are a pose gap (poses come at 10 Hz)
WALK_SPEED_MPS = 2.0  # across a pose gap or a limited/lost recovery the jump may be at most
WALK_SLACK_M = 0.3  # WALK_SPEED_MPS * gap + WALK_SLACK_M, else it is a relocalisation (vio_pose_jump)
GNSS_ALERT_SIGMAS = 3.0
PLAN_POINT_SIGMA_M = 0.25  # PLACEHOLDER anchor sigma, not measured (WP-M replaces it)
VIO_PAIR_MAX_DT_S = 0.5
FB_WINDOW_S = 30.0
FB_WINDOW_M = 40.0
FB_MIN_EXTENT_M = 8.0
FB_MIN_POINTS = 10
FB_ALPHA = 0.1
FB_MAX_YAW_JUMP_RAD = math.radians(10.0)
FB_REJECTS_TO_RESET = 5
PDR_STEP_M = 0.7
PDR_REFRACTORY_S = 0.3
PDR_LP_TAU_S = 0.05
PDR_MEAN_TAU_S = 2.0
PDR_THRESHOLD_MPS2 = 1.0
MOTION_SMOOTHED = "motion-smoothed"


class FusionError(ValueError):
    """Lock points are unusable (degenerate, non-finite) or the aligner is in the wrong state."""


class FusionState(StrEnum):
    UNLOCKED = "unlocked"
    PENDING_B = "pending_b"  # A captured, waiting for B
    LOCKED = "locked"
    LOCKED_LIMITED = "locked_limited"  # locked, tracking limited/lost for <= 1 s
    GNSS_ALIGNED = "gnss_aligned"  # fallback alignment


def _wrap(a: float) -> float:
    return (a + math.pi) % (2.0 * math.pi) - math.pi


def _finite(*vals: float) -> bool:
    return all(math.isfinite(float(v)) for v in vals)


def _walk_bound(gap_s: float) -> float:
    """The largest ground jump a walker can make in ``gap_s`` seconds (metres)."""
    return WALK_SPEED_MPS * gap_s + WALK_SLACK_M


def vio_ground(sample: VioSample) -> Point2:
    """Ground-plane coordinates (x, z) of a WebXR pose; height y is ignored."""
    return float(sample.x), float(sample.z)


@dataclass(frozen=True)
class Transform2D:
    """Yaw-only rigid map, scale 1, from WebXR ground (x, z) to plan ENU (e, n).

    ``yaw_rad`` is counter-clockwise seen from above, applied to (u, v) = (x, -z).
    """

    yaw_rad: float
    tx: float
    ty: float

    def apply(self, x: float, z: float) -> Point2:
        u, v = x, -z
        c, s = math.cos(self.yaw_rad), math.sin(self.yaw_rad)
        return c * u - s * v + self.tx, s * u + c * v + self.ty

    def inverse(self, e: float, n: float) -> Point2:
        """Plan ENU (e, n) back to WebXR ground (x, z)."""
        c, s = math.cos(self.yaw_rad), math.sin(self.yaw_rad)
        de, dn = e - self.tx, n - self.ty
        u = c * de + s * dn
        v = -s * de + c * dn
        return u, -v


@dataclass(frozen=True)
class ScaleCheck:
    ratio: float
    ok: bool
    message: str


@dataclass(frozen=True)
class FusedFix:
    """One fused position in plan ENU. ``fix_mode`` is VIO_FUSED only when locked, tracking and scale ok."""

    t: float
    e_m: float
    n_m: float
    source: str  # "plan_locked" | "gnss_aligned"
    fix_mode: FixMode | None
    sigma_m: float
    state: FusionState
    anchor: str  # "plan_points_ab" | "gnss_window"
    anchor_t: float | None = None
    relative_sigma_m_per_10m: float | None = None  # null until WP-M
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PdrStep:
    t: float
    index: int
    de: float
    dn: float
    heading_deg: float | None
    label: str = MOTION_SMOOTHED
    reasons: tuple[str, ...] = ()


class _TrackingWatch:
    """Applies the loss rules; returns a reason when the VIO frame can no longer be trusted."""

    def __init__(self) -> None:
        self._bad_since: float | None = None
        self._bad_kind = "limited"
        self.last: VioSample | None = None
        self.last_tracking: VioSample | None = None  # the last pose taken while tracking was good
        self.break_t: float | None = None  # time of the last sample that broke the frame (any reason)

    def reset(self) -> None:
        self._bad_since = None
        self.last = None
        self.last_tracking = None

    def _mark_break(self, t: float) -> None:
        """Note a frame break at ``t``. The break time only ever moves forward."""
        if math.isfinite(t) and (self.break_t is None or t > self.break_t):
            self.break_t = t

    @property
    def good(self) -> bool:
        return self.last is not None and self.last.tracking == "tracking"

    def update(self, v: VioSample) -> str | None:
        if not _finite(v.t, v.x, v.z):
            if self.last is not None:
                self._mark_break(self.last.t)  # never lowers or clears a break already noted
            return "vio_non_finite"
        reason: str | None = None
        last = self.last
        if last is not None:
            dt = v.t - last.t
            if dt < 0.0:
                reason = "vio_time_reversed"
            elif dt > TRACKING_LOSS_S:
                reason = "vio_gap"
            elif dt > 0.0 and last.tracking == "tracking" and v.tracking == "tracking":
                jump = math.hypot(v.x - last.x, v.z - last.z)
                if jump / dt > MAX_VIO_SPEED_MPS or (dt > POSE_GAP_S and jump > _walk_bound(dt)):
                    reason = "vio_pose_jump"
        ref = self.last_tracking
        if reason is None and v.tracking == "tracking" and last is not None and last.tracking != "tracking":
            # Recovery from limited/lost: the system may have relocalised the frame while it was
            # not tracking. Judge the jump against the last *tracking* pose, never the limited ones,
            # and by walking speed: a relocalisation is spliced only if a walker could have made it.
            if self._bad_since is not None and v.t - self._bad_since > TRACKING_LOSS_S:
                reason = f"tracking_{self._bad_kind}"
            elif ref is not None:
                gap = v.t - ref.t
                if gap > TRACKING_LOSS_S:
                    reason = "vio_gap"
                elif gap > 0.0 and math.hypot(v.x - ref.x, v.z - ref.z) > _walk_bound(gap):
                    reason = "vio_pose_jump"
        if v.tracking == "tracking":
            self._bad_since = None
            self.last_tracking = v
        elif self._bad_since is None:
            self._bad_since = v.t
            self._bad_kind = v.tracking
        if reason is None and self._bad_since is not None and v.t - self._bad_since > TRACKING_LOSS_S:
            reason = f"tracking_{v.tracking}"
        if reason is not None:
            self._mark_break(v.t)
        self.last = v
        return reason


def _two_point(a_vio: Point2, a_plan: Point2, b_vio: Point2, b_plan: Point2) -> Transform2D:
    au, av = a_vio[0], -a_vio[1]
    bu, bv = b_vio[0], -b_vio[1]
    yaw = _wrap(
        math.atan2(b_plan[1] - a_plan[1], b_plan[0] - a_plan[0]) - math.atan2(bv - av, bu - au)
    )
    mu, mv = (au + bu) / 2.0, (av + bv) / 2.0
    me, mn = (a_plan[0] + b_plan[0]) / 2.0, (a_plan[1] + b_plan[1]) / 2.0
    c, s = math.cos(yaw), math.sin(yaw)
    return Transform2D(yaw, me - (c * mu - s * mv), mn - (s * mu + c * mv))


class PlanLockedAligner:
    """Primary source: two reticle-locked plan points, no pull toward GNSS, never spliced."""

    def __init__(self, anchor_sigma_m: float = PLAN_POINT_SIGMA_M) -> None:
        self.anchor_sigma_m = anchor_sigma_m
        self.transform: Transform2D | None = None
        self.state = FusionState.UNLOCKED
        self.scale: ScaleCheck | None = None
        self.closure_m: float | None = None
        self.gnss_alert: str | None = None
        self.unlock_reason: str | None = None
        self.lock_t: float | None = None
        self.last_fix: FusedFix | None = None
        self._lock_reasons: tuple[str, ...] = ()
        self._pending_a: tuple[Point2, Point2] | None = None
        self._watch = _TrackingWatch()

    # ---------------------------------------------------------------- locking

    @staticmethod
    def scale_check(a_vio: Point2, a_plan: Point2, b_vio: Point2, b_plan: Point2) -> ScaleCheck:
        dv = math.dist(a_vio, b_vio)
        dp = math.dist(a_plan, b_plan)
        if dp <= 0.0 or not _finite(dv, dp):
            return ScaleCheck(float("nan"), False, "plan points coincide: scale undefined")
        ratio = dv / dp
        if abs(ratio - 1.0) <= SCALE_TOL:
            return ScaleCheck(ratio, True, f"scale check ok: measured {_metres(dv)} vs plan {_metres(dp)}")
        return ScaleCheck(
            ratio,
            False,
            f"plan points disagree with the measured distance (measured {_metres(dv)} vs plan {_metres(dp)}, "
            f"{(ratio - 1.0) * 100.0:+.1f} %): bad plan point or tracking drift",
        )

    def lock(
        self, a_vio: Point2, a_plan: Point2, b_vio: Point2, b_plan: Point2, t: float | None = None
    ) -> Transform2D:
        """Fit the transform from two reticle hits (x, z) and their plan points (e, n)."""
        if not _finite(*a_vio, *a_plan, *b_vio, *b_plan):
            raise FusionError("lock points must be finite")
        if math.dist(a_vio, b_vio) < LOCK_MIN_BASELINE_M:
            raise FusionError(f"measured points A and B are closer than {LOCK_MIN_BASELINE_M:g} m")
        if math.dist(a_plan, b_plan) < LOCK_MIN_BASELINE_M:
            raise FusionError(f"plan points A and B are closer than {LOCK_MIN_BASELINE_M:g} m")
        tr = _two_point(a_vio, a_plan, b_vio, b_plan)
        self.scale = self.scale_check(a_vio, a_plan, b_vio, b_plan)
        self._lock_reasons = () if self.scale.ok else ("scale_check_failed",)
        self.transform = tr
        self.state = FusionState.LOCKED
        self.lock_t = t
        self.closure_m = None
        self.gnss_alert = None
        self.unlock_reason = None
        self._pending_a = None
        return tr

    def _check_stream(self, vio_t: float | None) -> None:
        """Refuse a lock point the pose stream cannot vouch for.

        ``vio_t`` is the time of the last pose the phone had sent when it took the reticle hit.
        The hit is in the frame of that pose, so the watch must have reached it, and nothing may
        have broken the frame (loss, jump, reset) after it.
        """
        if not self._watch.good:
            raise FusionError("tracking is not good: cannot capture a lock point")
        if vio_t is None:
            return
        if not _finite(vio_t):
            raise FusionError("lock point time must be finite")
        last = self._watch.last
        if last is None or last.t < vio_t - 1e-6:
            raise FusionError("the pose stream has not reached this lock point yet: try again")
        brk = self._watch.break_t
        if brk is not None and brk > vio_t + 1e-6:
            raise FusionError("tracking was lost or reset after this lock point was taken: lock again")

    def capture_a(self, hit_xz: Point2, plan_en: Point2, vio_t: float | None = None) -> None:
        self._check_stream(vio_t)
        if not _finite(*hit_xz, *plan_en):
            raise FusionError("lock points must be finite")
        if self.transform is not None or self._pending_a is not None:
            self.unlock("relock")  # an unlocked aligner keeps the reason it was unlocked for
        self._pending_a = (hit_xz, plan_en)
        self.state = FusionState.PENDING_B

    def capture_b(self, hit_xz: Point2, plan_en: Point2, vio_t: float | None = None) -> Transform2D:
        """Lock B. The lock time is the last pose the server holds, never a client clock."""
        if self._pending_a is None:
            raise FusionError("capture A first (A is dropped by any tracking loss)")
        self._check_stream(vio_t)
        a_vio, a_plan = self._pending_a
        last = self._watch.last
        return self.lock(a_vio, a_plan, hit_xz, plan_en, None if last is None else last.t)

    def frame_reset(self, t: float) -> None:
        """The phone's reference space was reset: drop the lock and forget the old frame."""
        self.unlock("reference_space_reset")
        self._watch.reset()
        self._watch._mark_break(t)

    def unlock(self, reason: str = "user") -> None:
        self.transform = None
        self._pending_a = None
        self.state = FusionState.UNLOCKED
        self.unlock_reason = reason
        self.last_fix = None
        self._lock_reasons = ()

    # ---------------------------------------------------------------- running

    def on_sample(self, v: VioSample) -> FusedFix | None:
        """Map one pose to plan ENU; ``None`` while unlocked (including right after a loss)."""
        reason = self._watch.update(v)
        if reason is not None and (self.transform is not None or self._pending_a is not None):
            self.unlock(reason)
        tr = self.transform
        if tr is None:
            return None
        good = v.tracking == "tracking"
        self.state = FusionState.LOCKED if good else FusionState.LOCKED_LIMITED
        e, n = tr.apply(*vio_ground(v))
        reasons = self._lock_reasons + (() if good else (f"tracking_{v.tracking}",))
        mode = FixMode.VIO_FUSED if good and not self._lock_reasons else None
        fix = FusedFix(
            t=v.t,
            e_m=e,
            n_m=n,
            source="plan_locked",
            fix_mode=mode,
            sigma_m=self.anchor_sigma_m,
            state=self.state,
            anchor="plan_points_ab",
            anchor_t=self.lock_t,
            reasons=reasons,
        )
        self.last_fix = fix
        return fix

    def third_point_residual(self, c_vio: Point2, c_plan: Point2, vio_t: float | None = None) -> float:
        """Free check at a third known point, reported as "closure at C" (metres)."""
        if self.transform is None:
            raise FusionError("not locked")
        if vio_t is not None:
            self._check_stream(vio_t)
        if not _finite(*c_vio, *c_plan):
            raise FusionError("closure point must be finite")
        self.closure_m = math.dist(self.transform.apply(*c_vio), c_plan)
        return self.closure_m

    def gnss_disagreement(self, est: Point2, sigma: float) -> str | None:
        """Gross-error check only: never moves the locked frame."""
        fix = self.last_fix
        if fix is None or not _finite(*est, sigma) or sigma <= 0.0:
            self.gnss_alert = None
            return None
        d = math.dist((fix.e_m, fix.n_m), est)
        self.gnss_alert = (
            f"locked frame and filtered GNSS disagree by {_metres(d)} (> {GNSS_ALERT_SIGMAS:g} x {_metres(sigma)})"
            if d > GNSS_ALERT_SIGMAS * sigma
            else None
        )
        return self.gnss_alert


class FusionAligner:
    """Fallback: weighted Umeyama (scale 1) of VIO vs filtered GNSS over a sliding window."""

    def __init__(self, yaw_hint_rad: float | None = None) -> None:
        self._yaw_hint = yaw_hint_rad
        self._watch = _TrackingWatch()
        self._win: deque[tuple[float, float, float, float, float, float]] = deque()  # t,u,v,e,n,w
        self._yaw: float | None = None
        self._t: np.ndarray | None = None
        self.yaw_observed = False
        self.sigma_m = float("nan")
        self.reasons: tuple[str, ...] = ()
        self._rejects = 0
        self._loss_since_accept = False

    @property
    def yaw(self) -> float | None:
        return self._yaw

    @property
    def transform(self) -> Transform2D | None:
        if self._yaw is None or self._t is None:
            return None
        return Transform2D(self._yaw, float(self._t[0]), float(self._t[1]))

    def reset(self) -> None:
        self._win.clear()
        self._yaw = None
        self._t = None
        self.yaw_observed = False
        self._rejects = 0
        self._loss_since_accept = False
        self.sigma_m = float("nan")

    def clear_window(self) -> None:
        """Drop the pairs but keep the current alignment (e.g. after a GNSS reseed)."""
        self._win.clear()

    def on_vio(self, v: VioSample) -> FusedFix | None:
        reason = self._watch.update(v)
        if reason is not None:
            self.reset()
            self.reasons = (reason,)
        if v.tracking != "tracking":
            self._loss_since_accept = True
        return self.position(v)

    def add_gnss(self, t: float, e: float, n: float, sigma: float) -> FusedFix | None:
        v = self._watch.last
        if v is None or v.tracking != "tracking" or abs(t - v.t) > VIO_PAIR_MAX_DT_S:
            return None
        if not _finite(t, e, n, sigma):
            return None
        w = 1.0 / max(sigma, 1e-3) ** 2
        self._win.append((t, v.x, -v.z, e, n, w))
        self._trim()
        self._fit()
        return self.position(v)

    def position(self, v: VioSample) -> FusedFix | None:
        tr = self.transform
        if tr is None:
            return None
        e, n = tr.apply(*vio_ground(v))
        return FusedFix(
            t=v.t,
            e_m=e,
            n_m=n,
            source="gnss_aligned",
            fix_mode=None,
            sigma_m=self.sigma_m,
            state=FusionState.GNSS_ALIGNED,
            anchor="gnss_window",
            reasons=self.reasons,
        )

    def _path_len(self) -> float:
        pts = [(u, v) for (_, u, v, _, _, _) in self._win]
        return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))

    def _trim(self) -> None:
        while len(self._win) > 1 and (
            self._win[-1][0] - self._win[0][0] > FB_WINDOW_S or self._path_len() > FB_WINDOW_M
        ):
            self._win.popleft()

    def _fit(self) -> None:
        arr = np.array(self._win, dtype=float)
        p, q, w = arr[:, 1:3], arr[:, 3:5], arr[:, 5]
        ws = w / w.sum()
        cp, cq = ws @ p, ws @ q
        diff = p[:, None, :] - p[None, :, :]
        extent = float(np.sqrt((diff**2).sum(axis=2)).max())
        reasons: list[str] = []
        reinit_t = self._t is None
        if len(arr) >= FB_MIN_POINTS and extent >= FB_MIN_EXTENT_M:
            a, b = p - cp, q - cq
            num = float(np.sum(w * (a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0])))
            den = float(np.sum(w * (a[:, 0] * b[:, 0] + a[:, 1] * b[:, 1])))
            cand = math.atan2(num, den)
            if not self.yaw_observed or self._yaw is None:
                self._yaw, self.yaw_observed, reinit_t = cand, True, True
                self._rejects = 0
            else:
                d = _wrap(cand - self._yaw)
                if abs(d) > FB_MAX_YAW_JUMP_RAD and not self._loss_since_accept:
                    self._rejects += 1
                    reasons.append("yaw_jump_rejected")
                    if self._rejects < FB_REJECTS_TO_RESET:
                        self.reasons = tuple(reasons)
                        return  # keep the previous alignment untouched
                    self._yaw, reinit_t, self._rejects = cand, True, 0
                    reasons.append("yaw_reinitialised")
                elif abs(d) > FB_MAX_YAW_JUMP_RAD:
                    self._yaw, reinit_t, self._rejects = cand, True, 0  # jump explained by a tracking loss
                else:
                    self._yaw = _wrap(self._yaw + FB_ALPHA * d)
                    self._rejects = 0
            self._loss_since_accept = False
        else:
            reasons.append("yaw_held_translation_only" if self.yaw_observed else "yaw_unobserved_translation_only")
            if self._yaw is None:
                self._yaw = self._yaw_hint if self._yaw_hint is not None else 0.0
        c, s = math.cos(self._yaw), math.sin(self._yaw)
        rot = np.array([[c, -s], [s, c]])
        t_new = cq - rot @ cp
        self._t = t_new if reinit_t or self._t is None else self._t + FB_ALPHA * (t_new - self._t)
        res = q - (p @ rot.T + self._t)
        rms = float(np.sqrt(np.sum(ws * np.sum(res**2, axis=1))))
        self.sigma_m = max(rms, float(np.sqrt(1.0 / w.sum())))
        self.reasons = tuple(reasons)


class PdrStepper:
    """Step detector for the no-WebXR case. Output is labelled motion-smoothed and feeds the prediction only."""

    def __init__(self, step_m: float = PDR_STEP_M) -> None:
        self.step_m = step_m
        self.steps = 0
        self._lp: float | None = None
        self._mean = 0.0
        self._last_t: float | None = None
        self._last_step_t: float | None = None
        self._armed = True

    def update(self, m: ImuSample, heading_deg: float | None = None) -> PdrStep | None:
        a = math.sqrt(m.ax * m.ax + m.ay * m.ay + m.az * m.az)
        if not _finite(a, m.t):
            return None
        if self._lp is None or self._last_t is None:
            self._lp, self._mean, self._last_t = a, a, m.t
            return None
        dt = m.t - self._last_t
        if dt <= 0.0:
            return None
        self._last_t = m.t
        self._lp += dt / (PDR_LP_TAU_S + dt) * (a - self._lp)
        self._mean += dt / (PDR_MEAN_TAU_S + dt) * (self._lp - self._mean)
        dev = self._lp - self._mean
        if dev < 0.0:
            self._armed = True
        if not (self._armed and dev > PDR_THRESHOLD_MPS2):
            return None
        if self._last_step_t is not None and m.t - self._last_step_t < PDR_REFRACTORY_S:
            return None
        self._armed = False
        self._last_step_t = m.t
        self.steps += 1
        h = heading_deg
        if h is None and m.absolute and m.heading_deg is not None and math.isfinite(m.heading_deg):
            h = m.heading_deg
        if h is None:
            return PdrStep(m.t, self.steps, 0.0, 0.0, None, reasons=("no_heading",))
        r = math.radians(h)
        return PdrStep(m.t, self.steps, self.step_m * math.sin(r), self.step_m * math.cos(r), h % 360.0)
