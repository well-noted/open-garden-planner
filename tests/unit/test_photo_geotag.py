"""Unit tests for services/photo_geotag.py (ADR-080): EXIF -> position, bearing, FOV."""

from __future__ import annotations

import base64
import contextlib
import io
import math

import pytest
from PIL import Image

from open_garden_planner.core.georef import GeoReference
from open_garden_planner.services import photo_geotag as pg


def _jpeg(gps: dict | None = None, sub: dict | None = None, base: dict | None = None) -> bytes:
    img = Image.new("RGB", (8, 6), "green")
    exif = Image.Exif()
    for tag, value in (base or {}).items():
        exif[tag] = value
    if gps is not None:
        exif.get_ifd(0x8825).update(gps)
    if sub is not None:
        exif.get_ifd(0x8769).update(sub)
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


FULL_GPS = {
    1: "N", 2: (48.0, 8.0, 24.0),
    3: "E", 4: (11.0, 34.0, 30.0),
    5: 0, 6: 520.5,
    16: "T", 17: 90.0,
}


def test_full_geotag() -> None:
    tag = pg.parse_photo_exif(
        _jpeg(FULL_GPS, {0x9003: "2026:11:19 14:03:22", 0x920A: 4.25, 0xA405: 26})
    )
    assert tag.lat == pytest.approx(48.14) and tag.lon == pytest.approx(11.575)
    assert tag.altitude_m == pytest.approx(520.5)
    assert tag.bearing_deg == 90.0 and tag.bearing_reference == "true"
    assert tag.taken_at == "2026-11-19T14:03:22"
    assert tag.focal_length_mm == pytest.approx(4.25) and tag.focal_length_35mm == 26
    assert (tag.width_px, tag.height_px) == (8, 6)


def test_southern_western_hemisphere_and_below_sea_level() -> None:
    gps = {1: "S", 2: (33.0, 0.0, 0.0), 3: "W", 4: (70.0, 30.0, 0.0), 5: 1, 6: 10.0}
    tag = pg.parse_photo_exif(_jpeg(gps))
    assert tag.lat == -33.0 and tag.lon == -70.5 and tag.altitude_m == -10.0


def test_no_exif_returns_warning_not_error() -> None:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, "PNG")
    tag = pg.parse_photo_exif(buf.getvalue())
    assert not tag.has_position and any("no EXIF" in w for w in tag.warnings)


def test_no_gps_block() -> None:
    tag = pg.parse_photo_exif(_jpeg(None, {0x9003: "2026:01:02 03:04:05"}))
    assert tag.lat is None and tag.taken_at == "2026-01-02T03:04:05"
    assert any("no GPS" in w for w in tag.warnings)


def test_partial_gps_latitude_only() -> None:
    tag = pg.parse_photo_exif(_jpeg({1: "N", 2: (10.0, 0.0, 0.0)}))
    assert tag.lat is None and tag.lon is None
    assert any("only one" in w for w in tag.warnings)


def test_out_of_range_and_malformed_values_ignored() -> None:
    gps = {1: "N", 2: (95.0, 0.0, 0.0), 3: "E", 4: (10.0, 0.0)}
    tag = pg.parse_photo_exif(_jpeg(gps))
    assert not tag.has_position and len(tag.warnings) >= 2


def test_zero_zero_is_treated_as_no_fix() -> None:
    gps = {1: "N", 2: (0.0, 0.0, 0.0), 3: "E", 4: (0.0, 0.0, 0.0)}
    tag = pg.parse_photo_exif(_jpeg(gps))
    assert not tag.has_position and any("0,0" in w for w in tag.warnings)


def test_num_helper_tolerates_garbage() -> None:
    assert pg._num((1, 0)) is None
    assert pg._num(float("nan")) is None
    assert pg._num("x") is None


def test_magnetic_bearing_and_declination() -> None:
    gps = dict(FULL_GPS)
    gps[16], gps[17] = "M", 350.0
    tag = pg.parse_photo_exif(_jpeg(gps))
    assert tag.bearing_reference == "magnetic" and any("MAGNETIC" in w for w in tag.warnings)
    geo = GeoReference(48.14, 11.575)
    out = pg.project_to_plan(tag, geo, magnetic_declination_deg=5.0)
    assert out["true_bearing_deg"] == pytest.approx(355.0)
    out2 = pg.project_to_plan(tag, geo)
    assert out2["true_bearing_deg"] == pytest.approx(350.0)
    assert any("no declination" in w for w in out2["warnings"])


def test_bad_date_ignored() -> None:
    tag = pg.parse_photo_exif(_jpeg(FULL_GPS, {0x9003: "garbage-date-string!!"}))
    assert tag.taken_at is None and any("date" in w for w in tag.warnings)


def test_corrupt_bytes_raise_source_error() -> None:
    with pytest.raises(pg.PhotoGeotagError):
        pg.parse_photo_exif(b"not an image at all")


def test_truncated_jpeg_with_exif_does_not_crash() -> None:
    raw = _jpeg(FULL_GPS)
    with contextlib.suppress(pg.PhotoGeotagError):  # a source error is fine; a crash is not
        pg.parse_photo_exif(raw[:60])


def test_base64_and_data_url_sources() -> None:
    raw = _jpeg(FULL_GPS)
    b64 = base64.b64encode(raw).decode()
    assert pg.parse_photo(data_base64=b64).has_position
    assert pg.parse_photo(data_base64="data:image/jpeg;base64," + b64).has_position


def test_source_errors(tmp_path) -> None:
    with pytest.raises(pg.PhotoGeotagError):
        pg.photo_bytes_from_source()
    with pytest.raises(pg.PhotoGeotagError):
        pg.photo_bytes_from_source(path="a.jpg", data_base64="AA==")
    with pytest.raises(pg.PhotoGeotagError):
        pg.photo_bytes_from_source(path=str(tmp_path / "x.txt"))
    with pytest.raises(pg.PhotoGeotagError):
        pg.photo_bytes_from_source(path=str(tmp_path / "missing.jpg"))
    (tmp_path / "e.jpg").write_bytes(b"")
    with pytest.raises(pg.PhotoGeotagError):
        pg.photo_bytes_from_source(path=str(tmp_path / "e.jpg"))


def test_path_source_reads_file(tmp_path) -> None:
    p = tmp_path / "a.jpg"
    p.write_bytes(_jpeg(FULL_GPS))
    assert pg.parse_photo(path=str(p)).lat == pytest.approx(48.14)


def test_size_cap(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(pg, "MAX_PHOTO_BYTES", 100)
    p = tmp_path / "big.jpg"
    p.write_bytes(b"x" * 500)
    with pytest.raises(pg.PhotoGeotagError, match="larger"):
        pg.photo_bytes_from_source(path=str(p))
    with pytest.raises(pg.PhotoGeotagError, match="larger"):
        pg.photo_bytes_from_source(data_base64=base64.b64encode(b"x" * 500).decode())


class TestFov:
    def test_from_35mm(self) -> None:
        hfov, src = pg.estimate_hfov_deg(pg.PhotoGeotag(focal_length_35mm=26.0))
        assert src == "exif_35mm" and hfov == pytest.approx(math.degrees(2 * math.atan(36 / 52)))

    def test_from_real_focal_length_assumes_sensor(self) -> None:
        hfov, src = pg.estimate_hfov_deg(pg.PhotoGeotag(focal_length_mm=5.0))
        assert src == "exif_focal_length_assumed_sensor" and 50 < hfov < 90

    def test_default(self) -> None:
        assert pg.estimate_hfov_deg(pg.PhotoGeotag()) == (pg.DEFAULT_HFOV_DEG, "default_estimate")


class TestProjection:
    def _tag(self, bearing: float = 0.0) -> pg.PhotoGeotag:
        return pg.PhotoGeotag(lat=48.0001, lon=11.0, bearing_deg=bearing, bearing_reference="true")

    def test_north_up_plan_facing_north_points_plan_up(self) -> None:
        geo = GeoReference(48.0, 11.0)
        out = pg.project_to_plan(self._tag(0.0), geo, distance_cm=500)
        assert out["camera_y_cm"] > 0
        assert out["sightline"] == {"dx": 0.0, "dy": 1.0}
        assert out["target_y_cm"] == pytest.approx(out["camera_y_cm"] + 500)

    def test_east_facing_points_plan_right(self) -> None:
        out = pg.project_to_plan(self._tag(90.0), GeoReference(48.0, 11.0))
        assert out["sightline"]["dx"] == pytest.approx(1.0)
        assert out["sightline"]["dy"] == pytest.approx(0, abs=1e-9)

    def test_rotated_plan_honours_north_angle(self) -> None:
        # North is 90 deg clockwise from plan-up, i.e. true north points plan-RIGHT.
        geo = GeoReference(48.0, 11.0, rotation_deg=90.0)
        out = pg.project_to_plan(self._tag(0.0), geo)
        assert out["sightline"]["dx"] == pytest.approx(1.0)
        assert out["sightline"]["dy"] == pytest.approx(0, abs=1e-9)

    def test_sightline_agrees_with_gps_to_canvas_for_any_rotation(self) -> None:
        for rot in (0, 33, 180, 301):
            for bearing in (10, 135, 260):
                geo = GeoReference(48.0, 11.0, rotation_deg=rot)
                tag = pg.PhotoGeotag(lat=48.0, lon=11.0, bearing_deg=bearing, bearing_reference="true")
                out = pg.project_to_plan(tag, geo)
                # A point 100 m away along the true bearing, via the independent georef path.
                east, north = 100 * math.sin(math.radians(bearing)), 100 * math.cos(math.radians(bearing))
                lat, lon = geo.enu_to_gps(east, north)
                x, y = geo.gps_to_canvas(lat, lon)
                norm = math.hypot(x, y)
                assert out["sightline"]["dx"] == pytest.approx(x / norm, abs=1e-6)
                assert out["sightline"]["dy"] == pytest.approx(y / norm, abs=1e-6)

    def test_fov_cone_is_symmetric_about_the_sightline(self) -> None:
        out = pg.project_to_plan(self._tag(0.0), GeoReference(48.0, 11.0))
        assert out["fov_left"]["dx"] == pytest.approx(-out["fov_right"]["dx"])
        assert out["hfov_is_estimate"] is True and out["hfov_source"] == "default_estimate"

    def test_override_bearing_wins(self) -> None:
        out = pg.project_to_plan(self._tag(0.0), GeoReference(48.0, 11.0), bearing_override_deg=450)
        assert out["true_bearing_deg"] == pytest.approx(90.0)

    def test_not_georeferenced_or_no_position(self) -> None:
        out = pg.project_to_plan(self._tag(), None)
        assert "camera_x_cm" not in out and out["georeferenced"] is False
        out = pg.project_to_plan(pg.PhotoGeotag(bearing_deg=3.0), GeoReference(48.0, 11.0))
        assert "camera_x_cm" not in out and any("no usable GPS" in w for w in out["warnings"])

    def test_no_bearing_gives_position_only(self) -> None:
        tag = pg.PhotoGeotag(lat=48.0, lon=11.0)
        out = pg.project_to_plan(tag, GeoReference(48.0, 11.0))
        assert "camera_x_cm" in out and "sightline" not in out
