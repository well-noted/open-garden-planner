# NprStyle 3D spike: findings (measured, Direct3D11 on the dev box)

Throwaway harness in this folder (`run.py` renders `scene.qml` with `NprSpike.qml`,
two passes: edges into an RGBA8 buffer, then compose). Not wired into production.

Measured:
- A user Effect can read `DEPTH_TEXTURE` and `CAMERA_PROPERTIES`; a two-pass Effect with a
  `Buffer` + `BufferInput` compiles and renders (Qt 6.12, D3D11 RHI).
- Depth and INPUT share orientation (sky in the top rows for both): no Y flip when both are
  sampled with `INPUT_UV`.
- Depth decodes with the same GL-style `2*d-1` formula `PhotoBlur` already uses.
- Works with MSAA High selected. (Caveat: the NoAA run gave identical numbers, so the AA
  switch in the harness may not have taken effect; MSAA-resolved depth is not proven.)
- Resolution independence: ink width as share of picture height; edge pixels 0.69 % at
  640x360 and 0.47 % at 1920x1080 (silhouettes only), looks right in the 1080p image.
- Log-luminance quantisation gives clean tonal bands; sky/ground stay flat.

Design correction (important): the Sobel on log depth in the design doc fires on every
sloped surface (65 % of pixels were "edge" on a plain ground plane, because a plane has a
constant depth gradient). Use the SECOND difference of log depth
(`|z(+d) + z(-d) - 2 z0|`, max over the 4 directions): 0.5-0.7 % edge pixels.

Not measured: normal-texture availability, WARP/Mesa backends, Iris Xe cost, foliage and
instanced-grass ink noise, the 2-effect chain with SSR, folding DOF in, video flicker.
Verdict: basic feasibility passes; production wiring (WP9) still needs the owner's machine.
