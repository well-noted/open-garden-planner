"""The recipe a plant ends up with, and the edits the properties panel makes to it.

Pure data, no Qt: the panel shows :func:`effective_recipe` and turns each edited
field into the specimen's new ``traits3d_override`` with :func:`override_with_field`
(a ``SetPlantRecipeCommand`` carries it). Everything is validated by
``traits3d.validate_traits3d_override``, so the panel cannot store what the agent
tool would refuse (ADR-079, FR-RECIPE-06).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final

from .recipes import ARCHETYPES, PALETTES
from .resolve import resolve_sprite
from .traits3d import (
    BRANCHING_BOUNDS,
    CROWN_BOUNDS,
    apply_traits_override,
    layered_recipe_json,
    merge_overrides,
    traits_for,
    validate_traits3d_override,
)

#: The plant kinds a recipe can be set on (the kinds the 3D plant builder forges).
RECIPE_KINDS: Final[tuple[str, ...]] = ("TREE", "SHRUB", "PERENNIAL")

#: ``(section, key)`` of every field the panel edits.
FIELD_ARCHETYPE: Final[tuple[str | None, str]] = (None, "archetype")
FIELD_BASAL_STEMS: Final[tuple[str | None, str]] = ("branching", "basal_stems")
FIELD_FORK_HEIGHT: Final[tuple[str | None, str]] = ("branching", "fork_height_share")
FIELD_LEAN_AZIMUTH: Final[tuple[str | None, str]] = ("crown", "lobe_bias_az_deg")
FIELD_LEAN_STRENGTH: Final[tuple[str | None, str]] = ("crown", "lobe_bias_strength")
EDITABLE_FIELDS: Final[tuple[tuple[str | None, str], ...]] = (
    FIELD_ARCHETYPE,
    FIELD_BASAL_STEMS,
    FIELD_FORK_HEIGHT,
    FIELD_LEAN_AZIMUTH,
    FIELD_LEAN_STRENGTH,
)


@dataclass(frozen=True, slots=True)
class RecipeView:
    """The recipe the 3D view will build, in the fields the panel shows."""

    archetype: str
    basal_stems: int
    fork_height_share: float
    lean_azimuth_deg: float
    lean_strength: float
    palette: str
    #: True when a specimen override sits on top (so "reset" has something to do).
    specimen_overridden: bool
    #: True when the species layer (library species or item snapshot) overrides the bundle.
    species_overridden: bool


def _valid(raw: object) -> dict[str, Any] | None:
    """A stored override validated, ``None`` for absent or invalid (tolerant reader)."""
    try:
        return validate_traits3d_override(raw)
    except (ValueError, TypeError):
        return None


def effective_recipe(
    kind: str,
    species: str | None,
    category: str | None,
    species_override: object = None,
    instance_override: object = None,
) -> RecipeView | None:
    """The recipe a plant item builds with, or ``None`` for a kind with no recipe.

    Resolution is the 3D builder's own: specimen over species over the bundled sprite
    recipe (``resolve_sprite`` + ``traits_for``), so the panel shows what is drawn.
    """
    if kind not in RECIPE_KINDS:
        return None
    sprite = resolve_sprite(kind, species or "", category)
    if sprite is None:
        return None
    species_layer = _valid(species_override)
    instance_layer = _valid(instance_override)
    traits = apply_traits_override(
        traits_for(sprite), merge_overrides(species_layer, instance_layer)
    )
    return RecipeView(
        archetype=traits.archetype,
        basal_stems=traits.branching.basal_stems,
        fork_height_share=traits.branching.fork_height_share,
        lean_azimuth_deg=traits.crown.lobe_bias_az_deg,
        lean_strength=traits.crown.lobe_bias_strength,
        palette=traits.palette,
        specimen_overridden=instance_layer is not None,
        species_overridden=species_layer is not None,
    )


@lru_cache(maxsize=512)
def _tint_for(kind: str, species: str, category: str | None, recipe_json: str) -> str | None:
    if not recipe_json or kind not in RECIPE_KINDS:
        return None
    palette = json.loads(recipe_json).get("palette")
    if palette is None or palette not in PALETTES:
        return None
    sprite = resolve_sprite(kind, species, category)
    if sprite is None or traits_for(sprite).palette == palette:
        return None  # the sprite is already drawn in that palette: nothing to add
    return PALETTES[palette]["t0"]


def recipe_tint_hex(
    kind: str,
    species: str | None,
    category: str | None,
    species_override: object = None,
    instance_override: object = None,
) -> str | None:
    """Canopy colour (``#rrggbb``) of an OVERRIDDEN palette, for the 2D sprite's tint.

    The 2D sprite is the species recipe's own drawing; it cannot be redrawn from an
    override. The one honest 2D echo of a recipe is its palette: when specimen over
    species names a palette the bundled sprite is not drawn in, this returns that
    palette's canopy colour (``t0``), which the item lays over the sprite as the
    renderer's usual wash. ``None`` when no palette is overridden (the sprite is
    byte-identical to before). Memoised: the canvas asks on every paint.
    """
    recipe = layered_recipe_json(species_override, instance_override)
    return _tint_for(kind, species or "", category, recipe)


def archetype_choices() -> list[str]:
    """Every valid archetype key, sorted (the combo's entries)."""
    return sorted(ARCHETYPES)


def override_with_field(
    current: object,
    field: tuple[str | None, str],
    value: Any,
) -> dict[str, Any]:
    """``current`` (a specimen override or ``None``) with one field set, validated.

    Raises ``ValueError`` for a field the panel does not edit or a value out of bounds
    (``validate_traits3d_override`` names the field). The other fields are kept.
    """
    if field not in EDITABLE_FIELDS:
        raise ValueError(f"not an editable recipe field: {field!r}")
    base = _valid(current) or {}
    section, key = field
    if section is None:
        base[key] = value
    else:
        base[section] = {**base.get(section, {}), key: value}
    clean = validate_traits3d_override(base)
    assert clean is not None  # a field was just set
    return clean


def field_bounds(field: tuple[str | None, str]) -> tuple[float, float]:
    """The ``(lo, hi)`` bounds of a numeric panel field (single source: traits3d)."""
    section, key = field
    if section == "branching":
        return BRANCHING_BOUNDS[key]
    if section == "crown":
        return CROWN_BOUNDS[key]
    raise ValueError(f"{field!r} has no numeric bounds")


def same_recipe(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> bool:
    """True when two overrides are the same recipe (``None`` and ``{}`` are both none)."""
    return (a or None) == (b or None)


__all__ = [
    "EDITABLE_FIELDS",
    "FIELD_ARCHETYPE",
    "FIELD_BASAL_STEMS",
    "FIELD_FORK_HEIGHT",
    "FIELD_LEAN_AZIMUTH",
    "FIELD_LEAN_STRENGTH",
    "RECIPE_KINDS",
    "RecipeView",
    "archetype_choices",
    "effective_recipe",
    "field_bounds",
    "override_with_field",
    "recipe_tint_hex",
    "same_recipe",
]
