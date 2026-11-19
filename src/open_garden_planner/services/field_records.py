"""Sidecar store for field records and their photos (ADR-080).

A *field record* is what someone in the garden notes down: a plant, a place, a
photo, an age. The phone UI (Field Remote) writes them; the Agent API reads them
and can place one on the plan. They live BESIDE the plan, not in it::

    garden.ogp
    garden.fieldnotes.json          <- the records (this module owns the format)
    garden.fieldnotes/photos/       <- photos, named by record id

so the ``.ogp`` format and ``FILE_VERSION`` do not change, a plan opened on an
older build never sees them, and a plan can be copied without its raw photos.

Pure and Qt-free. Writes are atomic (temp file + ``os.replace``) and serialised
by one process-wide lock; a missing, empty, corrupt or newer-than-known file is
tolerated on read (empty list plus a warning), and a corrupt one is moved aside
before the next write instead of being overwritten. Photo files are always named
by the store (``<record-id>.<ext>``, the extension sniffed from the bytes), so a
client-supplied name can never escape the photos folder.
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import os
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from open_garden_planner.core.georef import GeorefError, validate_lat_lon

#: Version of the sidecar JSON. Bump only with a migration in :func:`_migrate`.
FIELD_RECORDS_VERSION = 2
MAX_RECORDS = 5000
#: Records waiting for a decision; a runaway phone cannot fill the review list (design 1.9).
MAX_PENDING = 500
MAX_POLYLINE_POINTS = 2000
MAX_DISTANCES = 50
MAX_PHOTO_BYTES = 25 * 1024 * 1024
MAX_TEXT = 4000
MAX_NAME = 200
SIDECAR_SUFFIX = ".fieldnotes.json"
PHOTO_DIR_SUFFIX = ".fieldnotes"
#: How the position of a record was obtained.
FIX_MODES = frozenset(
    {
        "gps", "exif", "map_tap", "manual", "remote_position",
        "rtk_fixed", "rtk_float", "vio_fused", "anchor_calibrated", "standard",
    }
)
KINDS = frozenset(
    {"plant", "irrigation_path", "reference_point", "note", "measurement", "placement_request"}
)
STATUSES = frozenset({"pending", "approved", "rejected", "placed", "superseded"})
SOURCES = frozenset({"field_remote", "agent", "desktop"})
PRECISION_HINTS = frozenset({"gps", "survey_in", "plan_locked", "trilateration"})
_COORD_LIMIT_CM = 1.0e8

_PHOTO_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "TIFF": ".tif", "MPO": ".jpg"}
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PHOTO_REL_RE = re.compile(r"^photos/[A-Za-z0-9_-]{1,64}\.(jpg|png|webp|tif)$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_KNOWN_KEYS = frozenset(
    {
        "id", "plant_name", "species_key", "age", "notes", "photo", "lat", "lon",
        "accuracy_m", "bearing_deg", "fix_mode", "timestamp", "placed_object_id",
        "kind", "status", "geometry", "sigma_m", "idempotency_key", "group_id", "what",
        "requester", "precision_hint", "source", "name", "diameter_mm", "flow_lph",
        "emitter_spacing_cm", "closes_on", "distances", "rms",
    }
)
#: The v2 keys written only when set, so a v1 record stays as small as it was.
_OPTIONAL_V2 = (
    "geometry", "sigma_m", "idempotency_key", "group_id", "what", "requester", "precision_hint",
    "source", "name", "diameter_mm", "flow_lph", "emitter_spacing_cm", "closes_on", "distances", "rms",
)
# Single writer (ADR-080/081): the Field Remote server runs in the GUI process, so the phone
# routes, the agent tools and the desktop panel are threads of one process under this one lock;
# every read-modify-write below holds it from load to the atomic replace.
_lock = threading.RLock()


class FieldRecordError(ValueError):
    """A record, photo or path was refused (the message is safe to show an agent)."""


class RecordNotFoundError(FieldRecordError):
    """No record has the given id."""


class RecordNotPendingError(FieldRecordError):
    """The phone may only change or withdraw a record that is still pending."""


class RecordCapError(FieldRecordError):
    """The record cap or the pending cap is reached."""


def sidecar_paths(plan_path: str | os.PathLike[str] | None) -> tuple[Path, Path]:
    """``(records json, folder holding photos/)`` for a saved plan path."""
    if plan_path is None or not str(plan_path):
        raise FieldRecordError("Save the plan first: field records live beside the saved .ogp file.")
    plan = Path(plan_path)
    return (
        plan.with_name(plan.stem + SIDECAR_SUFFIX),
        plan.with_name(plan.stem + PHOTO_DIR_SUFFIX),
    )


def _text(value: object, limit: int, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise FieldRecordError(f"{label} must be text")
    value = value.replace("\x00", "").strip()
    if len(value) > limit:
        raise FieldRecordError(f"{label} is longer than {limit} characters")
    return value or None


def _number(value: object, label: str, lo: float, hi: float) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise FieldRecordError(f"{label} must be a finite number")
    if not lo <= value <= hi:
        raise FieldRecordError(f"{label} must be between {lo:g} and {hi:g}")
    return float(value)


def _vertex(value: object) -> list[float | None]:
    """``[lat, lon, x_cm, y_cm]`` (x and y optional) from an untrusted vertex."""
    if not isinstance(value, (list, tuple)) or len(value) not in (2, 4):
        raise FieldRecordError("a geometry vertex must be [lat, lon] or [lat, lon, x_cm, y_cm]")
    try:
        lat, lon = validate_lat_lon(value[0], value[1])
    except GeorefError as error:
        raise FieldRecordError(str(error)) from error
    x = y = None
    if len(value) == 4:
        x = _number(value[2], "x_cm", -_COORD_LIMIT_CM, _COORD_LIMIT_CM)
        y = _number(value[3], "y_cm", -_COORD_LIMIT_CM, _COORD_LIMIT_CM)
        if (x is None) != (y is None):
            raise FieldRecordError("x_cm and y_cm must be given together")
    return [lat, lon, x, y]


def _geometry(value: object) -> dict[str, Any] | None:
    """Validate a point or polyline; keys outside the fixed shape are dropped."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise FieldRecordError("geometry must be an object")
    kind = value.get("type")
    if kind == "point":
        lat, lon, x, y = _vertex([value.get("lat"), value.get("lon"), value.get("x_cm"), value.get("y_cm")])
        return {"type": "point", "lat": lat, "lon": lon, "x_cm": x, "y_cm": y}
    if kind == "polyline":
        points = value.get("points")
        if not isinstance(points, (list, tuple)) or len(points) < 2:
            raise FieldRecordError("a polyline needs at least 2 points")
        if len(points) > MAX_POLYLINE_POINTS:
            raise FieldRecordError(f"a polyline has at most {MAX_POLYLINE_POINTS} points")
        return {"type": "polyline", "points": [_vertex(p) for p in points]}
    raise FieldRecordError("geometry.type must be 'point' or 'polyline'")


def _plain_value(value: object) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    return value is None or isinstance(value, (bool, int)) or (isinstance(value, str) and len(value) <= MAX_NAME)


def _distances(value: object) -> list[Any] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) > MAX_DISTANCES:
        raise FieldRecordError(f"distances must be a list of at most {MAX_DISTANCES} entries")
    out: list[Any] = []
    for item in value:
        if isinstance(item, Mapping):
            if len(item) > 8 or not all(
                isinstance(k, str) and len(k) <= 40 and _plain_value(v) for k, v in item.items()
            ):
                raise FieldRecordError("a distance entry must be a small object of plain values")
            out.append(dict(item))
        else:
            out.append(_number(item, "distance", 0.0, 1.0e7))
    return out


def _derive_status(data: Mapping[str, Any]) -> str:
    return "placed" if data.get("placed_object_id") else "pending"


@dataclass
class FieldRecord:
    """One field observation. ``photo`` is relative to the ``.fieldnotes`` folder."""

    id: str
    plant_name: str | None = None
    species_key: str | None = None
    age: str | None = None
    notes: str | None = None
    photo: str | None = None
    lat: float | None = None
    lon: float | None = None
    accuracy_m: float | None = None
    bearing_deg: float | None = None
    fix_mode: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    placed_object_id: str | None = None
    #: Schema v2 (design 1.9). ``kind`` plant = a v1 record; ``status`` is the reviewer's decision.
    kind: str = "plant"
    status: str = "pending"
    #: ``{"type": "point", lat, lon, x_cm, y_cm}`` or ``{"type": "polyline", "points": [[lat, lon, x, y], ...]}``.
    geometry: dict[str, Any] | None = None
    sigma_m: float | None = None
    idempotency_key: str | None = None
    group_id: str | None = None
    what: str | None = None
    requester: str | None = None
    precision_hint: str | None = None
    source: str | None = None
    name: str | None = None
    diameter_mm: float | None = None
    flow_lph: float | None = None
    emitter_spacing_cm: float | None = None
    closes_on: bool | None = None
    distances: list[Any] | None = None
    rms: float | None = None
    #: Keys this build does not know (a newer writer, the Field Remote); kept verbatim.
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        optional = {k: getattr(self, k) for k in _OPTIONAL_V2 if getattr(self, k) is not None}
        return {
            **self.extra,
            **optional,
            "kind": self.kind,
            "status": self.status,
            "id": self.id,
            "plant_name": self.plant_name,
            "species_key": self.species_key,
            "age": self.age,
            "notes": self.notes,
            "photo": self.photo,
            "lat": self.lat,
            "lon": self.lon,
            "accuracy_m": self.accuracy_m,
            "bearing_deg": self.bearing_deg,
            "fix_mode": self.fix_mode,
            "timestamp": self.timestamp,
            "placed_object_id": self.placed_object_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, record_id: str | None = None) -> FieldRecord:
        """Validate an untrusted mapping (an agent, the phone, or the file)."""
        if not isinstance(data, Mapping):
            raise FieldRecordError("a field record must be an object")
        rid = record_id if record_id is not None else data.get("id")
        if rid is None:
            rid = uuid.uuid4().hex
        if not isinstance(rid, str) or not _ID_RE.match(rid):
            raise FieldRecordError("id must be 1-64 letters, digits, '-' or '_'")
        lat, lon = data.get("lat"), data.get("lon")
        if (lat is None) != (lon is None):
            raise FieldRecordError("lat and lon must be given together")
        if lat is not None:
            try:
                lat, lon = validate_lat_lon(lat, lon)
            except GeorefError as error:
                raise FieldRecordError(str(error)) from error
        photo = data.get("photo")
        if photo is not None and (not isinstance(photo, str) or not _PHOTO_REL_RE.match(photo)):
            raise FieldRecordError("photo must be a path the store issued (photos/<id>.<ext>)")
        fix_mode = _text(data.get("fix_mode"), 40, "fix_mode")
        if fix_mode is not None and fix_mode not in FIX_MODES:
            raise FieldRecordError("fix_mode must be one of " + ", ".join(sorted(FIX_MODES)))
        bearing = _number(data.get("bearing_deg"), "bearing_deg", 0.0, 360.0)
        stamp = data.get("timestamp")
        if stamp is None:
            stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not isinstance(stamp, str) or not 10 <= len(stamp) <= 40:
            raise FieldRecordError("timestamp must be an ISO date-time text")
        placed = _text(data.get("placed_object_id"), 64, "placed_object_id")
        extra = {k: v for k, v in data.items() if isinstance(k, str) and k not in _KNOWN_KEYS}
        kind = data.get("kind", "plant")
        if kind not in KINDS:
            raise FieldRecordError("kind must be one of " + ", ".join(sorted(KINDS)))
        status = data.get("status")
        if status is None:
            status = _derive_status(data)
        if status not in STATUSES:
            raise FieldRecordError("status must be one of " + ", ".join(sorted(STATUSES)))
        geometry = _geometry(data.get("geometry"))
        if geometry is not None and geometry["type"] == "point" and lat is None:
            lat, lon = geometry["lat"], geometry["lon"]
        key = data.get("idempotency_key")
        if key is not None and (not isinstance(key, str) or not _KEY_RE.match(key)):
            raise FieldRecordError("idempotency_key must be 8-64 letters, digits, '-' or '_'")
        source = _text(data.get("source"), 40, "source")
        if source is not None and source not in SOURCES:
            raise FieldRecordError("source must be one of " + ", ".join(sorted(SOURCES)))
        hint = _text(data.get("precision_hint"), 40, "precision_hint")
        if hint is not None and hint not in PRECISION_HINTS:
            raise FieldRecordError("precision_hint must be one of " + ", ".join(sorted(PRECISION_HINTS)))
        closes = data.get("closes_on")
        if closes is not None and not isinstance(closes, bool):
            raise FieldRecordError("closes_on must be true or false")
        return cls(
            id=rid,
            plant_name=_text(data.get("plant_name"), MAX_NAME, "plant_name"),
            species_key=_text(data.get("species_key"), MAX_NAME, "species_key"),
            age=_text(data.get("age"), MAX_NAME, "age"),
            notes=_text(data.get("notes"), MAX_TEXT, "notes"),
            photo=photo,
            lat=lat,
            lon=lon,
            accuracy_m=_number(data.get("accuracy_m"), "accuracy_m", 0.0, 100000.0),
            bearing_deg=None if bearing is None else bearing % 360.0,
            fix_mode=fix_mode,
            timestamp=stamp,
            placed_object_id=placed,
            kind=kind,
            status=status,
            geometry=geometry,
            sigma_m=_number(data.get("sigma_m"), "sigma_m", 0.0, 100000.0),
            idempotency_key=key,
            group_id=_text(data.get("group_id"), 64, "group_id"),
            what=_text(data.get("what"), MAX_TEXT, "what"),
            requester=_text(data.get("requester"), MAX_NAME, "requester"),
            precision_hint=hint,
            source=source,
            name=_text(data.get("name"), MAX_NAME, "name"),
            diameter_mm=_number(data.get("diameter_mm"), "diameter_mm", 0.0, 10000.0),
            flow_lph=_number(data.get("flow_lph"), "flow_lph", 0.0, 1.0e6),
            emitter_spacing_cm=_number(data.get("emitter_spacing_cm"), "emitter_spacing_cm", 0.0, 100000.0),
            closes_on=closes,
            distances=_distances(data.get("distances")),
            rms=_number(data.get("rms"), "rms", 0.0, 1.0e6),
            extra=extra,
        )


@dataclass
class LoadResult:
    records: list[FieldRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: True when the file exists but could not be trusted (corrupt / newer version).
    damaged: bool = False
    #: True when the file was written by a newer build; never overwritten.
    newer_version: bool = False
    #: True when the file exists but could not be READ (permissions, sharing
    #: violation, I/O error): its content is unknown, so nothing may be written.
    unreadable: bool = False
    #: Top-level keys this build does not know; written back unchanged.
    extra: dict[str, Any] = field(default_factory=dict)
    #: Raw entries that failed validation, duplicated an id or lay beyond
    #: ``MAX_RECORDS``; written back unchanged so a save loses nothing.
    rejected: list[Any] = field(default_factory=list)


def _migrate(data: dict[str, Any], version: int) -> dict[str, Any]:
    """Bring an older sidecar up to :data:`FIELD_RECORDS_VERSION`.

    1 -> 2: every record gains ``kind`` (plant) and ``status`` (placed when it has a
    ``placed_object_id``, else pending); nothing else is touched and unknown keys stay.
    Entries that are not objects are left for the loader to reject and keep.
    """
    if version < 2:
        migrated: list[Any] = []
        for entry in data.get("records", []):
            if isinstance(entry, dict):
                entry = {**entry}
                entry.setdefault("kind", "plant")
                entry.setdefault("status", _derive_status(entry))
            migrated.append(entry)
        data = {**data, "records": migrated}
    return data


def load_records(plan_path: str | os.PathLike[str] | None) -> LoadResult:
    """Read the sidecar of *plan_path*; never raises for a missing or bad file."""
    json_path, _ = sidecar_paths(plan_path)
    result = LoadResult()
    with _lock:
        try:
            raw = json_path.read_bytes()
        except FileNotFoundError:
            return result
        except OSError as error:
            result.warnings.append(f"cannot read the field notes file: {error.strerror or error}")
            result.unreadable = True
            return result
    if not raw.strip():
        return result
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("records", []), list):
            raise ValueError("unexpected structure")
        version = data.get("version", 1)
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError("bad version")
    except (ValueError, UnicodeDecodeError) as error:
        result.warnings.append(f"the field notes file is corrupt and was ignored ({error})")
        result.damaged = True
        return result
    original = list(data.get("records", []))  # entries a build rejects are kept as written, unmigrated
    if version > FIELD_RECORDS_VERSION:
        result.warnings.append(
            f"the field notes file is version {version}, newer than this build understands "
            f"({FIELD_RECORDS_VERSION}); showing what can be read, and not modifying it"
        )
        result.damaged = result.newer_version = True
    else:
        data = _migrate(data, version)
    result.extra = {k: v for k, v in data.items() if k not in ("version", "records")}
    seen: set[str] = set()
    for index, entry in enumerate(data.get("records", [])):
        if len(result.records) >= MAX_RECORDS:
            result.rejected.append(original[index])
            result.warnings.append(f"record {index + 1} kept unread: more than {MAX_RECORDS} records")
            continue
        try:
            rec = FieldRecord.from_dict(entry)
        except FieldRecordError as error:
            result.rejected.append(original[index])
            result.warnings.append(f"record {index + 1} skipped (kept in the file): {error}")
            continue
        if rec.id in seen:
            result.rejected.append(original[index])
            result.warnings.append(f"record {index + 1} skipped (kept in the file): duplicate id {rec.id}")
            continue
        seen.add(rec.id)
        result.records.append(rec)
    return result


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def save_records(
    plan_path: str | os.PathLike[str] | None,
    records: list[FieldRecord],
    base: LoadResult | None = None,
) -> None:
    """Atomically write *records*; the one load -> merge -> write path.

    *base* is the :class:`LoadResult` the caller built *records* from (so the
    file is not re-read between merge and write); it is loaded here when
    omitted. Refused when the file could not be read, or is newer than this
    build. A corrupt file is kept as ``.corrupt-<time>-<id>``. Unknown keys and
    entries this build rejected are written back unchanged.
    """
    json_path, _ = sidecar_paths(plan_path)
    if len(records) > MAX_RECORDS:
        raise FieldRecordError(f"at most {MAX_RECORDS} field records are kept per plan")
    with _lock:
        existing = base if base is not None else load_records(plan_path)
        if existing.unreadable:
            raise FieldRecordError(
                "the field notes file could not be read; refusing to write over it ("
                + "; ".join(existing.warnings)
                + ")"
            )
        if existing.newer_version:
            raise FieldRecordError(
                "the field notes file was written by a newer version of the app; refusing to overwrite it"
            )
        document = {
            **existing.extra,
            "version": FIELD_RECORDS_VERSION,
            "records": [r.to_dict() for r in records] + list(existing.rejected),
        }
        payload = json.dumps(document, indent=2, ensure_ascii=False).encode("utf-8")
        if existing.damaged and json_path.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
            try:
                os.replace(json_path, json_path.with_name(json_path.name + f".corrupt-{stamp}"))
            except OSError as error:
                raise FieldRecordError(f"cannot set the damaged field notes file aside: {error}") from error
        _write_atomic(json_path, payload)


def sniff_photo(raw: bytes) -> str:
    """The file extension of a photo's real format; refuses non-images and oversized data."""
    if not raw:
        raise FieldRecordError("the photo is empty")
    if len(raw) > MAX_PHOTO_BYTES:
        raise FieldRecordError(f"the photo is larger than {MAX_PHOTO_BYTES // (1024 * 1024)} MB")
    try:
        with Image.open(io.BytesIO(raw)) as img:
            fmt = img.format or ""
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
        raise FieldRecordError("the photo is not a supported image (jpg, png, webp, tiff)") from error
    ext = _PHOTO_FORMATS.get(fmt)
    if ext is None:
        raise FieldRecordError(f"image format {fmt or 'unknown'} is not supported for field photos")
    return ext


def _load_for_write(plan_path: str | os.PathLike[str] | None) -> LoadResult:
    """Load the sidecar for a read-modify-write; refuses a file whose content is unknown."""
    loaded = load_records(plan_path)
    if loaded.unreadable:
        raise FieldRecordError(
            "the field notes file could not be read, so nothing was changed (" + "; ".join(loaded.warnings) + ")"
        )
    if loaded.newer_version:
        raise FieldRecordError(
            "the field notes file was written by a newer version of the app; refusing to change it"
        )
    return loaded


def _pending_count(records: list[FieldRecord]) -> int:
    return sum(1 for r in records if r.status == "pending")


def create_record(
    plan_path: str | os.PathLike[str] | None,
    data: Mapping[str, Any],
    photo_bytes: bytes | None = None,
) -> tuple[FieldRecord, bool]:
    """Validate and append one record; ``(record, created)``.

    A record whose ``idempotency_key`` is already stored is NOT added again: the stored one is
    returned with ``created=False`` (a phone's retry from its outbox). The check and the write
    sit under one lock hold, so two racing retries create one record. The photo is written
    before the record; if the record write then fails the photo is removed again.
    """
    _, photo_root = sidecar_paths(plan_path)
    with _lock:
        loaded = load_records(plan_path)
        if loaded.unreadable:
            raise FieldRecordError(
                "the field notes file could not be read, so nothing was added ("
                + "; ".join(loaded.warnings)
                + ")"
            )
        key = data.get("idempotency_key")
        if isinstance(key, str):
            for existing in loaded.records:
                if existing.idempotency_key == key:
                    return existing, False
        if len(loaded.records) >= MAX_RECORDS:
            raise RecordCapError(f"at most {MAX_RECORDS} field records are kept per plan")
        record_id = uuid.uuid4().hex
        known = {k: v for k, v in dict(data).items() if k in _KNOWN_KEYS}
        record = FieldRecord.from_dict({**known, "photo": None}, record_id=record_id)
        if record.status == "pending" and _pending_count(loaded.records) >= MAX_PENDING:
            raise RecordCapError(f"at most {MAX_PENDING} records may wait for review; approve or reject some first")
        photo_file: Path | None = None
        if photo_bytes is not None:
            ext = sniff_photo(photo_bytes)
            record.photo = f"photos/{record_id}{ext}"
            photo_file = photo_root / record.photo
            _write_atomic(photo_file, photo_bytes)
        try:
            save_records(plan_path, [*loaded.records, record], loaded)
        except BaseException:
            if photo_file is not None:
                with contextlib.suppress(OSError):
                    photo_file.unlink()
            raise
    return record, True


def add_record(
    plan_path: str | os.PathLike[str] | None,
    data: Mapping[str, Any],
    photo_bytes: bytes | None = None,
) -> FieldRecord:
    """:func:`create_record` without the ``created`` flag."""
    return create_record(plan_path, data, photo_bytes)[0]


def get_record(plan_path: str | os.PathLike[str] | None, record_id: str) -> FieldRecord:
    for rec in load_records(plan_path).records:
        if rec.id == record_id:
            return rec
    raise RecordNotFoundError(f"no field record {record_id!r}")


def find_by_idempotency_key(plan_path: str | os.PathLike[str] | None, key: str) -> FieldRecord | None:
    """The record stored under a client's ``idempotency_key``, if any."""
    for rec in load_records(plan_path).records:
        if rec.idempotency_key == key:
            return rec
    return None


#: What the phone may change on its own pending record (no status, no placement link).
PHONE_EDITABLE = frozenset(
    {
        "plant_name", "species_key", "age", "notes", "geometry", "lat", "lon", "accuracy_m", "sigma_m",
        "bearing_deg", "fix_mode", "what", "requester", "name", "diameter_mm", "flow_lph",
        "emitter_spacing_cm", "closes_on", "distances", "rms", "precision_hint",
    }
)


def update_record(
    plan_path: str | os.PathLike[str] | None, record_id: str, **changes: Any
) -> FieldRecord:
    """Change fields of one record (validated as a whole). ``id``, ``photo`` and the key are fixed."""
    forbidden = {"id", "photo", "idempotency_key"} & set(changes)
    if forbidden:
        raise FieldRecordError("id, photo and idempotency_key cannot be changed")
    with _lock:
        loaded = _load_for_write(plan_path)
        for index, rec in enumerate(loaded.records):
            if rec.id == record_id:
                merged = {**rec.to_dict(), **changes}  # to_dict carries unknown keys
                updated = FieldRecord.from_dict(merged)
                if (
                    updated.status == "pending"
                    and rec.status != "pending"
                    and _pending_count(loaded.records) >= MAX_PENDING
                ):
                    raise RecordCapError(f"at most {MAX_PENDING} records may wait for review")
                loaded.records[index] = updated
                save_records(plan_path, loaded.records, loaded)
                return updated
    raise RecordNotFoundError(f"no field record {record_id!r}")


def edit_pending_record(
    plan_path: str | os.PathLike[str] | None, record_id: str, changes: Mapping[str, Any]
) -> FieldRecord:
    """The phone's PATCH: only whitelisted fields, only while the record is pending.

    The status check and the write share one lock hold, so a desktop approval that lands first
    wins and the edit is refused rather than applied to a placed record.
    """
    unknown = set(changes) - PHONE_EDITABLE
    if unknown:
        raise FieldRecordError("these fields cannot be changed from the phone: " + ", ".join(sorted(unknown)))
    with _lock:
        current = get_record(plan_path, record_id)
        if current.status != "pending":
            raise RecordNotPendingError("the record was already decided on the computer and can no longer be changed")
        return update_record(plan_path, record_id, **dict(changes))


def delete_pending_record(plan_path: str | os.PathLike[str] | None, record_id: str) -> FieldRecord:
    """The phone's DELETE: withdraw a record (and its photo) while it is still pending."""
    with _lock:
        loaded = _load_for_write(plan_path)
        for index, rec in enumerate(loaded.records):
            if rec.id != record_id:
                continue
            if rec.status != "pending":
                raise RecordNotPendingError("the record was already decided on the computer and cannot be withdrawn")
            del loaded.records[index]
            save_records(plan_path, loaded.records, loaded)
            if rec.photo:
                with contextlib.suppress(FieldRecordError, OSError):
                    resolve_photo(plan_path, rec).unlink()
            return rec
    raise RecordNotFoundError(f"no field record {record_id!r}")


def supersede_siblings(plan_path: str | os.PathLike[str] | None, record_id: str) -> list[FieldRecord]:
    """Approve one candidate of a group: its still-open siblings become ``superseded``.

    One load and one write, so a reader never sees half the group changed. The chosen record
    itself is untouched (the caller sets ``placed``). Open means pending or approved; rejected
    siblings stay rejected. A record without a ``group_id`` has no siblings.
    """
    with _lock:
        loaded = _load_for_write(plan_path)
        chosen = next((r for r in loaded.records if r.id == record_id), None)
        if chosen is None:
            raise RecordNotFoundError(f"no field record {record_id!r}")
        if not chosen.group_id:
            return []
        changed = [
            r for r in loaded.records
            if r.group_id == chosen.group_id and r.id != chosen.id and r.status in ("pending", "approved")
        ]
        for rec in changed:
            rec.status = "superseded"
        if changed:
            save_records(plan_path, loaded.records, loaded)
        return changed


def reopen_group(
    plan_path: str | os.PathLike[str] | None, group_id: str, *, include_rejected: bool = False
) -> list[FieldRecord]:
    """Return a group to review: superseded and placed candidates become ``pending`` again.

    A placed candidate loses its ``placed_object_id`` (its object was undone or removed). Rejected
    candidates stay rejected unless *include_rejected*. Refused when the pending cap would be exceeded.
    """
    with _lock:
        loaded = _load_for_write(plan_path)
        states = {"superseded", "placed", "approved"} | ({"rejected"} if include_rejected else set())
        changed = [r for r in loaded.records if r.group_id == group_id and r.status in states]
        if not changed:
            return []
        if _pending_count(loaded.records) + len(changed) > MAX_PENDING:
            raise RecordCapError(f"reopening would leave more than {MAX_PENDING} records waiting for review")
        for rec in changed:
            rec.status = "pending"
            rec.placed_object_id = None
        save_records(plan_path, loaded.records, loaded)
        return changed


def resolve_photo(plan_path: str | os.PathLike[str] | None, record: FieldRecord) -> Path:
    """Absolute path of a record's photo, proven to sit inside the photos folder."""
    if not record.photo or not _PHOTO_REL_RE.match(record.photo):
        raise FieldRecordError("the record has no photo")
    _, root = sidecar_paths(plan_path)
    path = (root / record.photo).resolve()
    if not path.is_relative_to(root.resolve()):
        raise FieldRecordError("the photo path escapes the field notes folder")
    if not path.is_file():
        raise FieldRecordError("the photo file is missing")
    return path


def read_photo(plan_path: str | os.PathLike[str] | None, record: FieldRecord) -> bytes:
    """The bytes of a record's photo (size-capped)."""
    path = resolve_photo(plan_path, record)
    if path.stat().st_size > MAX_PHOTO_BYTES:
        raise FieldRecordError("the photo file is larger than the allowed size")
    return path.read_bytes()
