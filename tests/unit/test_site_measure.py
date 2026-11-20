"""Unit tests for the site-measurement core and its Agent API wrappers (Qt-free)."""

from __future__ import annotations

import math
from typing import Any

import pytest

from open_garden_planner.agent_api import site_queries as q
from open_garden_planner.core import object_height as oh
from open_garden_planner.core import site_measure as sm


def _poly(points: list[tuple[float, float]], **extra: Any) -> dict[str, Any]:
    return {"type": "polygon", "points": [{"x": x, "y": y} for x, y in points], **extra}


def _rect(x: float, y: float, w: float, h: float, **extra: Any) -> dict[str, Any]:
    return {"type": "rectangle", "x": x, "y": y, "width": w, "height": h, **extra}


def _shape(obj: dict[str, Any]) -> sm.Shape:
    shape = sm.shape_from_object(obj)
    assert shape is not None
    return shape


class TestAreaPerimeter:
    def test_rotated_rectangle_keeps_area_and_perimeter(self) -> None:
        shape = _shape(_rect(0, 0, 200, 100, rotation_angle=30.0))
        area, perimeter, holes = sm.area_and_perimeter(shape)
        assert area == pytest.approx(20000.0)
        assert perimeter == pytest.approx(600.0)
        assert holes == 0

    def test_rotated_rectangle_corners_follow_the_qt_convention(self) -> None:
        shape = _shape(_rect(-1, -1, 2, 2, rotation_angle=90.0))
        corners = {(round(x), round(y)) for x, y in shape.parts[0].points}
        assert corners == {(-1, -1), (1, -1), (1, 1), (-1, 1)}
        # A 4 x 2 rectangle turned 90 degrees stands 2 wide and 4 high.
        tall = _shape(_rect(-2, -1, 4, 2, rotation_angle=90.0))
        assert tall.bbox == pytest.approx((-1.0, -2.0, 1.0, 2.0))

    def test_concave_polygon_area(self) -> None:
        l_shape = [(0, 0), (30, 0), (30, 10), (10, 10), (10, 30), (0, 30)]
        area, perimeter, _ = sm.area_and_perimeter(_shape(_poly(l_shape)))
        assert area == pytest.approx(500.0)
        assert perimeter == pytest.approx(120.0)

    def test_polygon_with_hole_subtracts_it(self) -> None:
        obj = _poly([(0, 0), (100, 0), (100, 100), (0, 100)],
                    holes=[[{"x": 40, "y": 40}, {"x": 60, "y": 40}, {"x": 60, "y": 60}, {"x": 40, "y": 60}]])
        area, perimeter, holes = sm.area_and_perimeter(_shape(obj))
        assert (area, perimeter, holes) == (pytest.approx(9600.0), pytest.approx(400.0), 1)

    def test_circle_and_ellipse_use_closed_forms(self) -> None:
        circle = _shape({"type": "circle", "center_x": 5, "center_y": 5, "radius": 10})
        area, perimeter, _ = sm.area_and_perimeter(circle)
        assert area == pytest.approx(math.pi * 100)
        assert perimeter == pytest.approx(2 * math.pi * 10)
        ellipse = _shape({"type": "ellipse", "center_x": 0, "center_y": 0, "semi_x": 30, "semi_y": 10,
                          "rotation_angle": 45.0})
        area, perimeter, _ = sm.area_and_perimeter(ellipse)
        assert area == pytest.approx(math.pi * 300)
        assert perimeter == pytest.approx(sm.ellipse_perimeter(30, 10))
        # Rotation turns the box: a 60 x 20 ellipse at 90 degrees is 20 wide, 60 high.
        turned = _shape({"type": "ellipse", "center_x": 0, "center_y": 0, "semi_x": 30, "semi_y": 10,
                         "rotation_angle": 90.0})
        assert turned.bbox[2] - turned.bbox[0] == pytest.approx(20.0, abs=0.01)

    def test_line_has_no_area(self) -> None:
        with pytest.raises(ValueError):
            sm.area_and_perimeter(_shape({"type": "polyline", "points": [{"x": 0, "y": 0}, {"x": 1, "y": 1}]}))


class TestLength:
    def test_polyline_sums_segments(self) -> None:
        obj = {"type": "polyline", "points": [{"x": 0, "y": 0}, {"x": 30, "y": 40}, {"x": 30, "y": 140}]}
        assert sm.length_of(_shape(obj)) == (pytest.approx(150.0), False)
        assert _shape(obj).vertex_count == 3

    def test_arc_uses_exact_arc_length(self) -> None:
        obj = {"type": "arc", "center_x": 0, "center_y": 0, "radius": 100, "start_deg": 0, "span_deg": 90}
        length, closed = sm.length_of(_shape(obj))
        assert length == pytest.approx(50 * math.pi) and not closed

    def test_bezier_straight_handles_equals_chord(self) -> None:
        obj = {"type": "bezier",
               "anchors": [{"x": 0, "y": 0}, {"x": 300, "y": 0}],
               "handles_in": [{"x": 0, "y": 0}, {"x": 200, "y": 0}],
               "handles_out": [{"x": 100, "y": 0}, {"x": 300, "y": 0}]}
        length, _ = sm.length_of(_shape(obj))
        assert length == pytest.approx(300.0, rel=1e-9)

    def test_bezier_with_a_bend_is_longer_than_its_chord(self) -> None:
        obj = {"type": "bezier",
               "anchors": [{"x": 0, "y": 0}, {"x": 100, "y": 0}],
               "handles_in": [{"x": 0, "y": 0}, {"x": 100, "y": 100}],
               "handles_out": [{"x": 0, "y": 100}, {"x": 100, "y": 0}]}
        length, _ = sm.length_of(_shape(obj))
        assert 100.0 < length < 300.0

    def test_closed_shape_length_is_its_perimeter(self) -> None:
        assert sm.length_of(_shape(_rect(0, 0, 10, 20))) == (pytest.approx(60.0), True)


class TestClearance:
    def test_disjoint_rectangles(self) -> None:
        result = sm.clearance(_shape(_rect(0, 0, 100, 100)), _shape(_rect(130, 40, 50, 20)))
        assert result.distance_cm == pytest.approx(30.0)
        assert result.relation == "disjoint" and not result.overlaps
        assert result.point_a[0] == pytest.approx(100.0) and result.point_b[0] == pytest.approx(130.0)

    def test_diagonal_corner_gap(self) -> None:
        result = sm.clearance(_shape(_rect(0, 0, 10, 10)), _shape(_rect(13, 14, 10, 10)))
        assert result.distance_cm == pytest.approx(5.0)
        assert result.point_a == pytest.approx((10.0, 10.0)) and result.point_b == pytest.approx((13.0, 14.0))

    def test_touching_shares_an_edge_but_does_not_overlap(self) -> None:
        result = sm.clearance(_shape(_rect(0, 0, 10, 10)), _shape(_rect(10, 0, 10, 10)))
        assert result.distance_cm == 0.0 and result.relation == "touching" and not result.overlaps

    def test_overlapping_by_crossing_edges(self) -> None:
        result = sm.clearance(_shape(_rect(0, 0, 10, 10)), _shape(_rect(5, 5, 10, 10)))
        assert result.distance_cm == 0.0 and result.relation == "overlapping" and result.overlaps

    def test_contained_shape(self) -> None:
        result = sm.clearance(_shape(_rect(0, 0, 100, 100)), _shape(_rect(40, 40, 10, 10)))
        assert result.distance_cm == 0.0 and result.relation == "contained" and result.overlaps

    def test_inside_a_hole_measures_to_the_hole_edge(self) -> None:
        ring = _poly([(0, 0), (100, 0), (100, 100), (0, 100)],
                     holes=[[{"x": 20, "y": 20}, {"x": 80, "y": 20}, {"x": 80, "y": 80}, {"x": 20, "y": 80}]])
        result = sm.clearance(_shape(ring), _shape(_rect(45, 45, 10, 10)))
        assert result.distance_cm == pytest.approx(25.0) and not result.overlaps

    def test_rotated_rectangle_clearance_uses_scene_coordinates(self) -> None:
        # A 100 x 10 bar centred on x=50 turned 90 degrees stands 10 wide, 100 high.
        bar = _shape(_rect(0, -5, 100, 10, rotation_angle=90.0))
        other = _shape(_rect(100, 0, 10, 10))
        assert sm.clearance(bar, other).distance_cm == pytest.approx(45.0)

    def test_circle_to_rectangle_within_polygon_tolerance(self) -> None:
        circle = _shape({"type": "circle", "center_x": 0, "center_y": 0, "radius": 50})
        result = sm.clearance(circle, _shape(_rect(80, -10, 20, 20)))
        assert result.distance_cm == pytest.approx(30.0, abs=0.01)

    def test_ellipse_clearance_along_the_long_axis(self) -> None:
        ellipse = _shape({"type": "ellipse", "center_x": 0, "center_y": 0, "semi_x": 100, "semi_y": 20})
        result = sm.clearance(ellipse, _shape(_rect(150, -5, 10, 10)))
        assert result.distance_cm == pytest.approx(50.0, abs=0.01)

    def test_line_to_line(self) -> None:
        a = _shape({"type": "polyline", "points": [{"x": 0, "y": 0}, {"x": 100, "y": 0}]})
        b = _shape({"type": "polyline", "points": [{"x": 50, "y": 30}, {"x": 50, "y": 90}]})
        assert sm.clearance(a, b).distance_cm == pytest.approx(30.0)
        crossing = _shape({"type": "polyline", "points": [{"x": 50, "y": -10}, {"x": 50, "y": 10}]})
        assert sm.clearance(a, crossing).relation == "overlapping"

    def test_group_members_are_offset_by_the_group_position(self) -> None:
        group = {"type": "group", "x": 100.0, "y": 0.0, "children": [_rect(0, 0, 10, 10)]}
        result = sm.clearance(_shape(group), _shape(_rect(0, 0, 40, 10)))
        assert result.distance_cm == pytest.approx(60.0)


class TestPointToShape:
    def test_outside_returns_edge_point(self) -> None:
        res = sm.point_to_shape((130.0, 50.0), _shape(_rect(0, 0, 100, 100)))
        assert res.distance_cm == pytest.approx(30.0) and not res.inside
        assert res.nearest == pytest.approx((100.0, 50.0))

    def test_inside_is_zero_with_flag_and_depth(self) -> None:
        res = sm.point_to_shape((10.0, 50.0), _shape(_rect(0, 0, 100, 100)))
        assert res.distance_cm == 0.0 and res.inside and res.edge_distance_cm == pytest.approx(10.0)

    def test_on_the_edge_is_not_inside(self) -> None:
        res = sm.point_to_shape((100.0, 50.0), _shape(_rect(0, 0, 100, 100)))
        assert res.distance_cm == 0.0 and not res.inside


class TestBearing:
    @pytest.mark.parametrize(
        ("b", "expected"),
        [((0, 10), 0.0), ((10, 0), 90.0), ((0, -10), 180.0), ((-10, 0), 270.0), ((-10, 10), 315.0)],
    )
    def test_plan_bearing_is_clockwise_from_plan_up_in_y_up(self, b: tuple[float, float], expected: float) -> None:
        assert sm.plan_bearing_deg((0, 0), b) == pytest.approx(expected)

    def test_compass_bearing_wraps_around_zero(self) -> None:
        # North turned 90 degrees clockwise (to plan-right): plan-right IS north.
        assert sm.compass_bearing_deg((0, 0), (10, 0), 90.0) == pytest.approx(0.0)
        # Plan-up is then 270 (west); plan-left is 180 (south).
        assert sm.compass_bearing_deg((0, 0), (0, 10), 90.0) == pytest.approx(270.0)
        assert sm.compass_bearing_deg((0, 0), (-10, 0), 90.0) == pytest.approx(180.0)

    def test_wrap_near_north(self) -> None:
        # 5 degrees west of plan-up with north at 10 degrees: bearing 345 on the plan,
        # minus 10 -> 335; with north at 350 the same line reads 15.
        a, b = (0.0, 0.0), (-math.sin(math.radians(5)), math.cos(math.radians(5)))
        assert sm.compass_bearing_deg(a, b, 10.0) == pytest.approx(345.0)
        assert sm.compass_bearing_deg(a, b, 350.0) == pytest.approx(5.0)

    def test_zero_vector_has_no_bearing(self) -> None:
        assert sm.plan_bearing_deg((1, 1), (1, 1)) is None


class TestGeoLink:
    SNAP = {"georef": {"origin_lat": 33.0, "origin_lon": -111.0, "scale_meters_per_unit": 0.01},
            "north_angle_deg": 0.0}

    def test_north_offset_maps_to_plus_y(self) -> None:
        link = sm.GeoLink.from_snapshot(self.SNAP)
        assert link is not None
        x, y = link.to_plan(33.0 + 1e-4, -111.0)
        assert x == pytest.approx(0.0, abs=1e-6)
        assert y == pytest.approx(math.radians(1e-4) * sm.EARTH_RADIUS_M * 100.0)

    def test_east_offset_follows_the_plan_north_angle(self) -> None:
        snap = {**self.SNAP, "north_angle_deg": 90.0}
        link = sm.GeoLink.from_snapshot(snap)
        assert link is not None
        x, y = link.to_plan(33.0, -111.0 + 1e-4)
        assert x == pytest.approx(0.0, abs=1e-6) and y < 0  # east points plan-down

    def test_missing_georef_is_none(self) -> None:
        assert sm.GeoLink.from_snapshot({}) is None


class TestValidation:
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), 1e9, True, "5", None])
    def test_finite_cm_rejects(self, bad: object) -> None:
        with pytest.raises(ValueError):
            sm.finite_cm(bad, "x_cm")


# --- Agent API wrappers ------------------------------------------------------


def _snapshot() -> dict[str, Any]:
    return {
        "canvas": {"width": 1000.0, "height": 800.0},
        "layers": [{"id": "L1", "name": "Base"}, {"id": "L2", "name": "Empty"}],
        "agent_meta": {"as_of_date": "2031-01-01"},
        "north_angle_deg": 90.0,
        "objects": [
            {"type": "rectangle", "item_id": "house", "x": 100, "y": 100, "width": 300, "height": 200,
             "object_type": "HOUSE", "name": "House", "layer_id": "L1"},
            {"type": "polyline", "item_id": "wall", "object_type": "WALL", "name": "Boundary wall",
             "layer_id": "L1",
             "points": [{"x": 0, "y": 0}, {"x": 0, "y": 500}, {"x": 800, "y": 500}, {"x": 800, "y": 0}]},
            {"type": "background_image", "item_id": "bg", "position": {"x": -5000, "y": -5000}},
        ],
    }


class TestQueries:
    def test_unknown_ids_return_none(self) -> None:
        snap = _snapshot()
        assert q.measure_area(snap, "nope") is None
        assert q.measure_length(snap, "nope") is None
        assert q.measure_clearance(snap, "house", "nope") is None
        assert q.measure_to_object(snap, {"x_cm": 0, "y_cm": 0}, "nope") is None
        assert q.get_object_height(snap, "nope") is None

    def test_clearance_to_boundary_alias(self) -> None:
        result = q.measure_clearance(_snapshot(), "house", "boundary")
        assert result is not None
        assert result.distance_cm == pytest.approx(100.0) and result.distance_m == pytest.approx(1.0)

    def test_measure_area_and_length(self) -> None:
        area = q.measure_area(_snapshot(), "house")
        assert area is not None and area.area_m2 == pytest.approx(6.0) and area.perimeter_cm == 1000.0
        length = q.measure_length(_snapshot(), "wall")
        assert length is not None and length.length_cm == 1800.0 and length.vertex_count == 4

    def test_measure_points_modes_and_bearing(self) -> None:
        snap = _snapshot()
        res = q.measure_points(snap, {"x_cm": 0, "y_cm": 0}, "house")
        assert res.b.kind == "object" and (res.b.x_cm, res.b.y_cm) == (250.0, 200.0)
        assert res.distance_cm == pytest.approx(math.hypot(250, 200))
        east = q.measure_points(snap, {"x_cm": 0, "y_cm": 0}, {"x_cm": 100, "y_cm": 0})
        assert east.plan_bearing_deg == pytest.approx(90.0)
        assert east.compass_bearing_deg == pytest.approx(0.0)  # north is plan-right
        assert east.north_angle_deg == 90.0

    def test_geographic_point_needs_a_georeference(self) -> None:
        with pytest.raises(ValueError, match="no georeference"):
            q.measure_points(_snapshot(), {"lat": 33.0, "lon": -111.0}, {"x_cm": 0, "y_cm": 0})
        snap = {**_snapshot(), "georef": {"origin_lat": 33.0, "origin_lon": -111.0}, "north_angle_deg": 0.0}
        res = q.measure_points(snap, {"lat": 33.0, "lon": -111.0}, {"x_cm": 300, "y_cm": 400})
        assert res.distance_cm == pytest.approx(500.0, abs=1e-6)

    @pytest.mark.parametrize(
        "bad",
        [{"x_cm": float("nan"), "y_cm": 0}, {"x_cm": 0, "y_cm": float("inf")}, {"x_cm": 1e12, "y_cm": 0},
         {"x_cm": 1}, {"foo": 1}, 5, "missing-id", {"lat": 95.0, "lon": 0.0}],
    )
    def test_untrusted_points_are_refused(self, bad: object) -> None:
        snap = {**_snapshot(), "georef": {"origin_lat": 33.0, "origin_lon": -111.0}}
        with pytest.raises(ValueError):
            q.measure_points(snap, bad, {"x_cm": 0, "y_cm": 0})

    def test_path_length_legs_and_closing(self) -> None:
        pts = [{"x_cm": 0, "y_cm": 0}, {"x_cm": 30, "y_cm": 0}, {"x_cm": 30, "y_cm": 40}]
        open_path = q.measure_path(_snapshot(), pts)
        assert open_path.leg_lengths_cm == [30.0, 40.0] and open_path.total_cm == 70.0
        closed = q.measure_path(_snapshot(), pts, closed=True)
        assert closed.leg_lengths_cm == [30.0, 40.0, 50.0] and closed.total_cm == 120.0 and closed.closed

    def test_path_point_count_bounds(self) -> None:
        one = [{"x_cm": 0, "y_cm": 0}]
        with pytest.raises(ValueError):
            q.measure_path(_snapshot(), one)
        many = [{"x_cm": i, "y_cm": 0} for i in range(sm.MAX_PATH_POINTS + 1)]
        with pytest.raises(ValueError, match="2 to 200"):
            q.measure_path(_snapshot(), many)
        assert q.measure_path(_snapshot(), many[: sm.MAX_PATH_POINTS]).total_cm == sm.MAX_PATH_POINTS - 1

    def test_measure_to_object(self) -> None:
        res = q.measure_to_object(_snapshot(), {"x_cm": 50, "y_cm": 200}, "house")
        assert res is not None and res.distance_cm == pytest.approx(50.0) and not res.inside
        inside = q.measure_to_object(_snapshot(), {"x_cm": 150, "y_cm": 200}, "house")
        assert inside is not None and inside.inside and inside.distance_cm == 0.0

    def test_site_extents_is_bounded_and_excludes_the_background(self) -> None:
        ext = q.site_extents(_snapshot())
        assert ext.object_count == 2
        assert (ext.min_x_cm, ext.min_y_cm, ext.max_x_cm, ext.max_y_cm) == (0.0, 0.0, 800.0, 500.0)
        assert ext.layer_counts == {"Base": 2, "Empty": 0}
        assert ext.boundary is not None and ext.boundary.closed_implicitly
        assert ext.boundary.area_cm2 == 400000.0 and ext.boundary.perimeter_cm == 2600.0
        assert ext.tallest is not None and ext.tallest.item_id == "house" and ext.tallest.height_cm == 450.0
        assert ext.canvas_width_cm == 1000.0


class TestHeights:
    @pytest.mark.parametrize(
        ("object_type", "metadata", "source"),
        [
            ("TREE", {"object_height_cm": 700.0}, oh.SOURCE_CUSTOM),
            ("CONTAINER", {"container_height_cm": 45.0}, oh.SOURCE_CONTAINER),
            ("TREE", {"plant_species": {"max_height_cm": 500.0},
                      "plant_instance": {"planting_date": "2026-01-01", "current_height_cm": 100.0}},
             oh.SOURCE_GROWN),
            ("TREE", {"plant_instance": {"current_height_cm": 120.0}}, oh.SOURCE_CURRENT),
            ("TREE", {"plant_species": {"max_height_cm": 800.0}}, oh.SOURCE_SPECIES),
            ("HOUSE", {}, oh.SOURCE_DEFAULT),
            ("LAWN", {}, oh.SOURCE_NONE),
        ],
    )
    def test_each_source_branch_matches_the_core_ladder(
        self, object_type: str, metadata: dict[str, Any], source: str
    ) -> None:
        snap = _snapshot()
        snap["objects"] = [{"type": "circle", "item_id": "o", "object_type": object_type,
                            "metadata": metadata, "name": "O", "center_x": 0, "center_y": 0, "radius": 5}]
        info = q.get_object_height(snap, "o")
        assert info is not None and info.source == source
        from datetime import date

        expected, expected_source = oh.resolve_height(object_type, metadata, date(2031, 1, 1))
        assert (info.height_cm, info.source) == (expected, expected_source)
        assert info.as_of_date == "2031-01-01"
        assert info.height_m == (None if expected is None else expected / 100.0)

    def test_plant_reports_current_and_mature(self) -> None:
        snap = _snapshot()
        snap["agent_meta"] = {"as_of_date": None}
        snap["objects"] = [{"type": "circle", "item_id": "t", "object_type": "TREE", "center_x": 0,
                            "center_y": 0, "radius": 5,
                            "metadata": {"plant_species": {"max_height_cm": 500.0},
                                         "plant_instance": {"current_height_cm": 120.0}}}]
        info = q.get_object_height(snap, "t")
        assert info is not None and info.current_height_cm == 120.0 and info.mature_height_cm == 500.0
        assert info.source == oh.SOURCE_CURRENT and info.as_of_date is None

    def test_list_is_sorted_bounded_and_filtered(self) -> None:
        snap = _snapshot()
        snap["objects"] = [
            {"type": "rectangle", "item_id": f"h{i}", "x": 0, "y": 0, "width": 1, "height": 1,
             "object_type": "FENCE", "name": f"F{i}", "layer_id": "L1",
             "metadata": {"object_height_cm": 100.0 + i}} for i in range(5)
        ] + [{"type": "rectangle", "item_id": "lawn", "x": 0, "y": 0, "width": 1, "height": 1,
              "object_type": "LAWN", "layer_id": "L1"}]
        rows = q.list_object_heights(snap, limit=3)
        assert [r.item_id for r in rows] == ["h4", "h3", "h2"]
        assert len(q.list_object_heights(snap)) == 5  # the lawn has no height
        assert len(q.list_object_heights(snap, layer="Base", type="fence")) == 5
        assert q.list_object_heights(snap, layer="Empty") == []
        for bad in (0, 201, True, "5"):
            with pytest.raises(ValueError):
                q.list_object_heights(snap, limit=bad)  # type: ignore[arg-type]
