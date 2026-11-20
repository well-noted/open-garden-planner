"""3D morphological and phenological traits per plant recipe (Phase 17 L3.0, #387).

The 2D sprite recipes in ``recipes.py`` describe a plant from straight above: its
archetype, leaf palette, and feature accents. Building the same plant in 3D and
animating it across the year requires the vertical and seasonal traits a top-down
sprite never needed:

- **Crown shape** (``CrownTraits``): crown form name, clear-trunk fraction, vertical
  taper (pyramid/egg vs. vase), leaf/twig droop, and horizontal aspect.
- **Branching** (``BranchingTraits``): space-colonisation attractor count, pipe-model
  exponent, trunk base radius ratio, apical tropism, branch gnarl, and 2D-sourced
  bark colour + linear-light shade (``WHITE_BARK_SHADE = 0.7`` for birch so sun-lit
  white bark never clips).
- **Leaves** (``LeafTraits``): leaf shape kind, reference leaf length (cm),
  width-to-length ratio, recipe ``leaf_scale``, ``frilly`` and ``narrow`` flags.
- **Season & phenology** (on ``Traits3D``): ``deciduous`` flag, Northern-Hemisphere
  illustrative ``bloom_months`` (1..12) and ``blossom_spec``, ``harvest_weeks``
  (weeks relative to last spring frost from ``plant_species.json``), ``fruit_months``
  derived from the harvest window via ``fruit_months_from_harvest_window``, and
  ``autumn_palette`` (always a key of ``recipes.PALETTES``).

Standard library only (no Qt, no numpy — pinned by ``tests/unit/test_plant_art.py``).
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Final

from .recipes import ARCHETYPES, CATEGORIES, FLOWERS, PALETTES, SPECIES, Recipe, palette_key
from .resolve import GROUP_CATEGORIES, GROUP_SPECIES, SpriteRef, resolve_sprite

#: Linear-light multiplier for birch white bark (``FLOWERS["white"][0] == "#e8e4d8"``)
#: so a low winter sun (14° elevation, n·l ≈ 0.97) never clips trunk pixels to 255.
WHITE_BARK_SHADE: Final[float] = 0.7

#: Reference temperate Northern-Hemisphere last spring frost date (25 April) used when
#: converting ``(harvest_start, harvest_end)`` week offsets to default ``fruit_months``
#: without a plan-specific frost date.
REFERENCE_LAST_FROST: Final[datetime.date] = datetime.date(2026, 4, 25)

#: Allowed crown shape names.
CROWN_SHAPES: Final[frozenset[str]] = frozenset({
    "round",
    "oval",
    "pyramidal",
    "columnar",
    "vase",
    "spreading",
    "weeping",
    "conical",
    "tiered",
    "fan",
    "mound",
    "flat",
    "upright",
})

#: Allowed 3D leaf shape names.
LEAF_SHAPES: Final[frozenset[str]] = frozenset({
    "diamond",
    "ovate",
    "lobed",
    "needle",
    "strap",
    "frond",
    "pinnate",
    "cordate",
    "cupped",
})

#: Allowed 2D-sourced bark hex colours (from ``object_art.materials.MATERIALS`` and
#: ``plant_art.recipes``; drift-guarded in ``tests/unit/test_plant_art.py``).
BARK_WALNUT: Final[str] = "#66432c"       # MATERIALS["walnut"]["mid"]
BARK_WALNUT_DARK: Final[str] = "#3f2617"  # MATERIALS["walnut"]["dark"]
BARK_OAK: Final[str] = "#7d5029"          # MATERIALS["oak"]["dark"]
BARK_TEAK: Final[str] = "#69401c"         # MATERIALS["teak"]["dark"]
BARK_PINE: Final[str] = "#54401e"         # MATERIALS["pine"]["occ"]
BARK_SHINGLE: Final[str] = "#78695a"      # MATERIALS["shingle"]["mid"]
BARK_GREY: Final[str] = "#5f584b"         # MATERIALS["grey_wood"]["dark"]
BARK_BIRCH: Final[str] = FLOWERS["white"][0]  # "#e8e4d8", shaded by WHITE_BARK_SHADE
BARK_STEM: Final[str] = PALETTES["olive"]["b0"]  # "#46601f", herbaceous green stem

BARK_COLORS: Final[frozenset[str]] = frozenset({
    BARK_WALNUT,
    BARK_WALNUT_DARK,
    BARK_OAK,
    BARK_TEAK,
    BARK_PINE,
    BARK_SHINGLE,
    BARK_GREY,
    BARK_BIRCH,
    BARK_STEM,
})


@dataclass(frozen=True, slots=True)
class CrownTraits:
    """3D crown envelope parameters."""

    shape: str = "round"
    trunk_share: float = 0.30
    taper: float = 0.0
    droop: float = 0.15
    aspect_xy: float = 1.0
    #: Directional lean of the canopy (a tree growing away from a wall): the scene
    #: azimuth the heavier side faces (0 = +y/plan-up, clockwise, degrees) and how
    #: strongly (0 = symmetric, the bundled default).
    lobe_bias_az_deg: float = 0.0
    lobe_bias_strength: float = 0.0


@dataclass(frozen=True, slots=True)
class BranchingTraits:
    """Space-colonisation and pipe-model branching parameters."""

    attractors: int = 520
    pipe_exponent: float = 2.5
    trunk_base_ratio: float = 0.018
    apical_bias: float = 0.12
    gnarl: float = 0.08
    bark_color: str = BARK_WALNUT
    bark_shade: float = 1.0
    #: Multi-stem / forked form: 1 = a single trunk (the bundled default), 2 = a V-fork,
    #: 3..5 = a multi-stem bush. ``fork_height_share`` is where the split starts as a
    #: fraction of the plant's height (0 = at the soil line); ``fork_spread_deg`` is
    #: each stem's outward lean from vertical.
    basal_stems: int = 1
    fork_height_share: float = 0.0
    fork_spread_deg: float = 28.0


@dataclass(frozen=True, slots=True)
class LeafTraits:
    """3D micro-leaf / blade / frond geometry traits."""

    shape: str = "ovate"
    length_cm: float = 12.0
    width_ratio: float = 0.60
    leaf_scale: float = 1.0
    frilly: bool = False
    narrow: bool = False


@dataclass(frozen=True, slots=True)
class Traits3D:
    """Complete 3D morphological and phenological traits for one sprite recipe."""

    sprite: SpriteRef
    archetype: str
    palette: str
    crown: CrownTraits
    branching: BranchingTraits
    leaf: LeafTraits
    deciduous: bool
    bloom_months: tuple[int, ...]
    blossom_spec: str | None
    fruit_months: tuple[int, ...]
    harvest_weeks: tuple[int, int] | None
    autumn_palette: str

    @property
    def evergreen(self) -> bool:
        """True when the plant retains its foliage through winter."""
        return not self.deciduous


def fruit_months_from_harvest_window(
    harvest_start: int | float | None,
    harvest_end: int | float | None,
    last_frost: datetime.date = REFERENCE_LAST_FROST,
) -> tuple[int, ...]:
    """Derive sorted calendar months ``(1..12)`` from frost-relative harvest weeks.

    ``harvest_start`` and ``harvest_end`` are week offsets relative to ``last_frost``
    (matching ``plant_species.json`` and ``task_generator.calendar_windows``). Multi-year
    establishment offsets (``harvest_start >= 52``, e.g. asparagus or rhubarb) are folded
    into the annual cycle while preserving window duration.
    """
    if harvest_start is None or harvest_end is None:
        return ()
    start_w = float(harvest_start)
    end_w = float(harvest_end)
    if end_w < start_w:
        start_w, end_w = end_w, start_w
    duration_w = min(end_w - start_w, 52.0)
    if start_w >= 52.0:
        start_w = start_w % 52.0
        end_w = start_w + duration_w
    start_date = last_frost + datetime.timedelta(weeks=start_w)
    end_date = last_frost + datetime.timedelta(weeks=end_w)
    months: set[int] = {start_date.month, end_date.month}
    cursor = start_date
    while cursor <= end_date:
        months.add(cursor.month)
        cursor += datetime.timedelta(days=5)
    return tuple(sorted(months))


#: Harvest windows ``(harvest_start, harvest_end)`` in weeks relative to last spring frost
#: from ``resources/data/plant_species.json`` for species recipes with harvestable parts.
HARVEST_WINDOWS_WEEKS: Final[dict[str, tuple[int, int]]] = {
    # fruiting vegetables & cucurbits
    "tomato": (10, 20),
    "pepper": (12, 20),
    "eggplant": (14, 20),
    "zucchini": (6, 10),
    "cucumber": (8, 12),
    "pumpkin": (14, 20),
    "squash": (14, 20),
    "bean": (7, 16),
    "pea": (8, 14),
    "corn": (10, 14),
    "okra": (10, 16),
    # roots, tubers & alliums
    "carrot": (10, 16),
    "radish": (3, 5),
    "beet": (8, 12),
    "turnip": (6, 10),
    "potato": (10, 18),
    "onion": (18, 24),
    "garlic": (12, 16),
    "leek": (22, 36),
    # brassicas, heads & leafy greens
    "celery": (16, 22),
    "broccoli": (10, 16),
    "cauliflower": (12, 18),
    "cabbage": (12, 20),
    "kale": (8, 20),
    "spinach": (5, 8),
    "lettuce": (6, 10),
    "arugula": (4, 8),
    "chard": (8, 20),
    "artichoke": (18, 26),
    "asparagus": (2, 8),
    "rhubarb": (4, 12),
    "sunflower": (14, 18),
}

#: Curated Northern-Hemisphere fruiting months for perennial trees and shrubs that carry
#: ``fruit`` in their recipe (perennials in ``plant_species.json`` do not define frost-week
#: sowing/harvest offsets).
_PERENNIAL_FRUIT_MONTHS: Final[dict[str, tuple[int, ...]]] = {
    "apple_tree": (8, 9, 10),
    "pear_tree": (8, 9, 10),
    "plum_tree": (7, 8, 9),
    "peach_tree": (7, 8),
    "cherry_tree": (6, 7),
    "fig_tree": (8, 9, 10),
    "olive_tree": (9, 10, 11),
    "lemon_tree": (1, 2, 3, 10, 11, 12),
    "orange_tree": (1, 2, 11, 12),
    "walnut_tree": (9, 10),
    "holly": (1, 2, 10, 11, 12),
    "barberry": (9, 10, 11),
    "blueberry": (6, 7, 8),
    "raspberry": (6, 7, 8, 9),
    "blackberry": (7, 8, 9),
    "gooseberry": (6, 7, 8),
    "currant": (6, 7, 8),
    "fruit_tree": (8, 9, 10),
}

#: Curated Northern-Hemisphere illustrative bloom months ``(1..12)`` per recipe stem.
_BLOOM_MONTHS: Final[dict[str, tuple[int, ...]]] = {
    # spring-blossoming fruit & ornamental trees
    "apple_tree": (4, 5),
    "pear_tree": (4, 5),
    "plum_tree": (3, 4),
    "peach_tree": (3, 4),
    "cherry_tree": (4, 5),
    "olive_tree": (5, 6),
    "lemon_tree": (4, 5, 6),
    "orange_tree": (4, 5),
    "magnolia": (3, 4, 5),
    "fruit_tree": (4, 5),
    # flowering & berry shrubs
    "forsythia": (3, 4),
    "camellia": (2, 3, 4),
    "rhododendron": (4, 5, 6),
    "lilac": (5, 6),
    "viburnum": (5, 6),
    "spirea": (5, 6, 7),
    "elderberry": (5, 6),
    "privet": (6, 7),
    "barberry": (4, 5),
    "blueberry": (4, 5),
    "raspberry": (5, 6),
    "blackberry": (5, 6),
    "gooseberry": (4, 5),
    "currant": (4, 5),
    "rose": (5, 6, 7, 8, 9),
    "hydrangea": (6, 7, 8, 9),
    "hibiscus": (7, 8, 9),
    # climbers
    "wisteria": (4, 5, 6),
    "clematis": (5, 6, 7, 8),
    "jasmine": (6, 7, 8, 9),
    "climbing_plant": (5, 6, 7, 8),
    # vegetables with blossoms
    "tomato": (5, 6, 7),
    "pepper": (6, 7),
    "eggplant": (6, 7),
    "zucchini": (6, 7, 8),
    "cucumber": (6, 7, 8),
    "pumpkin": (6, 7),
    "squash": (6, 7),
    "bean": (6, 7),
    "pea": (5, 6),
    "potato": (6, 7),
    "okra": (7, 8),
    "corn": (7, 8),
    # herbs with flowers/umbels/clusters
    "rosemary": (4, 5, 6),
    "thyme": (5, 6, 7),
    "sage": (5, 6, 7),
    "cilantro": (6, 7),
    "dill": (6, 7, 8),
    "chives": (5, 6, 7),
    "oregano": (6, 7, 8),
    "chamomile": (5, 6, 7, 8),
    "fennel": (7, 8),
    "marjoram": (6, 7, 8),
    "borage": (5, 6, 7, 8),
    "herb": (5, 6, 7),
    # ornamentals & bulbs
    "crocus": (2, 3, 4),
    "daffodil": (3, 4),
    "tulip": (4, 5),
    "pansy": (3, 4, 5, 6, 9, 10),
    "peony": (5, 6),
    "iris": (5, 6),
    "lily": (6, 7, 8),
    "lavender": (6, 7, 8),
    "geranium": (5, 6, 7, 8, 9),
    "petunia": (5, 6, 7, 8, 9),
    "marigold": (6, 7, 8, 9, 10),
    "zinnia": (6, 7, 8, 9),
    "cosmos": (6, 7, 8, 9, 10),
    "sunflower": (7, 8, 9),
    "dahlia": (7, 8, 9, 10),
    "aster": (8, 9, 10),
    "chrysanthemum": (9, 10, 11),
    "flowering_perennial": (5, 6, 7, 8),
    "ground_cover": (4, 5, 6, 7),
}

#: Blossoming species whose recipe carries ``fruit`` rather than ``flowers`` still have a
#: spring blossom colour in ``recipes.FLOWERS``.
_FRUIT_BLOSSOM_SPEC: Final[dict[str, str]] = {
    "apple_tree": "pink",
    "pear_tree": "white",
    "plum_tree": "white",
    "peach_tree": "pink",
    "cherry_tree": "pink",
    "olive_tree": "cream",
    "lemon_tree": "white",
    "orange_tree": "white",
    "barberry": "yellow",
    "blueberry": "white",
    "raspberry": "white",
    "blackberry": "white",
    "gooseberry": "cream",
    "currant": "cream",
    "fruit_tree": "pink",
}

#: Evergreen recipes (never shed leaves in winter).
_EVERGREEN_STEMS: Final[frozenset[str]] = frozenset({
    # conifers & palms
    "pine",
    "spruce",
    "juniper",
    "conifer",
    "palm",
    # evergreen trees
    "olive_tree",
    "lemon_tree",
    "orange_tree",
    # evergreen shrubs, hedges & woody Mediterranean herbs
    "boxwood",
    "holly",
    "camellia",
    "rhododendron",
    "bay_laurel",
    "rosemary",
    "lavender",
    "thyme",
    "sage",
    "compact_shrub",
    "hedge_section",
    "ground_cover",
})

#: Woody deciduous stems (shed leaves in winter; turn autumn colours in fall).
_DECIDUOUS_WOODY_STEMS: Final[frozenset[str]] = frozenset({
    # deciduous trees
    "apple_tree",
    "pear_tree",
    "plum_tree",
    "peach_tree",
    "cherry_tree",
    "fig_tree",
    "walnut_tree",
    "oak",
    "maple",
    "birch",
    "willow",
    "magnolia",
    "round_deciduous",
    "columnar_tree",
    "weeping_tree",
    "fruit_tree",
    # deciduous shrubs & woody climbers
    "privet",
    "forsythia",
    "lilac",
    "viburnum",
    "barberry",
    "spirea",
    "elderberry",
    "blueberry",
    "raspberry",
    "blackberry",
    "gooseberry",
    "currant",
    "rose",
    "hydrangea",
    "hibiscus",
    "spreading_shrub",
    "jasmine",
    "clematis",
    "wisteria",
    "climbing_plant",
})

#: Per-species crown & branching overrides for canopy and conifer trees.
#: Preserves the dendrological habits measured in the L0 spike and L1.7.
_TREE_OVERRIDES: Final[dict[str, tuple[CrownTraits, BranchingTraits, str, str]]] = {
    # stem: (CrownTraits, BranchingTraits, leaf_shape, autumn_palette)
    "apple_tree": (
        CrownTraits("spreading", trunk_share=0.20, taper=0.15, droop=0.45, aspect_xy=1.05),
        BranchingTraits(520, 2.5, 0.020, 0.08, 0.12, BARK_WALNUT, 1.0),
        "ovate",
        "autumn",
    ),
    "pear_tree": (
        CrownTraits("pyramidal", trunk_share=0.24, taper=0.70, droop=0.10, aspect_xy=0.88),
        BranchingTraits(520, 2.5, 0.018, 0.24, 0.06, BARK_WALNUT_DARK, 1.0),
        "ovate",
        "redleaf",
    ),
    "plum_tree": (
        CrownTraits("round", trunk_share=0.28, taper=0.05, droop=0.25, aspect_xy=1.0),
        BranchingTraits(500, 2.5, 0.018, 0.12, 0.09, BARK_WALNUT_DARK, 1.0),
        "ovate",
        "yellow",
    ),
    "peach_tree": (
        CrownTraits("spreading", trunk_share=0.22, taper=-0.10, droop=0.30, aspect_xy=1.04),
        BranchingTraits(480, 2.5, 0.017, 0.10, 0.10, BARK_TEAK, 1.0),
        "diamond",
        "autumn",
    ),
    "cherry_tree": (
        CrownTraits("oval", trunk_share=0.30, taper=0.10, droop=0.15, aspect_xy=1.0),
        BranchingTraits(520, 2.5, 0.019, 0.14, 0.08, BARK_TEAK, 1.0),
        "ovate",
        "autumn",
    ),
    "fig_tree": (
        CrownTraits("spreading", trunk_share=0.18, taper=-0.20, droop=0.20, aspect_xy=1.08),
        BranchingTraits(460, 2.4, 0.022, 0.06, 0.13, BARK_GREY, 1.0),
        "lobed",
        "yellow",
    ),
    "olive_tree": (
        CrownTraits("spreading", trunk_share=0.26, taper=-0.12, droop=0.22, aspect_xy=1.05),
        BranchingTraits(500, 2.4, 0.024, 0.05, 0.15, BARK_SHINGLE, 1.0),
        "diamond",
        "silver",
    ),
    "lemon_tree": (
        CrownTraits("round", trunk_share=0.22, taper=0.05, droop=0.18, aspect_xy=1.0),
        BranchingTraits(480, 2.5, 0.017, 0.12, 0.08, BARK_GREY, 1.0),
        "ovate",
        "dark",
    ),
    "orange_tree": (
        CrownTraits("round", trunk_share=0.24, taper=0.0, droop=0.15, aspect_xy=1.0),
        BranchingTraits(500, 2.5, 0.018, 0.12, 0.07, BARK_GREY, 1.0),
        "ovate",
        "dark",
    ),
    "walnut_tree": (
        CrownTraits("spreading", trunk_share=0.40, taper=-0.20, droop=0.15, aspect_xy=1.06),
        BranchingTraits(540, 2.5, 0.022, 0.10, 0.10, BARK_SHINGLE, 1.0),
        "pinnate",
        "yellow",
    ),
    "oak": (
        CrownTraits("spreading", trunk_share=0.32, taper=-0.10, droop=0.12, aspect_xy=1.08),
        BranchingTraits(560, 2.5, 0.024, 0.08, 0.14, BARK_WALNUT_DARK, 1.0),
        "lobed",
        "autumn",
    ),
    "maple": (
        CrownTraits("oval", trunk_share=0.30, taper=-0.15, droop=0.15, aspect_xy=1.02),
        BranchingTraits(540, 2.5, 0.020, 0.14, 0.08, BARK_GREY, 1.0),
        "lobed",
        "redleaf",
    ),
    "birch": (
        CrownTraits("oval", trunk_share=0.20, taper=0.25, droop=0.65, aspect_xy=0.88),
        BranchingTraits(500, 2.5, 0.014, 0.20, 0.06, BARK_BIRCH, WHITE_BARK_SHADE),
        "diamond",
        "yellow",
    ),
    "willow": (
        CrownTraits("weeping", trunk_share=0.22, taper=0.18, droop=0.82, aspect_xy=1.06),
        BranchingTraits(540, 2.5, 0.021, -0.08, 0.11, BARK_OAK, 1.0),
        "strap",
        "yellow",
    ),
    "magnolia": (
        CrownTraits("vase", trunk_share=0.12, taper=-0.45, droop=0.05, aspect_xy=1.04),
        BranchingTraits(480, 2.5, 0.018, 0.16, 0.09, BARK_SHINGLE, 1.0),
        "ovate",
        "autumn",
    ),
    "round_deciduous": (
        CrownTraits("round", trunk_share=0.30, taper=0.0, droop=0.15, aspect_xy=1.0),
        BranchingTraits(520, 2.5, 0.018, 0.12, 0.08, BARK_WALNUT, 1.0),
        "ovate",
        "autumn",
    ),
    "columnar_tree": (
        CrownTraits("columnar", trunk_share=0.16, taper=0.35, droop=0.08, aspect_xy=0.72),
        BranchingTraits(500, 2.5, 0.016, 0.32, 0.05, BARK_GREY, 1.0),
        "ovate",
        "yellow",
    ),
    "weeping_tree": (
        CrownTraits("weeping", trunk_share=0.22, taper=0.18, droop=0.80, aspect_xy=1.05),
        BranchingTraits(520, 2.5, 0.020, -0.06, 0.10, BARK_OAK, 1.0),
        "strap",
        "yellow",
    ),
    "fruit_tree": (
        CrownTraits("spreading", trunk_share=0.22, taper=0.12, droop=0.38, aspect_xy=1.04),
        BranchingTraits(520, 2.5, 0.019, 0.10, 0.10, BARK_WALNUT, 1.0),
        "ovate",
        "autumn",
    ),
}


def _blossom_spec_for(stem: str, recipe: Recipe) -> str | None:
    flowers = recipe.get("flowers")
    if isinstance(flowers, dict) and isinstance(flowers.get("spec"), str):
        return str(flowers["spec"])
    if "clusters" in recipe or "puffs" in recipe:
        if stem in ("lilac", "wisteria", "lavender", "chives"):
            return "lilac"
        if stem == "hydrangea":
            return "blue"
        if stem == "rhododendron":
            return "pink"
        if stem == "geranium":
            return "red"
        return "white"
    if "umbels" in recipe:
        return "yellow" if stem in ("dill", "fennel", "corn") else "cream"
    return _FRUIT_BLOSSOM_SPEC.get(stem)


def _build_traits(group: str, stem: str, recipe: Recipe) -> Traits3D:
    ref = SpriteRef(group, stem)
    archetype = str(recipe["a"])
    pal = palette_key(recipe)
    leaf_scale = float(recipe.get("leaf_scale", 1.0))
    frilly = bool(recipe.get("frilly", False))
    narrow = bool(recipe.get("narrow", False))
    deciduous = stem in _DECIDUOUS_WOODY_STEMS and stem not in _EVERGREEN_STEMS

    harvest_weeks = HARVEST_WINDOWS_WEEKS.get(stem)
    if harvest_weeks is not None and any(k in recipe for k in ("fruit", "fruit2", "pods", "bulb", "head")):
        fruit_months = fruit_months_from_harvest_window(harvest_weeks[0], harvest_weeks[1])
    elif stem in _PERENNIAL_FRUIT_MONTHS:
        fruit_months = _PERENNIAL_FRUIT_MONTHS[stem]
    else:
        fruit_months = ()

    bloom_months = _BLOOM_MONTHS.get(stem, ())
    blossom_spec = _blossom_spec_for(stem, recipe)

    if stem in _TREE_OVERRIDES:
        crown, branching, leaf_shape, autumn_pal = _TREE_OVERRIDES[stem]
        width_ratio = 0.28 if leaf_shape == "strap" else (0.72 if leaf_shape == "lobed" else 0.60)
        leaf = LeafTraits(
            shape=leaf_shape,
            length_cm=round(13.0 * leaf_scale, 2),
            width_ratio=width_ratio,
            leaf_scale=leaf_scale,
            frilly=frilly,
            narrow=narrow,
        )
        return Traits3D(
            sprite=ref,
            archetype=archetype,
            palette=pal,
            crown=crown,
            branching=branching,
            leaf=leaf,
            deciduous=deciduous,
            bloom_months=bloom_months,
            blossom_spec=blossom_spec,
            fruit_months=fruit_months,
            harvest_weeks=harvest_weeks,
            autumn_palette=autumn_pal if deciduous else pal,
        )

    if archetype == "conifer":
        is_pine = stem == "pine"
        is_juniper = stem == "juniper"
        crown = CrownTraits(
            shape="tiered" if not is_juniper else "conical",
            trunk_share=0.22 if is_pine else (0.08 if is_juniper else 0.12),
            taper=0.65 if is_pine else 0.82,
            droop=0.28 if is_pine else 0.48,
            aspect_xy=1.0,
        )
        branching = BranchingTraits(220, 2.4, 0.020, 0.25, 0.05, BARK_PINE, 1.0)
        leaf = LeafTraits("needle", round(14.0 * leaf_scale, 2), 0.35, leaf_scale, frilly, narrow)
        return Traits3D(
            ref, archetype, pal, crown, branching, leaf, False,
            bloom_months, blossom_spec, fruit_months, harvest_weeks, pal,
        )

    if archetype == "palm":
        crown = CrownTraits("fan", trunk_share=0.62, taper=-0.35, droop=0.55, aspect_xy=1.0)
        branching = BranchingTraits(32, 2.2, 0.028, 0.35, 0.04, BARK_TEAK, 1.0)
        leaf = LeafTraits("frond", round(42.0 * leaf_scale, 2), 0.26, leaf_scale, frilly, narrow)
        return Traits3D(
            ref, archetype, pal, crown, branching, leaf, False,
            bloom_months, blossom_spec, fruit_months, harvest_weeks, pal,
        )

    if archetype == "hedge":
        crown = CrownTraits("flat", trunk_share=0.0, taper=0.04, droop=0.10, aspect_xy=1.0)
        branching = BranchingTraits(140, 2.4, 0.012, 0.15, 0.06, BARK_WALNUT_DARK, 1.0)
        leaf = LeafTraits("ovate", 6.5, 0.62, leaf_scale, frilly, narrow)
        return Traits3D(
            ref, archetype, pal, crown, branching, leaf, False,
            bloom_months, blossom_spec, fruit_months, harvest_weeks, pal,
        )

    if archetype == "climber":
        crown = CrownTraits("upright", trunk_share=0.04, taper=0.0, droop=0.25, aspect_xy=0.75)
        branching = BranchingTraits(180, 2.3, 0.010, 0.28, 0.12, BARK_OAK if deciduous else BARK_STEM, 1.0)
        leaf = LeafTraits(
            "cordate" if frilly else "ovate",
            round(8.5 * leaf_scale, 2),
            0.65,
            leaf_scale,
            frilly,
            narrow,
        )
        autumn_pal = "autumn" if deciduous else pal
        return Traits3D(
            ref, archetype, pal, crown, branching, leaf, deciduous,
            bloom_months, blossom_spec, fruit_months, harvest_weeks, autumn_pal,
        )

    if archetype in ("rosette", "head"):
        crown = CrownTraits("mound", trunk_share=0.0, taper=0.10, droop=-0.10, aspect_xy=1.0)
        branching = BranchingTraits(0, 2.2, 0.010, 0.20, 0.03, BARK_STEM, 1.0)
        leaf = LeafTraits(
            "cupped" if recipe.get("heart", True) else "lobed" if frilly else "ovate",
            round(10.0 * leaf_scale, 2),
            0.74,
            leaf_scale,
            frilly,
            narrow,
        )
        return Traits3D(
            ref, archetype, pal, crown, branching, leaf, False,
            bloom_months, blossom_spec, fruit_months, harvest_weeks, pal,
        )

    if archetype in ("grass", "allium"):
        crown = CrownTraits("upright", trunk_share=0.0, taper=-0.20, droop=0.35, aspect_xy=1.0)
        branching = BranchingTraits(0, 2.2, 0.010, 0.35, 0.03, BARK_STEM, 1.0)
        leaf = LeafTraits("strap", round(18.0 * leaf_scale, 2), 0.18, leaf_scale, frilly, narrow)
        return Traits3D(
            ref, archetype, pal, crown, branching, leaf, False,
            bloom_months, blossom_spec, fruit_months, harvest_weeks, pal,
        )

    if archetype == "feathery":
        crown = CrownTraits("mound", trunk_share=0.04, taper=-0.10, droop=0.28, aspect_xy=1.0)
        branching = BranchingTraits(64, 2.2, 0.010, 0.22, 0.06, BARK_STEM, 1.0)
        leaf = LeafTraits("pinnate", round(11.0 * leaf_scale, 2), 0.38, leaf_scale, frilly, narrow)
        return Traits3D(
            ref, archetype, pal, crown, branching, leaf, False,
            bloom_months, blossom_spec, fruit_months, harvest_weeks, pal,
        )

    if archetype == "flower":
        crown = CrownTraits("upright", trunk_share=0.12, taper=0.15, droop=0.18, aspect_xy=1.0)
        branching = BranchingTraits(24, 2.3, 0.016, 0.38, 0.04, BARK_STEM, 1.0)
        leaf = LeafTraits("cordate", round(14.0 * leaf_scale, 2), 0.72, leaf_scale, frilly, narrow)
        return Traits3D(
            ref, archetype, pal, crown, branching, leaf, False,
            bloom_months, blossom_spec, fruit_months, harvest_weeks, pal,
        )

    if archetype == "ground":
        crown = CrownTraits("flat", trunk_share=0.0, taper=0.25, droop=0.08, aspect_xy=1.08)
        branching = BranchingTraits(48, 2.2, 0.008, 0.04, 0.10, BARK_STEM, 1.0)
        leaf = LeafTraits("ovate", round(4.5 * leaf_scale, 2), 0.68, leaf_scale, frilly, narrow)
        return Traits3D(
            ref, archetype, pal, crown, branching, leaf, False,
            bloom_months, blossom_spec, fruit_months, harvest_weeks, pal,
        )

    # archetype == "mound" (shrubs, herbs, bushy vegetables & perennials)
    is_woody_shrub = deciduous or stem in ("boxwood", "holly", "camellia", "rhododendron", "bay_laurel", "compact_shrub")
    crown = CrownTraits(
        shape="mound" if not narrow else "upright",
        trunk_share=0.08 if is_woody_shrub else 0.04,
        taper=-0.08 if is_woody_shrub else 0.05,
        droop=0.18 if not narrow else 0.08,
        aspect_xy=1.0,
    )
    branching = BranchingTraits(
        attractors=180 if is_woody_shrub else 90,
        pipe_exponent=2.4,
        trunk_base_ratio=0.014 if is_woody_shrub else 0.010,
        apical_bias=0.14,
        gnarl=0.10,
        bark_color=BARK_WALNUT if is_woody_shrub else BARK_STEM,
        bark_shade=1.0,
    )
    leaf = LeafTraits(
        shape="strap" if narrow else ("lobed" if frilly else "ovate"),
        length_cm=round(7.5 * leaf_scale, 2),
        width_ratio=0.30 if narrow else (0.72 if frilly else 0.60),
        leaf_scale=leaf_scale,
        frilly=frilly,
        narrow=narrow,
    )
    autumn_pal = (
        "redleaf"
        if stem in ("blueberry", "viburnum", "barberry", "currant")
        else ("autumn" if deciduous else pal)
    )
    return Traits3D(
        ref, archetype, pal, crown, branching, leaf, deciduous,
        bloom_months, blossom_spec, fruit_months, harvest_weeks, autumn_pal,
    )


#: 3D traits for every species recipe (108).
SPECIES_TRAITS: Final[dict[str, Traits3D]] = {
    stem: _build_traits(GROUP_SPECIES, stem, recipe) for stem, recipe in SPECIES.items()
}

#: 3D traits for every category recipe (15).
CATEGORY_TRAITS: Final[dict[str, Traits3D]] = {
    stem: _build_traits(GROUP_CATEGORIES, stem, recipe) for stem, recipe in CATEGORIES.items()
}


def traits_for(sprite: SpriteRef) -> Traits3D:
    """Return the ``Traits3D`` for ``sprite``, or raise ``KeyError``."""
    if sprite.group == GROUP_SPECIES:
        return SPECIES_TRAITS[sprite.name]
    if sprite.group == GROUP_CATEGORIES:
        return CATEGORY_TRAITS[sprite.name]
    raise KeyError(sprite)


# --- Custom recipes (per species / per specimen) --------------------------------------

_LOG = logging.getLogger(__name__)

_HEX_COLOR_RE: Final[re.Pattern[str]] = re.compile(r"^#[0-9a-fA-F]{6}$")

#: Longest override JSON accepted (a hostile file must not park megabytes in a record).
MAX_OVERRIDE_JSON_CHARS: Final[int] = 4096

#: Top-level keys of a ``traits3d_override``.
OVERRIDE_TOP_KEYS: Final[frozenset[str]] = frozenset({
    "archetype", "palette", "autumn_palette", "deciduous", "blossom_spec",
    "bloom_months", "fruit_months", "crown", "branching", "leaf",
})
#: ``(lo, hi)`` per numeric field of each sub-recipe: THE bounds, also served to agents.
CROWN_BOUNDS: Final[dict[str, tuple[float, float]]] = {
    "trunk_share": (0.0, 0.85),
    "taper": (-1.0, 1.0),
    "droop": (-0.5, 1.0),
    "aspect_xy": (0.4, 2.5),
    "lobe_bias_az_deg": (0.0, 360.0),
    "lobe_bias_strength": (0.0, 0.55),
}
BRANCHING_BOUNDS: Final[dict[str, tuple[float, float]]] = {
    "attractors": (0, 1200),
    "pipe_exponent": (1.5, 3.5),
    "trunk_base_ratio": (0.005, 0.08),
    "apical_bias": (-0.5, 0.8),
    "gnarl": (0.0, 0.5),
    "bark_shade": (0.2, 1.0),
    "basal_stems": (1, 5),
    "fork_height_share": (0.0, 0.65),
    "fork_spread_deg": (5.0, 55.0),
}
LEAF_BOUNDS: Final[dict[str, tuple[float, float]]] = {
    "length_cm": (1.0, 120.0),
    "width_ratio": (0.05, 1.5),
    "leaf_scale": (0.2, 3.0),
}
_INT_FIELDS: Final[frozenset[str]] = frozenset({"attractors", "basal_stems"})
_BOOL_LEAF_FIELDS: Final[frozenset[str]] = frozenset({"frilly", "narrow"})


def _num(value: Any, name: str, lo: float, hi: float, *, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if integer and not isinstance(value, int) and not float(value).is_integer():
        raise ValueError(f"{name} must be a whole number")
    try:
        number = float(value)
    except OverflowError as error:
        raise ValueError(f"{name} must be finite") from error
    if not math.isfinite(number) or not lo <= number <= hi:
        raise ValueError(f"{name} must be within [{lo}, {hi}], got {value!r}")
    return int(number) if integer else number


def _months(value: Any, name: str) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a list of months 1..12")
    return tuple(sorted({int(_num(m, f"{name}[]", 1, 12, integer=True)) for m in value}))


def _check_section(
    raw: Any,
    name: str,
    bounds: Mapping[str, tuple[float, float]],
    extra: frozenset[str],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{name} must be an object")
    allowed = set(bounds) | set(extra)
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in allowed:
            raise ValueError(f"unknown {name} field {key!r}")
        if value is None:
            continue  # null = "keep the base value"
        if key in bounds:
            lo, hi = bounds[key]
            clean[key] = _num(value, f"{name}.{key}", lo, hi, integer=key in _INT_FIELDS)
        elif key in _BOOL_LEAF_FIELDS:
            if not isinstance(value, bool):
                raise ValueError(f"{name}.{key} must be a bool")
            clean[key] = value
        elif key == "shape":
            shapes = CROWN_SHAPES if name == "crown" else LEAF_SHAPES
            if not isinstance(value, str) or value not in shapes:
                raise ValueError(f"{name}.shape must be one of {sorted(shapes)}")
            clean[key] = value
        elif key == "bark_color":
            if not isinstance(value, str) or not (
                value in BARK_COLORS or _HEX_COLOR_RE.fullmatch(value)
            ):
                raise ValueError("branching.bark_color must be a known bark colour or #rrggbb")
            clean[key] = value.lower()
    return clean


def validate_traits3d_override(raw: Any) -> dict[str, Any] | None:
    """Validate a per-species / per-specimen recipe override and return a clean copy.

    ``None`` (or ``{}``) means "no override". Anything else must be an object whose
    every field is known and in bounds: ``archetype`` one of ``ARCHETYPES``, palettes
    keys of ``PALETTES``, ``blossom_spec`` a key of ``FLOWERS``, months 1..12, every
    number finite (no booleans) and within the bounds above. Raises ``ValueError``
    otherwise: the write path (agent tool, command) fails closed; the reader
    (:func:`traits_with_override_json`) logs and ignores a bad stored override.
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("traits3d_override must be an object or null")
    unknown = set(raw) - OVERRIDE_TOP_KEYS
    if unknown:
        raise ValueError(f"unknown traits3d_override field(s): {sorted(unknown)}")
    clean: dict[str, Any] = {}
    for key in ("archetype", "palette", "autumn_palette", "blossom_spec"):
        if raw.get(key) is not None and not isinstance(raw[key], str):
            raise ValueError(f"{key} must be a string")  # never an unhashable TypeError
    if raw.get("archetype") is not None:
        if raw["archetype"] not in ARCHETYPES:
            raise ValueError(f"archetype must be one of {sorted(ARCHETYPES)}")
        clean["archetype"] = raw["archetype"]
    for key in ("palette", "autumn_palette"):
        if raw.get(key) is not None:
            if raw[key] not in PALETTES:
                raise ValueError(f"{key} must be one of {sorted(PALETTES)}")
            clean[key] = raw[key]
    if raw.get("deciduous") is not None:
        if not isinstance(raw["deciduous"], bool):
            raise ValueError("deciduous must be a bool")
        clean["deciduous"] = raw["deciduous"]
    if raw.get("blossom_spec") is not None:
        if raw["blossom_spec"] not in FLOWERS:
            raise ValueError(f"blossom_spec must be one of {sorted(FLOWERS)}")
        clean["blossom_spec"] = raw["blossom_spec"]
    for key in ("bloom_months", "fruit_months"):
        if raw.get(key) is not None:
            clean[key] = list(_months(raw[key], key))
    for key, bounds, extra in (
        ("crown", CROWN_BOUNDS, frozenset({"shape"})),
        ("branching", BRANCHING_BOUNDS, frozenset({"bark_color"})),
        ("leaf", LEAF_BOUNDS, frozenset({"shape", "frilly", "narrow"})),
    ):
        if raw.get(key) is not None:
            section = _check_section(raw[key], key, bounds, extra)
            if section:
                clean[key] = section
    if len(override_signature(clean)) > MAX_OVERRIDE_JSON_CHARS:
        raise ValueError("traits3d_override is too large")
    return clean or None


def merge_overrides(
    species: Mapping[str, Any] | None,
    instance: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Layer a specimen override over a species override (instance wins, key by key).

    The whole recipe resolves as: instance override -> species override -> bundled
    ``SPECIES`` match -> ``CATEGORIES`` fallback. Sub-recipes (``crown``,
    ``branching``, ``leaf``) merge field by field, so a vision model describing one
    tree need only state what differs from its species.
    """
    merged: dict[str, Any] = {}
    for layer in (species, instance):
        if not isinstance(layer, Mapping):
            continue
        for key, value in layer.items():
            if isinstance(value, Mapping):
                previous = merged.get(key)
                merged[key] = {**(previous if isinstance(previous, dict) else {}), **value}
            else:
                merged[key] = value
    return merged or None


def override_signature(override: Mapping[str, Any] | None) -> str:
    """Canonical JSON of an override: the cache / record-signature string ("" = none)."""
    if not override:
        return ""
    return json.dumps(override, sort_keys=True, separators=(",", ":"))


def layered_recipe_json(species_raw: object, instance_raw: object) -> str:
    """The canonical JSON of the recipe a plant item carries ("" = none): THE one reader.

    ``species_raw`` / ``instance_raw`` are the stored ``traits3d_override`` values of the
    item's ``plant_species`` and ``plant_instance`` metadata. Each layer is validated on
    its own - a bad one is logged and dropped, never the other - then the specimen is
    layered over the species. Used by the 3D snapshot so ``Record.params`` (and with it
    ``geometry_sig`` and the Plant Forge cache key) change exactly when a recipe does.
    """
    layers: list[Mapping[str, Any] | None] = []
    for raw in (species_raw, instance_raw):
        try:
            layers.append(validate_traits3d_override(raw))
        except (ValueError, TypeError):
            _LOG.warning("plant recipe: ignoring an invalid stored traits3d_override")
            layers.append(None)
    return override_signature(merge_overrides(layers[0], layers[1]))


def apply_traits_override(base: Traits3D, override: Mapping[str, Any] | None) -> Traits3D:
    """Return ``base`` with a validated ``override`` applied (``ValueError`` if invalid)."""
    clean = validate_traits3d_override(override)
    if clean is None:
        return base
    fields: dict[str, Any] = {
        key: clean[key]
        for key in ("archetype", "palette", "autumn_palette", "deciduous", "blossom_spec")
        if key in clean
    }
    for key in ("bloom_months", "fruit_months"):
        if key in clean:
            fields[key] = tuple(clean[key])
    deciduous = bool(clean.get("deciduous", base.deciduous))
    if "palette" in clean and "autumn_palette" not in clean and not deciduous:
        fields["autumn_palette"] = clean["palette"]  # an evergreen keeps its one colour
    return replace(
        base,
        crown=replace(base.crown, **clean.get("crown", {})),
        branching=replace(base.branching, **clean.get("branching", {})),
        leaf=replace(base.leaf, **clean.get("leaf", {})),
        **fields,
    )


def traits_with_override_json(base: Traits3D, raw: object) -> Traits3D:
    """``base`` with the override carried as JSON text (a record param), tolerantly.

    The 3D view must never crash on a hand-edited file: a missing, malformed or
    out-of-bounds override is logged and ignored, leaving the bundled recipe.
    """
    if not isinstance(raw, str) or not raw:
        return base
    try:
        return apply_traits_override(base, json.loads(raw))
    except (ValueError, TypeError, RecursionError):
        _LOG.warning("plant recipe: ignoring an invalid traits3d_override")
        return base


def resolve_traits(
    kind: str,
    species: str | None = None,
    category: str | None = None,
    *,
    override: Mapping[str, Any] | None = None,
) -> Traits3D | None:
    """Resolve ``(kind, species, category)`` through ``resolve_sprite`` to its ``Traits3D``.

    ``override`` (already layered by :func:`merge_overrides`) is applied on top.
    """
    sprite = resolve_sprite(kind, species or "", category)
    if sprite is None:
        return None
    return apply_traits_override(traits_for(sprite), override)


__all__ = [
    "BARK_BIRCH",
    "BRANCHING_BOUNDS",
    "CROWN_BOUNDS",
    "LEAF_BOUNDS",
    "MAX_OVERRIDE_JSON_CHARS",
    "OVERRIDE_TOP_KEYS",
    "BARK_COLORS",
    "BARK_GREY",
    "BARK_OAK",
    "BARK_PINE",
    "BARK_SHINGLE",
    "BARK_STEM",
    "BARK_TEAK",
    "BARK_WALNUT",
    "BARK_WALNUT_DARK",
    "CATEGORY_TRAITS",
    "CROWN_SHAPES",
    "HARVEST_WINDOWS_WEEKS",
    "LEAF_SHAPES",
    "REFERENCE_LAST_FROST",
    "SPECIES_TRAITS",
    "WHITE_BARK_SHADE",
    "BranchingTraits",
    "CrownTraits",
    "LeafTraits",
    "Traits3D",
    "apply_traits_override",
    "fruit_months_from_harvest_window",
    "layered_recipe_json",
    "merge_overrides",
    "override_signature",
    "resolve_traits",
    "traits_for",
    "traits_with_override_json",
    "validate_traits3d_override",
]
