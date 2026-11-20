"""Unit tests for core/georef.py (pure, no Qt)."""

from __future__ import annotations

import math
import random

import pytest

from open_garden_planner.core.georef import (
    GeoReference,
    GeorefError,
    ReferencePoint,
    fit_from_reference_points,
    from_geo_metadata,
    georef_from_plan,
)
from open_garden_planner.core.plan_north import north_direction

LATS = [0.0, 37.77, 48.14, 60.0, 78.0, -33.9, -54.8]


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class TestProjection:
    @pytest.mark.parametrize("lat", LATS)
    @pytest.mark.parametrize("rot", [0.0, 33.0, 90.0, 135.0, 271.5])
    def test_round_trip_under_1mm_at_1km(self, lat, rot):
        g = GeoReference(lat, 11.5, rotation_deg=rot)
        for x, y in [(0, 0), (100000, 0), (0, 100000), (-70000, 70000), (50000, -90000)]:
            la, lo = g.canvas_to_gps(x, y)
            x2, y2 = g.gps_to_canvas(la, lo)
            assert math.hypot(x2 - x, y2 - y) * 0.01 < 1e-3  # metres

    def test_origin_maps_to_origin(self):
        g = GeoReference(48.0, 11.0)
        assert g.gps_to_canvas(48.0, 11.0) == (0.0, 0.0)
        assert g.canvas_to_gps(0, 0) == (48.0, 11.0)

    def test_north_east_axes_at_zero_rotation(self):
        g = GeoReference(48.0, 11.0)
        x, y = g.gps_to_canvas(48.0 + 0.001, 11.0)  # ~111 m north
        assert abs(x) < 1e-6 and y == pytest.approx(11119.5, rel=1e-3)
        x, y = g.gps_to_canvas(48.0, 11.0 + 0.001)  # east
        assert y == pytest.approx(0, abs=1e-6) and x == pytest.approx(7441, rel=1e-3)

    @pytest.mark.parametrize("rot", [0.0, 45.0, 90.0, 135.0, 200.0])
    def test_north_matches_plan_north_direction(self, rot):
        g = GeoReference(40.0, -3.0, rotation_deg=rot)
        x, y = g.gps_to_canvas(40.0 + 0.0005, -3.0)
        norm = math.hypot(x, y)
        dx, dy = north_direction(rot)
        assert (x / norm, y / norm) == pytest.approx((dx, dy), abs=1e-9)

    def test_rotation_90_north_points_right(self):
        g = GeoReference(10.0, 10.0, rotation_deg=90.0)
        x, y = g.gps_to_canvas(10.001, 10.0)
        assert x > 0 and abs(y) < 1e-6

    @pytest.mark.parametrize("lat", LATS)
    def test_distance_agrees_with_haversine_within_model_error(self, lat):
        # Equirectangular error ~ d^2 tan(lat) / 2R: 15 cm bound at 1 km, 78 deg.
        g = GeoReference(lat, 5.0)
        la, lo = g.canvas_to_gps(70000, 70000)  # ~990 m
        d_canvas = math.hypot(70000, 70000) * 0.01
        d_true = haversine_m(lat, 5.0, la, lo)
        assert d_true == pytest.approx(d_canvas, abs=0.15)
        la, lo = g.canvas_to_gps(7000, 7000)  # ~99 m: the error shrinks 100x
        assert haversine_m(lat, 5.0, la, lo) == pytest.approx(math.hypot(7000, 7000) * 0.01, abs=0.002)

    def test_antimeridian(self):
        g = GeoReference(60.0, 179.9999)
        x, y = g.gps_to_canvas(60.0, -179.9999)
        assert x > 0  # east, across the line, ~22 m
        assert x * 0.01 == pytest.approx(0.0002 * 111195 * 0.5, rel=1e-2)
        la, lo = g.canvas_to_gps(x, y)
        assert lo == pytest.approx(-179.9999, abs=1e-9)

    def test_scale(self):
        g = GeoReference(0.0, 0.0, scale_meters_per_unit=1.0)  # unit = metre
        x, y = g.gps_to_canvas(0.0, 0.00001)
        assert x == pytest.approx(1.1119, rel=1e-3)

    @pytest.mark.parametrize(
        "bad",
        [float("nan"), float("inf"), "48", None, True],
    )
    def test_invalid_origin_rejected(self, bad):
        with pytest.raises(GeorefError):
            GeoReference(bad, 11.0)
        with pytest.raises(GeorefError):
            GeoReference(48.0, bad)

    def test_range_checks(self):
        for lat, lon in [(91, 0), (0, 181), (89.5, 0)]:
            with pytest.raises(GeorefError):
                GeoReference(lat, lon)
        with pytest.raises(GeorefError):
            GeoReference(1, 1, scale_meters_per_unit=0)
        with pytest.raises(GeorefError):
            GeoReference(1, 1, scale_meters_per_unit=-1)

    def test_rotation_normalised(self):
        assert GeoReference(1, 1, rotation_deg=-90).rotation_deg == 270.0
        assert GeoReference(1, 1, rotation_deg=725).rotation_deg == 5.0

    def test_conversion_of_non_finite_is_error(self):
        g = GeoReference(1, 1)
        with pytest.raises(GeorefError):
            g.canvas_to_gps(float("nan"), 0)
        with pytest.raises(GeorefError):
            g.gps_to_canvas(float("inf"), 0)


class TestSerialisation:
    def test_rotation_not_persisted(self):
        g = GeoReference(48, 11, rotation_deg=77)
        assert "rotation_deg" not in g.to_dict()

    def test_round_trip_full(self):
        pts = (ReferencePoint("A", 48.0, 11.0, 0, 0), ReferencePoint("B", 48.001, 11.0, 0, 11000))
        g = GeoReference(
            48.0, 11.0, 0.01, 12.0, parcel_boundary=((48.0, 11.0), (48.1, 11.1)),
            reference_points=pts, fit_rms_m=0.05, source="fit",
        )
        h = GeoReference.from_dict(g.to_dict(), rotation_deg=12.0)
        assert h == g

    @pytest.mark.parametrize(
        "data",
        [None, [], "x", 5, {}, {"origin_lat": "a", "origin_lon": 1},
         {"origin_lat": float("nan"), "origin_lon": 1},
         {"origin_lat": 1, "origin_lon": 1, "scale_meters_per_unit": -3}],
    )
    def test_hostile_blocks_read_as_none(self, data):
        assert georef_from_plan(data, 0.0) is None

    def test_bad_optional_parts_are_dropped(self):
        g = georef_from_plan(
            {
                "origin_lat": 10, "origin_lon": 10,
                "parcel_boundary": [[1, 2], "x", [float("nan"), 1], [1], 5, [95, 0]],
                "reference_points": [{"lat": 1}, "x", {"lat": 1, "lon": 2, "x": 0, "y": 0, "name": 7}],
                "fit_rms_m": "huge", "source": 12,
            },
            0.0,
        )
        assert g is not None
        assert g.parcel_boundary == ((1.0, 2.0),)
        assert len(g.reference_points) == 1 and g.reference_points[0].name == ""
        assert g.fit_rms_m is None and g.source == "manual"

    def test_string_parcel_ignored(self):
        g = georef_from_plan({"origin_lat": 1, "origin_lon": 1, "parcel_boundary": "abcd"}, 0.0)
        assert g is not None and g.parcel_boundary is None


def make_points(geo: GeoReference, canvas, noise_m=0.0, seed=1):
    rnd = random.Random(seed)
    out = []
    for i, (x, y) in enumerate(canvas):
        la, lo = geo.canvas_to_gps(x, y)
        if noise_m:
            la += rnd.gauss(0, noise_m) / 111195.0
            lo += rnd.gauss(0, noise_m) / (111195.0 * math.cos(math.radians(la)))
        out.append(ReferencePoint(f"P{i}", la, lo, x, y))
    return out


SQUARE = [(0, 0), (4000, 0), (4000, 3000), (0, 3000)]


class TestFit:
    def test_one_point_translation(self):
        truth = GeoReference(52.5, 13.4, rotation_deg=20)
        pts = make_points(truth, [(1234, 567)])
        fit = fit_from_reference_points(pts, rotation_deg=20)
        assert fit.method == "translation" and fit.rms_m < 1e-6
        x, y = fit.georef.gps_to_canvas(pts[0].lat, pts[0].lon)
        assert (x, y) == pytest.approx((1234, 567), abs=1e-3)
        # moving 10 m on the plan matches truth
        la, lo = truth.canvas_to_gps(2234, 567)
        assert fit.georef.gps_to_canvas(la, lo) == pytest.approx((2234, 567), abs=0.5)

    @pytest.mark.parametrize("lat", [0.0, 48.1, 70.0, -40.0])
    @pytest.mark.parametrize("rot", [0.0, 37.0, 180.0, 300.0])
    def test_two_points_recover_transform(self, lat, rot):
        truth = GeoReference(lat, 8.0, 0.01, rot)
        pts = make_points(truth, [(100, 200), (5000, 3500)])
        fit = fit_from_reference_points(pts)
        assert fit.method == "similarity" and fit.rms_m < 1e-6
        assert fit.georef.rotation_deg == pytest.approx(rot, abs=1e-6) or abs(
            (fit.georef.rotation_deg - rot + 180) % 360 - 180) < 1e-6
        for x, y in [(0, 0), (-3000, 9000), (8000, -2000)]:
            la, lo = truth.canvas_to_gps(x, y)
            assert fit.georef.gps_to_canvas(la, lo) == pytest.approx((x, y), abs=0.1)

    def test_four_points_exact(self):
        truth = GeoReference(47.0, 8.5, 0.01, 110.0)
        fit = fit_from_reference_points(make_points(truth, SQUARE))
        assert fit.rms_m < 1e-6 and len(fit.residuals_m) == 4
        assert fit.georef.scale_meters_per_unit == pytest.approx(0.01, rel=1e-9)
        assert fit.georef.rotation_deg == pytest.approx(110.0, abs=1e-6)
        assert fit.georef.origin_lat == pytest.approx(47.0, abs=1e-8)
        assert fit.georef.origin_lon == pytest.approx(8.5, abs=1e-8)

    def test_noise_rms_near_noise_level_and_pose_close(self):
        truth = GeoReference(47.0, 8.5, 0.01, 25.0)
        fit = fit_from_reference_points(make_points(truth, SQUARE, noise_m=0.5, seed=3))
        assert 0.05 < fit.rms_m < 1.5
        assert fit.georef.rotation_deg == pytest.approx(25.0, abs=1.5)
        assert fit.georef.scale_meters_per_unit == pytest.approx(0.01, rel=0.02)
        assert fit.georef.fit_rms_m == pytest.approx(fit.rms_m)

    def test_outlier_shows_in_residuals(self):
        truth = GeoReference(47.0, 8.5, 0.01, 0.0)
        pts = make_points(truth, SQUARE + [(2000, 1500)])
        bad = ReferencePoint("bad", pts[4].lat + 0.0003, pts[4].lon, pts[4].x, pts[4].y)
        fit = fit_from_reference_points(pts[:4] + [bad])
        assert max(fit.residuals_m) > 5
        assert fit.rms_m > 5

    def test_affine_flag_reports_smaller_or_equal_residuals(self):
        truth = GeoReference(47.0, 8.5, 0.01, 25.0)
        pts = make_points(truth, SQUARE, noise_m=0.7, seed=5)
        sim = fit_from_reference_points(pts)
        aff = fit_from_reference_points(pts, allow_affine=True)
        assert aff.method == "affine" and aff.rms_m <= sim.rms_m + 1e-9

    def test_accepts_dict_points(self):
        truth = GeoReference(47.0, 8.5)
        pts = [p.to_dict() for p in make_points(truth, SQUARE)]
        assert fit_from_reference_points(pts).rms_m < 1e-6

    def test_across_antimeridian(self):
        truth = GeoReference(65.0, 179.9995, 0.01, 40.0)
        fit = fit_from_reference_points(make_points(truth, SQUARE))
        assert fit.rms_m < 1e-6
        assert fit.georef.origin_lon == pytest.approx(179.9995, abs=1e-8)

    @pytest.mark.parametrize(
        "pts",
        [
            [],
            [ReferencePoint("a", 1, 1, 0, 0), ReferencePoint("b", 1, 1, 100, 0)],  # same GPS
            [ReferencePoint("a", 1, 1, 0, 0), ReferencePoint("b", 1.001, 1, 0, 0)],  # same canvas
        ],
    )
    def test_degenerate_rejected(self, pts):
        with pytest.raises(GeorefError):
            fit_from_reference_points(pts)

    def test_collinear_rejected(self):
        truth = GeoReference(47.0, 8.5)
        with pytest.raises(GeorefError, match="collinear"):
            fit_from_reference_points(make_points(truth, [(0, 0), (1000, 0), (2000, 0), (3000, 0)]))

    def test_bad_input_rejected(self):
        with pytest.raises(GeorefError):
            fit_from_reference_points([{"lat": float("nan"), "lon": 1, "x": 0, "y": 0}])
        with pytest.raises(GeorefError):
            fit_from_reference_points([{"lat": 99, "lon": 1, "x": 0, "y": 0}])
        with pytest.raises(GeorefError):
            fit_from_reference_points(["nope"])
        with pytest.raises(GeorefError):
            fit_from_reference_points([{"lat": 1, "lon": 1, "x": 0, "y": 0}], scale_meters_per_unit=0)


class TestFromGeoMetadata:
    META = {
        "center": [48.137, 11.575],
        "bbox_nw": [48.1375, 11.5742],
        "bbox_se": [48.1365, 11.5758],
        "zoom": 20,
        "meters_per_pixel": 0.05,
        "source": "google_static_maps",
    }

    def test_image_centre_is_bbox_centre(self):
        g = from_geo_metadata(self.META, image_center=(2500.0, 1800.0), scale_factor=0.2)
        la, lo = g.canvas_to_gps(2500.0, 1800.0)
        assert (la, lo) == pytest.approx((48.137, 11.575), abs=1e-9)
        assert g.scale_meters_per_unit == pytest.approx(0.01)
        assert g.rotation_deg == 0.0 and g.source == "google_static_maps"

    def test_north_of_centre_is_plus_y(self):
        g = from_geo_metadata(self.META, image_center=(2500.0, 1800.0), scale_factor=0.2)
        x, y = g.gps_to_canvas(48.1375, 11.575)  # bbox top edge
        assert y > 1800 and x == pytest.approx(2500, abs=1e-3)
        nw = g.gps_to_canvas(48.1375, 11.5742)
        se = g.gps_to_canvas(48.1365, 11.5758)
        assert nw[0] < se[0] and nw[1] > se[1]

    def test_calibrated_scale_changes_k(self):
        g = from_geo_metadata(self.META, image_center=(0, 0), scale_factor=0.4)
        assert g.scale_meters_per_unit == pytest.approx(0.02)

    @pytest.mark.parametrize("meta", [{}, {"center": [1, 1]}, {"center": "x", "meters_per_pixel": 1},
                                      {"center": [1, 1], "meters_per_pixel": -1}])
    def test_bad_metadata(self, meta):
        with pytest.raises(GeorefError):
            from_geo_metadata(meta, image_center=(0, 0), scale_factor=1)


class TestBounds:
    @pytest.mark.parametrize("scale", [1e-300, 1e-7, 1e4, 1e300])
    def test_absurd_scales_are_refused(self, scale):
        with pytest.raises(GeorefError):
            GeoReference(10.0, 10.0, scale_meters_per_unit=scale)

    def test_a_point_too_far_for_the_origin_is_refused_not_emitted(self):
        g = GeoReference(10.0, 10.0, scale_meters_per_unit=1.0)
        with pytest.raises(GeorefError, match="too far"):
            g.canvas_to_gps(0.0, 2e7)  # 20,000 km north
        assert g.canvas_to_gps(0.0, 1000.0)[0] > 10.0

    def test_hostile_scale_in_a_file_reads_as_not_georeferenced(self):
        assert georef_from_plan({"origin_lat": 1, "origin_lon": 1, "scale_meters_per_unit": 1e300}, 0.0) is None
