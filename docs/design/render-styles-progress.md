# Render styles (Package 4): progress

Branch `claude/render-styles-wp3-310d4a` (renamed on request if the name matters), from master. Nothing pushed.

## Done
- `core/render_style.py`: `RenderStyle`, `RENDER_STYLES`, lenient `normalise_render_style`,
  strict `parse_render_style` (design 1.1).
- `ui/canvas/style/geometry.py` (WP3, pure, Qt types only): LOD band + paper-mm units,
  crc32 seeds, value noise, exact scanline `hatch_segments` (holes, concave, scene-anchored
  phase, budget returns None), `wobble_polyline`, `wobble_closed`, `scallop_outline`,
  `edge_ring`, `cluster_circles`, `united_circles`.
- Seam: `fill_paint.paint_shape` asks `style_of(item)` (scene `render_style`, default
  illustrated) and tries `STYLE_SHAPE_PAINTERS[style]` first; registry is empty, so nothing
  changes today. Existing fill tests: 370 pass.
- `tests/unit/style/`: 26 tests (determinism across `PYTHONHASHSEED`, bounds, hatch
  exactness/phase, AST lint: no hash/random/time in the style package). All green.
- 3D spike: `docs/design/npr-spike/FINDINGS.md`. Basic feasibility passes on D3D11.
- `ruff check src/` and bandit (high) clean on touched code.

## Not done
- WP1/WP2 (scene `render_style`, `_styled_scene`, `render_scene_region(render_style)`,
  `StyleState`, `pen_for_painter`): `path_style_paint` and `scene_rendering` seams are NOT
  wired because the scene has no style state yet; wiring them now would be dead code.
- No painters (WP4-6), UI (WP7), agent (WP11), NprStyle production code (WP9).
- No new user-visible strings, so no translation changes needed yet.
- Full suite not run (only touched areas).

## Needs the owner
- Design correction from the spike: depth edges use second difference, not Sobel (see findings).
- Run the harness on the Iris Xe / real driver; check MSAA depth and foliage ink noise.
- Open-question assumptions (Q2, Q3, Q7, Q8) are unrecorded until the ADR (WP13).
