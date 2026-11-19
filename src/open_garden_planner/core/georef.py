"""Plan georeferencing: a local tangent-plane link between the canvas and GPS (ADR-079).

Pure and Qt-free. The plan's scene is Y-up, in centimetres (ADR-002): ``+x`` is
plan-right, ``+y`` is plan-up. A :class:`GeoReference` says which latitude and
longitude the scene origin ``(0, 0)`` is, and how the plan is turned against the
compass.

**One north.** The plan already owns its north: ``north_angle_deg`` (ADR-037
addendum, ``core/plan_north.py``) is the direction of TRUE NORTH on the plan, in
degrees *clockwise from plan-up*. ``GeoReference.rotation_deg`` IS that angle,
never a second copy: the persisted ``georef`` block omits it
(:meth:`GeoReference.to_dict`), ``ProjectManager.georef`` fills it from the plan,
and a change that touches both (a fit) is ONE undo step
(``SetGeoreferenceCommand``).

Sign convention, with ``a = rotation_deg`` and ``(e, n)`` the east/north metres
from the origin: the plan is the ENU frame turned *clockwise* by ``a``::

    x_m = e * cos(a) + n * sin(a)           # plan-right
    y_m = -e * sin(a) + n * cos(a)          # plan-up

so north ``(e, n) = (0, 1)`` is the plan vector ``(sin a, cos a)`` -- exactly
``plan_north.north_direction`` -- and east is ``(cos a, -sin a)``. At
``a = 0`` east is plan-right, north plan-up; at ``a = 90`` north is plan-right.
The scene is already Y-up (the view flips for display only), so there is NO
extra Y flip here: ``+n`` maps to ``+y``.

**Projection.** Local equirectangular on a sphere of the IUGG mean radius::

    n = R * radians(lat - origin_lat)
    e = R * radians(wrap(lon - origin_lon)) * cos(origin_lat)

The longitude difference is wrapped into ``[-180, 180)`` so a plan across the
antimeridian works. The neglected term is ``~ d^2 tan(lat) / 2R`` against a
true geodesic: about 5 cm at 1 km and 60 deg latitude, 5 mm at 300 m. The round
trip ``gps -> canvas -> gps`` is exact to floating point (far below 1 mm). A
garden is not a country: beyond a few kilometres use a real map projection.

``scale_meters_per_unit`` converts a scene unit to metres (default 0.01: the
scene is centimetres).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

#: Mean Earth radius (IUGG), metres.
EARTH_RADIUS_M = 6371008.8
#: Scene unit is a centimetre.
DEFAULT_SCALE_M_PER_UNIT = 0.01
#: Latitudes beyond this make the local longitude scale meaningless.
MAX_ABS_LATITUDE = 89.0
#: Reference points closer together than this (metres RMS spread) coincide.
MIN_SPREAD_M = 1e-3
#: A set is collinear when the minor/major principal std-dev ratio is below this.
COLLINEAR_RATIO = 1e-3
MAX_REFERENCE_POINTS = 1000
MAX_PARCEL_VERTICES = 10000

_M_PER_DEG = math.radians(1.0) * EARTH_RADIUS_M


class GeorefError(ValueError):
    """An invalid georeference, or a fit that cannot be solved."""


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GeorefError(f"{name} must be a number, not {type(value).__name__}")
    try:
        number = float(value)
    except OverflowError as error:
        raise GeorefError(f"{name} is not finite") from error
    if not math.isfinite(number):
        raise GeorefError(f"{name} is not finite")
    return number


def validate_lat_lon(lat: object, lon: object) -> tuple[float, float]:
    """Return ``(lat, lon)`` as floats, or raise :class:`GeorefError`."""
    la = _finite(lat, "latitude")
    lo = _finite(lon, "longitude")
    if not -90.0 <= la <= 90.0:
        raise GeorefError("latitude must be within [-90, 90]")
    if not -180.0 <= lo <= 180.0:
        raise GeorefError("longitude must be within [-180, 180]")
    return la, lo


def wrap_lon_delta(delta_deg: float) -> float:
    """Wrap a longitude difference into ``[-180, 180)``."""
    return (delta_deg + 180.0) % 360.0 - 180.0


def wrap_lon(lon: float) -> float:
    """Bring a longitude into ``[-180, 180]`` (in-range values pass unchanged)."""
    if -180.0 <= lon <= 180.0:
        return lon
    return wrap_lon_delta(lon)


@dataclass(frozen=True)
class ReferencePoint:
    """One surveyed correspondence: a canvas point (scene units) and its GPS."""

    name: str
    lat: float
    lon: float
    x: float
    y: float

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "lat": self.lat, "lon": self.lon, "x": self.x, "y": self.y}

    @classmethod
    def from_dict(cls, data: object) -> ReferencePoint:
        """Validate untrusted *data*; raise :class:`GeorefError` if unusable."""
        if not isinstance(data, Mapping):
            raise GeorefError("reference point must be an object")
        lat, lon = validate_lat_lon(data.get("lat"), data.get("lon"))
        name = data.get("name", "")
        return cls(
            name=name[:200] if isinstance(name, str) else "",
            lat=lat,
            lon=lon,
            x=_finite(data.get("x"), "x"),
            y=_finite(data.get("y"), "y"),
        )


@dataclass(frozen=True)
class GeoReference:
    """Where the scene origin is on Earth, and how the plan is turned.

    Attributes:
        origin_lat / origin_lon: WGS-84 degrees of scene point ``(0, 0)``.
        scale_meters_per_unit: metres per scene unit (0.01 for cm).
        rotation_deg: true north, clockwise from plan-up, in ``[0, 360)``.
            Equals the plan's ``north_angle_deg`` (see module docstring); it is
            not persisted in the ``georef`` block.
        parcel_boundary: optional closed outline as ``(lat, lon)`` pairs.
        reference_points: surveyed correspondences the fit used or the user keeps.
        fit_rms_m: RMS residual of the last fit, metres (``None`` if never fit).
        source: provenance, e.g. ``"manual"``, ``"google_static_maps"``, ``"fit"``.
    """

    origin_lat: float
    origin_lon: float
    scale_meters_per_unit: float = DEFAULT_SCALE_M_PER_UNIT
    rotation_deg: float = 0.0
    parcel_boundary: tuple[tuple[float, float], ...] | None = None
    reference_points: tuple[ReferencePoint, ...] = field(default_factory=tuple)
    fit_rms_m: float | None = None
    source: str = "manual"

    def __post_init__(self) -> None:
        la, lo = validate_lat_lon(self.origin_lat, self.origin_lon)
        if abs(la) > MAX_ABS_LATITUDE:
            raise GeorefError(f"origin latitude must be within +-{MAX_ABS_LATITUDE} degrees")
        scale = _finite(self.scale_meters_per_unit, "scale")
        if scale <= 0.0:
            raise GeorefError("scale must be positive")
        rot = _finite(self.rotation_deg, "rotation") % 360.0
        object.__setattr__(self, "origin_lat", la)
        object.__setattr__(self, "origin_lon", lo)
        object.__setattr__(self, "scale_meters_per_unit", scale)
        object.__setattr__(self, "rotation_deg", rot)
        if self.fit_rms_m is not None:
            rms = _finite(self.fit_rms_m, "fit_rms_m")
            if rms < 0.0:
                raise GeorefError("fit_rms_m must be >= 0")
            object.__setattr__(self, "fit_rms_m", rms)
        if self.parcel_boundary is not None:
            if len(self.parcel_boundary) > MAX_PARCEL_VERTICES:
                raise GeorefError("parcel boundary has too many vertices")
            ring = tuple(validate_lat_lon(p[0], p[1]) for p in self.parcel_boundary)
            object.__setattr__(self, "parcel_boundary", ring or None)
        if len(self.reference_points) > MAX_REFERENCE_POINTS:
            raise GeorefError("too many reference points")
        object.__setattr__(self, "reference_points", tuple(self.reference_points))
        if not isinstance(self.source, str):
            raise GeorefError("source must be a string")

    # -- projection -------------------------------------------------------

    @property
    def _m_per_deg_lon(self) -> float:
        return _M_PER_DEG * math.cos(math.radians(self.origin_lat))

    def gps_to_enu(self, lat: float, lon: float) -> tuple[float, float]:
        """East/north metres of ``(lat, lon)`` from the origin."""
        la, lo = validate_lat_lon(lat, lon)
        north = (la - self.origin_lat) * _M_PER_DEG
        east = wrap_lon_delta(lo - self.origin_lon) * self._m_per_deg_lon
        return east, north

    def enu_to_gps(self, east: float, north: float) -> tuple[float, float]:
        """Inverse of :meth:`gps_to_enu`."""
        lat = self.origin_lat + _finite(north, "north") / _M_PER_DEG
        lon = self.origin_lon + _finite(east, "east") / self._m_per_deg_lon
        return lat, wrap_lon(lon)

    def gps_to_canvas(self, lat: float, lon: float) -> tuple[float, float]:
        """Scene ``(x, y)`` (cm for the default scale) of a GPS position."""
        east, north = self.gps_to_enu(lat, lon)
        a = math.radians(self.rotation_deg)
        c, s = math.cos(a), math.sin(a)
        k = self.scale_meters_per_unit
        return (east * c + north * s) / k, (-east * s + north * c) / k

    def canvas_to_gps(self, x: float, y: float) -> tuple[float, float]:
        """GPS ``(lat, lon)`` of a scene point."""
        k = self.scale_meters_per_unit
        x_m = _finite(x, "x") * k
        y_m = _finite(y, "y") * k
        a = math.radians(self.rotation_deg)
        c, s = math.cos(a), math.sin(a)
        return self.enu_to_gps(x_m * c - y_m * s, x_m * s + y_m * c)

    # -- (de)serialisation ------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """The persisted ``georef`` block. ``rotation_deg`` is NOT written."""
        data: dict[str, Any] = {
            "origin_lat": self.origin_lat,
            "origin_lon": self.origin_lon,
            "scale_meters_per_unit": self.scale_meters_per_unit,
            "source": self.source,
        }
        if self.parcel_boundary:
            data["parcel_boundary"] = [[la, lo] for la, lo in self.parcel_boundary]
        if self.reference_points:
            data["reference_points"] = [p.to_dict() for p in self.reference_points]
        if self.fit_rms_m is not None:
            data["fit_rms_m"] = self.fit_rms_m
        return data

    @classmethod
    def from_dict(cls, data: object, *, rotation_deg: float = 0.0) -> GeoReference:
        """Build from an untrusted block; raise :class:`GeorefError` if unusable.

        Only the origin and scale decide. A malformed optional part (a parcel
        vertex, a reference point, the RMS) is dropped, not fatal.
        """
        if not isinstance(data, Mapping):
            raise GeorefError("georef must be an object")
        parcel: tuple[tuple[float, float], ...] | None = None
        raw_parcel = data.get("parcel_boundary")
        if isinstance(raw_parcel, Sequence) and not isinstance(raw_parcel, (str, bytes)):
            ring: list[tuple[float, float]] = []
            for vertex in raw_parcel[:MAX_PARCEL_VERTICES]:
                try:
                    if isinstance(vertex, Sequence) and not isinstance(vertex, (str, bytes)):
                        ring.append(validate_lat_lon(vertex[0], vertex[1]))
                except (GeorefError, IndexError):
                    continue
            parcel = tuple(ring) or None
        points: list[ReferencePoint] = []
        raw_points = data.get("reference_points")
        if isinstance(raw_points, Sequence) and not isinstance(raw_points, (str, bytes)):
            for raw in raw_points[:MAX_REFERENCE_POINTS]:
                try:
                    points.append(ReferencePoint.from_dict(raw))
                except GeorefError:
                    continue
        rms_value: float | None = None
        if data.get("fit_rms_m") is not None:
            try:
                rms_value = _finite(data["fit_rms_m"], "fit_rms_m")
            except GeorefError:
                rms_value = None
            if rms_value is not None and rms_value < 0:
                rms_value = None
        source = data.get("source", "manual")
        return cls(
            origin_lat=data.get("origin_lat"),  # type: ignore[arg-type]
            origin_lon=data.get("origin_lon"),  # type: ignore[arg-type]
            scale_meters_per_unit=data.get("scale_meters_per_unit", DEFAULT_SCALE_M_PER_UNIT),
            rotation_deg=rotation_deg,
            parcel_boundary=parcel,
            reference_points=tuple(points),
            fit_rms_m=rms_value,
            source=source if isinstance(source, str) else "manual",
        )

    def with_rotation(self, rotation_deg: float) -> GeoReference:
        """A copy turned to *rotation_deg* (the plan's north angle)."""
        return replace(self, rotation_deg=rotation_deg)


def georef_from_plan(data: object, rotation_deg: float) -> GeoReference | None:
    """The georeference a loaded plan carries, or ``None`` for a missing/bad one.

    Load is a trust boundary: an unreadable block reads as "not georeferenced".
    """
    if data is None:
        return None
    try:
        return GeoReference.from_dict(data, rotation_deg=rotation_deg)
    except (GeorefError, TypeError, ValueError, OverflowError):
        return None


# -- fitting ---------------------------------------------------------------


@dataclass(frozen=True)
class FitResult:
    """Outcome of :func:`fit_from_reference_points`."""

    georef: GeoReference
    rms_m: float
    residuals_m: tuple[float, ...]
    #: Plan-metres per ground-metre the fit found (1.0 when scale was not solved).
    scale_factor: float
    method: str  # "translation" | "similarity" | "affine"


def _as_points(points: Sequence[Any]) -> list[ReferencePoint]:
    if len(points) > MAX_REFERENCE_POINTS:
        raise GeorefError("too many reference points")
    return [p if isinstance(p, ReferencePoint) else ReferencePoint.from_dict(p) for p in points]


def _require_not_collinear(pts: Sequence[tuple[float, float]], what: str) -> None:
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    syy = sum((p[1] - my) ** 2 for p in pts)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
    half_tr = (sxx + syy) / 2
    root = math.sqrt(max(half_tr * half_tr - (sxx * syy - sxy * sxy), 0.0))
    big, small = half_tr + root, max(half_tr - root, 0.0)
    if big <= 0 or math.sqrt(small / big) < COLLINEAR_RATIO:
        raise GeorefError(f"{what} reference points are collinear")


def fit_from_reference_points(
    points: Sequence[Any],
    *,
    scale_meters_per_unit: float = DEFAULT_SCALE_M_PER_UNIT,
    rotation_deg: float | None = None,
    allow_affine: bool = False,
    source: str = "fit",
) -> FitResult:
    """Solve a georeference from surveyed canvas <-> GPS correspondences.

    * **1 point:** pure translation. Scale (*scale_meters_per_unit*) and
      rotation (*rotation_deg*, default 0) are taken as given.
    * **2 points:** similarity (scale and rotation solved exactly).
    * **3+ points:** least-squares similarity (Umeyama, closed form in 2-D).
      With ``allow_affine=True`` the RESIDUALS reported are those of a full
      2-D affine fit, a diagnostic of shear/anisotropy; the returned
      ``georef`` is still the best similarity, because a georeference is one.

    The ground scale is fitted, so ``georef.scale_meters_per_unit`` may differ
    from the one passed in. ``rms_m`` and ``residuals_m`` are in metres.
    Raises :class:`GeorefError` for no points, coincident points, a collinear
    set of 3+, or any non-finite / out-of-range value.
    """
    pts = _as_points(points)
    n = len(pts)
    if n == 0:
        raise GeorefError("at least one reference point is required")
    scale0 = _finite(scale_meters_per_unit, "scale")
    if scale0 <= 0:
        raise GeorefError("scale must be positive")

    can = [(p.x * scale0, p.y * scale0) for p in pts]  # plan metres at nominal scale
    lat0 = sum(p.lat for p in pts) / n
    lon0 = wrap_lon(pts[0].lon + sum(wrap_lon_delta(p.lon - pts[0].lon) for p in pts) / n)
    if abs(lat0) > MAX_ABS_LATITUDE:
        raise GeorefError("reference points are too close to a pole")

    # The model's longitude scale depends on the ORIGIN latitude, which the fit
    # itself finds; iterate the projection frame onto the answer (converges in
    # two or three rounds because the origin is within a plot of the points).
    frame = GeoReference(lat0, lon0)
    a = math.radians((rotation_deg or 0.0) % 360.0)
    s_fit = 1.0
    method = "translation"
    geo = frame
    for _ in range(4):
        enu = [frame.gps_to_enu(p.lat, p.lon) for p in pts]
        if n >= 2:
            a, s_fit = _solve_similarity(can, enu, n)
            method = "similarity"
        c, sn = math.cos(a), math.sin(a)
        # plan = s * R_cw(a) * enu + t
        rot = [(s_fit * (e * c + nn * sn), s_fit * (-e * sn + nn * c)) for e, nn in enu]
        tx = sum(q[0] - r[0] for q, r in zip(can, rot, strict=True)) / n
        ty = sum(q[1] - r[1] for q, r in zip(can, rot, strict=True)) / n
        # ENU of plan (0, 0): R_cw(-a) * (-t) / s
        e0 = -(tx * c - ty * sn) / s_fit
        n0 = -(tx * sn + ty * c) / s_fit
        lat_o = frame.origin_lat + n0 / _M_PER_DEG
        if abs(lat_o) > MAX_ABS_LATITUDE:
            raise GeorefError("the fitted origin is out of range")
        lon_o = wrap_lon(frame.origin_lon + e0 / frame._m_per_deg_lon)
        geo = GeoReference(
            origin_lat=lat_o,
            origin_lon=lon_o,
            scale_meters_per_unit=scale0 / s_fit,
            rotation_deg=math.degrees(a),
            reference_points=tuple(pts),
            source=source,
        )
        if abs(lat_o - frame.origin_lat) < 1e-12 and abs(lon_o - frame.origin_lon) < 1e-12:
            break
        frame = GeoReference(lat_o, lon_o)

    k = geo.scale_meters_per_unit
    residuals = [
        math.hypot(gx - p.x, gy - p.y) * k
        for p in pts
        for gx, gy in (geo.gps_to_canvas(p.lat, p.lon),)
    ]
    if allow_affine and n >= 3:
        method = "affine"
        residuals = _affine_residuals(can, [geo.gps_to_enu(p.lat, p.lon) for p in pts])
    rms = math.sqrt(sum(r * r for r in residuals) / n)
    geo = replace(geo, fit_rms_m=rms)
    return FitResult(georef=geo, rms_m=rms, residuals_m=tuple(residuals), scale_factor=s_fit, method=method)


def _solve_similarity(
    can: Sequence[tuple[float, float]], enu: Sequence[tuple[float, float]], n: int
) -> tuple[float, float]:
    """``(a, s)`` with ``plan ~= s * R_cw(a) * enu`` (complex-number Umeyama)."""
    cx = sum(q[0] for q in can) / n
    cy = sum(q[1] for q in can) / n
    ex = sum(e[0] for e in enu) / n
    ey = sum(e[1] for e in enu) / n
    spread_c = math.sqrt(sum((q[0] - cx) ** 2 + (q[1] - cy) ** 2 for q in can) / n)
    spread_e = math.sqrt(sum((e[0] - ex) ** 2 + (e[1] - ey) ** 2 for e in enu) / n)
    if spread_c < MIN_SPREAD_M or spread_e < MIN_SPREAD_M:
        raise GeorefError("reference points coincide: they must be spread apart")
    if n >= 3:
        _require_not_collinear(can, "canvas")
        _require_not_collinear(enu, "GPS")
    num = 0j
    den = 0.0
    for q, e in zip(can, enu, strict=True):
        zc = complex(q[0] - cx, q[1] - cy)
        ze = complex(e[0] - ex, e[1] - ey)
        num += zc * ze.conjugate()
        den += abs(ze) ** 2
    if abs(num) < 1e-12 or den < 1e-12:
        raise GeorefError("reference points are degenerate")
    # plan = s e^{-ia} enu  (clockwise turn), so arg(num) = -a.
    return -math.atan2(num.imag, num.real), abs(num) / den


def _affine_residuals(
    can: Sequence[tuple[float, float]], enu: Sequence[tuple[float, float]]
) -> list[float]:
    """Residuals (plan metres) of the full least-squares affine ENU -> plan."""
    s = [[0.0] * 3 for _ in range(3)]
    bx = [0.0] * 3
    by = [0.0] * 3
    for (e, nn), (x, y) in zip(enu, can, strict=True):
        row = (e, nn, 1.0)
        for i in range(3):
            for j in range(3):
                s[i][j] += row[i] * row[j]
            bx[i] += row[i] * x
            by[i] += row[i] * y
    mx = _solve3(s, bx)
    my = _solve3(s, by)
    return [
        math.hypot(mx[0] * e + mx[1] * nn + mx[2] - x, my[0] * e + my[1] * nn + my[2] - y)
        for (e, nn), (x, y) in zip(enu, can, strict=True)
    ]


def _solve3(a: list[list[float]], b: list[float]) -> list[float]:
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            raise GeorefError("reference points are degenerate")
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(3):
            if r != col:
                f = m[r][col] / m[col][col]
                for c in range(col, 4):
                    m[r][c] -= f * m[col][c]
    return [m[i][3] / m[i][i] for i in range(3)]


# -- satellite imports -----------------------------------------------------


def from_geo_metadata(
    geo_metadata: Mapping[str, Any],
    *,
    image_center: tuple[float, float],
    scale_factor: float,
    rotation_deg: float = 0.0,
    source: str | None = None,
) -> GeoReference:
    """Derive a georeference from a satellite background's ``geo_metadata``.

    A satellite image is north-up and the ``BackgroundImageItem`` pixmap is
    flipped for the Y-up view, so image-north is plan ``+y``. *image_center* is
    the image's centre in scene units: for an un-rotated item that is
    ``pos + (w_px / 2, h_px / 2)`` (``transformOriginPoint`` sits at the local
    centre, so the scale does not move it). It sits at the bbox centre
    (``geo_metadata["center"]``). *scale_factor* is the item's pixels per scene
    unit (calibration included); ground metres per scene unit is then
    ``meters_per_pixel * scale_factor``.

    *rotation_deg* must be 0 for an un-rotated image; it is a parameter only so
    the caller states it.
    """
    center = geo_metadata.get("center")
    try:
        if center is not None:
            clat, clon = validate_lat_lon(center[0], center[1])
        else:
            nw, se = geo_metadata["bbox_nw"], geo_metadata["bbox_se"]
            clat = (_finite(nw[0], "lat") + _finite(se[0], "lat")) / 2.0
            clon = wrap_lon(
                _finite(nw[1], "lon") + wrap_lon_delta(_finite(se[1], "lon") - _finite(nw[1], "lon")) / 2.0
            )
            clat, clon = validate_lat_lon(clat, clon)
    except (KeyError, IndexError, TypeError) as error:
        raise GeorefError("geo_metadata lacks a centre or bounding box") from error
    mpp = _finite(geo_metadata.get("meters_per_pixel"), "meters_per_pixel")
    sf = _finite(scale_factor, "scale_factor")
    if mpp <= 0 or sf <= 0:
        raise GeorefError("meters_per_pixel and scale_factor must be positive")
    k = mpp * sf
    cx = _finite(image_center[0], "image centre x")
    cy = _finite(image_center[1], "image centre y")
    a = math.radians(_finite(rotation_deg, "rotation") % 360.0)
    # ENU offset of the image centre from the origin (plan -> ENU is R_cw(-a)).
    e_c = (cx * math.cos(a) - cy * math.sin(a)) * k
    n_c = (cx * math.sin(a) + cy * math.cos(a)) * k
    lat_o = clat - n_c / _M_PER_DEG
    if abs(lat_o) > MAX_ABS_LATITUDE:
        raise GeorefError("the image is too far from the plan origin")
    # The longitude scale belongs to the ORIGIN latitude, so divide by that.
    lon_o = wrap_lon(clon - e_c / (_M_PER_DEG * math.cos(math.radians(lat_o))))
    return GeoReference(
        origin_lat=lat_o,
        origin_lon=lon_o,
        scale_meters_per_unit=k,
        rotation_deg=rotation_deg,
        source=source or str(geo_metadata.get("source") or "satellite"),
    )
