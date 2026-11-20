"""Survey-In, calibration and track-recording routes (design 1.6, 2.5-2.7, 4.2 items 3, 4, 6).

Session state only: none of these touches the plan or the field notes (a recorded track becomes
a record only when the phone then posts it to ``/api/records``, which has its own guard), so
they stay allowed while "phone may save records" is off, like ``/api/position``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from open_garden_planner.core.geo_frame import GeoFrame
from open_garden_planner.core.positioning import GnssSample
from open_garden_planner.core.positioning.tree_height import (
    HeightError,
    TapSample,
    TiltSample,
    compute_tree_height,
)
from open_garden_planner.field_remote import projection
from open_garden_planner.field_remote.session import RecordingSession, SessionError

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import BaseRoute

Number = Annotated[float, Field(allow_inf_nan=False)]

#: Paths the guard lets through with the records toggle off (they write nothing to disk).
SESSION_PATHS = frozenset(
    {
        "/api/survey", "/api/calibrate", "/api/track/start", "/api/track/finish", "/api/track/cancel",
        "/api/ar/lock/a", "/api/ar/lock/b", "/api/ar/check", "/api/ar/unlock",
        # computes a number from the posted samples and writes nothing; saving is /api/records
        "/api/ar/height",
    }
)


class _Strict(BaseModel):
    # allow_inf_nan=False: an Infinity or 1e400 would be stored and then break every JSON answer.
    # strict: a number sent as a string or a bool is refused, like every other phone route.
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


#: The one sample-time bound for every phone route (``routes`` imports it): epoch seconds,
#: before 2100-01-01. A time beyond this is not a phone clock.
MAX_EPOCH_S = 4_102_444_800.0
EpochS = Annotated[float, Field(ge=0, le=MAX_EPOCH_S, allow_inf_nan=False)]
#: Bounds for the AR lock models. WebXR ``local`` space is metres around the session start;
#: plan centimetres are bounded like any plan coordinate.
XR_M = Annotated[float, Field(ge=-100_000, le=100_000, allow_inf_nan=False)]
PLAN_CM = Annotated[float, Field(ge=-1e9, le=1e9, allow_inf_nan=False)]


class SurveySample(_Strict):
    t: EpochS
    lat: Annotated[float, Field(ge=-90, le=90)]
    lon: Annotated[float, Field(ge=-180, le=180)]
    accuracy_m: Annotated[float, Field(ge=0, le=100_000)]


class SurveyIn(_Strict):
    samples: Annotated[list[SurveySample], Field(min_length=1, max_length=300)]


class CalPoint(_Strict):
    survey_id: Annotated[str, Field(pattern=r"^s[0-9]{1,9}$")]
    plan_x_cm: Number
    plan_y_cm: Number


class CalibrateIn(_Strict):
    points: Annotated[list[CalPoint], Field(min_length=1, max_length=4)]
    allow_affine: bool = False


class FinishIn(_Strict):
    close: Literal["start", "point"] | None = None
    target_x_cm: Number | None = None
    target_y_cm: Number | None = None
    smooth: bool = True


class PlanSpot(_Strict):
    """A known spot on the plan, in plan centimetres (the same frame as the map the phone shows)."""

    x_cm: PLAN_CM
    y_cm: PLAN_CM


class ArHit(_Strict):
    """A reticle hit-test position in the WebXR ``local`` space (y up, -z forward). y is unused."""

    x: XR_M
    y: XR_M
    z: XR_M


Unit = Annotated[float, Field(ge=-1.5, le=1.5, allow_inf_nan=False)]


class TapIn(_Strict):
    """One tap of the tree-height mode: the viewer pose at that moment (WebXR ``local`` space)."""

    position: Annotated[list[XR_M], Field(min_length=3, max_length=3)]
    quaternion: Annotated[list[Unit], Field(min_length=4, max_length=4)]  # x, y, z, w
    tracking: Literal["tracking", "limited", "lost"]
    t: EpochS


class TiltIn(_Strict):
    """No-WebXR fallback: the phone's deviceorientation beta and gamma at the tap, degrees."""

    beta_deg: Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)]
    gamma_deg: Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)]


class HeightIn(_Strict):
    """Either two ``taps`` (optionally with the trunk-base ``ground`` hit and/or a typed
    ``manual_distance_m``) or, with no WebXR, two ``tilts`` and ``manual_distance_m``."""

    tap1: TapIn | None = None
    tap2: TapIn | None = None
    ground: ArHit | None = None
    manual_distance_m: Annotated[float, Field(gt=0, le=1000, allow_inf_nan=False)] | None = None
    tilt1: TiltIn | None = None
    tilt2: TiltIn | None = None


class ArLockIn(_Strict):
    """``vio_t`` is the time of the last pose the phone had sent when it took the hit (the lock
    is refused until the server's pose stream has reached it). The lock time is the server's."""

    plan: PlanSpot
    hit: ArHit
    vio_t: EpochS


def tracking_routes(
    session: RecordingSession,
    snapshot: Callable[[], Any],
    error: Callable[..., Response],
    ok: Callable[..., Response],
) -> list[BaseRoute]:
    from starlette.concurrency import run_in_threadpool
    from starlette.routing import Route

    async def geo_of() -> tuple[GeoFrame | None, Response | None]:
        try:
            snap = await run_in_threadpool(snapshot)
        except (TimeoutError, RuntimeError):
            return None, error(503, "plan_unavailable", "The computer is busy; try again.")
        geo = projection.georef_of(snap)
        if geo is None:
            return None, error(409, "not_georeferenced", "The plan has no georeference.")
        return geo, None

    async def body(request: Request, model: type[BaseModel]) -> Any:
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return error(415, "unsupported_media_type", "Send application/json.")
        try:
            return model.model_validate_json(await request.body())
        except ValidationError as exc:
            return error(422, "invalid_request", "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5]))

    def refuse(exc: SessionError) -> Response:
        return error(exc.status, exc.code, exc.detail)

    async def survey(request: Request) -> Response:
        parsed = await body(request, SurveyIn)
        if not isinstance(parsed, SurveyIn):
            return parsed
        geo, bad = await geo_of()
        if geo is None:
            return bad or error(409, "not_georeferenced")
        samples = [GnssSample(s.t, s.lat, s.lon, s.accuracy_m) for s in parsed.samples]
        try:
            return ok(await run_in_threadpool(session.survey, samples, geo))
        except SessionError as exc:
            return refuse(exc)

    async def calibrate(request: Request) -> Response:
        geo, bad = await geo_of()
        if geo is None:
            return bad or error(409, "not_georeferenced")
        if request.method == "DELETE":
            try:
                await run_in_threadpool(session.clear_calibration, geo)
            except SessionError as exc:
                return refuse(exc)
            return ok({"cleared": True})
        parsed = await body(request, CalibrateIn)
        if not isinstance(parsed, CalibrateIn):
            return parsed
        pts = [(p.survey_id, p.plan_x_cm, p.plan_y_cm) for p in parsed.points]
        try:
            return ok(await run_in_threadpool(lambda: session.calibrate(pts, geo, time.time(), allow_affine=parsed.allow_affine)))
        except SessionError as exc:
            return refuse(exc)

    async def start(_request: Request) -> Response:
        geo, bad = await geo_of()
        if geo is None:
            return bad or error(409, "not_georeferenced")
        await run_in_threadpool(session.start_track, geo)
        return ok({"recording": True})

    async def preview(_request: Request) -> Response:
        geo, bad = await geo_of()
        if geo is None:
            return bad or error(409, "not_georeferenced")
        pts = await run_in_threadpool(session.preview, geo)
        return ok({"recording": session.recording, "points_cm": pts})

    async def cancel(_request: Request) -> Response:
        geo, bad = await geo_of()
        if geo is None:
            return bad or error(409, "not_georeferenced")
        await run_in_threadpool(session.cancel_track, geo)
        return ok({"recording": False})

    async def finish(request: Request) -> Response:
        parsed = await body(request, FinishIn)
        if not isinstance(parsed, FinishIn):
            return parsed
        geo, bad = await geo_of()
        if geo is None:
            return bad or error(409, "not_georeferenced")
        target = None
        if parsed.close == "point":
            if parsed.target_x_cm is None or parsed.target_y_cm is None:
                return error(422, "invalid_request", "close=point needs target_x_cm and target_y_cm.")
            target = (parsed.target_x_cm, parsed.target_y_cm)
        try:
            return ok(
                await run_in_threadpool(
                    lambda: session.finish_track(geo, close=parsed.close, target_cm=target, smooth=parsed.smooth)
                )
            )
        except SessionError as exc:
            return refuse(exc)

    def ar_point(which: str) -> Callable[[Request], Any]:
        async def handler(request: Request) -> Response:
            parsed = await body(request, ArLockIn)
            if not isinstance(parsed, ArLockIn):
                return parsed
            geo, bad = await geo_of()
            if geo is None:
                return bad or error(409, "not_georeferenced")
            hit = (float(parsed.hit.x), float(parsed.hit.z))
            plan = (float(parsed.plan.x_cm), float(parsed.plan.y_cm))
            try:
                return ok(await run_in_threadpool(session.ar_lock, which, geo, hit, plan, float(parsed.vio_t)))
            except SessionError as exc:
                return refuse(exc)

        return handler

    async def ar_unlock(_request: Request) -> Response:
        geo, bad = await geo_of()
        if geo is None:
            return bad or error(409, "not_georeferenced")
        return ok(await run_in_threadpool(session.ar_unlock, geo))

    async def ar_status(_request: Request) -> Response:
        geo, bad = await geo_of()
        if geo is None:
            return bad or error(409, "not_georeferenced")
        return ok(await run_in_threadpool(session.ar_status, geo))

    async def ar_height(request: Request) -> Response:
        parsed = await body(request, HeightIn)
        if not isinstance(parsed, HeightIn):
            return parsed

        def tap(t: TapIn | None) -> TapSample | None:
            if t is None:
                return None
            return TapSample(
                (float(t.position[0]), float(t.position[1]), float(t.position[2])),
                (float(t.quaternion[0]), float(t.quaternion[1]), float(t.quaternion[2]), float(t.quaternion[3])),
                t.tracking,
                float(t.t),
            )

        def tilt(t: TiltIn | None) -> TiltSample | None:
            return None if t is None else TiltSample(float(t.beta_deg), float(t.gamma_deg))

        g = parsed.ground
        try:
            result = compute_tree_height(
                tap(parsed.tap1),
                tap(parsed.tap2),
                ground=None if g is None else (float(g.x), float(g.y), float(g.z)),
                manual_distance_m=parsed.manual_distance_m,
                tilt1=tilt(parsed.tilt1),
                tilt2=tilt(parsed.tilt2),
            )
        except HeightError as exc:
            return error(422, exc.code, exc.detail)
        result["trunk_plan"] = None
        if g is not None:  # the trunk's place on the plan, when an AR lock and a georeference exist
            try:
                snap = await run_in_threadpool(snapshot)
                geo = projection.georef_of(snap)
            except (TimeoutError, RuntimeError):
                geo = None
            if geo is not None:
                result["trunk_plan"] = await run_in_threadpool(session.ar_plan_point, geo, (float(g.x), float(g.z)))
        return ok(result)

    return [
        Route("/api/ar/height", ar_height, methods=["POST"]),
        Route("/api/ar/lock/a", ar_point("a"), methods=["POST"]),
        Route("/api/ar/lock/b", ar_point("b"), methods=["POST"]),
        Route("/api/ar/check", ar_point("c"), methods=["POST"]),
        Route("/api/ar/unlock", ar_unlock, methods=["POST"]),
        Route("/api/ar/status", ar_status, methods=["GET"]),
        Route("/api/survey", survey, methods=["POST"]),
        Route("/api/calibrate", calibrate, methods=["POST", "DELETE"]),
        Route("/api/track/start", start, methods=["POST"]),
        Route("/api/track/preview", preview, methods=["GET"]),
        Route("/api/track/cancel", cancel, methods=["POST"]),
        Route("/api/track/finish", finish, methods=["POST"]),
    ]
