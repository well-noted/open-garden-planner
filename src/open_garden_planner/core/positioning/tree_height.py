"""Two-tap tree height from WebXR viewer poses (pure math; the single source of truth).

The phone posts raw samples (viewer position, orientation quaternion, tracking state, time) and
shows this module's answer; it never repeats the maths. Frame: WebXR ``local`` space, right
handed, x right, y up, -z forward (the frame ``fusion.vio_ground`` reads). The pitch of a tap is
the elevation of the viewer's forward ray ``f = rotate(q, (0, 0, -1))``: ``asin(f.y)``, positive
up. ``deviceorientation`` is used only by the explicit ``manual_orientation`` fallback.

Modes
-----
``hit_test``  tap 1 gave the ground point G under the trunk (AR hit-test); tap 2 aims at the
              crown top. ``height = e2 + d2 * tan(pitch_crown)`` with ``d2`` the horizontal
              distance from tap 2's pose to G and ``e2 = p2.y - G.y`` the eye height there.
``manual``    the user typed the horizontal distance ``d`` to the trunk; tap 1 aims at the base,
              tap 2 at the crown: ``height = d * (tan(pitch_crown) - tan(pitch_base))``.
``both``      both inputs exist: the typed distance wins, the hit-test answer and the
              disagreement are reported beside it.
``manual_orientation``  no WebXR: the phone's own tilt (beta, gamma) for each tap and a typed
              distance; the same formula as ``manual`` with a coarser pitch.

Nothing here claims an accuracy. The ``uncertainty_m`` is first-order propagation of the ASSUMED
errors in the tuning block below, which are NOT MEASURED; the real figure comes from the on-device
protocol (WP-M).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# ---------------------------------------------------------------- tuning block
# ASSUMED, NOT MEASURED. Used only to size the +/- range shown to the user; replace with the
# on-device measurements (WP-M) before saying anything about accuracy.
ASSUMED_ANGLE_ERROR_DEG = 1.0  # per tap
ASSUMED_DISTANCE_REL_ERROR = 0.03  # of d

# Quality gates (thresholds for warnings, not accuracy claims).
CROWN_PITCH_WARN_DEG = 70.0
CROWN_PITCH_HARD_DEG = 89.0  # tan blows up: refused
BASE_PITCH_MAX_DEG = -5.0  # hit-test mode: the base tap must look down at least this much
ROLL_WARN_DEG = 25.0
MIN_TAP_GAP_S = 0.5
MIN_DISTANCE_M = 2.0
MAX_DISTANCE_M = 40.0
GROUND_NOMINAL_EYE_M = 1.4  # a hand-held phone; only used to catch an absurd hit point
GROUND_OFF_PLANE_M = 2.0
GEOMETRY_MISMATCH_REL = 0.05
DISTANCE_DISAGREE_REL = 0.10
QUAT_NORM_TOL = 0.05  # a pose quaternion further than this from unit length is refused
MODES = ("hit_test", "manual", "both", "manual_orientation")

Vec3 = tuple[float, float, float]


class HeightError(ValueError):
    """Inputs that cannot give a height at all (``code`` is machine-readable)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class TapSample:
    """One tap: the viewer pose at the moment of the tap."""

    position: Vec3
    quaternion: tuple[float, float, float, float]  # x, y, z, w
    tracking: str
    t: float


@dataclass(frozen=True)
class TiltSample:
    """``manual_orientation`` fallback: deviceorientation beta and gamma, degrees."""

    beta_deg: float
    gamma_deg: float


def _finite(values: Sequence[float]) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)


def _unit(q: Sequence[float]) -> tuple[float, float, float, float]:
    if len(q) != 4 or not _finite(q):
        raise HeightError("invalid_quaternion", "A pose orientation must be four finite numbers.")
    norm = math.sqrt(sum(c * c for c in q))
    if abs(norm - 1.0) > QUAT_NORM_TOL:
        raise HeightError("invalid_quaternion", "A pose orientation is not a unit quaternion.")
    return q[0] / norm, q[1] / norm, q[2] / norm, q[3] / norm


def forward_ray(q: Sequence[float]) -> Vec3:
    """``rotate(q, (0, 0, -1))``: where the viewer looks, in the WebXR frame."""
    x, y, z, w = _unit(q)
    return (-2.0 * (x * z + w * y), -2.0 * (y * z - w * x), -(1.0 - 2.0 * (x * x + y * y)))


def pitch_deg(q: Sequence[float]) -> float:
    """Elevation of the forward ray in degrees, positive up."""
    return math.degrees(math.asin(max(-1.0, min(1.0, forward_ray(q)[1]))))


def roll_deg(q: Sequence[float]) -> float:
    """Rotation of the viewer about its own forward axis relative to the horizon, degrees (0 when level)."""
    x, y, z, w = _unit(q)
    right_y = 2.0 * (x * y + w * z)
    up_y = 1.0 - 2.0 * (x * x + z * z)
    return math.degrees(math.atan2(right_y, up_y))


def tilt_pitch_deg(tilt: TiltSample) -> float:
    """Rear-camera elevation from deviceorientation: ``asin(-cos(beta) cos(gamma))``.

    A phone upright in portrait has beta 90 (camera on the horizon); beta above 90 aims it up.
    """
    b, g = math.radians(tilt.beta_deg), math.radians(tilt.gamma_deg)
    return math.degrees(math.asin(max(-1.0, min(1.0, -math.cos(b) * math.cos(g)))))


def _sec2(angle_deg: float) -> float:
    c = math.cos(math.radians(angle_deg))
    return 1.0 / (c * c)


def _check_pitch(label: str, pitch: float) -> None:
    if pitch >= CROWN_PITCH_HARD_DEG:
        raise HeightError("pitch_unusable", f"The {label} is aimed almost straight up; step back and retake.")


def _uncertainty_hit(d: float, crown: float) -> float:
    sa = math.radians(ASSUMED_ANGLE_ERROR_DEG)
    from_angle = d * _sec2(crown) * sa
    from_dist = math.tan(math.radians(crown)) * ASSUMED_DISTANCE_REL_ERROR * d
    return math.hypot(from_angle, from_dist)


def _uncertainty_manual(d: float, crown: float, base: float) -> float:
    sa = math.radians(ASSUMED_ANGLE_ERROR_DEG)
    from_angles = math.hypot(d * _sec2(crown) * sa, d * _sec2(base) * sa)
    from_dist = (math.tan(math.radians(crown)) - math.tan(math.radians(base))) * ASSUMED_DISTANCE_REL_ERROR * d
    return math.hypot(from_angles, from_dist)


def _hit_test(tap1: TapSample, tap2: TapSample, ground: Vec3, warnings: list[str]) -> dict[str, float]:
    d1 = math.hypot(ground[0] - tap1.position[0], ground[2] - tap1.position[2])
    d2 = math.hypot(ground[0] - tap2.position[0], ground[2] - tap2.position[2])
    e1 = tap1.position[1] - ground[1]
    e2 = tap2.position[1] - ground[1]
    crown = pitch_deg(tap2.quaternion)
    base = pitch_deg(tap1.quaternion)
    out: dict[str, float] = {
        "height_m": e2 + d2 * math.tan(math.radians(crown)),
        "d_m": d2,
        "eye_height_m": e2,
        "uncertainty_m": _uncertainty_hit(d2, crown),
    }
    if base > BASE_PITCH_MAX_DEG:
        warnings.append("base_pitch_not_down")
    else:
        # the ray to G and G.y imply a distance of their own; it should match the horizontal one
        d_alt = e1 / math.tan(math.radians(-base))
        out["d_alt_m"] = d_alt
        if d1 > 0 and abs(d_alt - d1) / d1 > GEOMETRY_MISMATCH_REL:
            warnings.append("ground_geometry_mismatch")
    if any(abs(e - GROUND_NOMINAL_EYE_M) > GROUND_OFF_PLANE_M for e in (e1, e2)):
        warnings.append("ground_off_plane")
    return out


def _manual(d: float, crown: float, base: float) -> dict[str, float]:
    return {
        "height_m": d * (math.tan(math.radians(crown)) - math.tan(math.radians(base))),
        "d_m": d,
        "uncertainty_m": _uncertainty_manual(d, crown, base),
    }


def compute_tree_height(
    tap1: TapSample | None = None,
    tap2: TapSample | None = None,
    *,
    ground: Vec3 | None = None,
    manual_distance_m: float | None = None,
    tilt1: TiltSample | None = None,
    tilt2: TiltSample | None = None,
) -> dict[str, object]:
    """The height answer for one measurement; raises :class:`HeightError` when there is none.

    Give two taps (and optionally ``ground`` and/or ``manual_distance_m``) or, with no WebXR,
    two tilts and ``manual_distance_m``. The return value is JSON-ready.
    """
    warnings: list[str] = []
    detail: dict[str, object] = {}
    manual_d = manual_distance_m
    if manual_d is not None and (not _finite([manual_d]) or manual_d <= 0):
        raise HeightError("invalid_distance", "The distance must be a positive number of metres.")

    if tilt1 is not None or tilt2 is not None:
        if tilt1 is None or tilt2 is None or tap1 is not None or tap2 is not None or ground is not None:
            raise HeightError("invalid_inputs", "Give either two tilts or two taps, not a mix.")
        if manual_d is None:
            raise HeightError("distance_required", "Without AR the distance to the trunk is needed.")
        if not _finite([tilt1.beta_deg, tilt1.gamma_deg, tilt2.beta_deg, tilt2.gamma_deg]):
            raise HeightError("invalid_inputs", "Tilt angles must be finite numbers.")
        base, crown = tilt_pitch_deg(tilt1), tilt_pitch_deg(tilt2)
        _check_pitch("crown", crown)
        mode = "manual_orientation"
        warnings.append("orientation_only_coarse")
        chosen = _manual(manual_d, crown, base)
        rolls = [abs(tilt1.gamma_deg), abs(tilt2.gamma_deg)]
        if max(rolls) > ROLL_WARN_DEG:
            warnings.append("roll_high")
        detail["roll_deg"] = round(max(rolls), 2)
        tracking_ok = True
        eye = None
    else:
        if tap1 is None or tap2 is None:
            raise HeightError("invalid_inputs", "Two taps are needed.")
        if ground is None and manual_d is None:
            raise HeightError("distance_required", "Give the trunk-base hit point or a distance.")
        if not _finite([*tap1.position, *tap2.position, tap1.t, tap2.t]) or (
            ground is not None and not _finite(ground)
        ):
            raise HeightError("invalid_inputs", "Positions and times must be finite numbers.")
        base, crown = pitch_deg(tap1.quaternion), pitch_deg(tap2.quaternion)
        _check_pitch("crown", crown)
        rolls = [abs(roll_deg(tap1.quaternion)), abs(roll_deg(tap2.quaternion))]
        if max(rolls) > ROLL_WARN_DEG:
            warnings.append("roll_high")
        detail["roll_deg"] = round(max(rolls), 2)
        tracking_ok = tap1.tracking == "tracking" and tap2.tracking == "tracking"
        hit = _hit_test(tap1, tap2, ground, warnings) if ground is not None else None
        eye = hit["eye_height_m"] if hit is not None else None
        man = _manual(manual_d, crown, base) if manual_d is not None else None
        if man is not None:
            chosen = man
            mode = "both" if hit is not None else "manual"
            if hit is not None:
                detail["hit_test"] = {k: round(v, 3) for k, v in hit.items()}
                rel = abs(manual_d - hit["d_m"]) / max(manual_d, 1e-9)
                detail["distance_disagreement_rel"] = round(rel, 4)
                if rel > DISTANCE_DISAGREE_REL:
                    warnings.append("distance_disagreement")
        else:
            assert hit is not None
            chosen = hit
            mode = "hit_test"
        gap = abs(tap2.t - tap1.t)
        detail["tap_gap_s"] = round(gap, 3)
        if gap < MIN_TAP_GAP_S:
            warnings.append("taps_too_close_in_time")

    if not tracking_ok:
        warnings.append("tracking_not_good")
    if crown > CROWN_PITCH_WARN_DEG:
        warnings.append("crown_pitch_steep")
    d = chosen["d_m"]
    if d < MIN_DISTANCE_M:
        warnings.append("distance_too_short")
    elif d > MAX_DISTANCE_M:
        warnings.append("distance_too_long")
    height = chosen["height_m"]
    if not math.isfinite(height) or height <= 0:
        raise HeightError("crown_not_above_base", "The crown tap is not above the base: retake both taps.")
    unc = chosen["uncertainty_m"]
    out: dict[str, object] = {
        "mode": mode,
        "height_m": round(height, 3),
        "uncertainty_m": round(unc, 3),
        "range_m": [round(max(0.0, height - unc), 3), round(height + unc, 3)],
        "d_m": round(d, 3),
        "eye_height_m": None if eye is None else round(eye, 3),
        "pitch_base_deg": round(base, 3),
        "pitch_crown_deg": round(crown, 3),
        "warnings": warnings,
        "assumed": {
            "angle_error_deg": ASSUMED_ANGLE_ERROR_DEG,
            "distance_rel_error": ASSUMED_DISTANCE_REL_ERROR,
            "note": "ASSUMED, NOT MEASURED: the range sizes these inputs; it is not a measured accuracy.",
        },
        **detail,
    }
    if "d_alt_m" in chosen:
        out["d_alt_m"] = round(chosen["d_alt_m"], 3)
    return out
