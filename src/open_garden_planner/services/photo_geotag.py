"""Read the GPS fix, compass bearing and lens of a photo's EXIF (ADR-080).

Pure and Qt-free (Pillow only, already a dependency). Everything is tolerant:
a photo with no EXIF, a partial GPS block, an out-of-range value or a corrupt
file yields a partial :class:`PhotoGeotag` plus ``warnings`` — never an
exception for bad *content* (only for a bad *source*: not an image at all, too
large, unreadable path).

Then :func:`project_to_plan` turns the camera position into canvas centimetres
through :class:`~open_garden_planner.core.georef.GeoReference` and the compass
bearing into a canvas direction. With ``a`` the plan's north angle (clockwise
from plan-up to true north) a compass bearing ``b`` (clockwise from true north)
points along canvas ``(sin(a + b), cos(a + b))`` on the Y-up scene.

The horizontal field of view is an ESTIMATE: from the 35 mm-equivalent focal
length when the photo carries it, else from the real focal length with an
assumed phone-class crop factor, else a fixed default. ``fov_source`` says which.
"""

from __future__ import annotations

import base64
import binascii
import io
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from open_garden_planner.core.georef import GeoReference, GeorefError

#: Largest photo this module will open, bytes (a phone JPEG is 2-8 MB).
MAX_PHOTO_BYTES = 25 * 1024 * 1024
#: Extensions accepted for a path source.
PHOTO_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"})

#: Horizontal FOV used when the photo says nothing about its lens (about a 28 mm
#: equivalent landscape frame). An estimate, reported as such.
DEFAULT_HFOV_DEG = 65.0
#: Crop factor assumed when only the real focal length is known (phone sensors).
ASSUMED_CROP_FACTOR = 6.0
#: Width of a full-frame 35 mm sensor, mm.
_FULL_FRAME_WIDTH_MM = 36.0

_TAG_EXIF_IFD = 0x8769
_TAG_GPS_IFD = 0x8825
_TAG_DATETIME = 0x0132
_TAG_DATETIME_ORIGINAL = 0x9003
_TAG_FOCAL_LENGTH = 0x920A
_TAG_FOCAL_LENGTH_35 = 0xA405
_GPS_LAT_REF, _GPS_LAT, _GPS_LON_REF, _GPS_LON = 1, 2, 3, 4
_GPS_ALT_REF, _GPS_ALT = 5, 6
_GPS_DOP = 11
_GPS_DIR_REF, _GPS_DIR = 16, 17


class PhotoGeotagError(ValueError):
    """The photo source itself is unusable (not an image, too large, unreadable)."""


@dataclass
class PhotoGeotag:
    """What the EXIF of one photo says. Every field may be ``None``."""

    lat: float | None = None
    lon: float | None = None
    altitude_m: float | None = None
    gps_accuracy_m: float | None = None
    bearing_deg: float | None = None
    bearing_reference: str | None = None  # "true" | "magnetic"
    focal_length_mm: float | None = None
    focal_length_35mm: float | None = None
    taken_at: str | None = None  # ISO "YYYY-MM-DDTHH:MM:SS", camera local time
    width_px: int | None = None  # as DISPLAYED (EXIF Orientation applied)
    height_px: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "lat": self.lat,
            "lon": self.lon,
            "altitude_m": self.altitude_m,
            "gps_accuracy_m": self.gps_accuracy_m,
            "bearing_deg": self.bearing_deg,
            "bearing_reference": self.bearing_reference,
            "focal_length_mm": self.focal_length_mm,
            "focal_length_35mm": self.focal_length_35mm,
            "taken_at": self.taken_at,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------
# Opening a source
# --------------------------------------------------------------------------


def _safe_local_path(path: str) -> Path:
    """*path* resolved, refused when it names a network share or a device.

    ``\\\\host\\share`` would make the app open an SMB connection (leaking the
    user's NTLM hash to a hostile host); ``\\\\?\\`` and ``\\\\.\\`` reach devices.
    Checked before AND after ``resolve()`` (a mapped path can resolve to a UNC one).
    """
    if "\x00" in path:
        raise PhotoGeotagError("the path is invalid")

    def bad(text: str) -> bool:
        return text.startswith(("\\\\", "//"))

    if bad(path):
        raise PhotoGeotagError("network and device paths are not accepted; use a local file")
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise PhotoGeotagError("the path is invalid") from error
    if bad(str(resolved)):
        raise PhotoGeotagError("network and device paths are not accepted; use a local file")
    return resolved


def photo_bytes_from_source(path: str | None = None, data_base64: str | None = None) -> bytes:
    """The raw bytes of a photo given as a file path OR base64 (exactly one)."""
    if (path is None) == (data_base64 is None):
        raise PhotoGeotagError("give exactly one of path or data_base64")
    if data_base64 is not None:
        text = data_base64.strip()
        if text.lower().startswith("data:") and "," in text:  # data URL
            text = text.split(",", 1)[1]
        # Bound before decoding: base64 inflates by 4/3.
        if len(text) > (MAX_PHOTO_BYTES * 4) // 3 + 8:
            raise PhotoGeotagError(f"the photo is larger than {MAX_PHOTO_BYTES // (1024 * 1024)} MB")
        try:
            raw = base64.b64decode(text, validate=False)
        except (binascii.Error, ValueError) as error:
            raise PhotoGeotagError("data_base64 is not valid base64") from error
    else:
        assert path is not None
        p = _safe_local_path(path)
        if p.suffix.lower() not in PHOTO_EXTENSIONS:
            raise PhotoGeotagError("the path must point to an image file (jpg, png, webp, tiff)")
        try:
            size = p.stat().st_size
            if not p.is_file():
                raise PhotoGeotagError("the path is not a file")
            if size > MAX_PHOTO_BYTES:
                raise PhotoGeotagError(f"the photo is larger than {MAX_PHOTO_BYTES // (1024 * 1024)} MB")
            raw = p.read_bytes()
        except OSError as error:
            raise PhotoGeotagError(f"cannot read the photo: {error.strerror or error}") from error
    if not raw:
        raise PhotoGeotagError("the photo is empty")
    if len(raw) > MAX_PHOTO_BYTES:
        raise PhotoGeotagError(f"the photo is larger than {MAX_PHOTO_BYTES // (1024 * 1024)} MB")
    return raw


# --------------------------------------------------------------------------
# EXIF parsing
# --------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    """A finite float from an IFDRational / int / float / (num, den) tuple, else None."""
    try:
        if isinstance(value, tuple) and len(value) == 2:
            den = float(value[1])
            if den == 0.0:
                return None
            out = float(value[0]) / den
        else:
            out = float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return out if math.isfinite(out) else None


def _dms(values: Any, ref: Any, limit: float, label: str, warnings: list[str]) -> float | None:
    """Degrees/minutes/seconds + N/S/E/W reference -> signed decimal degrees."""
    if values is None:
        return None
    try:
        parts = [_num(v) for v in values]
    except TypeError:
        parts = []
    if len(parts) != 3 or any(p is None for p in parts):
        warnings.append(f"GPS {label} is malformed and was ignored")
        return None
    d, m, s = (float(p) for p in parts if p is not None)
    deg = d + m / 60.0 + s / 3600.0
    ref_text = ref.decode("ascii", "ignore") if isinstance(ref, bytes) else str(ref or "")
    ref_text = ref_text.strip().upper()[:1]
    if ref_text in ("S", "W"):
        deg = -deg
    elif ref_text not in ("N", "E"):
        warnings.append(f"GPS {label} has no valid hemisphere reference; assumed positive (N/E)")
    if abs(deg) > limit:
        warnings.append(f"GPS {label} is outside the valid range and was ignored")
        return None
    return deg


def _ascii(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", "ignore").strip("\x00 ").strip()
    return str(value or "").strip("\x00 ").strip()


def _exif_datetime(text: str) -> str | None:
    """``"2026:11:19 14:03:22"`` -> ``"2026-11-19T14:03:22"``; ``None`` when malformed."""
    s = text.strip()
    if len(s) < 19 or s[4] != ":" or s[7] != ":":
        return None
    try:
        year, month, day = int(s[0:4]), int(s[5:7]), int(s[8:10])
        hour, minute, sec = int(s[11:13]), int(s[14:16]), int(s[17:19])
    except ValueError:
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour < 24 and minute < 60 and sec < 61):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{min(sec, 59):02d}"


def parse_photo_exif(raw: bytes) -> PhotoGeotag:
    """Parse the geotag EXIF of photo *raw* bytes. Never raises for bad EXIF.

    Raises :class:`PhotoGeotagError` only when the bytes are not an image Pillow
    can open at all.
    """
    result = PhotoGeotag()
    try:
        with Image.open(io.BytesIO(raw)) as img:
            result.width_px, result.height_px = img.size
            try:
                exif = img.getexif()
                if exif.get(0x0112) in (5, 6, 7, 8):  # rotated 90/270: displayed w/h swap
                    result.width_px, result.height_px = img.size[1], img.size[0]
            except Exception as error:  # corrupt EXIF block
                result.warnings.append(f"the EXIF block is unreadable ({type(error).__name__})")
                return result
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
        raise PhotoGeotagError("the data is not an image Pillow can open") from error

    if not exif:
        result.warnings.append("the photo has no EXIF metadata")
        return result

    def ifd(tag: int) -> dict[int, Any]:
        try:
            block = exif.get_ifd(tag)
            return dict(block) if block else {}
        except Exception:
            result.warnings.append(f"EXIF block 0x{tag:04X} is unreadable")
            return {}

    gps = ifd(_TAG_GPS_IFD)
    sub = ifd(_TAG_EXIF_IFD)

    # Position.
    lat = _dms(gps.get(_GPS_LAT), gps.get(_GPS_LAT_REF), 90.0, "latitude", result.warnings)
    lon = _dms(gps.get(_GPS_LON), gps.get(_GPS_LON_REF), 180.0, "longitude", result.warnings)
    if lat is not None and lon is not None:
        result.lat, result.lon = lat, lon
    elif gps and (lat is not None or lon is not None):
        result.warnings.append("only one of GPS latitude/longitude is present; no position")
    elif not gps:
        result.warnings.append("the photo has no GPS block (location services off?)")
    else:
        result.warnings.append("the GPS block has no position")
    if result.lat == 0.0 and result.lon == 0.0:
        result.warnings.append("GPS position is exactly 0,0 (a typical 'no fix' placeholder); ignored")
        result.lat = result.lon = None

    alt = _num(gps.get(_GPS_ALT))
    if alt is not None:
        below = gps.get(_GPS_ALT_REF)
        if below in (1, b"\x01"):
            alt = -alt
        result.altitude_m = alt
    dop = _num(gps.get(_GPS_DOP))
    if dop is not None and dop > 0:
        # DOP is not a distance; a rough 5 m per DOP unit (consumer receivers).
        result.gps_accuracy_m = round(dop * 5.0, 1)

    # Direction the camera faces.
    direction = _num(gps.get(_GPS_DIR))
    if direction is not None:
        if 0.0 <= direction <= 360.0:
            result.bearing_deg = direction % 360.0
            ref = _ascii(gps.get(_GPS_DIR_REF)).upper()[:1]
            result.bearing_reference = "magnetic" if ref == "M" else "true"
            if ref not in ("T", "M"):
                result.warnings.append("GPSImgDirectionRef missing; the bearing is assumed true north")
            if ref == "M":
                result.warnings.append(
                    "the bearing is MAGNETIC; give magnetic_declination_deg to convert it to true"
                )
        else:
            result.warnings.append("GPSImgDirection is outside 0-360 and was ignored")
    elif result.has_position:
        result.warnings.append("the photo has no compass direction (GPSImgDirection)")

    # Lens.
    focal = _num(sub.get(_TAG_FOCAL_LENGTH))
    if focal is not None and focal > 0:
        result.focal_length_mm = focal
    f35 = _num(sub.get(_TAG_FOCAL_LENGTH_35))
    if f35 is not None and f35 > 0:
        result.focal_length_35mm = f35

    # Time.
    taken = _ascii(sub.get(_TAG_DATETIME_ORIGINAL)) or _ascii(exif.get(_TAG_DATETIME))
    if taken:
        iso = _exif_datetime(taken)
        if iso is None:
            result.warnings.append("the EXIF date is malformed and was ignored")
        result.taken_at = iso
    return result


def parse_photo(path: str | None = None, data_base64: str | None = None) -> PhotoGeotag:
    """:func:`photo_bytes_from_source` then :func:`parse_photo_exif`."""
    return parse_photo_exif(photo_bytes_from_source(path, data_base64))


# --------------------------------------------------------------------------
# Field of view and projection onto the plan
# --------------------------------------------------------------------------


#: Diagonal of a full-frame 35 mm sensor, mm: FocalLengthIn35mmFilm is diagonal-based.
_FULL_FRAME_DIAGONAL_MM = 43.27


def _sensor_width_mm(tag: PhotoGeotag) -> float:
    """35 mm-equivalent width of the DISPLAYED frame: 36 mm landscape 3:2, 24 mm portrait."""
    if tag.width_px and tag.height_px:
        return _FULL_FRAME_DIAGONAL_MM * tag.width_px / math.hypot(tag.width_px, tag.height_px)
    return _FULL_FRAME_WIDTH_MM


def estimate_hfov_deg(tag: PhotoGeotag) -> tuple[float, str]:
    """``(horizontal FOV in degrees, source)``; the source states how good it is.

    ``"exif_35mm"`` (from FocalLengthIn35mmFilm), ``"exif_focal_length_assumed_sensor"``
    (real focal length with an assumed phone crop factor) or ``"default_estimate"``.
    All are estimates. The frame's displayed shape (EXIF Orientation applied) sets
    the sensor width, so a portrait photo gets a narrower view than a landscape one;
    with no pixel size a 3:2 landscape frame is assumed.
    """
    width = _sensor_width_mm(tag)
    if tag.focal_length_35mm:
        return _hfov(tag.focal_length_35mm, width), "exif_35mm"
    if tag.focal_length_mm:
        return (
            _hfov(tag.focal_length_mm * ASSUMED_CROP_FACTOR, width),
            "exif_focal_length_assumed_sensor",
        )
    return DEFAULT_HFOV_DEG, "default_estimate"


def _hfov(focal_35mm: float, width_mm: float = _FULL_FRAME_WIDTH_MM) -> float:
    return math.degrees(2.0 * math.atan(width_mm / (2.0 * focal_35mm)))


def canvas_direction(true_bearing_deg: float, north_angle_deg: float) -> tuple[float, float]:
    """Unit canvas vector (Y-up) of a TRUE compass bearing on a plan turned *north_angle_deg*."""
    angle = math.radians(north_angle_deg + true_bearing_deg)
    return math.sin(angle), math.cos(angle)


def project_to_plan(
    tag: PhotoGeotag,
    geo: GeoReference | None,
    *,
    magnetic_declination_deg: float | None = None,
    bearing_override_deg: float | None = None,
    distance_cm: float | None = None,
) -> dict[str, Any]:
    """Place the photo's camera and sightline on the plan.

    *geo* carries the plan's north (``rotation_deg``). ``bearing_override_deg`` is
    a TRUE bearing that replaces the EXIF one. ``magnetic_declination_deg`` (east
    positive) converts a magnetic EXIF bearing to true. ``distance_cm`` adds the
    ``target`` point along the sightline. The result has ``georeferenced`` False
    and no canvas data when *geo* is ``None`` or the photo has no position.
    """
    warnings = list(tag.warnings)
    out: dict[str, Any] = {"georeferenced": geo is not None}
    bearing = bearing_override_deg if bearing_override_deg is not None else tag.bearing_deg
    if bearing_override_deg is not None:
        bearing = bearing_override_deg % 360.0
    elif bearing is not None and tag.bearing_reference == "magnetic":
        if magnetic_declination_deg is not None:
            bearing = (bearing + magnetic_declination_deg) % 360.0
        else:
            warnings.append("magnetic bearing used as true because no declination was given")
    hfov, hfov_source = estimate_hfov_deg(tag)
    out.update(
        {
            "true_bearing_deg": bearing,
            "hfov_deg": round(hfov, 2),
            "hfov_source": hfov_source,
            "hfov_is_estimate": True,
        }
    )
    if geo is None:
        warnings.append("the plan is not georeferenced; no canvas position computed")
    elif not tag.has_position:
        warnings.append("the photo has no usable GPS position; no canvas position computed")
    else:
        assert tag.lat is not None and tag.lon is not None
        try:
            x, y = geo.gps_to_canvas(tag.lat, tag.lon)
        except GeorefError as error:
            warnings.append(f"the position cannot be placed on this plan: {error}")
        else:
            out["camera_x_cm"], out["camera_y_cm"] = x, y
            if bearing is not None:
                half = hfov / 2.0
                out["sightline"] = _vec(canvas_direction(bearing, geo.rotation_deg))
                out["fov_left"] = _vec(canvas_direction(bearing - half, geo.rotation_deg))
                out["fov_right"] = _vec(canvas_direction(bearing + half, geo.rotation_deg))
                if distance_cm is not None and math.isfinite(distance_cm) and distance_cm > 0:
                    dx, dy = canvas_direction(bearing, geo.rotation_deg)
                    out["target_x_cm"], out["target_y_cm"] = x + dx * distance_cm, y + dy * distance_cm
    out["warnings"] = warnings
    return out


def _vec(v: tuple[float, float]) -> dict[str, float]:
    return {"dx": round(v[0], 9), "dy": round(v[1], 9)}
