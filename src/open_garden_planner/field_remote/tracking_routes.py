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

from open_garden_planner.core.georef import GeoReference
from open_garden_planner.core.positioning import GnssSample
from open_garden_planner.field_remote import projection
from open_garden_planner.field_remote.session import RecordingSession, SessionError

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import BaseRoute

Number = Annotated[float, Field(allow_inf_nan=False)]

#: Paths the guard lets through with the records toggle off (they write nothing to disk).
SESSION_PATHS = frozenset(
    {"/api/survey", "/api/calibrate", "/api/track/start", "/api/track/finish", "/api/track/cancel"}
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SurveySample(_Strict):
    t: Annotated[float, Field(ge=0)]
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


def tracking_routes(
    session: RecordingSession,
    snapshot: Callable[[], Any],
    error: Callable[..., Response],
    ok: Callable[..., Response],
) -> list[BaseRoute]:
    from starlette.concurrency import run_in_threadpool
    from starlette.routing import Route

    async def geo_of() -> tuple[GeoReference | None, Response | None]:
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

    return [
        Route("/api/survey", survey, methods=["POST"]),
        Route("/api/calibrate", calibrate, methods=["POST", "DELETE"]),
        Route("/api/track/start", start, methods=["POST"]),
        Route("/api/track/preview", preview, methods=["GET"]),
        Route("/api/track/cancel", cancel, methods=["POST"]),
        Route("/api/track/finish", finish, methods=["POST"]),
    ]
