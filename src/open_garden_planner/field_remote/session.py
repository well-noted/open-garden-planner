"""The phone's positioning session: one :class:`PositioningEngine` shared by the paired phones.

Holds the Kalman filter, the survey results awaiting calibration and the track being
recorded. Session state only: nothing here is written to disk, and a different georeference
(the user re-fitted the plan) starts a fresh engine, dropping the anchor, because a correction
made against the old frame would be wrong in the new one.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

from open_garden_planner.core.georef import GeoReference
from open_garden_planner.core.positioning import GnssSample, ImuSample, PositioningEngine, VioSample
from open_garden_planner.core.positioning.fusion import FusionError
from open_garden_planner.core.positioning.survey import SurveyResult
from open_garden_planner.field_remote import projection

#: With no pose heard for this long (server clock), the lock is reported stale: a phone that
#: was backgrounded sends nothing, so the sample clock alone never ages the lock out.
VIO_STREAM_STALE_S = 3.0

MAX_SURVEYS = 8
MIN_SURVEY_SAMPLES = 4  # core/positioning/survey.py MIN_SAMPLES
MAX_SURVEY_SAMPLES = 300
MAX_CLOCK_JUMP_S = 3600.0
#: A recording shorter than this or with fewer vertices is refused at save time (design 4.2 item 6).
MIN_PATH_LENGTH_M = 3.0
MIN_PATH_VERTICES = 3

_NOTE = "A computed spread of this phone's own fixes, not a validated accuracy."


class SessionError(ValueError):
    """A request the session cannot honour; ``code`` is the API error code."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def _geo_key(geo: GeoReference) -> str:
    return json.dumps([geo.to_dict(), geo.rotation_deg], sort_keys=True, default=str)


class RecordingSession:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._engine: PositioningEngine | None = None
        self._key: str | None = None
        self._surveys: OrderedDict[str, SurveyResult] = OrderedDict()
        self._next = 0
        self.recording = False
        self._vio_heard: float | None = None  # server monotonic time of the last pose batch
        self._clock = time.monotonic

    def engine_for(self, geo: GeoReference) -> PositioningEngine:
        with self.lock:
            key = _geo_key(geo)
            if self._engine is None or key != self._key:
                self._engine, self._key = PositioningEngine(geo), key
                self._surveys.clear()
                self.recording = False
            return self._engine

    # -- position ---------------------------------------------------------------------------

    def _feed_ar(
        self, engine: PositioningEngine, vios: Sequence[tuple[VioSample, bool]], imus: Sequence[ImuSample]
    ) -> None:
        """VIO first, then IMU (the order the engine documents), before any GNSS fix.

        Each VIO item carries the phone's ``reset`` flag (a reference-space reset moved the
        frame): that drops the lock before the sample is used.
        """
        for v, reset in vios:
            if reset:
                engine.ar_frame_reset(v.t)
            engine.add_vio(v)
        if vios:
            self._vio_heard = self._clock()
        for m in imus:
            engine.add_imu(m)

    def solver(
        self,
        fixes: Sequence[projection.Fix],
        geo: GeoReference | None,
        vios: Sequence[tuple[VioSample, bool]] = (),
        imus: Sequence[ImuSample] = (),
    ) -> dict[str, Any]:
        """The telemetry solver hook of :func:`projection.telemetry`: filtered, gated, anchored.

        ``vios``/``imus`` are fed first. While a plan lock is active and tracking is good, the
        plan-locked VIO estimate is the published track (``track_source`` ``vio``); the GNSS
        filter result is published otherwise.
        """
        if geo is None or not fixes:
            raise projection.ProjectionError(422, "invalid_position", "No position or plan georeference.")
        with self.lock:
            engine = self.engine_for(geo)
            last_t = getattr(engine, "_last_t", None)
            if last_t is not None and abs(fixes[0].t - last_t) > MAX_CLOCK_JUMP_S and not self.recording:
                was_locked = engine.ar_status()["state"] != "unlocked"
                self._engine = None  # a new epoch (clock jump or a long gap): never refuse every later fix
                engine = self.engine_for(geo)
                if was_locked:
                    engine.ar_unlock("clock_jump")  # the dropped lock says why, like any other unlock
            self._feed_ar(engine, vios, imus)
            estimate = None
            for f in fixes:
                got = engine.add_gnss(
                    GnssSample(f.t, f.lat, f.lon, f.sigma_m if f.sigma_m is not None else f.accuracy_m,
                               f.alt, f.heading_deg, f.speed_mps)
                )
                estimate = got or estimate
            ref_t = max(f.t for f in fixes)
            fused = None if self._stream_stale() else engine.fused_estimate(ref_t)
            if estimate is None and fused is None:  # every fix refused before any was accepted: say so, plainly
                return {
                    **projection.telemetry(fixes, geo), "gated": True, "reject_reason": engine.last_reject_reason,
                    "fused": self._status(engine, ref_t),
                }
            return self._telemetry(engine, geo, fused or estimate, fixes[-1], len(fixes), ref_t)

    def vio_only(
        self, vios: Sequence[tuple[VioSample, bool]], imus: Sequence[ImuSample], geo: GeoReference
    ) -> dict[str, Any]:
        """A VIO/IMU batch with no GNSS fix. Answers with the locked track when there is one."""
        with self.lock:
            engine = self.engine_for(geo)
            self._feed_ar(engine, vios, imus)
            ref_t = max((v.t for v, _ in vios), default=None)
            fused = None if ref_t is None or self._stream_stale() else engine.fused_estimate(ref_t)
            if fused is None:
                return {
                    "x_cm": None, "y_cm": None, "gated": False, "georeferenced": True,
                    "recording": self.recording, "fused": self._status(engine, ref_t), "track_source": "gnss",
                    "samples_used": 0,
                }
            return self._telemetry(engine, geo, fused, None, 0, ref_t)

    def _stream_stale(self) -> bool:
        """No pose batch heard for a while: the locked track is not published (one answer, one source)."""
        heard = self._vio_heard
        return heard is None or self._clock() - heard > VIO_STREAM_STALE_S

    def _status(self, engine: PositioningEngine, t: float | None) -> dict[str, Any]:
        return engine.ar_status(t, stream_stale=self._stream_stale())

    def _telemetry(
        self,
        engine: PositioningEngine,
        geo: GeoReference,
        estimate: Any,
        last: projection.Fix | None,
        used: int,
        ref_t: float | None,
    ) -> dict[str, Any]:
        vio_track = estimate.track_source == "vio"
        # a receiver's RTK label describes the GNSS fix, never a VIO-sourced position
        reported = not vio_track and last is not None and last.fix_quality in ("rtk_float", "rtk_fixed")
        heading_canvas = None
        if estimate.heading_deg is not None:
            heading_canvas = round(projection.canvas_heading_deg(estimate.heading_deg, geo.rotation_deg), 2)
        fix_mode = last.fix_quality if reported and last is not None else str(estimate.fix_mode)
        return {
            "t": estimate.t,
            "lat": round(estimate.lat, 9),
            "lon": round(estimate.lon, 9),
            "x_cm": round(estimate.x_cm, 1),
            "y_cm": round(estimate.y_cm, 1),
            # the plan-lock sigma is an unmeasured placeholder (fused.sigma_m): never shown as a spread
            "sigma_m": None if vio_track else round(estimate.sigma_m, 2),
            "horizontal_accuracy_m": estimate.horizontal_accuracy_m,
            "heading_deg": estimate.heading_deg,
            "heading_source": estimate.heading_source,
            "heading_canvas_deg": heading_canvas,
            "speed_mps": round(estimate.speed_mps, 2),
            "fix_mode": fix_mode,
            "fix_mode_reported": reported,
            "fix_source": last.source if last is not None else "webxr",
            "track_source": estimate.track_source,
            "motion_model": estimate.motion_model,
            "anchor_source": estimate.anchor_source,
            "anchor_age_s": estimate.anchor_age_s,
            "stationary": estimate.stationary,
            "gated": estimate.gated,
            "reject_reason": engine.last_reject_reason,
            "quality": estimate.quality,
            "samples_used": used,
            "georeferenced": True,
            "recording": self.recording,
            "fused": self._status(engine, ref_t),
        }

    # -- AR plan lock (WP-H) ------------------------------------------------------------------

    def _plan_en(self, geo: GeoReference, plan: tuple[float, float]) -> tuple[float, float]:
        from open_garden_planner.core.positioning.calibration import _plan_enu

        try:
            return _plan_enu(geo, *plan)
        except ValueError as error:  # GeorefError is a ValueError
            raise SessionError(422, "invalid_plan_point", str(error)) from error

    def ar_lock(
        self, which: str, geo: GeoReference, hit_xz: tuple[float, float], plan_cm: tuple[float, float], vio_t: float
    ) -> dict[str, Any]:
        """Capture lock point ``a`` or ``b`` (the reticle hit, not the phone), or ``c`` for the closure check.

        ``vio_t`` is the last pose the phone had sent when it took the hit. The lock time is the
        server's last pose from the stream, never a client clock.
        """
        with self.lock:
            engine = self.engine_for(geo)
            plan_en = self._plan_en(geo, plan_cm)
            try:
                if which == "a":
                    engine.ar_capture_a(hit_xz, plan_en, vio_t)
                elif which == "b":
                    engine.ar_capture_b(hit_xz, plan_en, vio_t)
                else:
                    closure = engine.ar_closure(hit_xz, plan_en, vio_t)
                    return {"closure_m": round(closure, 3), "label": "closure at C", "fused": self._status(engine, None)}
            except FusionError as error:
                raise SessionError(409, "ar_lock_refused", str(error)) from error
            status = self._status(engine, None)
            return {"state": status["state"], "scale": status["scale"], "fused": status}

    def ar_unlock(self, geo: GeoReference) -> dict[str, Any]:
        with self.lock:
            engine = self.engine_for(geo)
            engine.ar_unlock("user")
            return {"state": engine.ar_status()["state"]}

    def ar_plan_point(self, geo: GeoReference, xz: tuple[float, float]) -> dict[str, float] | None:
        """Where a WebXR ground point is on the plan (cm and GPS), only while the AR lock holds."""
        with self.lock:
            cm = self.engine_for(geo).ar_plan_cm(xz)
        if cm is None:
            return None
        try:
            lat, lon = geo.canvas_to_gps(*cm)
        except ValueError:
            return None
        return {"x_cm": round(cm[0], 1), "y_cm": round(cm[1], 1), "lat": round(lat, 9), "lon": round(lon, 9)}

    def ar_status(self, geo: GeoReference) -> dict[str, Any]:
        with self.lock:
            return self._status(self.engine_for(geo), None)

    # -- survey-in and calibration -------------------------------------------------------------

    def survey(self, samples: Sequence[GnssSample], geo: GeoReference) -> dict[str, Any]:
        if not MIN_SURVEY_SAMPLES <= len(samples) <= MAX_SURVEY_SAMPLES:
            raise SessionError(422, "invalid_survey", f"Send {MIN_SURVEY_SAMPLES} to {MAX_SURVEY_SAMPLES} fixes.")
        with self.lock:
            engine = self.engine_for(geo)
            try:
                result = engine.survey_in(samples)
            except ValueError as error:
                raise SessionError(422, "invalid_survey", str(error)) from error
            self._next += 1
            sid = f"s{self._next}"
            self._surveys[sid] = result
            while len(self._surveys) > MAX_SURVEYS:
                self._surveys.popitem(last=False)
            x_cm, y_cm = geo.gps_to_canvas(result.lat, result.lon)
            return {
                "survey_id": sid, "lat": result.lat, "lon": result.lon,
                "x_cm": round(x_cm, 1), "y_cm": round(y_cm, 1),
                "sigma_m": round(result.sigma_m, 2), "n_used": result.n_used,
                "n_discarded": result.n_discarded, "duration_s": round(result.duration_s, 1), "note": _NOTE,
            }

    def calibrate(
        self, points: Sequence[tuple[str, float, float]], geo: GeoReference, t: float, *, allow_affine: bool = False
    ) -> dict[str, Any]:
        from open_garden_planner.core.positioning.calibration import CalibrationError

        with self.lock:
            engine = self.engine_for(geo)
            if self.recording:  # a frame change mid-track would leave a silent jump in the polyline
                raise SessionError(409, "recording_active", "Finish or discard the recording before calibrating.")
            pairs = []
            for sid, x, y in points:
                survey = self._surveys.get(sid)
                if survey is None:
                    raise SessionError(404, "unknown_survey", "That survey is gone: stand still and survey again.")
                pairs.append(((x, y), survey))
            try:
                if len(pairs) == 1:
                    engine.set_anchor(pairs[0][0], pairs[0][1], t)
                    return {"kind": "gnss_point", "n_points": 1, "fit_rms_m": None, "residuals_m": []}
                fit = engine.set_anchor_points(pairs, t, allow_affine=allow_affine)
            except CalibrationError as error:
                raise SessionError(422, "calibration_refused", str(error)) from error
            return {
                "kind": "plan_points", "n_points": len(pairs), "fit_rms_m": round(fit.rms_m, 3),
                "residuals_m": [round(r, 3) for r in fit.residuals_m], "method": fit.method,
            }

    def clear_calibration(self, geo: GeoReference) -> None:
        with self.lock:
            if self.recording:
                raise SessionError(409, "recording_active", "Finish or discard the recording first.")
            self.engine_for(geo).clear_anchor()

    # -- track recording ----------------------------------------------------------------------

    def start_track(self, geo: GeoReference) -> None:
        with self.lock:
            self.engine_for(geo).start_track()
            self.recording = True

    def preview(self, geo: GeoReference) -> list[list[float]]:
        with self.lock:
            engine = self.engine_for(geo)
            pts = engine.track_preview()
        out = []
        for e, n in pts[-500:]:
            x, y = geo.gps_to_canvas(*geo.enu_to_gps(e, n))
            out.append([round(x, 1), round(y, 1)])
        return out

    def cancel_track(self, geo: GeoReference) -> None:
        with self.lock:
            self.engine_for(geo).start_track()  # resets the buffer
            self.engine_for(geo).finish_track()
            self.recording = False

    def finish_track(
        self, geo: GeoReference, *, close: str | None, target_cm: tuple[float, float] | None, smooth: bool
    ) -> dict[str, Any]:
        from open_garden_planner.core.positioning.closure import ClosureError

        with self.lock:
            engine = self.engine_for(geo)
            try:
                result = engine.finish_track(smooth=smooth, close=close, target_plan_cm=target_cm)
            except ClosureError as error:
                raise SessionError(422, "closure_refused", str(error)) from error
            finally:
                self.recording = False
        vertices = []
        for (e, n), (x, y) in zip(result.points_enu, result.points_cm, strict=True):
            lat, lon = geo.enu_to_gps(e, n)
            vertices.append([round(lat, 9), round(lon, 9), round(x, 1), round(y, 1)])
        closure = result.closure
        problems = []
        if result.length_m < MIN_PATH_LENGTH_M:
            problems.append("too_short")
        if len(vertices) < MIN_PATH_VERTICES:
            problems.append("too_few_points")
        return {
            "points": vertices,
            "length_m": round(result.length_m, 2),
            "epsilon_m": round(result.epsilon_m, 3),
            "n_vertices": len(vertices),
            "n_filter_steps": result.n_raw,
            "smoothed": result.smoothed,
            "truncated": result.truncated,
            "closure": None if closure is None else {
                "closure_m": round(closure.closure_m, 3), "ratio": round(closure.ratio, 4),
                "path_length_m": round(closure.path_length_m, 2),
            },
            "saveable": not problems,
            "problems": problems,
        }
