# Render styles

A render style is how the plan is drawn. It is a view preference: switching it
never changes the plan, is not saved in the `.ogp` file, adds no undo step and
does not mark the plan modified.

| Style | Look |
|---|---|
| Illustrated | The full-colour textured plan (the default). |
| Architectural CAD | Black ink on white paper, hatched buildings and roofs, exact line weights, grey beds and lawns, dashed irrigation. In 3D it stays the plain 3D view. |
| Line & Wash | Hand-drawn ink lines with soft watercolour washes on grained paper, in 2D and in 3D (ink silhouettes and creases over banded washes). |

## Switching style

- **View menu, Render Style:** a radio group with the three styles, and the
  **Leader callouts** toggle. The toggle works only in Architectural CAD and is
  greyed out otherwise.
- **Style toolbar:** a combo box next to the other toolbars.
- **3D pane:** a Render Style button in the strip above the 3D view.

All three show the same choice. The running 2D canvas and the open 3D view change
at once, and the choice is remembered for the next start.

## Exporting

The PNG, SVG, PDF report and print dialogs have a **Style** group. The default,
"Same as canvas", draws the output in the style the canvas shows; the other
entries override it for that output only. The last choice is remembered.

### SVG and PDF fidelity

| | SVG | PDF |
|---|---|---|
| Architectural CAD | Fully vector: hatching as clipped line segments, dashes, ink. | Fully vector. |
| Line & Wash ink | Vector; the hand wobble is baked into the geometry, so it matches the PNG. | Vector. |
| Washes and edge rings | Vector, laid over each other with transparency (overlaps read flatter than in the PNG). | Vector, with transparency. |
| Granulation and paper grain | Left out by default. The checkbox "Include paper texture (raster)" embeds one grain image under the vector drawing. | Opt-in, an embedded raster. |
| Brush-width variation | Not drawn. | Not drawn. |

A Line & Wash SVG or PDF is therefore an approximation of the PNG, and the dialog
says so: "SVG/PDF: washes without paper texture or glazing". PNG output and print
keep everything the screen shows. The PDF report places the plan pages as pictures.

## Limits

- The Paper Space viewport follows the style of the canvas.
- Line & Wash in 3D costs about one extra millisecond per frame on the reference
  laptop; the style changes the look only, never the geometry.
- A live procedural wobble (SVG filters) cannot be written: the wobble in a vector
  file is fixed geometry.

For the decisions behind this page see the
[architecture documentation](https://github.com/cofade/open-garden-planner/blob/master/docs/05-building-block-view/README.md).
