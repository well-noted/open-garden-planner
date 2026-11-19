"""Field Remote record routes (design 1.6-1.9, ADR-080 addendum). No Qt, no plan access.

The phone never edits the plan. These routes read and write only the field-record sidecar
(``services/field_records``), and writes need the phone-permission toggle. Nothing here calls
``run_on_main``, touches the command stack or the scene: the only thing the server learns about
the plan is its saved file path, through the *plan_path* callable the host passes in.

Every request has already passed ``FieldRemoteGuard`` (host, origin, token, rate, body cap), so
this module adds only: the toggle, the unsaved-plan refusal, strict body validation, and error
bodies that never echo input, paths or the token.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    ValidationError,
)

from open_garden_planner.field_remote import limits, projection, server
from open_garden_planner.field_remote.events import EventHub
from open_garden_planner.services import field_records as fr

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import BaseRoute

logger = logging.getLogger(__name__)

PlanPath = Callable[[], str | None]

#: The JSON part of a create/patch; a photo travels as a second multipart part.
MAX_META_BYTES = 128 * 1024
MAX_LIST = 500
_PHOTO_TYPES = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".tif": "image/tiff"}

Number = StrictInt | StrictFloat
Text = Annotated[str, Field(max_length=fr.MAX_TEXT)]
Name = Annotated[str, Field(max_length=fr.MAX_NAME)]
Vertex = Annotated[list[Number], Field(min_length=2, max_length=4)]


class _Strict(BaseModel):
    # forbid: a client can never set status, source, id, photo or placed_object_id
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class PointIn(_Strict):
    type: Literal["point"]
    lat: Number
    lon: Number
    x_cm: Number | None = None
    y_cm: Number | None = None


class PolylineIn(_Strict):
    type: Literal["polyline"]
    points: Annotated[list[Vertex], Field(min_length=2, max_length=fr.MAX_POLYLINE_POINTS)]


Geometry = Annotated[PointIn | PolylineIn, Field(discriminator="type")]


class _Fields(_Strict):
    plant_name: Name | None = None
    species_key: Name | None = None
    age: Name | None = None
    notes: Text | None = None
    geometry: Geometry | None = None
    accuracy_m: Number | None = None
    sigma_m: Number | None = None
    bearing_deg: Number | None = None
    fix_mode: str | None = Field(default=None, max_length=40)
    what: Text | None = None
    requester: Name | None = None
    name: Name | None = None
    precision_hint: str | None = Field(default=None, max_length=40)
    diameter_mm: Number | None = None
    flow_lph: Number | None = None
    emitter_spacing_cm: Number | None = None
    closes_on: StrictBool | None = None
    distances: Annotated[list[Number | dict[str, Any]], Field(max_length=fr.MAX_DISTANCES)] | None = None
    rms: Number | None = None


class RecordIn(_Fields):
    """``POST /api/records`` body."""

    kind: Literal["plant", "irrigation_path", "reference_point", "note", "measurement", "placement_request"]
    idempotency_key: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")]
    group_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")] | None = None
    timestamp: Annotated[str, Field(min_length=10, max_length=40)] | None = None
    lat: Number | None = None
    lon: Number | None = None


class RecordPatch(_Fields):
    """``PATCH /api/records/{id}`` body: any of the editable fields, at least one."""

    lat: Number | None = None
    lon: Number | None = None


def plan_id(path: str) -> str:
    """A short id of the open plan. Writes must quote it, so a plan switched on the computer
    between a phone's read and its write is refused instead of silently receiving the record."""
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]


def _json(payload: dict[str, Any], status: int = 200) -> Response:
    from starlette.responses import JSONResponse

    return JSONResponse(payload, status_code=status)


def _error(status: int, code: str, detail: str = "") -> Response:
    return _json({"error": code, "detail": detail} if detail else {"error": code}, status)


def _problems(error: ValidationError) -> str:
    """Field paths and messages only: the rejected input (which may be large) is never echoed."""
    parts = []
    for item in error.errors(include_input=False, include_url=False, include_context=False)[:5]:
        where = ".".join(str(p) for p in item["loc"]) or "body"
        parts.append(f"{where}: {item['msg']}")
    return "; ".join(parts)


def _view(rec: fr.FieldRecord) -> dict[str, Any]:
    """A record as the phone sees it: the store's photo name becomes a URL."""
    out = rec.to_dict()
    out["photo"] = None
    out.pop("payload_hash", None)
    out["photo_url"] = f"/api/field-records/{rec.id}/photo" if rec.photo else None
    return out


def _store_error(error: fr.FieldRecordError) -> Response:
    if isinstance(error, fr.RecordNotFoundError):
        return _error(404, "not_found", "No such record.")
    if isinstance(error, fr.RecordNotPendingError):
        return _error(409, "not_pending", str(error))
    if isinstance(error, fr.RecordCapError):
        return _error(409, "record_limit", str(error))
    if isinstance(error, fr.RecordConflictError):
        return _error(409, "idempotency_conflict", str(error))
    if isinstance(error, fr.RecordGoneError):
        return _error(410, "withdrawn", str(error))
    if isinstance(error, fr.RecordStorageError):  # detail is logged by the store, never sent
        return _error(503, "storage_unavailable", "The computer cannot save field records right now.")
    return _error(422, "invalid_record", str(error))


async def _read_create(request: Request) -> tuple[RecordIn, bytes | None] | Response:
    """The validated body and optional photo bytes, or the error response."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    photo: bytes | None = None
    try:
        if content_type == "application/json":
            raw = await request.body()
            if len(raw) > MAX_META_BYTES:
                return _error(413, "body_too_large")
        elif content_type == "multipart/form-data":
            form = await request.form(max_files=1, max_fields=2)
            meta = form.get("meta")
            if not isinstance(meta, str):
                return _error(400, "bad_request", "The 'meta' part must be JSON text.")
            raw = meta.encode("utf-8")
            if len(raw) > MAX_META_BYTES:
                return _error(413, "body_too_large")
            part = form.get("photo")
            if part is not None:
                if isinstance(part, str):
                    return _error(400, "bad_request", "The 'photo' part must be a file.")
                photo = await part.read(limits.MAX_PHOTO_BYTES + 1)
                if len(photo) > limits.MAX_PHOTO_BYTES:
                    return _error(413, "photo_too_large", f"Photos may be at most {limits.MAX_PHOTO_BYTES // (1024 * 1024)} MB.")
        else:
            return _error(415, "unsupported_media_type", "Send application/json or multipart/form-data.")
        body = RecordIn.model_validate_json(raw)
    except ValidationError as error:
        return _error(422, "invalid_record", _problems(error))
    except ValueError:  # malformed multipart or non-UTF-8 text
        return _error(400, "bad_request", "The request body could not be read.")
    return body, photo


def records_routes(plan_path: PlanPath) -> list[BaseRoute]:
    """The record routes; *plan_path* returns the saved plan's path, or None while unsaved."""
    from starlette.concurrency import run_in_threadpool
    from starlette.routing import Route

    def saved_plan() -> tuple[str | None, Response | None]:
        path = plan_path()
        if not path:
            return None, _error(409, "plan_not_saved", "Save the plan on the computer first.")
        return path, None

    async def guard_write(request: Request) -> tuple[str | None, Response | None]:
        blocked = server.require_records_enabled(request)
        if blocked is not None:
            return None, blocked
        path, refusal = saved_plan()
        if refusal is not None or path is None:
            return None, refusal
        quoted = request.headers.get("x-plan-id")
        if not quoted:
            return None, _error(400, "plan_id_required", "Send the plan id (from GET /api/field-records) in X-Plan-Id.")
        if quoted != plan_id(path):
            return None, _error(409, "plan_changed", "The plan on the computer changed; reload before saving.")
        return path, None

    async def list_records(request: Request) -> Response:
        path, refusal = saved_plan()
        if refusal is not None or path is None:
            return refusal or _error(409, "plan_not_saved")
        params = request.query_params
        try:
            limit = int(params.get("limit", "200"))
        except ValueError:
            limit = 0
        if not 1 <= limit <= MAX_LIST:
            return _error(400, "bad_request", f"limit must be 1-{MAX_LIST}.")
        status, kind = params.get("status"), params.get("kind")
        if (status is not None and status not in fr.STATUSES) or (kind is not None and kind not in fr.KINDS):
            return _error(400, "bad_request", "Unknown status or kind.")
        loaded = await run_in_threadpool(fr.load_records, path)
        # The phone sees only what it wrote: the agent's and the desktop's records (and the plan's
        # legacy ones) carry GPS fixes and photos that the permission toggle does not cover.
        own = [r for r in loaded.records if r.source == fr.OWN_SOURCE]
        recs = sorted(own, key=lambda r: r.timestamp[:19], reverse=True)
        recs = [r for r in recs if (status is None or r.status == status) and (kind is None or r.kind == kind)]
        return _json(
            {
                "plan_id": plan_id(path),
                "total": len(own),
                "pending": sum(1 for r in own if r.status == "pending"),
                "returned": min(len(recs), limit),
                "records": [_view(r) for r in recs[:limit]],
                "limits": {
                    "max_records": fr.MAX_RECORDS,
                    "max_pending": fr.MAX_PENDING,
                    "max_photo_bytes": limits.MAX_PHOTO_BYTES,
                    "max_polyline_points": fr.MAX_POLYLINE_POINTS,
                },
            }
        )

    async def get_photo(request: Request) -> Response:
        from starlette.responses import Response as RawResponse

        path, refusal = saved_plan()
        if refusal is not None or path is None:
            return refusal or _error(409, "plan_not_saved")
        try:
            rec = await run_in_threadpool(fr.get_record, path, request.path_params["record_id"])
            if rec.source != fr.OWN_SOURCE:
                raise fr.RecordNotFoundError("not the phone's record")
            raw = await run_in_threadpool(fr.read_photo, path, rec)
        except fr.RecordNotFoundError:
            return _error(404, "not_found", "No such record.")
        except fr.FieldRecordError:
            return _error(404, "not_found", "The record has no photo.")
        media = _PHOTO_TYPES.get(rec.photo[rec.photo.rfind("."):] if rec.photo else "", "application/octet-stream")
        return RawResponse(
            raw, media_type=media, headers={"content-disposition": "attachment"}  # nosniff comes from the guard
        )

    async def create(request: Request) -> Response:
        path, refusal = await guard_write(request)
        if refusal is not None or path is None:
            return refusal or _error(409, "plan_not_saved")
        parsed = await _read_create(request)
        if isinstance(parsed, tuple):
            body, photo = parsed
        else:
            return parsed
        data = body.model_dump(exclude_none=True)
        data.update({"status": "pending", "source": "field_remote"})
        try:
            rec, created = await run_in_threadpool(fr.create_record, path, data, photo)
        except fr.FieldRecordError as error:
            return _store_error(error)
        except OSError:
            logger.exception("Field Remote could not write the field notes")
            return _error(500, "storage_error", "The computer could not save the record.")
        return _json({"plan_id": plan_id(path), "record": _view(rec), "created": created}, 201 if created else 200)

    async def patch(request: Request) -> Response:
        path, refusal = await guard_write(request)
        if refusal is not None or path is None:
            return refusal or _error(409, "plan_not_saved")
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return _error(415, "unsupported_media_type", "Send application/json.")
        raw = await request.body()
        try:
            body = RecordPatch.model_validate_json(raw)
        except ValidationError as error:
            return _error(422, "invalid_record", _problems(error))
        changes = body.model_dump(exclude_none=True)
        if not changes:
            return _error(422, "invalid_record", "Nothing to change.")
        try:
            rec = await run_in_threadpool(fr.edit_pending_record, path, request.path_params["record_id"], changes)
        except fr.FieldRecordError as error:
            return _store_error(error)
        except OSError:
            logger.exception("Field Remote could not write the field notes")
            return _error(500, "storage_error", "The computer could not save the record.")
        return _json({"record": _view(rec)})

    async def delete(request: Request) -> Response:
        path, refusal = await guard_write(request)
        if refusal is not None or path is None:
            return refusal or _error(409, "plan_not_saved")
        try:
            rec = await run_in_threadpool(fr.delete_pending_record, path, request.path_params["record_id"])
        except fr.FieldRecordError as error:
            return _store_error(error)
        except OSError:
            logger.exception("Field Remote could not write the field notes")
            return _error(500, "storage_error", "The computer could not save the change.")
        return _json({"deleted": rec.id})

    return [
        Route("/api/field-records", list_records, methods=["GET"]),
        Route("/api/field-records/{record_id}/photo", get_photo, methods=["GET"]),
        Route("/api/records", create, methods=["POST"]),
        Route("/api/records/{record_id}", patch, methods=["PATCH"]),
        Route("/api/records/{record_id}", delete, methods=["DELETE"]),
    ]


# -- read API (WP-C): state, scene, basemap, species, stream, position ---------------------------

#: Seconds one /api/stream connection lives; the phone reconnects with ``since`` (bounded).
STREAM_LIFETIME_S = 60.0
STREAM_POLL_S = 0.5
STREAM_HEARTBEAT_S = 15.0
MAX_STREAMS = 2


class PlanSource(Protocol):
    """What the read routes need of the desktop. Implemented by ``FieldRemoteHost``.

    ``snapshot`` returns a plain ``snapshot_dict`` of the open plan, fetched on the Qt thread
    through the main-thread bridge (it may block, so routes call it from the thread pool).
    It raises ``TimeoutError`` or ``RuntimeError`` when the desktop cannot answer.
    ``on_position`` receives each accepted telemetry dict and may be called from any thread.
    """

    def snapshot(self) -> dict[str, Any]: ...

    def plan_name(self) -> str | None: ...

    def on_position(self, telemetry: dict[str, Any]) -> None: ...


class PositionIn(_Strict):
    """One position report (design 1.6). ``vio``/``imu`` are accepted for later packages and unused."""

    t: Annotated[Number, Field(ge=0)]
    lat: Annotated[Number, Field(ge=-90, le=90)]
    lon: Annotated[Number, Field(ge=-180, le=180)]
    accuracy_m: Annotated[Number, Field(ge=0, le=100_000)]
    alt: Number | None = None
    heading_deg: Annotated[Number, Field(ge=0, le=360)] | None = None
    speed_mps: Annotated[Number, Field(ge=0, le=500)] | None = None
    fix_quality: Literal["none", "gps", "dgps", "rtk_float", "rtk_fixed"] | None = None
    hdop: Annotated[Number, Field(ge=0, le=1000)] | None = None
    sigma_m: Annotated[Number, Field(gt=0, le=100_000)] | None = None
    source: Literal["browser", "nmea_usb", "nmea_ble", "native_fused"] = "browser"
    vio: dict[str, Any] | None = None
    imu: dict[str, Any] | None = None


class PositionBatchIn(_Strict):
    positions: Annotated[list[PositionIn], Field(min_length=1, max_length=projection.MAX_BATCH)]


def _cached(request: Request, body: dict[str, Any], etag: str) -> Response:
    """The JSON *body* with its ETag, or a 304 when the phone already has it."""
    from starlette.responses import Response as RawResponse

    if projection.etag_matches(request.headers.get("if-none-match"), etag):
        return RawResponse(status_code=304, headers={"ETag": etag})
    response = _json(body)
    response.headers["ETag"] = etag
    return response


def _projection_error(error: projection.ProjectionError) -> Response:
    return _error(error.status, error.code, error.detail)


def _ndjson(payload: dict[str, Any]) -> bytes:
    import json

    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def _fix_from(p: PositionIn) -> projection.Fix:
    def opt(value: Any) -> float | None:
        return None if value is None else float(value)

    return projection.Fix(
        t=float(p.t), lat=float(p.lat), lon=float(p.lon), accuracy_m=float(p.accuracy_m), alt=opt(p.alt),
        heading_deg=opt(p.heading_deg), speed_mps=opt(p.speed_mps), fix_quality=p.fix_quality,
        hdop=opt(p.hdop), sigma_m=opt(p.sigma_m), source=p.source,
    )


def read_routes(source: PlanSource, plan_path: PlanPath, hub: EventHub | None = None) -> list[BaseRoute]:
    """The read and position routes. Nothing here mutates the plan or the sidecar."""
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import Response as RawResponse
    from starlette.responses import StreamingResponse
    from starlette.routing import Route

    events = hub if hub is not None else EventHub()
    basemap_cache: dict[str, projection.Basemap] = {}

    async def snapshot() -> tuple[dict[str, Any] | None, Response | None]:
        try:
            return await run_in_threadpool(source.snapshot), None
        except (TimeoutError, RuntimeError):
            return None, _error(503, "plan_unavailable", "The computer is busy; try again.")

    async def state(request: Request) -> Response:
        snap, failure = await snapshot()
        if snap is None:
            return failure or _error(503, "plan_unavailable")
        body, etag = projection.plan_state(
            snap,
            plan_name=source.plan_name(),
            saved=bool(plan_path()),
            records_enabled=request.app.state.field_remote.records_enabled,
        )
        # server_time changes every second, so the ETag (which excludes it) drives the 304.
        return _cached(request, body, etag)

    async def scene(request: Request) -> Response:
        try:
            bbox = projection.parse_bbox(request.query_params.get("bbox"))
            layers = projection.parse_layers(request.query_params.get("layers"))
        except projection.ProjectionError as error:
            return _projection_error(error)
        snap, failure = await snapshot()
        if snap is None:
            return failure or _error(503, "plan_unavailable")
        body, etag = await run_in_threadpool(lambda: projection.project_scene(snap, bbox=bbox, layers=layers))
        return _cached(request, body, etag)

    async def basemap(request: Request) -> Response:
        snap, failure = await snapshot()
        if snap is None:
            return failure or _error(503, "plan_unavailable")
        key = projection.basemap_key(snap)
        if key is None:
            return _error(404, "no_basemap", "The plan has no background image.")
        cached = basemap_cache.get(key)
        if cached is None:
            try:
                cached = await run_in_threadpool(projection.project_basemap, snap)
            except projection.ProjectionError as error:
                return _projection_error(error)
            basemap_cache.clear()  # one background at a time
            basemap_cache[key] = cached
        rect = ",".join(str(cached.rect[k]) for k in ("x_cm", "y_cm", "width_cm", "height_cm"))
        headers = {"ETag": cached.etag, "X-Basemap-Rect": rect}
        if projection.etag_matches(request.headers.get("if-none-match"), cached.etag):
            return RawResponse(status_code=304, headers=headers)
        if request.query_params.get("meta") == "1":  # the rectangle only, no pixels
            meta = {
                "rect": cached.rect,
                "width_px": cached.width_px,
                "height_px": cached.height_px,
                "source_px": list(cached.source_px),
                "units": "cm",
                "y_up": True,
            }
            return _json(meta)
        return RawResponse(cached.jpeg, media_type="image/jpeg", headers=headers)

    async def species(request: Request) -> Response:
        rows = await run_in_threadpool(projection.species_rows, request.query_params.get("q"))
        return _json({"species": rows, "count": len(rows), "limit": projection.SPECIES_LIMIT})

    async def position(request: Request) -> Response:
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return _error(415, "unsupported_media_type", "Send application/json.")
        raw = await request.body()
        try:
            stripped = raw.lstrip()
            if stripped.startswith(b"["):  # a bare array is a batch
                items = PositionBatchIn.model_validate_json(b'{"positions":' + stripped + b"}").positions
            elif stripped.startswith(b"{") and b'"positions"' in stripped[:64]:
                items = PositionBatchIn.model_validate_json(raw).positions
            else:
                items = [PositionIn.model_validate_json(raw)]
        except ValidationError as error:
            return _error(422, "invalid_position", _problems(error))
        fixes = [_fix_from(p) for p in items]
        # A position needs no plan to be echoed: if the desktop cannot answer, x/y stay null.
        snap, _failure = await snapshot()
        geo = projection.georef_of(snap) if snap is not None else None
        try:
            result = projection.telemetry(fixes, geo)
        except projection.ProjectionError as error:
            return _projection_error(error)
        source.on_position(result)
        return _json({"accepted": len(fixes), "telemetry": result})

    async def stream(request: Request) -> Response:
        try:
            since = int(request.query_params.get("since", "-1"))
        except ValueError:
            return _error(400, "bad_request", "since must be an integer.")
        if not events.open_stream(MAX_STREAMS):
            return _error(429, "stream_busy", "Too many streams are open; close one first.")
        hello_records = request.app.state.field_remote.records_enabled

        async def generate():  # type: ignore[no-untyped-def]
            try:
                seen = events.last_seq if since < 0 else since
                yield _ndjson({"type": "hello", "seq": seen, "records_enabled": hello_records})
                started = last_sent = time.monotonic()
                while time.monotonic() - started < STREAM_LIFETIME_S:
                    for event in events.since(seen):
                        seen = event["seq"]
                        yield _ndjson(event)
                        last_sent = time.monotonic()
                    if time.monotonic() - last_sent >= STREAM_HEARTBEAT_S:
                        yield _ndjson({"type": "ping", "t": round(time.time(), 1)})
                        last_sent = time.monotonic()
                    await asyncio.sleep(STREAM_POLL_S)
                yield _ndjson({"type": "end", "seq": seen, "reconnect": True})
            finally:
                events.close_stream()

        return StreamingResponse(generate(), media_type="application/x-ndjson", headers={"X-Accel-Buffering": "no"})

    return [
        Route("/api/state", state, methods=["GET"]),
        Route("/api/scene", scene, methods=["GET"]),
        Route("/api/basemap", basemap, methods=["GET"]),
        Route("/api/species", species, methods=["GET"]),
        Route("/api/position", position, methods=["POST"]),
        Route("/api/stream", stream, methods=["GET"]),
    ]


__all__ = ["EventHub", "PlanSource", "RecordIn", "RecordPatch", "read_routes", "records_routes"]
