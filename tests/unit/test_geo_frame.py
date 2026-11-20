"""``core/geo_frame.GeoFrame``: the field line's view of the georeference of record (ADR-079/080).

Pins that the frame adds nothing of its own to the projection (it IS ``gps_to_scene`` /
``scene_to_gps``), that ENU metres and the measured rotation/scale agree with the record
for a plain, a turned and a calibrated georeference, and that plans written before this
integration (no georeference, the record's key, the older top-level ``georef`` key) load.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from open_garden_planner.core.geo_frame import (
    GeoFrame,
    GeoreferenceError,
    frame_from_snapshot,
    make_frame,
    validate_lat_lon,
)
from open_garden_planner.core.georeference import (
    ControlPoint,
    PlanGeoreference,
    fit_affine_calibration,
    georeference_from_legacy_block,
    gps_to_scene,
    meters_per_degree,
    scene_to_gps,
)
from open_garden_planner.core.project import ProjectData, ProjectManager

LAT, LON = 33.4332, -111.7490


class TestFrameIsTheRecord:
    @pytest.mark.parametrize("north", [0.0, 37.0, 90.0, 215.0])
    def test_scene_gps_is_exactly_the_record(self, north: float) -> None:
        geo = PlanGeoreference(LAT, LON, origin_x_cm=250.0, origin_y_cm=-40.0)
        frame = GeoFrame(geo, north)
        for x, y in [(0.0, 0.0), (1234.5, -987.0), (-2500.0, 4000.0)]:
            assert frame.canvas_to_gps(x, y) == scene_to_gps(x, y, geo, north)
            lat, lon = frame.canvas_to_gps(x, y)
            assert frame.gps_to_canvas(lat, lon) == gps_to_scene(lat, lon, geo, north)
            assert frame.gps_to_canvas(lat, lon) == pytest.approx((x, y), abs=1e-6)

    def test_enu_uses_the_records_tangent_plane(self) -> None:
        frame = make_frame(LAT, LON)
        m_lat, m_lon = meters_per_degree(LAT)
        e, n = frame.gps_to_enu(LAT + 1e-4, LON + 2e-4)
        assert n == pytest.approx(1e-4 * m_lat) and e == pytest.approx(2e-4 * m_lon)
        assert frame.enu_to_gps(e, n) == pytest.approx((LAT + 1e-4, LON + 2e-4), abs=1e-12)

    def test_enu_to_canvas_follows_the_plan_north(self) -> None:
        # North 90: true north is plan-right, east is plan-down (ADR-037 addendum).
        frame = make_frame(LAT, LON, rotation_deg=90.0)
        x, y = frame.gps_to_canvas(*frame.enu_to_gps(0.0, 10.0))
        assert (x, y) == pytest.approx((1000.0, 0.0), abs=1e-3)
        x, y = frame.gps_to_canvas(*frame.enu_to_gps(10.0, 0.0))
        assert (x, y) == pytest.approx((0.0, -1000.0), abs=1e-3)

    def test_unit_conversions_are_measured_through_the_mapping(self) -> None:
        for rot in (0.0, 23.0, 181.0):
            frame = make_frame(LAT, LON, scale_meters_per_unit=0.02, rotation_deg=rot)
            assert frame.rotation_deg == pytest.approx(rot, abs=1e-6)
            assert frame.scale_meters_per_unit == pytest.approx(0.02, rel=1e-9)
            assert frame.fit_rms_m is None and frame.source == "manual"

    def test_a_calibrated_georeference_reports_the_turn_it_applies(self) -> None:
        base = PlanGeoreference(LAT, LON)
        # Three surveyed points that are the base projection turned by 10 degrees.
        a = math.radians(10.0)
        pts = []
        for east, north in [(0.0, 0.0), (80.0, 5.0), (30.0, 60.0)]:
            lat = LAT + north / meters_per_degree(LAT)[0]
            lon = LON + east / meters_per_degree(LAT)[1]
            x0, y0 = gps_to_scene(lat, lon, base, 0.0)
            pts.append(
                ControlPoint(
                    lat, lon, x0 * math.cos(a) - y0 * math.sin(a), x0 * math.sin(a) + y0 * math.cos(a)
                )
            )
        fit = fit_affine_calibration(base, pts, 0.0)
        frame = GeoFrame(fit.georeference, 0.0)
        assert frame.rotation_deg == pytest.approx(350.0, abs=1e-6)  # north turned 10 deg anticlockwise
        assert frame.source == "fit" and frame.fit_rms_m == pytest.approx(0.0, abs=1e-6)

    def test_untrusted_values_are_refused(self) -> None:
        with pytest.raises(GeoreferenceError):
            validate_lat_lon(91.0, 0.0)
        with pytest.raises(GeoreferenceError):
            make_frame(LAT, LON, scale_meters_per_unit=0.0)
        with pytest.raises(GeoreferenceError):
            make_frame(LAT, LON).enu_to_gps(float("nan"), 0.0)


class TestSnapshotAndOldFiles:
    def test_snapshot_without_a_georeference_is_none(self) -> None:
        assert frame_from_snapshot({}) is None
        assert frame_from_snapshot({"metadata": {"georeference": {"origin_lat": "x"}}}) is None

    def test_snapshot_carries_the_record_and_the_north(self) -> None:
        geo = PlanGeoreference(LAT, LON)
        frame = frame_from_snapshot({"metadata": {"georeference": geo.to_dict()}, "north_angle_deg": 30.0})
        assert frame == GeoFrame(geo, 30.0)

    def test_a_plan_saved_before_the_georeference_still_loads(self, qtbot, tmp_path: Path) -> None:
        plan = {"version": "1.4", "canvas": {"width": 5000.0, "height": 3000.0}, "layers": [], "objects": []}
        path = tmp_path / "old.ogp"
        path.write_text(json.dumps(plan), encoding="utf-8")
        pm = ProjectManager()
        data = ProjectData.from_dict(json.loads(path.read_text(encoding="utf-8")))
        assert data.georeference is None and pm.georeference is None

    def test_the_older_top_level_georef_key_is_read_once_and_rewritten_as_the_record(self) -> None:
        legacy = {
            "version": "1.4",
            "canvas": {"width": 5000.0, "height": 3000.0},
            "north_angle_deg": 20.0,
            "georef": {"origin_lat": LAT, "origin_lon": LON, "scale_meters_per_unit": 0.01, "source": "google_static_maps"},
        }
        data = ProjectData.from_dict(legacy)
        assert data.georeference is not None
        out = data.to_dict()
        assert "georef" not in out and out["metadata"]["georeference"]["origin_lat"] == LAT
        again = ProjectData.from_dict(out)
        assert again.georeference == data.georeference  # a second round trip changes nothing

    def test_the_records_own_key_wins_over_a_stale_legacy_key(self) -> None:
        record = PlanGeoreference(10.0, 20.0).to_dict()
        data = ProjectData.from_dict(
            {"metadata": {"georeference": record}, "georef": {"origin_lat": LAT, "origin_lon": LON}}
        )
        assert data.georeference == record

    def test_legacy_reference_points_land_where_they_were_measured(self) -> None:
        raw = {
            "origin_lat": LAT,
            "origin_lon": LON,
            "scale_meters_per_unit": 0.01,
            "reference_points": [
                {"name": "A", "lat": LAT, "lon": LON, "x": 120.0, "y": -80.0},
                {"name": "B", "lat": LAT, "lon": LON + 0.0010, "x": 9000.0, "y": -60.0},
            ],
        }
        block = georeference_from_legacy_block(raw, 0.0)
        assert block is not None
        geo = PlanGeoreference.from_dict(block)
        for p in geo.control_points:
            assert gps_to_scene(p.lat, p.lon, geo, 0.0) == pytest.approx((p.x_cm, p.y_cm), abs=1.0)

    @pytest.mark.parametrize(
        "bad",
        [None, 5, "x", {}, {"origin_lat": 91.0, "origin_lon": 0.0}, {"origin_lat": 1.0, "origin_lon": 1.0, "scale_meters_per_unit": -1}],
    )
    def test_an_unusable_legacy_block_is_dropped(self, bad: object) -> None:
        assert georeference_from_legacy_block(bad, 0.0) is None
        assert ProjectData.from_dict({"georef": bad}).georeference is None
