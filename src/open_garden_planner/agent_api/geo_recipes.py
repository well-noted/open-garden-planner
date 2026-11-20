"""Qt-free helpers behind the plant-recipe and georeference agent tools.

The app-side bodies (``app/agent_host``) run on the Qt main thread and apply ONE
undoable command; everything that can be decided without Qt - the recipe catalogue,
the georeference read view, building and calibrating a georeference from tool
arguments - lives here so it is unit-testable and shared.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from open_garden_planner.core.georeference import (
    ControlPoint,
    GeoreferenceError,
    PlanGeoreference,
    fit_affine_calibration,
    georeference_bounds,
)
from open_garden_planner.core.plant_art.recipes import ARCHETYPES, FLOWERS, FRUITS, PALETTES
from open_garden_planner.core.plant_art.traits3d import (
    BARK_COLORS,
    BRANCHING_BOUNDS,
    CROWN_BOUNDS,
    CROWN_SHAPES,
    LEAF_BOUNDS,
    LEAF_SHAPES,
    MAX_OVERRIDE_JSON_CHARS,
    BranchingTraits,
    CrownTraits,
    LeafTraits,
)

RECIPE_TARGETS = ("instance", "species", "library", "plan_species")


def _bounds_schema(
    bounds: Mapping[str, tuple[float, float]], defaults: object
) -> dict[str, dict[str, Any]]:
    return {
        key: {"min": lo, "max": hi, "default": getattr(defaults, key)}
        for key, (lo, hi) in bounds.items()
    }


def plant_archetype_catalogue() -> dict[str, Any]:
    """Everything an agent needs to write a valid ``set_plant_recipe`` payload."""
    return {
        "archetypes": sorted(ARCHETYPES),
        "palettes": sorted(PALETTES),
        "autumn_palettes": sorted(PALETTES),
        "blossom_specs": sorted(FLOWERS),
        # Fruit styles are part of the 2D recipe, not of the 3D override: listed for reference.
        "fruit_styles_reference_only": sorted(FRUITS),
        "crown_shapes": sorted(CROWN_SHAPES),
        "leaf_shapes": sorted(LEAF_SHAPES),
        "bark_colors": sorted(BARK_COLORS),
        "recipe_schema": {
            "archetype": "one of archetypes: the form builder (canopy = lollipop/standard "
            "tree, mound = bush/shrub, conifer, palm, hedge, climber, ...)",
            "palette": "one of palettes (leaf colour family)",
            "autumn_palette": "one of palettes (colour in autumn; deciduous only)",
            "deciduous": "bool: sheds its leaves in winter",
            "blossom_spec": "one of blossom_specs, or null for none",
            "bloom_months": "list of months 1..12 (Northern-Hemisphere calendar)",
            "fruit_months": "list of months 1..12",
            "crown": {
                "shape": "one of crown_shapes",
                **_bounds_schema(CROWN_BOUNDS, CrownTraits()),
                "_notes": "trunk_share = clear-trunk fraction of the height; "
                "lobe_bias_az_deg = scene azimuth (0 = plan-up, clockwise) the heavier "
                "side of the canopy faces, lobe_bias_strength how strongly (0 = symmetric)",
            },
            "branching": {
                "bark_color": "one of bark_colors or a #rrggbb hex",
                **_bounds_schema(BRANCHING_BOUNDS, BranchingTraits()),
                "_notes": "basal_stems 1 = single trunk, 2 = V-fork, 3..5 = multi-stem "
                "bush; fork_height_share = where the split starts as a fraction of the "
                "height (0 = at the soil); fork_spread_deg = each stem's outward lean",
            },
            "leaf": {
                "shape": "one of leaf_shapes",
                **_bounds_schema(LEAF_BOUNDS, LeafTraits()),
                "frilly": "bool",
                "narrow": "bool",
            },
        },
        "max_recipe_json_chars": MAX_OVERRIDE_JSON_CHARS,
        "precedence": "specimen recipe (target 'instance') over species recipe (target "
        "'species' / 'library') over the bundled species match over the bundled category "
        "fallback; crown/branching/leaf merge field by field, so send only what differs. "
        "Send recipe=null to clear.",
        "targets": {
            "instance": "this plant only (plant_instance metadata)",
            "species": "THIS plant's own species snapshot only; other plants of the same "
            "species on the plan keep theirs",
            "plan_species": "every plant of this plant's species on the plan (matched by "
            "scientific name, else common name), as one undo step",
            "library": "also the species record in the local custom plant library "
            "(future plants added from it keep the recipe)",
        },
        "examples": {
            "v_forked_leaning_tree": {
                "crown": {"shape": "vase", "trunk_share": 0.34,
                          "lobe_bias_az_deg": 225.0, "lobe_bias_strength": 0.25},
                "branching": {"basal_stems": 2, "fork_height_share": 0.18,
                              "fork_spread_deg": 34.0, "gnarl": 0.16},
            },
            "bush_form_instead_of_standard": {
                "archetype": "mound",
                "crown": {"shape": "mound", "trunk_share": 0.06, "droop": 0.22},
                "branching": {"basal_stems": 3, "fork_height_share": 0.0,
                              "fork_spread_deg": 38.0},
            },
        },
    }


def georeference_view(
    georef: PlanGeoreference | None,
    north_angle_deg: float,
    canvas_width_cm: float,
    canvas_height_cm: float,
) -> dict[str, Any]:
    """The read view: the anchor, the canvas's corners in (lat, lon), conversion status."""
    if georef is None:
        return {
            "configured": False,
            "georeference": None,
            "north_angle_deg": north_angle_deg,
            "bounds": None,
            "conversion": "unavailable: no georeference is set (set_georeference or "
            "import a satellite background)",
        }
    locked = georef.affine_matrix is not None and georef.fit_north_angle_deg is not None
    return {
        "configured": True,
        "georeference": georef.to_dict(),
        "north_angle_deg": north_angle_deg,
        "calibrated": georef.affine_matrix is not None,
        "fit_north_angle_deg": georef.fit_north_angle_deg,
        "north_locked": locked,
        "calibration_note": (
            f"calibrated at north {georef.fit_north_angle_deg}; the conversion stays locked "
            "to that angle, so changing the plan's north does not move it"
            if locked else None
        ),
        "control_point_count": len(georef.control_points),
        "bounds": georeference_bounds(georef, canvas_width_cm, canvas_height_cm, north_angle_deg),
        "conversion": "ok: scene (x_cm, y_cm) <-> (lat, lon), +x east / +y north on a "
        "north-up plan; the plan's north angle is applied automatically",
    }


def build_georeference(
    *,
    origin_lat: float,
    origin_lon: float,
    origin_x_cm: float = 0.0,
    origin_y_cm: float = 0.0,
    cm_per_meter_x: float = 100.0,
    cm_per_meter_y: float | None = None,
    rotation_deg: float = 0.0,
) -> PlanGeoreference:
    """A fresh (uncalibrated) georeference from tool arguments; ``GeoreferenceError`` if bad."""
    return PlanGeoreference(
        origin_lat=origin_lat,
        origin_lon=origin_lon,
        origin_x_cm=origin_x_cm,
        origin_y_cm=origin_y_cm,
        cm_per_meter_x=cm_per_meter_x,
        cm_per_meter_y=cm_per_meter_x if cm_per_meter_y is None else cm_per_meter_y,
        rotation_deg=rotation_deg,
    )


def calibrate(
    current: PlanGeoreference | None,
    new_points: Sequence[Mapping[str, Any]],
    north_angle_deg: float,
    *,
    append: bool,
) -> tuple[PlanGeoreference, dict[str, Any]]:
    """Fit control points and report per-point residuals (cm).

    ``current`` may be None: the first point then becomes the origin at true scale, so an
    agent can georeference a bare plan from nothing but ground-control pairs. A previous
    affine is replaced, never composed; ``append`` keeps the existing control points.
    """
    points = [ControlPoint.from_dict(p) for p in new_points]
    if not points:
        raise GeoreferenceError("at least one control point is required")
    if current is None:
        first = points[0]
        current = PlanGeoreference(
            origin_lat=first.lat,
            origin_lon=first.lon,
            origin_x_cm=first.x_cm,
            origin_y_cm=first.y_cm,
            # rotation_deg stays 0, exactly as set_georeference's default: the plan's
            # north applies on top (ADR-079).
        )
    all_points = [*current.control_points, *points] if append else points
    result = fit_affine_calibration(current, all_points, north_angle_deg)
    report = {
        "method": result.method,
        "point_count": len(all_points),
        "rms_error_cm": result.rms_error_cm,
        "max_error_cm": max(result.residuals_cm),
        "residuals_cm": [
            {"label": p.label, "lat": p.lat, "lon": p.lon, "error_cm": err}
            for p, err in zip(all_points, result.residuals_cm, strict=True)
        ],
    }
    return result.georeference, report


__all__ = [
    "RECIPE_TARGETS",
    "build_georeference",
    "calibrate",
    "georeference_view",
    "plant_archetype_catalogue",
]

