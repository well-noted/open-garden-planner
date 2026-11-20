"""core.plant_art.recipe_edit: the effective recipe and the panel's field edits (FR-RECIPE-06)."""

from __future__ import annotations

import pytest

from open_garden_planner.core.plant_art import recipe_edit as re_
from open_garden_planner.core.plant_art.traits3d import BRANCHING_BOUNDS, CROWN_BOUNDS


def test_bundled_recipe_is_reported_without_overrides() -> None:
    view = re_.effective_recipe("TREE", "Quercus robur", "DECIDUOUS_TREE")
    assert view is not None
    assert view.archetype in re_.archetype_choices()
    assert view.basal_stems == 1
    assert not view.specimen_overridden and not view.species_overridden


def test_a_kind_without_a_recipe_has_no_view() -> None:
    assert re_.effective_recipe("RAISED_BED", "", None) is None


def test_specimen_over_species_over_bundle() -> None:
    species = {"branching": {"basal_stems": 2}, "crown": {"lobe_bias_strength": 0.2}}
    specimen = {"branching": {"basal_stems": 4}}
    v = re_.effective_recipe("TREE", "Quercus robur", None, species, specimen)
    assert v is not None
    assert v.basal_stems == 4  # specimen wins
    assert v.lean_strength == pytest.approx(0.2)  # species layer kept
    assert v.specimen_overridden and v.species_overridden


def test_invalid_stored_layer_is_ignored_not_raised() -> None:
    v = re_.effective_recipe("TREE", "Quercus robur", None, {"archetype": "nope"}, {"x": 1})
    assert v is not None
    assert not v.species_overridden and not v.specimen_overridden


def test_override_with_field_keeps_other_fields() -> None:
    first = re_.override_with_field(None, re_.FIELD_BASAL_STEMS, 3)
    both = re_.override_with_field(first, re_.FIELD_FORK_HEIGHT, 0.2)
    assert both == {"branching": {"basal_stems": 3, "fork_height_share": 0.2}}
    arch = re_.override_with_field(both, re_.FIELD_ARCHETYPE, re_.archetype_choices()[0])
    assert arch["branching"] == both["branching"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (re_.FIELD_BASAL_STEMS, 6),
        (re_.FIELD_FORK_HEIGHT, 0.9),
        (re_.FIELD_LEAN_STRENGTH, 0.9),
        (re_.FIELD_LEAN_AZIMUTH, 400.0),
        (re_.FIELD_ARCHETYPE, "not-an-archetype"),
        (re_.FIELD_BASAL_STEMS, True),
        (re_.FIELD_FORK_HEIGHT, float("nan")),
    ],
)
def test_out_of_bounds_edits_are_refused(field, value) -> None:
    with pytest.raises(ValueError):
        re_.override_with_field(None, field, value)


def test_a_field_the_panel_does_not_edit_is_refused() -> None:
    with pytest.raises(ValueError):
        re_.override_with_field(None, ("leaf", "length_cm"), 10.0)


def test_field_bounds_come_from_traits3d() -> None:
    assert re_.field_bounds(re_.FIELD_BASAL_STEMS) == BRANCHING_BOUNDS["basal_stems"]
    assert re_.field_bounds(re_.FIELD_LEAN_STRENGTH) == CROWN_BOUNDS["lobe_bias_strength"]
    with pytest.raises(ValueError):
        re_.field_bounds(re_.FIELD_ARCHETYPE)


def test_same_recipe_treats_none_and_empty_alike() -> None:
    assert re_.same_recipe(None, {})
    assert not re_.same_recipe({"archetype": "mound"}, None)


def test_tint_only_for_an_overridden_palette() -> None:
    from open_garden_planner.core.plant_art.recipes import PALETTES

    assert re_.recipe_tint_hex("TREE", "Quercus robur", None) is None
    assert re_.recipe_tint_hex("TREE", "Quercus robur", None, {"branching": {"basal_stems": 2}}) is None
    tint = re_.recipe_tint_hex("TREE", "Quercus robur", None, None, {"palette": "redleaf"})
    assert tint == PALETTES["redleaf"]["t0"]
    # specimen wins over species
    both = re_.recipe_tint_hex(
        "TREE", "Quercus robur", None, {"palette": "autumn"}, {"palette": "redleaf"}
    )
    assert both == PALETTES["redleaf"]["t0"]


def test_tint_is_none_when_palette_equals_the_sprite_s_own() -> None:
    from open_garden_planner.core.plant_art.resolve import resolve_sprite
    from open_garden_planner.core.plant_art.traits3d import traits_for

    own = traits_for(resolve_sprite("TREE", "Quercus robur", None)).palette
    assert re_.recipe_tint_hex("TREE", "Quercus robur", None, None, {"palette": own}) is None


def test_tint_ignores_invalid_and_non_plant_input() -> None:
    assert re_.recipe_tint_hex("TREE", "Quercus robur", None, None, {"palette": "nope"}) is None
    assert re_.recipe_tint_hex("RAISED_BED", "", None, None, {"palette": "redleaf"}) is None
