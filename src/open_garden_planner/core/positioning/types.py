"""Sample and estimate types plus the tuning constants (design section 2.2/2.3)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class FixMode(StrEnum):
    """Public fix mode. ``rtk_*`` is inferred from sustained accuracy, never measured."""

    STANDARD = "standard"
    ANCHOR_CALIBRATED = "anchor_calibrated"
    VIO_FUSED = "vio_fused"
    RTK_FLOAT = "rtk_float"
    RTK_FIXED = "rtk_fixed"


@dataclass(frozen=True)
class GnssSample:
    """One browser geolocation fix. ``t`` is seconds; accuracy is the reported 1-sigma-ish radius."""

    t: float
    lat: float
    lon: float
    accuracy_m: float
    alt: float | None = None
    heading_deg: float | None = None
    speed_mps: float | None = None


@dataclass(frozen=True)
class VioSample:
    """Visual-inertial pose (WebXR). Consumed by the WP-H fusion layer, not here."""

    t: float
    x: float
    y: float
    z: float
    qx: float
    qy: float
    qz: float
    qw: float
    tracking: Literal["tracking", "limited", "lost"] = "tracking"


@dataclass(frozen=True)
class ImuSample:
    """Device motion/orientation sample (``absolute`` = from ``deviceorientationabsolute``)."""

    t: float
    ax: float
    ay: float
    az: float
    alpha: float = 0.0
    beta: float = 0.0
    gamma: float = 0.0
    heading_deg: float | None = None
    absolute: bool = False


@dataclass(frozen=True)
class PositionEstimate:
    """Filtered position. ``sigma_m`` is the filter's own spread, not a validated accuracy."""

    t: float
    e_m: float
    n_m: float
    x_cm: float
    y_cm: float
    lat: float
    lon: float
    sigma_m: float
    heading_deg: float | None
    speed_mps: float
    fix_mode: FixMode
    stationary: bool
    quality: str
    gated: bool
    heading_source: str = "none"  # "gnss" | "compass" | "none"
    anchor_source: str = "none"  # "gnss_point" | "plan_points" | "none"
    anchor_age_s: float | None = None
    horizontal_accuracy_m: float | None = None
    track_source: str = "gnss"  # "gnss" (Kalman) | "vio" (plan-locked VIO while locked and tracking)
    motion_model: str | None = None  # "motion-smoothed" when a step prior fed the prediction


# --- Kalman / gating tuning (replay-harness tunable; keep every number here) ---
SIGMA_A_WALK = 0.8  # m/s^2, gardener stops and turns
SIGMA_A_STILL = 0.15  # m/s^2 when stationary
R_MIN_M = 1.5  # floor on measurement sigma: reported accuracy is optimistic
R_INFLATE = 2.0  # applied when reported accuracy rises more than ACC_JUMP_RATIO in 1 s
ACC_JUMP_RATIO = 3.0
DT_MIN_S = 0.05
DT_MAX_S = 5.0
V0_SIGMA = 2.0  # initial velocity sigma, m/s
RESEED_P_SCALE = 3.0  # position sigma multiplier when re-seeding after repeated rejects

ACC_MAX_M = 50.0  # reject fixes reporting worse than this
MAHALANOBIS_GATE = 9.21  # chi-square 2 dof, 99 %
MAX_SPEED_MPS = 3.0  # implied-speed jump limit
REJECTS_TO_RESEED = 3
REJECT_WINDOW_S = 5.0

ZUPT_WINDOW_S = 3.0
ZUPT_SPEED_MPS = 0.3
ZUPT_MIN_DISP_M = 1.0
ZUPT_DISP_ACC_FACTOR = 1.0  # design says 1.5 x accuracy on a 3 s extent; see zupt.py
ZUPT_DISP_WINDOW_S = 6.0
ZUPT_ACCEL_VAR = 0.05
ZUPT_EXIT_SAMPLES = 2
ZUPT_R_V = 0.05

CRUMB_MIN_M = 0.5
RDP_MIN_EPS_M = 0.15
RDP_SIGMA_FACTOR = 0.5
CRUMB_MIN_SPACING_M = 0.3
CRUMB_MAX_VERTICES = 2000

ANCHOR_STALE_S = 600.0
ANCHOR_FIT_OK_RMS_M = 2.0
RTK_FLOAT_ACC_M = 0.5
RTK_FIXED_ACC_M = 0.05
RTK_SUSTAIN_SAMPLES = 5
