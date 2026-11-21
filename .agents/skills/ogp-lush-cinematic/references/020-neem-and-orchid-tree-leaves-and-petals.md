# Neem and orchid tree: the compound and the butterfly leaf, and the petals that were never seen

Two Arizona species drew as the generic round tree: a lollipop of diamond leaves. They now carry
a species recipe (`traits3d_override` in `plant_species.json`, ADR-079) and the archetype grew
what the recipe needed, so other species can use it.

- **`LEAF_SHAPES` += `bilobed`, `compound`.** `leaf.shape` was stored but never drawn: every tree
  leaf was a diamond. `plant_forge/foliage.LEAF_TEMPLATES` now draws a template (6 triangles
  for the butterfly, 11 for the compound leaf) through `template_leaves`; the diamond path
  (`micro_leaves`) is untouched, so every bundled species is byte-identical. Only LOD0 draws a
  template (the leaf budget divides by `leaf_triangles`); LOD1 and LOD2 keep the diamond.
- **`blossom_density`, `blossom_size`, `blossom_form` (`cup` | `panicle`)** on `Traits3D`, in the
  override and bounded (`BLOSSOM_BOUNDS`). A tree wreathed in flowers reserves their triangles
  before its crown spends the budget (`accents.blossom_triangle_estimate`; only when a recipe
  asks, a bundled species keeps its 512-triangle reserve). Found because the accent budget
  was cut to the first 514 triangles: only the flowers' discs survived, never the petals.
- **Petals** were wound against their normal on a back-face-culled `vc` part, so no flower was
  ever seen from outside; they are wound with it now, and each petal has a back (lit like the
  front). This moved the 24 flower parts of the 40 golden tree meshes (apple, pear, plum, cherry,
  magnolia, olive): `tests/fixtures/plant_forge/forged_trees_golden.json`; wood and foliage did not move.
- **Old plans:** a plan's species snapshot lacks a recipe added later; the 3D snapshot takes the
  bundled record's (`snapshot._species_recipe`) unless the key is there (a cleared recipe stays cleared).
- Orchid tree: 3 basal stems, fork at 10 %, spreading crown, deciduous (bare in February at 33 N,
  bloom Feb-Apr), 28x the blossoms. Neem: evergreen, short thick dark trunk, round crown, compound
  leaf, 9x white panicles in Mar-May. Not drawn: fissured bark (no bark relief exists), the fruit.
