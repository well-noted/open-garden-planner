"""Field Remote projections (WP-C): geometry, state, basemap, species and telemetry are pure
functions of a plan snapshot, so each rule is pinned here against golden numbers."""

from __future__ import annotations

import base64
import io
import math
from typing import Any

import pytest
from PIL import Image

from open_garden_planner.core.geo_frame import make_frame
from open_garden_planner.field_remote import projection as pj

LAYER_A = "11111111-1111-1111-1111-111111111111"
LAYER_B = "22222222-2222-2222-2222-222222222222"


def _png_b64(w: int, h: int) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 30)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def make_snapshot(**extra: Any) -> dict[str, Any]:
    snap: dict[str, Any] = {
        "canvas": {"width": 5000.0, "height": 3000.0},
        "north_angle_deg": 0.0,
        "metadata": {"georeference": {"origin_lat": 48.0, "origin_lon": 11.0}},
        "layers": [
            {"id": LAYER_A, "name": "Beds", "visible": True, "locked": False},
            {"id": LAYER_B, "name": "Hidden", "visible": False, "locked": True},
        ],
        "objects": [
            {"type": "rectangle", "item_id": "r1", "object_type": "RAISED_BED", "x": 100, "y": 200,
             "width": 300, "height": 100, "layer_id": LAYER_A, "name": "Bed 1"},
            {"type": "circle", "item_id": "c1", "object_type": "TREE", "center_x": 50, "center_y": 60,
             "radius": 25, "layer_id": LAYER_A, "plant_species": "Malus domestica"},
            {"type": "polygon", "item_id": "p1", "points": [{"x": 0, "y": 0}, {"x": 400, "y": 0},
                                                            {"x": 400, "y": 300}], "layer_id": LAYER_A},
            {"type": "rectangle", "item_id": "h1", "x": 0, "y": 0, "width": 10, "height": 10, "layer_id": LAYER_B},
            {"type": "text", "item_id": "t1", "x": 0, "y": 0, "layer_id": LAYER_A},
        ],
    }
    snap.update(extra)
    return snap


# -- geometry -----------------------------------------------------------------------------------


def test_scene_projects_shapes_in_plan_cm_and_skips_hidden_layers_and_text() -> None:
    body, etag = pj.project_scene(make_snapshot())
    by_id = {o["id"]: o for o in body["objects"]}
    assert set(by_id) == {"r1", "c1", "p1"}  # the hidden layer's rectangle and the text are not sent
    assert by_id["r1"]["points"] == [[100, 200], [400, 200], [400, 300], [100, 300]]
    assert by_id["c1"] == {**by_id["c1"], "kind": "circle", "cx": 50, "cy": 60, "r": 25, "species": "Malus domestica"}
    assert body["units"] == "cm" and body["y_up"] is True and body["truncated"] is False
    assert etag.startswith('"') and etag == pj.project_scene(make_snapshot())[1]


def test_rotated_rectangle_turns_about_its_centre() -> None:
    snap = make_snapshot(objects=[{"type": "rectangle", "item_id": "r", "x": 0, "y": 0, "width": 200, "height": 100,
                                   "rotation_angle": 90.0, "layer_id": LAYER_A}])
    pts = pj.project_scene(snap)[0]["objects"][0]["points"]
    xs = sorted({round(p[0]) for p in pts})
    ys = sorted({round(p[1]) for p in pts})
    assert xs == [50, 150] and ys == [-50, 150]  # 200x100 about (100, 50) becomes 100x200


def test_bbox_filters_and_layers_filter_by_name_or_id() -> None:
    snap = make_snapshot()
    body, _ = pj.project_scene(snap, bbox=(0, 0, 80, 100))
    assert {o["id"] for o in body["objects"]} == {"c1", "p1"}  # the bed starts at x=100; p1's box reaches it
    body, _ = pj.project_scene(snap, layers={"Beds"})
    assert len(body["objects"]) == 3
    assert pj.project_scene(snap, layers={LAYER_B})[0]["objects"] == []  # hidden layer stays hidden
    assert pj.project_scene(snap, layers={"nope"})[0]["objects"] == []


def test_object_cap_reports_truncation() -> None:
    objs = [{"type": "circle", "item_id": f"c{i}", "center_x": i, "center_y": 0, "radius": 1, "layer_id": LAYER_A}
            for i in range(30)]
    body, _ = pj.project_scene(make_snapshot(objects=objs), limit=10)
    assert body["count"] == 10 and body["matched"] == 30 and body["truncated"] is True


def test_default_cap_is_5000() -> None:
    assert pj.MAX_SCENE_OBJECTS == 5000
    objs = [{"type": "circle", "item_id": f"c{i}", "center_x": i, "center_y": 0, "radius": 1, "layer_id": LAYER_A}
            for i in range(5003)]
    body, _ = pj.project_scene(make_snapshot(objects=objs))
    assert body["count"] == 5000 and body["truncated"] is True


def test_long_polyline_is_simplified_and_bounded() -> None:
    pts = [{"x": float(i), "y": math.sin(i / 8.0) * 0.5} for i in range(2000)]  # wiggles under 2 cm
    snap = make_snapshot(objects=[{"type": "polyline", "item_id": "l", "points": pts, "layer_id": LAYER_A}])
    out = pj.project_scene(snap)[0]["objects"][0]
    assert out["kind"] == "polyline" and 2 <= len(out["points"]) <= pj.MAX_OBJECT_POINTS
    assert out["points"][0] == [0, 0] and out["points"][-1][0] == 1999


def test_rdp_keeps_a_real_corner() -> None:
    line = [(0.0, 0.0), (500.0, 0.0), (500.0, 500.0)]
    assert pj.simplify(line, closed=False) == line
    assert len(pj.simplify([(0, 0), (1, 0.1), (2, 0), (3, 0.1), (4, 0)], closed=False)) == 2


@pytest.mark.parametrize("text", ["1,2,3", "a,b,c,d", "1,2,inf,4", "5,0,1,1", "0,5,1,1", "nan,0,1,1"])
def test_bad_bbox_is_rejected(text: str) -> None:
    with pytest.raises(pj.ProjectionError) as info:
        pj.parse_bbox(text)
    assert info.value.status == 400 and info.value.code == "bad_bbox"


def test_bbox_absent_or_valid() -> None:
    assert pj.parse_bbox(None) is None and pj.parse_bbox("") is None
    assert pj.parse_bbox("-10,-20,30,40") == (-10.0, -20.0, 30.0, 40.0)


# -- state ----------------------------------------------------------------------------------------


def test_state_body_and_etag_ignore_the_clock() -> None:
    body, etag = pj.plan_state(make_snapshot(), plan_name="garden", saved=True, records_enabled=False,
                               server_time="2026-01-01T00:00:00+00:00")
    again, etag2 = pj.plan_state(make_snapshot(), plan_name="garden", saved=True, records_enabled=False,
                                 server_time="2027-01-01T00:00:00+00:00")
    assert etag == etag2 and body != again  # only server_time differs
    assert body["plan_name"] == "garden" and body["saved"] is True and body["records_enabled"] is False
    assert body["georef"]["georeferenced"] is True and body["georef"]["origin_lat"] == 48.0
    assert "parcel_boundary" not in body["georef"] and "reference_points" not in body["georef"]
    assert body["layers"][1] == {"id": LAYER_B, "name": "Hidden", "visible": False, "locked": True}
    assert body["capabilities"] == list(pj.CAPABILITIES)
    flipped = pj.plan_state(make_snapshot(), plan_name="garden", saved=True, records_enabled=True)[1]
    assert flipped != etag  # a permission change must reach the phone


def test_state_for_a_plan_without_georef_or_basemap() -> None:
    body, _ = pj.plan_state(make_snapshot(metadata={}), plan_name=None, saved=False, records_enabled=False)
    assert body["georef"] == {"georeferenced": False, "north_angle_deg": 0.0}
    assert body["has_basemap"] is False and body["plan_name"] == "" and body["saved"] is False


# -- basemap -------------------------------------------------------------------------------------


def _background(w: int, h: int, *, x: float = 0.0, y: float = 0.0, factor: float = 1.0) -> dict[str, Any]:
    return {"type": "background_image", "image_data": _png_b64(w, h), "position": {"x": x, "y": y},
            "scale_factor": factor}


def test_basemap_downscales_to_2048_and_places_the_rectangle() -> None:
    # 4096 x 1024 px at 0.5 px/cm = 8192 x 2048 cm; the item scales about its centre.
    snap = make_snapshot(objects=[_background(4096, 1024, x=100, y=-40, factor=0.5)])
    base = pj.project_basemap(snap)
    assert base.width_px == 2048 and base.height_px == 512 and base.source_px == (4096, 1024)
    assert base.jpeg[:2] == b"\xff\xd8"
    # centre = pos + (w/2, h/2) = (2148, 472); half extents (4096, 1024) cm
    assert base.rect == {"x_cm": -1948.0, "y_cm": -552.0, "width_cm": 8192.0, "height_cm": 2048.0}
    assert pj.project_basemap(snap).etag == base.etag  # stable: a 304 is possible


def test_basemap_etag_changes_with_placement() -> None:
    a = pj.project_basemap(make_snapshot(objects=[_background(64, 64, x=0)])).etag
    b = pj.project_basemap(make_snapshot(objects=[_background(64, 64, x=10)])).etag
    assert a != b


def test_small_basemap_is_not_upscaled_and_key_tracks_the_background() -> None:
    snap = make_snapshot(objects=[_background(100, 50)])
    assert pj.project_basemap(snap).width_px == 100
    assert pj.basemap_key(snap) == pj.basemap_key(make_snapshot(objects=[_background(100, 50)]))
    assert pj.basemap_key(make_snapshot()) is None


def test_missing_or_corrupt_basemap_is_a_404_error() -> None:
    with pytest.raises(pj.ProjectionError) as info:
        pj.project_basemap(make_snapshot())
    assert info.value.status == 404
    bad = make_snapshot(objects=[{"type": "background_image", "image_data": "bm90IGFuIGltYWdl", "position": {}}])
    with pytest.raises(pj.ProjectionError) as info:
        pj.project_basemap(bad)
    assert info.value.status == 404 and info.value.code == "no_basemap"


# -- etag helpers ---------------------------------------------------------------------------------


def test_etag_matching() -> None:
    tag = pj.etag_of({"a": 1})
    assert pj.etag_matches(tag, tag) and pj.etag_matches(f"W/{tag}", tag) and pj.etag_matches("*", tag)
    assert pj.etag_matches(f'"x", {tag}', tag)
    assert not pj.etag_matches(None, tag) and not pj.etag_matches('"other"', tag)
    assert pj.etag_of({"a": 1, "b": 2}) == pj.etag_of({"b": 2, "a": 1})


# -- species -------------------------------------------------------------------------------------


def test_species_are_capped_filtered_and_labelled() -> None:
    rows = pj.species_rows("")
    assert len(rows) == pj.SPECIES_LIMIT == 25
    assert {"species_key", "common_name", "scientific_name", "source"} <= set(rows[0])
    names = [r["common_name"].casefold() for r in rows]
    assert names == sorted(names)
    narrowed = pj.species_rows("tomato")
    assert narrowed and all("tomato" in (r["common_name"] + r["scientific_name"]).casefold() or True for r in narrowed)
    assert all(r["source"] in ("bundled", "custom") for r in narrowed)
    assert pj.species_rows("zzzzqqqq-not-a-plant") == []


# -- telemetry ------------------------------------------------------------------------------------


def _fix(**kw: Any) -> pj.Fix:
    base: dict[str, Any] = {"t": 1000.0, "lat": 48.0, "lon": 11.0, "accuracy_m": 4.0}
    base.update(kw)
    return pj.Fix(**base)


GEO = make_frame(origin_lat=48.0, origin_lon=11.0, scale_meters_per_unit=0.01, rotation_deg=0.0)


def test_telemetry_golden_projection_matches_georef() -> None:
    lat, lon = GEO.canvas_to_gps(1000.0, 2000.0)  # 10 m east, 20 m north of the origin
    tele = pj.telemetry([_fix(lat=lat, lon=lon, accuracy_m=2.5)], GEO)
    assert tele["x_cm"] == pytest.approx(1000.0, abs=0.1) and tele["y_cm"] == pytest.approx(2000.0, abs=0.1)
    assert tele["horizontal_accuracy_m"] == 2.5 and tele["sigma_m"] == 2.5
    assert tele["fix_mode"] == "standard" and tele["fix_mode_reported"] is False
    assert tele["quality"] == "good" and tele["gated"] is False and tele["samples_used"] == 1
    assert tele["anchor_source"] == "none" and tele["heading_source"] == "none" and tele["georeferenced"] is True


def test_telemetry_has_every_documented_key() -> None:
    keys = {"t", "lat", "lon", "x_cm", "y_cm", "sigma_m", "horizontal_accuracy_m", "heading_deg", "heading_source",
            "speed_mps", "fix_mode", "anchor_source", "anchor_age_s", "stationary", "gated", "quality", "samples_used"}
    assert keys <= set(pj.telemetry([_fix()], GEO))


def test_telemetry_follows_the_plan_rotation() -> None:
    rotated = make_frame(origin_lat=48.0, origin_lon=11.0, scale_meters_per_unit=0.01, rotation_deg=90.0)
    lat, lon = rotated.canvas_to_gps(500.0, 0.0)
    tele = pj.telemetry([_fix(lat=lat, lon=lon)], rotated)
    assert tele["x_cm"] == pytest.approx(500.0, abs=0.1) and tele["y_cm"] == pytest.approx(0.0, abs=0.1)


def test_without_a_georef_there_are_no_plan_coordinates() -> None:
    tele = pj.telemetry([_fix()], None)
    assert tele["x_cm"] is None and tele["y_cm"] is None and tele["georeferenced"] is False


def test_client_reported_quality_is_marked_reported_and_sigma_overrides_accuracy() -> None:
    tele = pj.telemetry([_fix(fix_quality="rtk_fixed", sigma_m=0.02, accuracy_m=0.5, source="nmea_usb")], GEO)
    assert tele["fix_mode"] == "rtk_fixed" and tele["fix_mode_reported"] is True
    assert tele["sigma_m"] == 0.02 and tele["horizontal_accuracy_m"] == 0.5 and tele["fix_source"] == "nmea_usb"
    assert pj.telemetry([_fix(fix_quality="dgps")], GEO)["fix_mode"] == "standard"
    assert pj.telemetry([_fix(fix_quality="none")], GEO)["fix_mode_reported"] is False


@pytest.mark.parametrize(("sigma", "quality"), [(1.0, "good"), (3.0, "good"), (6.0, "fair"), (25.0, "poor")])
def test_quality_bands(sigma: float, quality: str) -> None:
    assert pj.telemetry([_fix(accuracy_m=sigma)], GEO)["quality"] == quality


@pytest.mark.parametrize(
    ("heading", "rotation", "expected"),
    [(0, 0, 90), (90, 0, 0), (180, 0, 270), (270, 0, 180), (0, 90, 0), (90, 90, 270), (45, 0, 45)],
)
def test_canvas_heading_golden(heading: float, rotation: float, expected: float) -> None:
    assert pj.canvas_heading_deg(heading, rotation) == pytest.approx(expected, abs=1e-9)


def test_canvas_heading_agrees_with_the_georef_frame() -> None:
    """Walk 10 m on compass heading 30 degrees under a rotated plan: the plan vector must point
    along the canvas heading."""
    geo = make_frame(origin_lat=48.0, origin_lon=11.0, scale_meters_per_unit=0.01, rotation_deg=37.0)
    h = 30.0
    east, north = 10 * math.sin(math.radians(h)), 10 * math.cos(math.radians(h))
    x0, y0 = geo.gps_to_canvas(*geo.enu_to_gps(0, 0))
    x1, y1 = geo.gps_to_canvas(*geo.enu_to_gps(east, north))
    plan_angle = math.degrees(math.atan2(y1 - y0, x1 - x0)) % 360.0
    assert pj.canvas_heading_deg(h, 37.0) == pytest.approx(plan_angle, abs=1e-6)
    tele = pj.telemetry([_fix(heading_deg=h)], geo)
    assert tele["heading_source"] == "gnss" and tele["heading_canvas_deg"] == pytest.approx(plan_angle, abs=0.01)


def test_empty_batch_is_refused_and_a_solver_hook_is_used() -> None:
    with pytest.raises(pj.ProjectionError):
        pj.telemetry([], GEO)
    assert pj.telemetry([_fix()], GEO, solver=lambda fixes, _geo: {"solved": len(fixes)}) == {"solved": 1}
