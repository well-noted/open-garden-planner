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

- WP2: `ui/canvas/style/base.py` (`StylePainter`, `STYLE_PAINTERS`, `style_of`,
  `styled_bounding_rect`, `pen_for_painter`) and `state.py` (`StyleState`, LRU of 2). The scene
  builds state on a style switch, a band change (`CanvasView._apply_transform` ->
  `set_style_band`), a coalesced deferred rebuild after a paint revision bump and an export
  prewarm (`style_band_for_render`); a paint only reads it. `fill_paint` and
  `PathStylePaint.paint(render_style=)` ask `STYLE_PAINTERS`. A switch or band change calls
  `update()` on every item (a device-cached item kept the previous style's picture).

- WP4 (CAD painters): `ui/canvas/style/cad.py` registered in `STYLE_PAINTERS`; `core/render_style_roles.py`;
  seams in rectangle/ellipse/polygon/circle/polyline items (sprites, painted shadows, ridge/greenhouse/pergola
  pictures skipped in CAD), `styled_bounding_rect` adopted by those five `boundingRect`s,
  `PathStylePaint(owner=)`, `CanopySpec`, `drawBackground` uses the style paper. Callouts stay WP5.

## Review fixes (Opus review of WP1/WP2)
- `styled_scene` always enters `CanvasScene.style_for_render(style|None, px_per_cm, dpi)`: a
  `None` style (print, PDF report, default exports) now paints at the output's LOD band.
- `style_for_render` switches style, band and output dpi without moving the paint revision,
  so the 2-entry state LRU keeps the screen's state across an export override and the
  restore is a cache hit. Geometry is re-announced on entry and exit when style or band
  differ (option chosen for the bounding pad: notify, not a band-independent pad).
- `_rebuild_style_state` repaints every item (device-cached pixmaps).
- Output dpi: `OUTPUT_DPI` context var in `style/base.py`, set by `style_for_render` (and
  mirrored as `scene.style_output_dpi`); `pen_for_painter` reads it when no `dpi` is given.
  PNG, SVG, PDF report and print pass their dpi; screen and the agent render stay at 96.
- Tests: default PNG against a bare `scene.render`; call-site rule scans all of `src/`
  with an allow-list; thumbnail writers must use `render_thumbnail_region`; the dirty
  assertion is wired to `stack_changed` as the application does.

## Deferred (P2 from the review, not done)
- Re-sync the LOD band when `devicePixelRatio` changes (moving the window between screens).
- `CanvasScene.drawBackground` still paints the canvas colour, not the style paper.
- The Agent API render tool takes no `render_style` argument yet (WP11).

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
- `pen_for_painter` takes an explicit `dpi` for exports (default 96 x devicePixelRatio);
  nothing passes it yet (WP7 wires the export dialog's dpi).
- The bounding pad helper (`styled_bounding_rect`) is not yet applied in the item classes'
  `boundingRect` overrides: with no painter registered the pad is 0, so adopting it now is
  dead code; each shape class adopts it with its first painter (WP4/WP6).
- A print dialog follows the canvas style (no per-dialog override until WP7).

## WP4 assumptions
- Q2: irrigation layer = `Layer.permaculture_layer == "irrigation"` (an existing serialised free string, no new
  field; no UI creates it yet). One colour #1f5fbf, 0.25 mm, 4/2 mm dash; an item on that layer of any type is irrigation.
- Q3: canopy radius = the item's visual (current) diameter / 2, not mature spread.
- Perennials/ground cover draw a faint dashed circle plus centre dot (design says no canopy) so they stay findable.
- Lawn 4 % and bed 6 % grey fills; hardscape/furniture/other shapes: white fill, ink outline. Water gets the 10 % tint.
- Furniture sprites, pergola, greenhouse bars and ridge-roof pictures are replaced by plain outlines in CAD; string lights keep their illustrated look.
- Pad is 0.8 mm paper for all styled items. Gravel/dirt stipple uses a crc32 seed of the style name.
- Unverified: owner visual check, timing/perf budgets, SVG/PDF in other viewers.
