"""Unit tests for the Qt-free field-record planner (WP-R)."""

from __future__ import annotations

import pytest

from open_garden_planner.core.georef import GeoReference
from open_garden_planner.services import field_record_placement as frp
from open_garden_planner.services import field_records as fr

GEO = GeoReference(48.0, 11.0)


def _rec(**kw):
    kw.setdefault("id", "rec1")
    return fr.FieldRecord.from_dict(kw)


def _species(_name):
    return {"scientific_name": "Malus domestica", "common_name": "Apple", "max_height_cm": 500, "max_spread_cm": 400}


def test_plant_uses_captured_plan_position_over_georef():
    rec = _rec(plant_name="Apple", lat=48.0001, lon=11.0001, bearing_deg=90,
               geometry={"type": "point", "lat": 48.0001, "lon": 11.0001, "x_cm": 123.0, "y_cm": 456.0})
    plan = frp.plan_record(rec, GEO, species_lookup=_species)
    assert (plan.spec.x, plan.spec.y) == (123.0, 456.0)
    assert plan.spec.object_type == "TREE" and plan.spec.species == "Malus domestica"
    assert plan.spec.radius == pytest.approx(200.0)
    assert plan.spec.metadata["field_record"]["record_id"] == "rec1"


def test_plant_without_geometry_projects_the_gps_fix():
    rec = _rec(plant_name="Apple", lat=48.0, lon=11.0)
    spec = frp.plan_record(rec, GEO, species_lookup=_species).spec
    assert (spec.x, spec.y) == pytest.approx((0.0, 0.0), abs=1e-6)


def test_unknown_species_and_missing_georef_are_refused():
    with pytest.raises(frp.PlacementError, match="No species"):
        frp.plan_record(_rec(plant_name="Nope", lat=48.0, lon=11.0), GEO, species_lookup=lambda _n: None)
    with pytest.raises(frp.PlacementError, match="not georeferenced"):
        frp.plan_record(_rec(plant_name="Apple", lat=48.0, lon=11.0), None, species_lookup=_species)


def test_note_and_unknown_wish_become_callouts():
    note = _rec(kind="note", notes="wet corner", lat=48.0, lon=11.0)
    spec = frp.plan_record(note, GEO).spec
    assert spec.object_type == "GENERIC_CALLOUT" and "wet corner" in spec.text
    wish = _rec(kind="placement_request", what="a swing", lat=48.0, lon=11.0, group_id="g1")
    placed = frp.plan_record(wish, GEO, species_lookup=lambda _n: None)
    assert placed.spec.object_type == "GENERIC_CALLOUT" and placed.warnings


def test_wish_with_known_species_is_a_plant():
    wish = _rec(kind="placement_request", what="apple", lat=48.0, lon=11.0, group_id="g1")
    assert frp.plan_record(wish, GEO, species_lookup=_species).spec.object_type == "TREE"


def test_irrigation_polyline_closes_and_keeps_vertices():
    pts = [[48.0, 11.0, 10.0, 10.0], [48.0, 11.0, 100.0, 10.0], [48.0, 11.0, 100.0, 90.0]]
    rec = _rec(kind="irrigation_path", geometry={"type": "polyline", "points": pts}, closes_on=True,
               diameter_mm=160, flow_lph=4)
    spec = frp.plan_record(rec, GEO).spec
    assert spec.layer_name == "Irrigation" and spec.points[0] == spec.points[-1] and len(spec.points) == 4
    assert spec.stroke_width_cm == 16.0
    assert spec.metadata["field_record"]["flow_lph"] == 4


def test_irrigation_vertex_without_xy_uses_georef():
    rec = _rec(kind="irrigation_path",
               geometry={"type": "polyline", "points": [[48.0, 11.0, None, None], [48.0, 11.0001, None, None]]})
    spec = frp.plan_record(rec, GEO).spec
    assert spec.points[0] == pytest.approx((0.0, 0.0), abs=1e-6) and spec.points[1][0] > 0
    with pytest.raises(frp.PlacementError, match="not georeferenced"):
        frp.plan_record(rec, None)


def test_reference_point_needs_plan_position():
    rec = _rec(kind="reference_point", name="Corner", lat=48.0, lon=11.0,
               geometry={"type": "point", "lat": 48.0, "lon": 11.0, "x_cm": 5.0, "y_cm": 6.0})
    spec = frp.plan_record(rec, None).spec
    assert (spec.name, spec.x, spec.y) == ("Corner", 5.0, 6.0)
    bare = _rec(kind="reference_point", lat=48.0, lon=11.0)
    with pytest.raises(frp.PlacementError, match="plan position"):
        frp.plan_record(bare, None)


def test_decided_records_are_refused():
    for status in ("rejected", "superseded"):
        with pytest.raises(frp.PlacementError, match="reopen"):
            frp.plan_record(_rec(plant_name="A", lat=48.0, lon=11.0, status=status), GEO, species_lookup=_species)
    placed = _rec(plant_name="A", lat=48.0, lon=11.0, placed_object_id="x")
    with pytest.raises(frp.PlacementError, match="already placed"):
        frp.plan_record(placed, GEO, species_lookup=_species)
    assert frp.plan_record(placed, GEO, species_lookup=_species, allow_duplicate=True)
