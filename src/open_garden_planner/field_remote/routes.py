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

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    ValidationError,
)

from open_garden_planner.field_remote import limits, server
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
    group_id: Annotated[str, Field(max_length=64)] | None = None
    timestamp: Annotated[str, Field(min_length=10, max_length=40)] | None = None
    lat: Number | None = None
    lon: Number | None = None


class RecordPatch(_Fields):
    """``PATCH /api/records/{id}`` body: any of the editable fields, at least one."""

    lat: Number | None = None
    lon: Number | None = None


#: kind -> (geometry type it needs or None, further required fields)
_KIND_RULES: dict[str, tuple[str | None, tuple[str, ...]]] = {
    "irrigation_path": ("polyline", ()),
    "reference_point": ("point", ("name",)),
    "placement_request": ("point", ("what", "group_id")),
}


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
    out["photo_url"] = f"/api/field-records/{rec.id}/photo" if rec.photo else None
    return out


def _store_error(error: fr.FieldRecordError) -> Response:
    if isinstance(error, fr.RecordNotFoundError):
        return _error(404, "not_found", "No such record.")
    if isinstance(error, fr.RecordNotPendingError):
        return _error(409, "not_pending", str(error))
    if isinstance(error, fr.RecordCapError):
        return _error(409, "record_limit", str(error))
    return _error(422, "invalid_record", str(error))


def _check_kind(body: RecordIn) -> str | None:
    needed_geometry, needed_fields = _KIND_RULES.get(body.kind, (None, ()))
    if needed_geometry is not None and (body.geometry is None or body.geometry.type != needed_geometry):
        return f"{body.kind} needs a {needed_geometry} geometry"
    for name in needed_fields:
        if getattr(body, name) is None:
            return f"{body.kind} needs {name}"
    return None


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
        return saved_plan()

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
        recs = sorted(loaded.records, key=lambda r: r.timestamp[:19], reverse=True)
        recs = [r for r in recs if (status is None or r.status == status) and (kind is None or r.kind == kind)]
        return _json(
            {
                "total": len(loaded.records),
                "pending": sum(1 for r in loaded.records if r.status == "pending"),
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
            raw = await run_in_threadpool(fr.read_photo, path, rec)
        except fr.RecordNotFoundError:
            return _error(404, "not_found", "No such record.")
        except fr.FieldRecordError:
            return _error(404, "not_found", "The record has no photo.")
        media = _PHOTO_TYPES.get(rec.photo[rec.photo.rfind("."):] if rec.photo else "", "application/octet-stream")
        return RawResponse(raw, media_type=media)

    async def create(request: Request) -> Response:
        path, refusal = await guard_write(request)
        if refusal is not None or path is None:
            return refusal or _error(409, "plan_not_saved")
        parsed = await _read_create(request)
        if isinstance(parsed, tuple):
            body, photo = parsed
        else:
            return parsed
        problem = _check_kind(body)
        if problem:
            return _error(422, "invalid_record", problem)
        data = body.model_dump(exclude_none=True)
        data.update({"status": "pending", "source": "field_remote"})
        try:
            rec, created = await run_in_threadpool(fr.create_record, path, data, photo)
        except fr.FieldRecordError as error:
            return _store_error(error)
        except OSError:
            logger.exception("Field Remote could not write the field notes")
            return _error(500, "storage_error", "The computer could not save the record.")
        return _json({"record": _view(rec), "created": created}, 201 if created else 200)

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


__all__ = ["RecordIn", "RecordPatch", "records_routes"]
