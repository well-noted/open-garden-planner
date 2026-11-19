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
FIELD_RECORDS_VERSION = 1
MAX_RECORDS = 5000
MAX_PHOTO_BYTES = 25 * 1024 * 1024
MAX_TEXT = 4000
MAX_NAME = 200
SIDECAR_SUFFIX = ".fieldnotes.json"
PHOTO_DIR_SUFFIX = ".fieldnotes"
#: How the position of a record was obtained.
FIX_MODES = frozenset({"gps", "exif", "map_tap", "manual", "remote_position"})

_PHOTO_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "TIFF": ".tif", "MPO": ".jpg"}
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PHOTO_REL_RE = re.compile(r"^photos/[A-Za-z0-9_-]{1,64}\.(jpg|png|webp|tif)$")
_lock = threading.RLock()


class FieldRecordError(ValueError):
    """A record, photo or path was refused (the message is safe to show an agent)."""


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

    def to_dict(self) -> dict[str, Any]:
        return {
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
        )


@dataclass
class LoadResult:
    records: list[FieldRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: True when the file exists but could not be trusted (corrupt / newer version).
    damaged: bool = False
    #: True when the file was written by a newer build; never overwritten.
    newer_version: bool = False


def _migrate(data: dict[str, Any], _version: int) -> dict[str, Any]:
    """Bring an older sidecar up to :data:`FIELD_RECORDS_VERSION` (none older yet)."""
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
            result.damaged = True
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
    if version > FIELD_RECORDS_VERSION:
        result.warnings.append(
            f"the field notes file is version {version}, newer than this build understands "
            f"({FIELD_RECORDS_VERSION}); showing what can be read, and not modifying it"
        )
        result.damaged = result.newer_version = True
    else:
        data = _migrate(data, version)
    seen: set[str] = set()
    for index, entry in enumerate(data.get("records", [])[:MAX_RECORDS]):
        try:
            rec = FieldRecord.from_dict(entry)
        except FieldRecordError as error:
            result.warnings.append(f"record {index + 1} skipped: {error}")
            continue
        if rec.id in seen:
            result.warnings.append(f"record {index + 1} skipped: duplicate id {rec.id}")
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


def save_records(plan_path: str | os.PathLike[str] | None, records: list[FieldRecord]) -> None:
    """Atomically write *records*. A corrupt existing file is kept as ``.corrupt-<ts>``."""
    json_path, _ = sidecar_paths(plan_path)
    if len(records) > MAX_RECORDS:
        raise FieldRecordError(f"at most {MAX_RECORDS} field records are kept per plan")
    payload = json.dumps(
        {"version": FIELD_RECORDS_VERSION, "records": [r.to_dict() for r in records]},
        indent=2,
        ensure_ascii=False,
    ).encode("utf-8")
    with _lock:
        existing = load_records(plan_path)
        if existing.newer_version:
            raise FieldRecordError(
                "the field notes file was written by a newer version of the app; refusing to overwrite it"
            )
        if existing.damaged and json_path.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S")
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


def add_record(
    plan_path: str | os.PathLike[str] | None,
    data: Mapping[str, Any],
    photo_bytes: bytes | None = None,
) -> FieldRecord:
    """Validate and append one record, saving *photo_bytes* beside it first.

    The photo is written before the record; if the record write then fails the
    photo is removed again, so a failure leaves nothing behind.
    """
    _, photo_root = sidecar_paths(plan_path)
    with _lock:
        loaded = load_records(plan_path)
        if len(loaded.records) >= MAX_RECORDS:
            raise FieldRecordError(f"at most {MAX_RECORDS} field records are kept per plan")
        record_id = uuid.uuid4().hex
        record = FieldRecord.from_dict({**dict(data), "photo": None}, record_id=record_id)
        photo_file: Path | None = None
        if photo_bytes is not None:
            ext = sniff_photo(photo_bytes)
            record.photo = f"photos/{record_id}{ext}"
            photo_file = photo_root / record.photo
            _write_atomic(photo_file, photo_bytes)
        try:
            save_records(plan_path, [*loaded.records, record])
        except BaseException:
            if photo_file is not None:
                with contextlib.suppress(OSError):
                    photo_file.unlink()
            raise
    return record


def get_record(plan_path: str | os.PathLike[str] | None, record_id: str) -> FieldRecord:
    for rec in load_records(plan_path).records:
        if rec.id == record_id:
            return rec
    raise FieldRecordError(f"no field record {record_id!r}")


def update_record(
    plan_path: str | os.PathLike[str] | None, record_id: str, **changes: Any
) -> FieldRecord:
    """Change fields of one record (validated as a whole). ``id`` and ``photo`` are fixed."""
    forbidden = {"id", "photo"} & set(changes)
    if forbidden:
        raise FieldRecordError("id and photo cannot be changed")
    with _lock:
        loaded = load_records(plan_path)
        for index, rec in enumerate(loaded.records):
            if rec.id == record_id:
                merged = {**rec.to_dict(), **changes}
                updated = FieldRecord.from_dict(merged)
                loaded.records[index] = updated
                save_records(plan_path, loaded.records)
                return updated
    raise FieldRecordError(f"no field record {record_id!r}")


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
