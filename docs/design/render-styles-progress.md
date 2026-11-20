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
  `PathStylePaint(owner=)`, `CanopySpec`, `drawBackground` uses the style paper. Callouts are WP5.

- WP5 (CAD callouts): `ui/canvas/style/callouts.py` (layout + `StyleCalloutLayer`), hooked through
  `CadPainter.build_state` and `CanvasScene.ensure_style_state`/`set_cad_callouts`; off by default;
  tests in `tests/unit/style/test_cad_callouts.py` and `tests/integration/test_cad_callouts.py`; docs §8.38.1.

- WP6 (Line & Wash 2D): `ui/canvas/style/line_wash.py` registered in `STYLE_PAINTERS`; paper grain via
  `drawBackground`; `styled_label_font` + `HaloTextItem._label_font`; tests `tests/unit/style/test_line_wash_style.py`,
  `tests/integration/test_line_wash_render_style.py`. No callouts here (owner decision).

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
- Agent (WP11). WP9 (3D Line & Wash) is done: see the WP9 section.
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

## Assumptions (WP6)
- Wobble, scallop pitch and canopy masses use the 96-dpi paper-mm conversion of `geometry.py`
  (a `StyleState` is keyed without dpi, so it cannot depend on it); line weights, misregistration
  and ring widths are dpi aware. A 300-dpi export therefore has the screen's wobble in plan cm at
  the same zoom, with heavier ink.
- Wash colour is the item's `fill_color` run through `pigment`; buildings are always cool grey.
- The shadow crescent assumes the plan is painted y-up (light from the upper left on screen).
- Vector detection is by paint engine type (SVG, Pdf, Windows print, and the SVG exporter's
  recording engine `User`); everything else counts as raster.
- `scene_rendering.paint_style_paper` stays a no-op seam: the grain is painted by `drawBackground`,
  which `scene.render` also calls.
- Label hand changes only the font (halo colours unchanged); no `label_pen` yet.
- Roof ridge lines, lawn grass ticks and Paper Space specifics are not drawn (Paper Space viewports follow the scene style).

## WP9: 3D Line & Wash (NprStyle)
Done:
- `core/scene3d/look.py`: `NprSpec` (one table: effect values and the look overrides),
  `LINE_AND_WASH_NPR`, `LookSpec.npr` / `.unstyled`, `npr_for`, `styled_look` (plain styles
  return the same object; a styled look goes back to its plain look).
- `qml/NprStyle.qml` + `npr_edges.frag`, `npr_wash.frag`, `npr_compose.frag` (runtime-compiled
  like PhotoBlur's; the ogp.spec data loop ships the whole qml folder, no .qsb step exists).
- `Garden.qml`: `nprStyle`/`nprActive` (never in `topDown`), the effect is the ONLY user effect
  while drawn, PhotoBlur stands down (its DOF is folded into the wash pass), SSR/glow/vignette off,
  tone mapping None at exposure 1, shadow softening gated by `nprActive`; ground.frag
  `uStyleFlatten` on all three ground materials (guarded branch: 0 is the old shader exactly).
- Wiring: `LookController(render_style=...)`, `set_render_style`, `from_settings` reads the
  preference; workspace leaves lawn blades out under the style; `Quick3DHost.capture(render_style=)`
  (the view's look put back after); `render_records(render_style=)` (ignored for a TopDownShot).
- Tests: `tests/unit/test_render_style_3d_look.py` (17), render tier
  `tests/integration/test_render_style_3d.py` (4, driver scenario `npr`): truth view identical,
  Illustrated == default, deterministic, paper sky and ink on the boxes' outlines.

Measured (this machine: Intel Iris Xe, Direct3D 11, Qt 6.12, 2026-11-19):
- Default plain 3D is byte-identical to the branch tip before WP9: the Encanto plan (copy),
  1600x900 at DPR 1.5, compared pixel for pixel against a render from the stashed tree.
- Effect cost, shown host 1280x720 logical (1920x1080 device), Encanto, median of 30 grabs:
  plain 6.09 / 6.01 ms, Line & Wash 7.06 / 6.97 ms: about +1 ms (+16 %) per frame.
  One-shot offscreen frames (open to readback): 7.0-9.2 s both ways, dominated by setup.
- Edges: second difference of 1/depth (zero on planes), silhouettes on the near side only;
  creases at 0.55 ink; ink widened by a 3x3 max (`ink_spread`); crowded 3x3 (foliage) fades ink.
- Pre-existing, not WP9: `test_at_rest_a_vertex_with_full_wind_weight...` fails on this Iris Xe
  at HEAD too (foliage offset exactly 0.5 px against `< 0.5`).

Look pass (owner review of the Encanto render: flat lawn, tile/brick texture, speckled tree
ink, no ink on window frames):
- Effect is now five passes: edges, `npr_mass.frag` (depth dilated over `mass_share`, busy
  share per block, half size), `npr_close.frag` (eroded back: the depth CLOSED), wash, compose.
- Foliage: pass 1 marks leaf-size jumps (`busy_thresh`, near side only). Where a block is busy
  the leaves draw no ink of their own, their wash is averaged (`foliage_soften_share`), and the
  closed depth's outline is drawn instead, only where what lies beyond it is not foliage
  (otherwise a crown's back layers drew comb-like ink in a street-level view).
- Windows: a "step" class (`step_thresh` 0.0003, frame 3 cm proud = 0.001 at 30 m), inked only
  where the scene colour changes too (`step_contrast`). Without the colour gate the depth
  buffer's quantisation drew whole rows of ink across the drive (round 1, measured).
- Surfaces: `built_detail.frag` `uStyleFlatten` (`surface_flatten`) fades roof tiles, courses,
  plaster and their relief behind a guarded branch; `ground.frag` softens the tile picture by a
  mip bias (`ground_soften`) and lays a world-anchored uneven wash (`ground_wash`,
  `ground_wash_cm`), both inside the existing flatten guard.
- Wash: 5 bands with a softer step, a dried rim along a band step on smooth surfaces
  (`band_rim`), screen-anchored blooms (`wash_mottle`) and clumped granulation.
- Default plain 3D still byte-identical (Encanto copy, high and street-level views, against a
  `git archive` of 8ae38bd3). Truth view identical (render tier). New render-tier test: no ink
  rows on flat ground. Cost: the shown-host grab probe (median of 30, interleaved runs) reads
  plain 6.1-7.6 ms and Line & Wash 6.0-7.3 ms, old and new effect alike: the probe cannot
  resolve the added passes (under 1 ms here).
- Still weak: the crown outline is broken in places and slightly stair-stepped (half-size
  closing); stray leaves outside a crown still draw small dashes; near leaves in a street-level
  view keep one outline each; the hedge's dense dark texture is untouched; tonal bands remain
  subtle on evenly lit lawn (the uneven wash carries most of the variation); grain, blooms and
  granulation are screen-anchored (paper under a camera) and will not move with the scene.

Assumptions:
- Tone mapping: the design said "as illustrated"; the effect does its own exposure and ACES-fit
  curve and writes sRGB with the environment at None, so paper and ink are exact colours.
- Paper is the 2D paper `#f4eedf`; ink `#2a241d`. Bands are cut in display lightness (gamma 2.2).
- Shadow softening (factor x0.75, PCF x2) is applied in the scene only while the effect is drawn,
  not in the look, so the truth view's shadows (and the IoU gate) are the plain look's.
- CAD stays plain 3D (Q6). The Agent API `render_3d_view` takes no `render_style` yet (WP11).
- Live switching: done in WP7 (`Workspace.set_render_style` -> `LookController.set_render_style`).

Look, honestly (4 rounds, then stopped): ink silhouettes and roof creases read well; the sky and
far ground are paper with a soft ragged edge. Weak: tonal bands are hard to see (large flat
lawns read as a flat digital green, not a wash); roof tiles and wall bricks keep their texture;
foliage edges are speckled ink rather than a drawn outline; window frames get no ink (too
shallow for the crease threshold). Not judged: other moods (golden hour, night), video flicker,
WARP/llvmpipe, MSAA-resolved depth beyond this machine.

## WP5 assumptions
- Callout text is the common name (owner), else the object name, else Tree / Shrub / Plant; capped at 28 characters.
- Plants are the three plant roles (tree, shrub, ground); a plant that finds no free slot in 3 rings gets no callout.
- Boxes are reserved from a paper-mm text model (2.1 mm per character, 3.8 mm line), a generous over-estimate, so overlaps
  cannot appear with another font or an export's retargeted size; the text itself is 8 pt, retargeted in exports.
- Callouts follow the CAD paint revision only: a plant whose name changes shows the new name after the next rebuild.
- Unverified: the owner's visual check of the placement; font rendering (the offscreen sample has no fonts, text shows as boxes).

## WP7: UI and live switching
Done: `ui/render_style_controller.py` (one controller; View > Render Style radio group + Leader callouts,
toolbar combo in `ui/widgets/render_style_toolbar.py`, 3D pane button), `ui/widgets/export_style_group.py` (Style combo
with "Same as canvas", fidelity note, SVG/PDF grain opt-in) in the PNG/SVG (new `ExportSvgDialog`)/PDF report/print
dialogs, `PdfReportOptions.render_style`, `GardenPlannerApp` wiring, settings key `export/paper_texture_in_vector`,
wiki page `docs/wiki/Render-Styles.md`, FR-RS-01..05, glossary, translations (de/en/es/ru).
Tests: `tests/unit/test_render_style_ui.py`, `tests/integration/test_render_style_ui.py`.
Assumptions: the Leader callouts toggle only stores the preference (no callout layer until WP5); the PDF report
places plan pages as pictures, so its grain checkbox has no effect there; the toolbar combo is its own toolbar.
Unverified: no manual click-through; the real Quick3D host (the integration test uses the recording FakeHost).
