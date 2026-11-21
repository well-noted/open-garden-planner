"""Neem and orchid tree: the bilobed and compound leaf, the blossom fields, the species recipes."""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest

from open_garden_planner.core.plant_art import LEAF_SHAPES, SpriteRef
from open_garden_planner.core.plant_art.traits3d import (
    BLOSSOM_BOUNDS,
    BLOSSOM_FORMS,
    resolve_traits,
    traits_for,
    validate_traits3d_override,
)
from open_garden_planner.core.plant_forge import LOD0, LOD1, build_blossom_accents, forge_plant
from open_garden_planner.core.plant_forge.accents import blossom_triangle_estimate
from open_garden_planner.core.plant_forge.foliage import (
    LEAF_TEMPLATES,
    leaf_triangles,
    template_leaves,
)
from open_garden_planner.core.plant_forge.lod import LOD0_TRIANGLE_BUDGET
from open_garden_planner.services.bundled_species_db import lookup_species

ROUND = SpriteRef("categories", "round_deciduous")


def _recipe(name: str) -> dict:
    record = lookup_species(name)
    assert record is not None, name
    recipe = record.get("traits3d_override")
    assert recipe, f"{name} carries no 3D recipe"
    return recipe


def _forge(name: str, seed: int = 7, lod: int = LOD0):
    record = lookup_species(name)
    recipe = json.dumps(_recipe(name), sort_keys=True, separators=(",", ":"))
    height = float(record["max_height_cm"]) * 0.9
    radius = float(record["max_spread_cm"]) * 0.45
    parts = forge_plant(ROUND, height, radius, seed=seed, lod=lod, cache=None,
                        traits_override_json=recipe)
    return parts, height, radius


class TestVocabulary:
    def test_the_new_shapes_are_allowed(self) -> None:
        assert {"bilobed", "compound"} <= LEAF_SHAPES
        assert set(LEAF_TEMPLATES) <= LEAF_SHAPES
        assert {"cup", "panicle"} == BLOSSOM_FORMS

    def test_blossom_fields_validate_and_are_bounded(self) -> None:
        ok = validate_traits3d_override(
            {"blossom_density": 4, "blossom_size": 1.5, "blossom_form": "panicle",
             "leaf": {"shape": "bilobed"}})
        assert ok == {"blossom_density": 4.0, "blossom_size": 1.5, "blossom_form": "panicle",
                      "leaf": {"shape": "bilobed"}}
        for key, (lo, hi) in BLOSSOM_BOUNDS.items():
            for bad in (lo / 2, hi * 2, float("nan"), True):
                with pytest.raises(ValueError):
                    validate_traits3d_override({key: bad})
        with pytest.raises(ValueError):
            validate_traits3d_override({"blossom_form": "umbrella"})
        with pytest.raises(ValueError):
            validate_traits3d_override({"leaf": {"shape": "tripinnate"}})

    def test_defaults_leave_a_bundled_species_unchanged(self) -> None:
        traits = traits_for(SpriteRef("species", "apple_tree"))
        assert (traits.blossom_density, traits.blossom_size, traits.blossom_form) == (1.0, 1.0, "cup")


class TestLeafTemplates:
    @pytest.mark.parametrize("shape", sorted(LEAF_TEMPLATES))
    def test_a_leaf_faces_its_normal_and_is_finite(self, shape: str) -> None:
        rng = np.random.default_rng(3)
        k = 40
        pts = rng.normal(0, 50, (k, 3))
        out = pts / np.linalg.norm(pts, axis=1, keepdims=True)
        geo = template_leaves(shape, pts, out, 12.0, 8.0, np.ones((k, 4), np.float32), rng,
                              center=np.zeros(3), droop=0.1)
        nv = len(LEAF_TEMPLATES[shape][0])
        assert geo.vertex_count == k * nv
        assert geo.triangle_count == k * leaf_triangles(shape)
        assert np.isfinite(geo.pos).all() and np.isfinite(geo.nrm).all()
        np.testing.assert_allclose(np.linalg.norm(geo.nrm, axis=1), 1.0, atol=1e-4)
        tri = geo.pos[geo.tri].astype(np.float64)
        winding = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        assert (np.einsum("ij,ij->i", winding, geo.nrm[geo.tri[:, 0]]) > 0.0).mean() > 0.97
        assert (np.linalg.norm(winding, axis=1) > 1e-6).all()   # no degenerate leaf

    def test_the_diamond_costs_two_triangles(self) -> None:
        assert leaf_triangles("diamond") == 2 and leaf_triangles("bilobed") == 6
        assert leaf_triangles("compound") == 11


class TestSpeciesRecipes:
    def test_neem_is_a_bundled_arizona_species(self) -> None:
        record = lookup_species("Azadirachta indica")
        assert record is not None and record["common_name"] == "Neem"
        assert lookup_species("margosa") is record and lookup_species("nim") is record
        assert record["family"] == "Meliaceae" and "arizona_lower_desert" in record["regions"]
        assert 1000 <= record["max_height_cm"] <= 1500

    @pytest.mark.parametrize(("name", "shape", "form"),
                             [("Azadirachta indica", "compound", "panicle"),
                              ("Bauhinia variegata", "bilobed", "cup")])
    def test_the_recipe_is_valid_and_resolves(self, name: str, shape: str, form: str) -> None:
        recipe = _recipe(name)
        assert validate_traits3d_override(recipe) == recipe
        traits = resolve_traits("TREE", name, override=recipe)
        assert traits is not None
        assert traits.leaf.shape == shape and traits.blossom_form == form
        assert traits.blossom_spec is not None and traits.bloom_months

    def test_orchid_tree_is_multi_stemmed_and_bare_in_late_winter(self) -> None:
        traits = resolve_traits("TREE", "Orchid Tree", override=_recipe("Bauhinia variegata"))
        assert traits is not None and traits.deciduous
        assert traits.branching.basal_stems >= 2 and traits.branching.fork_height_share <= 0.2
        assert {2, 3} <= set(traits.bloom_months)

    def test_neem_is_evergreen_with_dark_bark(self) -> None:
        traits = resolve_traits("TREE", "Neem", override=_recipe("Azadirachta indica"))
        assert traits is not None and traits.evergreen
        assert traits.branching.bark_color == "#4a3a2e" and traits.crown.trunk_share <= 0.2


class TestForgedGeometry:
    @pytest.mark.parametrize("name", ["Azadirachta indica", "Bauhinia variegata"])
    def test_the_truth_gates(self, name: str) -> None:
        parts, height, radius = _forge(name)
        positions = np.concatenate([p.mesh.positions for p in parts])
        assert np.isfinite(positions).all()
        assert positions[:, 2].max() == pytest.approx(height, rel=0.03)
        reach = np.hypot(positions[:, 0], positions[:, 1]).max()
        assert reach <= radius * 1.15           # the spread is the data's
        assert sum(p.mesh.triangle_count for p in parts) <= LOD0_TRIANGLE_BUDGET
        for p in parts:
            np.testing.assert_allclose(np.linalg.norm(p.mesh.normals, axis=1), 1.0, atol=1e-3)

    def test_the_templated_leaf_is_drawn_at_the_finest_level_only(self) -> None:
        near, _, _ = _forge("Azadirachta indica", lod=LOD0)
        far, _, _ = _forge("Azadirachta indica", lod=LOD1)
        leaf_near = next(p for p in near if p.name == "foliage").mesh
        leaf_far = next(p for p in far if p.name == "foliage").mesh
        assert leaf_near.vertex_count % 33 == 0         # 11 triangles, 33 vertices a leaf
        assert leaf_far.vertex_count % 4 == 0           # the diamond
        assert sum(p.mesh.triangle_count for p in far) <= 6_000

    def test_deterministic_and_no_clones(self) -> None:
        def digest(seed: int) -> str:
            parts, _, _ = _forge("Bauhinia variegata", seed=seed)
            return hashlib.sha256(b"".join(p.mesh.positions.tobytes() for p in parts)).hexdigest()

        assert digest(5) == digest(5)
        assert digest(5) != digest(6)

    def test_the_orchid_tree_carries_many_blossoms_with_room_for_every_petal(self) -> None:
        parts, _, _ = _forge("Bauhinia variegata")
        flowers = next(p for p in parts if p.name == "flowers").mesh
        # the bundled habit is a dozen flowers (360 triangles); the recipe asks for far more
        assert flowers.triangle_count > 2_000
        traits = resolve_traits("TREE", "Orchid Tree", override=_recipe("Bauhinia variegata"))
        assert traits is not None
        assert blossom_triangle_estimate(traits, LOD0) >= flowers.triangle_count * 0.9
        # no flower is cut down to its disc (20 triangles) by the budget: 20 + 5 petals x 2 x 2
        assert flowers.triangle_count % (20 + 5 * 2 * 2) == 0

    def test_petals_have_a_front_and_a_back(self) -> None:
        traits = resolve_traits("TREE", "Orchid Tree", override=_recipe("Bauhinia variegata"))
        assert traits is not None
        geo = build_blossom_accents(traits, 500.0, 250.0, np.random.default_rng(1))
        tri = geo.pos[geo.tri].astype(np.float64)
        winding = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        up = winding[:, 2]
        # as many petal faces wind up as down: a culled front alone would leave a flower
        # on the far side of a crown a bare disc
        assert abs(int((up > 0).sum()) - int((up < 0).sum())) < 0.4 * len(up)
