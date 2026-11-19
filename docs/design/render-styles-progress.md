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

- WP1: `CanvasScene.render_style` / `set_render_style` (moves the paint revision only; no
  `plan_changed`, no undo, not in the .ogp), settings keys `appearance/render_style`,
  `appearance/cad_callouts` (default off, owner Q4), `export/render_style_override`;
  `scene_rendering.styled_scene`, `render_scene_region(render_style=)`, `style_background`,
  `paint_style_paper` (no-op until WP6), `THUMBNAIL_STYLE` + `render_thumbnail_region`;
  PNG/SVG/PDF/print/Agent call sites go through `styled_scene` (a test pins every site).

## Not done
- No painters (WP4-6), UI (WP7), agent (WP11), NprStyle production code (WP9).
- No new user-visible strings, so no translation changes needed yet.
- Full suite not run (only touched areas).

## Needs the owner
- Design correction from the spike: depth edges use second difference, not Sobel (see findings).
- Run the harness on the Iris Xe / real driver; check MSAA depth and foliage ink noise.
- Open-question assumptions (Q2, Q3, Q7, Q8) are unrecorded until the ADR (WP13).

## Assumptions
- The scene class is `CanvasScene` (the design says `GardenScene`).
- Public name `styled_scene` (the design writes `_styled_scene`): it is imported by
  `svg_export`, `pdf_report_service` and `print_dialog`.
- `appearance/cad_callouts` defaults to False: the owner decision Q4 overrides the design's True.
- No embedded .ogp thumbnail exists yet; the thumbnail rule is a constant
  (`THUMBNAIL_STYLE`) and a helper (`render_thumbnail_region`) for the first caller.
- `CanvasScene.drawBackground` is unchanged in WP1/WP2 (it paints the canvas colour); the
  style paper reaches it with the first painter (WP4/WP6). Exports fill `style_background`.
- A print dialog follows the canvas style (no per-dialog override until WP7).
