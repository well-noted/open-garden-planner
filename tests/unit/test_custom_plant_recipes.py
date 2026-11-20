"""Custom per-species / per-specimen Plant Forge recipes (``Traits3D`` overrides)."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest

from open_garden_planner.core.plant_art import SpriteRef, traits_for
from open_garden_planner.core.plant_art.recipes import ARCHETYPES, FLOWERS, PALETTES
from open_garden_planner.core.plant_art.traits3d import (
    BRANCHING_BOUNDS,
    CROWN_BOUNDS,
    LEAF_BOUNDS,
    BranchingTraits,
    CrownTraits,
    apply_traits_override,
    layered_recipe_json,
    merge_overrides,
    override_signature,
    resolve_traits,
    traits_with_override_json,
    validate_traits3d_override,
)
from open_garden_planner.core.plant_forge import ForgeCache, forge_plant, recipe_traits
from open_garden_planner.core.plant_forge.skeleton import grow_skeleton
from open_garden_planner.models.plant_data import PlantInstance, PlantSpeciesData

OAK = SpriteRef("species", "oak")
V_FORK: dict[str, Any] = {
    "crown": {"shape": "vase", "trunk_share": 0.34, "lobe_bias_az_deg": 225.0,
              "lobe_bias_strength": 0.25},
    "branching": {"basal_stems": 2, "fork_height_share": 0.18, "fork_spread_deg": 34.0,
                  "gnarl": 0.16},
}
BUSH: dict[str, Any] = {
    "archetype": "mound",
    "crown": {"shape": "mound", "trunk_share": 0.06, "droop": 0.22},
    "branching": {"basal_stems": 3, "fork_height_share": 0.0, "fork_spread_deg": 38.0},
}


def _positions(parts: Any) -> bytes:
    return b"".join(np.ascontiguousarray(p.mesh.positions).tobytes() for p in parts)


# --- defaults are unchanged -------------------------------------------------------


def test_every_bundled_recipe_keeps_the_single_trunk_symmetric_defaults() -> None:
    from open_garden_planner.core.plant_art.traits3d import CATEGORY_TRAITS, SPECIES_TRAITS

    for traits in (*SPECIES_TRAITS.values(), *CATEGORY_TRAITS.values()):
        assert traits.branching.basal_stems == 1
        assert traits.branching.fork_height_share == 0.0
        assert traits.crown.lobe_bias_strength == 0.0


def test_a_neutral_override_forges_the_identical_mesh() -> None:
    plain = forge_plant(OAK, 600, 250, seed=3, cache=None)
    neutral = override_signature(validate_traits3d_override(
        {"crown": {"lobe_bias_strength": 0.0}, "branching": {"basal_stems": 1}}
    ))
    assert _positions(forge_plant(OAK, 600, 250, seed=3, cache=None,
                                  traits_override_json=neutral)) == _positions(plain)


# --- validation --------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "x",
        [],
        {"unknown": 1},
        {"archetype": "tree"},
        {"palette": "magenta"},
        {"autumn_palette": 3},
        {"blossom_spec": "rainbow"},
        {"deciduous": "yes"},
        {"deciduous": 1},
        {"bloom_months": [0]},
        {"bloom_months": [13]},
        {"bloom_months": "4,5"},
        {"bloom_months": [True]},
        {"fruit_months": [1.5]},
        {"crown": "round"},
        {"crown": {"shape": "blob"}},
        {"crown": {"nope": 1}},
        {"crown": {"droop": float("nan")}},
        {"crown": {"droop": float("inf")}},
        {"crown": {"droop": True}},
        {"crown": {"droop": "0.2"}},
        {"crown": {"trunk_share": 0.9}},
        {"crown": {"lobe_bias_strength": 0.6}},
        {"crown": {"lobe_bias_az_deg": 361.0}},
        {"branching": {"basal_stems": 0}},
        {"branching": {"basal_stems": 6}},
        {"branching": {"basal_stems": 2.5}},
        {"branching": {"fork_spread_deg": 80.0}},
        {"branching": {"fork_height_share": 0.9}},
        {"branching": {"bark_color": "brown"}},
        {"branching": {"bark_color": "#12345"}},
        {"leaf": {"shape": "scale"}},
        {"leaf": {"length_cm": 0.1}},
        {"leaf": {"frilly": "no"}},
    ],
)
def test_invalid_recipes_are_rejected(bad: Any) -> None:
    with pytest.raises(ValueError):
        validate_traits3d_override(bad)


def test_valid_recipes_normalise_and_nothing_means_no_override() -> None:
    assert validate_traits3d_override(None) is None
    assert validate_traits3d_override({}) is None
    assert validate_traits3d_override({"crown": {}, "leaf": None}) is None
    clean = validate_traits3d_override(
        {"bloom_months": [5, 4, 4], "branching": {"bark_color": "#ABCDEF", "basal_stems": 2.0}}
    )
    assert clean == {"bloom_months": [4, 5], "branching": {"bark_color": "#abcdef", "basal_stems": 2}}
    assert validate_traits3d_override(V_FORK) == V_FORK
    assert validate_traits3d_override(BUSH) == BUSH


def test_every_bound_is_inclusive_and_enforced_at_both_ends() -> None:
    for section, bounds in (("crown", CROWN_BOUNDS), ("branching", BRANCHING_BOUNDS),
                            ("leaf", LEAF_BOUNDS)):
        for key, (lo, hi) in bounds.items():
            assert validate_traits3d_override({section: {key: lo}}) is not None
            assert validate_traits3d_override({section: {key: hi}}) is not None
            with pytest.raises(ValueError):
                validate_traits3d_override({section: {key: hi + 1000}})
            with pytest.raises(ValueError):
                validate_traits3d_override({section: {key: lo - 1000}})


# --- precedence and application ----------------------------------------------------


def test_override_changes_only_what_it_names() -> None:
    base = traits_for(OAK)
    out = apply_traits_override(base, V_FORK)
    assert out.branching.basal_stems == 2 and out.crown.shape == "vase"
    assert out.crown.lobe_bias_az_deg == 225.0
    assert out.branching.attractors == base.branching.attractors  # untouched
    assert out.leaf == base.leaf and out.palette == base.palette
    assert apply_traits_override(base, None) is base


def test_archetype_override_switches_the_builder_and_keeps_the_species_colours() -> None:
    lemon = SpriteRef("species", "lemon_tree")
    out = apply_traits_override(traits_for(lemon), BUSH)
    assert out.archetype == "mound" and out.palette == traits_for(lemon).palette
    assert out.evergreen  # still evergreen, same blossom spec
    assert out.blossom_spec == traits_for(lemon).blossom_spec


def test_palette_override_keeps_an_evergreens_single_colour() -> None:
    pal = next(k for k in PALETTES if k != traits_for(SpriteRef("species", "lemon_tree")).palette)
    out = apply_traits_override(traits_for(SpriteRef("species", "lemon_tree")), {"palette": pal})
    assert out.palette == pal and out.autumn_palette == pal
    deciduous = apply_traits_override(traits_for(OAK), {"palette": pal})
    assert deciduous.autumn_palette == traits_for(OAK).autumn_palette


def test_resolve_traits_applies_the_override_on_top_of_the_bundled_match() -> None:
    plain = resolve_traits("TREE", "oak")
    assert plain is not None
    fork = resolve_traits("TREE", "oak", override=V_FORK)
    assert fork is not None and fork.branching.basal_stems == 2
    assert resolve_traits("TREE", "oak", override=None) == plain
    with pytest.raises(ValueError):
        resolve_traits("TREE", "oak", override={"archetype": "bogus"})


def test_specimen_layers_over_species_field_by_field() -> None:
    species = {"crown": {"droop": 0.5, "shape": "weeping"}, "palette": next(iter(PALETTES))}
    instance = {"crown": {"droop": 0.1}, "deciduous": False}
    merged = merge_overrides(species, instance)
    assert merged == {"crown": {"droop": 0.1, "shape": "weeping"},
                      "palette": next(iter(PALETTES)), "deciduous": False}
    assert merge_overrides(None, None) is None
    assert merge_overrides(species, None) == species
    # The inputs are not mutated.
    assert species["crown"] == {"droop": 0.5, "shape": "weeping"}


def test_layered_recipe_json_is_canonical_and_survives_one_bad_layer() -> None:
    a = layered_recipe_json({"crown": {"droop": 0.2, "taper": 0.1}}, None)
    b = layered_recipe_json({"crown": {"taper": 0.1, "droop": 0.2}}, None)
    assert a == b == '{"crown":{"droop":0.2,"taper":0.1}}'
    assert layered_recipe_json(None, None) == ""
    # A corrupt specimen layer is dropped, the good species layer survives (and vice versa).
    assert layered_recipe_json({"crown": {"droop": 0.2}}, {"archetype": "bogus"}) == \
        '{"crown":{"droop":0.2}}'
    assert layered_recipe_json("garbage", {"crown": {"taper": 0.1}}) == '{"crown":{"taper":0.1}}'


def test_the_reader_ignores_a_bad_override_instead_of_crashing() -> None:
    base = traits_for(OAK)
    assert traits_with_override_json(base, "") is base
    assert traits_with_override_json(base, None) is base
    assert traits_with_override_json(base, "{not json") is base
    assert traits_with_override_json(base, json.dumps({"archetype": "bogus"})) is base
    assert traits_with_override_json(base, json.dumps(["x"])) is base
    assert traits_with_override_json(base, json.dumps(V_FORK)).branching.basal_stems == 2


# --- the skeleton ------------------------------------------------------------------


def _skeleton(crown: CrownTraits, branching: BranchingTraits, seed: int = 5) -> Any:
    return grow_skeleton(600.0, 250.0, crown, branching, np.random.default_rng(seed))


def test_a_v_fork_gives_the_trunk_base_two_stems_and_a_single_trunk_gives_one() -> None:
    crown = CrownTraits("vase", trunk_share=0.34)
    single = _skeleton(crown, BranchingTraits())
    forked = _skeleton(crown, BranchingTraits(basal_stems=2, fork_height_share=0.0))
    assert int(np.sum(single.parents == 0)) == 1
    assert int(np.sum(forked.parents == 0)) == 2


def test_a_split_up_the_trunk_keeps_a_shared_column_below_the_fork() -> None:
    crown = CrownTraits("vase", trunk_share=0.34)
    sk = _skeleton(crown, BranchingTraits(basal_stems=2, fork_height_share=0.18))
    children_of_root = np.flatnonzero(sk.parents == 0)
    assert len(children_of_root) == 1  # the shared column
    # Exactly one node (the fork) has the two stems as children.
    counts = np.bincount(sk.parents[sk.parents >= 0], minlength=len(sk.parents))
    forks = np.flatnonzero((counts >= 2) & (sk.order == 0))
    assert len(forks) >= 1
    assert 0.10 * 600.0 <= float(sk.positions[forks.min(), 2]) <= 0.34 * 600.0


@pytest.mark.parametrize("stems", [2, 3, 4, 5])
def test_multi_stem_skeletons_stay_in_the_node_budget_and_are_a_valid_tree(stems: int) -> None:
    sk = _skeleton(CrownTraits("mound", trunk_share=0.06),
                   BranchingTraits(basal_stems=stems, fork_spread_deg=55.0))
    n = len(sk.positions)
    assert n <= 650 and np.all(np.isfinite(sk.positions)) and np.all(np.isfinite(sk.radii))
    assert sk.parents[0] == -1 and np.all(sk.parents[1:] < np.arange(1, n))
    assert np.all(sk.radii > 0.0)


def test_canopy_lean_shifts_the_crown_towards_its_azimuth() -> None:
    base = BranchingTraits()
    level = _skeleton(CrownTraits("round"), base)
    east = _skeleton(CrownTraits("round", lobe_bias_az_deg=90.0, lobe_bias_strength=0.5), base)
    north = _skeleton(CrownTraits("round", lobe_bias_az_deg=0.0, lobe_bias_strength=0.5), base)
    tips = lambda s: s.positions[s.tip_indices]  # noqa: E731
    assert tips(east)[:, 0].mean() > tips(level)[:, 0].mean() + 10.0
    assert tips(north)[:, 1].mean() > tips(level)[:, 1].mean() + 10.0


# --- forging and the cache ---------------------------------------------------------


def test_a_recipe_changes_the_forged_mesh_deterministically() -> None:
    plain = _positions(forge_plant(OAK, 600, 250, seed=3, cache=None))
    forked = _positions(forge_plant(OAK, 600, 250, seed=3, cache=None,
                                    traits_override_json=override_signature(V_FORK)))
    again = _positions(forge_plant(OAK, 600, 250, seed=3, cache=None,
                                   traits_override_json=override_signature(V_FORK)))
    assert forked != plain and forked == again


def test_bush_form_recipe_builds_a_shrub_from_a_tree_species() -> None:
    lemon = SpriteRef("species", "lemon_tree")
    tree = forge_plant(lemon, 300, 150, seed=2, cache=None)
    bush = forge_plant(lemon, 300, 150, seed=2, cache=None,
                       traits_override_json=override_signature(BUSH))
    assert _positions(bush) != _positions(tree)
    for part in bush:
        assert part.mesh.triangle_count > 0
        assert np.all(np.isfinite(part.mesh.positions))
        assert int(part.mesh.indices.max()) < part.mesh.vertex_count


def test_the_cache_key_carries_the_recipe_so_an_edit_can_never_serve_the_old_mesh() -> None:
    cache = ForgeCache()
    a = override_signature(V_FORK)
    b = override_signature({"crown": {"droop": 0.9}})
    first = forge_plant(OAK, 600, 250, seed=3, cache=cache)
    with_a = forge_plant(OAK, 600, 250, seed=3, cache=cache, traits_override_json=a)
    with_b = forge_plant(OAK, 600, 250, seed=3, cache=cache, traits_override_json=b)
    assert len({id(first), id(with_a), id(with_b)}) == 3 and len(cache) == 3
    assert forge_plant(OAK, 600, 250, seed=3, cache=cache, traits_override_json=a) is with_a
    assert forge_plant(OAK, 600, 250, seed=3, cache=cache) is first
    assert cache.hits == 2


def test_a_corrupt_recipe_json_forges_the_bundled_plant() -> None:
    plain = _positions(forge_plant(OAK, 600, 250, seed=3, cache=None))
    assert _positions(forge_plant(OAK, 600, 250, seed=3, cache=None,
                                  traits_override_json="{broken")) == plain


def test_recipe_traits_reads_a_records_param() -> None:
    class _Rec:
        params = {"traits3d_override": override_signature({"archetype": "climber"})}

    assert recipe_traits(_Rec(), OAK).archetype == "climber"  # type: ignore[arg-type]

    class _Plain:
        params: dict[str, Any] = {}

    assert recipe_traits(_Plain(), OAK).archetype == "canopy"  # type: ignore[arg-type]


# --- models ------------------------------------------------------------------------


def _species(**kw: Any) -> PlantSpeciesData:
    return PlantSpeciesData(scientific_name="Citrus limon", common_name="Lemon", **kw)


def test_species_and_instance_round_trip_the_recipe() -> None:
    species = _species(traits3d_override=BUSH)
    again = PlantSpeciesData.from_dict(json.loads(json.dumps(species.to_dict())))
    assert again.traits3d_override == BUSH
    instance = PlantInstance(traits3d_override=V_FORK, species_data=species)
    back = PlantInstance.from_dict(json.loads(json.dumps(instance.to_dict())))
    assert back.traits3d_override == V_FORK
    assert back.species_data is not None and back.species_data.traits3d_override == BUSH


def test_without_a_recipe_nothing_new_is_serialised() -> None:
    assert _species().to_dict()["traits3d_override"] is None
    assert "traits3d_override" not in PlantInstance().to_dict()
    assert _species().traits3d_override is None
    assert PlantSpeciesData.from_dict(_species().to_dict()).traits3d_override is None
    legacy = _species().to_dict()
    assert PlantInstance.from_dict({"species_data": legacy}).traits3d_override is None


def test_serialised_recipes_are_copies() -> None:
    species = _species(traits3d_override={"crown": {"droop": 0.2}})
    out = species.to_dict()
    out["traits3d_override"]["crown"]["droop"] = 0.9
    assert species.traits3d_override == {"crown": {"droop": 0.2}}


@pytest.mark.parametrize(
    "bad",
    [
        "x",
        [1],
        {"crown": {"droop": float("nan")}},
        {"crown": {"droop": float("inf")}},
        {1: "x"},
        {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}},
        {"bloom_months": list(range(2000))},
        {"x": object()},
    ],
)
def test_malformed_stored_recipes_are_dropped_by_the_readers_and_rejected_by_the_validator(
    bad: Any,
) -> None:
    data = _species().to_dict() | {"traits3d_override": bad}
    assert PlantSpeciesData.from_dict(data).traits3d_override is None  # the species still loads
    assert PlantInstance.from_dict({"traits3d_override": bad}).traits3d_override is None
    from open_garden_planner.models.plant_data import validate_traits3d_override as structural

    with pytest.raises(ValueError):
        structural(bad)


@pytest.mark.parametrize("bad", [{"palette": ["x"]}, {"archetype": {}}, {"blossom_spec": [1]},
                                 {"autumn_palette": {"a": 1}}])
def test_unhashable_values_are_a_value_error_never_a_type_error(bad: Any) -> None:
    with pytest.raises(ValueError):
        validate_traits3d_override(bad)
    assert layered_recipe_json(bad, None) == ""
    assert layered_recipe_json({"crown": {"droop": 0.2}}, bad) == '{"crown":{"droop":0.2}}'


def test_the_bundled_catalogue_names_every_valid_key() -> None:
    from open_garden_planner.agent_api.geo_recipes import plant_archetype_catalogue

    cat = plant_archetype_catalogue()
    assert cat["archetypes"] == sorted(ARCHETYPES) and len(cat["archetypes"]) == 13
    assert cat["palettes"] == sorted(PALETTES) and cat["blossom_specs"] == sorted(FLOWERS)
    crown = cat["recipe_schema"]["crown"]
    assert crown["lobe_bias_strength"] == {"min": 0.0, "max": 0.55, "default": 0.0}
    assert cat["recipe_schema"]["branching"]["basal_stems"]["max"] == 5
    for example in cat["examples"].values():  # the documented examples are valid recipes
        assert validate_traits3d_override(example) == example
    json.dumps(cat)  # serialisable for MCP
