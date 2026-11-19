"""The phone's positioning session: one :class:`PositioningEngine` shared by the paired phones.

Holds the Kalman filter, the survey results awaiting calibration and the track being
recorded. Session state only: nothing here is written to disk, and a different georeference
(the user re-fitted the plan) starts a fresh engine, dropping the anchor, because a correction
made against the old frame would be wrong in the new one.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

from open_garden_planner.core.georef import GeoReference
from open_garden_planner.core.positioning import GnssSample, PositioningEngine
from open_garden_planner.core.positioning.survey import SurveyResult
from open_garden_planner.field_remote import projection

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

    def engine_for(self, geo: GeoReference) -> PositioningEngine:
        with self.lock:
            key = _geo_key(geo)
            if self._engine is None or key != self._key:
                self._engine, self._key = PositioningEngine(geo), key
                self._surveys.clear()
                self.recording = False
            return self._engine

    # -- position ---------------------------------------------------------------------------

    def solver(self, fixes: Sequence[projection.Fix], geo: GeoReference | None) -> dict[str, Any]:
        """The telemetry solver hook of :func:`projection.telemetry`: filtered, gated, anchored."""
        if geo is None or not fixes:
            raise projection.ProjectionError(422, "invalid_position", "No position or plan georeference.")
        with self.lock:
            engine = self.engine_for(geo)
            last_t = getattr(engine, "_last_t", None)
            if last_t is not None and abs(fixes[0].t - last_t) > MAX_CLOCK_JUMP_S and not self.recording:
                self._engine = None  # a new epoch (clock jump or a long gap): never refuse every later fix
                engine = self.engine_for(geo)
            estimate = None
            for f in fixes:
                got = engine.add_gnss(
                    GnssSample(f.t, f.lat, f.lon, f.sigma_m if f.sigma_m is not None else f.accuracy_m,
                               f.alt, f.heading_deg, f.speed_mps)
                )
                estimate = got or estimate
            if estimate is None:  # every fix refused before any was accepted: say so, plainly
                return {**projection.telemetry(fixes, geo), "gated": True, "reject_reason": engine.last_reject_reason}
            last = fixes[-1]
            reported = last.fix_quality in ("rtk_float", "rtk_fixed")
            heading_canvas = None
            if estimate.heading_deg is not None:
                heading_canvas = round(projection.canvas_heading_deg(estimate.heading_deg, geo.rotation_deg), 2)
            return {
                "t": estimate.t,
                "lat": round(estimate.lat, 9),
                "lon": round(estimate.lon, 9),
                "x_cm": round(estimate.x_cm, 1),
                "y_cm": round(estimate.y_cm, 1),
                "sigma_m": round(estimate.sigma_m, 2),
                "horizontal_accuracy_m": estimate.horizontal_accuracy_m,
                "heading_deg": estimate.heading_deg,
                "heading_source": estimate.heading_source,
                "heading_canvas_deg": heading_canvas,
                "speed_mps": round(estimate.speed_mps, 2),
                "fix_mode": last.fix_quality if reported else str(estimate.fix_mode),
                "fix_mode_reported": reported,
                "fix_source": last.source,
                "anchor_source": estimate.anchor_source,
                "anchor_age_s": estimate.anchor_age_s,
                "stationary": estimate.stationary,
                "gated": estimate.gated,
                "reject_reason": engine.last_reject_reason,
                "quality": estimate.quality,
                "samples_used": len(fixes),
                "georeferenced": True,
                "recording": self.recording,
            }

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
