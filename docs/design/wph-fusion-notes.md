# WP-H: VIO/PDR fusion

## 1. Design notes

**Frames**

- WebXR is right-handed: x right, y up, -z forward. Only the ground plane (x, z) is used, and y is ignored.
- **Correction 1: map the ground plane as (u, v) = (x, -z), not (x, z).** The pair (x, z) is a left-handed 2D frame seen from above, because x × z = -y. A yaw-only fit from (x, z) to ENU would need a reflection, and the rigid two-point fit would silently mirror the path. (x, -z) is right-handed, because x × (-z) = +y = up, which matches ENU (e × n = up).
- `vio_ground()` returns the raw (x, z). The flip lives in exactly one place, `Transform2D.apply`.
- The plan frame is the engine's ENU. Anchor corrections are already applied by the engine, so the transform maps straight into the frame of `PositionEstimate.e_m/n_m`.

**Transform**

- `e = cos(yaw)·u - sin(yaw)·v + tx` and `n = sin(yaw)·u + cos(yaw)·v + ty`.
- Scale is fixed at 1, and yaw is counter-clockwise seen from above.
- **Correction 2: `inverse` is a method `inverse(e, n) -> (x, z)`, not a `Transform2D`.** Because of the axis flip, the inverse is not the same kind of map.
- Two-point lock: yaw = bearing(plan AB) - bearing(vio AB) in (u, v). Translation is solved at the midpoints, which splits any length error symmetrically. The result is exact for consistent points.

**Reticle hit points**

- A lock point is the hit-test pose position (in the same `local` space as the viewer pose), never the viewer pose. This removes the arm offset.
- A is held as pending until B is captured. Any tracking loss between A and B discards A.
- Capture is refused unless the latest VIO sample is `tracking`.
- **Lock points are checked against the pose stream (review fix).** The phone sends `vio_t`, the time of the last pose it had queued when it took the hit, after flushing its queue. The server refuses the lock (409) when its stream has not reached `vio_t`, or when anything broke the frame (a loss, a jump, a reference-space reset) after `vio_t`: the hit would be in a frame the server no longer holds. The lock time is the server's last pose `t`, never a client clock; the old client `t` field is refused.

**Degenerate lock**

- `FusionError` is raised if any coordinate is non-finite, or if the VIO or plan baseline is under 2 m.

**Scale check**

- ratio = |VIO_AB| / |plan_AB|; ok when |ratio - 1| ≤ 0.02.
- **Correction 3: a failed scale check does not block the lock (the spec says "warn"), but the fix is never labelled `vio_fused` while the check fails.** The reason `scale_check_failed` is carried instead. Advertising a frame we have just proven inconsistent is wrong. The user re-locks or picks other points.

**Tracking loss**

- Non-`tracking` for more than 1 s (strictly) unlocks the frame, and the pending A is dropped.
- **Correction 4: these also count as loss,** because none of them is a "limited" state and all can silently move the frame:
  - a gap of more than 1 s with no VIO frames (session ended, page hidden);
  - a time reversal;
  - an implied VIO speed above 5 m/s between two `tracking` samples (reference-space reset);
  - **on recovery from limited/lost to `tracking` (review fix):** the jump is measured against the last *tracking* pose, not the limited ones, because the system may relocalise the frame while it is not tracking. A recovery more than 1 s after the last tracking pose, or limited for more than 1 s in total, also unlocks.
  - **the walking bound (round-2 fix):** on any recovery from limited/lost, and across any pose gap (two `tracking` poses more than `POSE_GAP_S` = 0.5 s apart; poses come at 10 Hz), the ground jump may be at most `WALK_SPEED_MPS * gap + WALK_SLACK_M` = 2 m/s × gap + 0.3 m, where gap is the time since the last tracking pose. A larger jump is a relocalisation and unlocks with `vio_pose_jump`. A 4.5 m jump in 0.95 s, which the 5 m/s check alone let through, now unlocks; a walk through a short blip stays locked.
  - the time of a frame break (loss, jump, reset) only ever moves forward: a non-finite sample can never lower or clear the break a reset left, so a lock point from before the reset stays refused.
- A reference-space reset resets both aligners (the GNSS-aligned fallback too), and the unlock reason of a loss is kept when A is captured again; only re-capturing A while locked reports `relock`.
- Between 0 and 1 s of limited tracking, positions are still produced but `fix_mode` is None, so the engine does not use them as the track.
- After an unlock, `on_sample` returns None until a new lock. A break is bridged only within the walking bound above; anything larger is never spliced.

**GNSS gross-error check**

- `gnss_disagreement(est, sigma)` compares the filtered GNSS position with the last locked position. It alerts when the distance is greater than 3·sigma.
- It never moves the locked frame (no pull toward GNSS).

**Fused sigma**

- `sigma_m` is the anchor sigma. `PLAN_POINT_SIGMA_M` is a labelled placeholder, not a measurement.
- `relative_sigma_m_per_10m` stays None until WP-M supplies a value.
- The plan-locked estimate has `quality` `unknown` (the phone shows no OK badge for it) and telemetry `sigma_m` null: the placeholder must never read as an accuracy.
- **Not in any sigma:** a review of the synthetic fallback runs saw the aligner's yaw keep a steady-state jitter of about 3.4° after convergence, which is about 1.2 m at 20 m from the window centroid. The fallback `sigma_m` (window residual rms, floored by the GNSS weight) does not include this term. It is a test observation, not a measured error; WP-M owns the real numbers.

**Fallback (no plan lock)**

- VIO is paired with filtered GNSS only when the two times are within 0.5 s and tracking is good.
- The window keeps whichever is smaller: 30 s or a VIO path length of 40 m.
- The fit is weighted (1/σ²) 2D Umeyama with scale 1, in closed form: yaw = atan2(Σw·cross, Σw·dot) on centred points. This has no reflection branch.
- Translation only (yaw held, or seeded from a hint) until there are ≥ 10 points and the VIO diameter is ≥ 8 m.
- Low-pass: α = 0.1 per GNSS update, on yaw (wrapped) and translation.
- A yaw step over 10° is rejected unless a non-`tracking` sample occurred since the last accepted fit. If one did, the candidate is taken directly.
- Any loss over 1 s resets the window.
- **Correction 5: after 5 consecutive rejections the yaw re-initialises,** flagged `yaw_reinitialised`. Otherwise a wrong first yaw would be locked in forever.
- **Correction 6: "20 s drag" vs α = 0.1 are inconsistent as written.** At 1 Hz, α = 0.1 per update is a time constant of about 9.5 s. I implemented α per update; the constant is tunable, and the replay harness or WP-M should settle it.
- Note on the gating wording: a long straight walk does constrain yaw. It is the short walk that does not. The 8 m / 10-point gate covers both cases, so it is kept as specified.
- Fallback output never carries `vio_fused` (fix_mode stays None).

**PDR**

- |a| is low-passed (τ = 0.05 s). A slow mean (τ = 2 s) is subtracted, so gravity included or not does not matter.
- A step is a crossing above +1.0 m/s², with re-arm below 0 and a 0.3 s refractory.
- Step length is 0.7 m. Heading comes only from an explicit argument or an absolute `ImuSample.heading_deg` (per §2.2).
- With no heading, the step is counted but its displacement is 0, with reason `no_heading`.
- Every step is labelled "motion-smoothed". PDR only feeds the prediction, never a measurement.

**Time base**

- **Correction 7: VIO `t` must be on the same clock as GNSS `t`.** On the phone this means `performance.timeOrigin + XRFrame time`, converted to seconds. Otherwise pairing and gap rules are meaningless.
- **Review fix:** every time the page sends that the server compares (GNSS, IMU, resets, lock `vio_t`) uses the same timeline, `performance.timeOrigin + performance.now()` (`clockNow()` in `api.js`); GNSS no longer uses the geolocation timestamp. Sample times are bounded (finite, 0 to 2100-01-01) and so are positions, quaternions and accelerations.
- **Freshness:** the lock is reported `plan_locked` only while the last VIO fix is at most 1 s older than the request's newest sample and the server has heard a pose batch in the last 3 s (a backgrounded phone sends nothing).
- **Recording:** GNSS and VIO arrive in separate requests, so at finish the recording is re-segmented: GNSS steps inside any VIO span (first fix to last fix + 1 s) are dropped whatever the arrival order, the rest is ordered by time, and nothing is smoothed across a change of source. VIO steps enter the recording at most twice a second, so `MAX_TRACK_STEPS` lasts hours rather than about 33 minutes.

No accuracy claim is made anywhere; all sigma values are internal spreads or placeholders.

## 4. Integration spec (for the Sonnet coder)

**Engine (`engine.py`)**

1. In `__init__`, add:
   - `self._locked = PlanLockedAligner()`, `self._fallback = FusionAligner()`, `self._pdr = PdrStepper()`;
   - `self._vio_fix: FusedFix | None = None`, `self._last_vio_t: float | None = None`.
2. `add_vio(v) -> PositionEstimate | None`:
   - Keep `latest_vio`. Call `fix = self._locked.on_sample(v)`, then also `self._fallback.on_vio(v)`.
   - If `fix` is not None and `fix.fix_mode is FixMode.VIO_FUSED`: store it and return `self._vio_estimate(fix)`. This is a `PositionEstimate` built from `fix.e_m`/`n_m`/`sigma_m` with `fix_mode=VIO_FUSED` (review fix: never an RTK label), `anchor_source="plan_points"`, `quality="unknown"`, and `gated=False`. While recording, append a crumb Step with `x_post = [e, n, 0, 0]` and `p = sigma²·I`.
   - Otherwise set `self._vio_fix = None` and return None.
3. `add_gnss` runs the Kalman exactly as today; the GNSS track is never modified.
   - When the filter accepts a fix while `self._locked.state` is LOCKED or LOCKED_LIMITED: call `self._locked.gnss_disagreement((kf.x[0], kf.x[1]), kf.sigma_m)` and put the alert in telemetry. The returned estimate is the Kalman one, flagged `track_source="gnss"`. The published puck and crumbs come from the VIO estimate only while `_vio_fix` is younger than 1 s.
   - When not locked: `self._fallback.add_gnss(s.t, e, n, r)`. Its output goes to telemetry as `fused` only. It never replaces the track and never sets `fix_mode`.
4. `_fix_mode(t)`:
   - The RTK checks stay first. If RTK is inferred, the Kalman track is also the source.
   - (Review fix: `VIO_FUSED` is no longer inferred here for GNSS estimates; the VIO estimate carries it.)
   - Then the anchor/standard checks unchanged. A reported `fix_quality` still overrides everything (§2.9).
5. `add_imu`:
   - Also call `step = self._pdr.update(m, heading)`, where `heading` is the compass heading only if it is absolute and ≤ 2 s old.
   - If there has been no VIO in the last 2 s and `step` has a heading: queue `(step.de, step.dn)`. The next `KalmanCV.predict` call consumes it as the displacement prior instead of constant velocity (new method `predict_displacement(t, de, dn, sigma_a=1.5)`), and the estimate gets `motion_model="motion-smoothed"`. GNSS updates are unchanged.
6. New engine methods:
   - `ar_capture_a(hit, plan_en, vio_t)`, `ar_capture_b(hit, plan_en, vio_t)`, `ar_unlock()`, `ar_frame_reset(t)`, `ar_closure(hit, plan_en, vio_t)`;
   - `ar_status() -> dict`: state, scale `{ratio, ok, message}`, `closure_m`, `gnss_alert`, `unlock_reason`, `lock_t`, `sigma_m`, `relative_sigma_m_per_10m: null`.
   - `FusionError` maps to HTTP 409 with the message.

**`POST /position`**

- Parse `vio` as `{t, x, y, z, qx, qy, qz, qw, tracking: "tracking"|"limited"|"lost", reset?: bool}`.
  - Reject non-finite values with 422.
  - If `reset` is true, call `ar_frame_reset(t)` before `add_vio` (both aligners forget the frame).
  - The `t` of `vio` and `imu` must be epoch seconds on the same clock as the GNSS `t`.
- `imu` maps to `ImuSample(t, ax, ay, az, alpha, beta, gamma, heading_deg, absolute)`.
- Order: `add_vio`, then `add_imu`, then `add_gnss`.
- Add a batch field `vio_batch: list[vio]` (max 50) so VIO can be sent at 5–10 Hz without GNSS.
- Telemetry gains `fused: {state, source, fix_mode, reasons, scale, closure_m, gnss_alert}`.

**Session API (same auth as `/position`)**

- `POST /ar/lock/a`
  - Body: `{plan:{x_cm,y_cm}, hit:{x,y,z}, vio_t}` (as built; see the lock-point rule above).
  - Response: `{state}`.
- `POST /ar/lock/b`
  - Body: same as lock A.
  - Response: `{state, scale:{ratio, ok, message}}`.
- `POST /ar/check`
  - Body: `{plan_point_id, hit}`.
  - Response: `{closure_m, label:"closure at C"}`.
- `POST /ar/unlock`
- `GET /ar/status`
- Plan points are resolved server-side to plan ENU.

**`static/xr.js` contract**

- Probe `navigator.xr?.isSessionSupported('immersive-ar')`. Only when true, show "AR lock (experimental)".
- Never start a session without a user tap.
- Request the session with `requiredFeatures: ['hit-test','local']` and `optionalFeatures: ['dom-overlay']`, with `domOverlay: {root: mapInset}`. The map becomes a small inset.
- Before the tap, show this permission text: "Uses the camera to track movement; no images leave the phone".
- Camera denial: `NotAllowedError` from `requestSession`. Show "camera denied: GNSS/step mode" and continue with the PDR path.
- Every frame:
  - Read the pose from `frame.getViewerPose(localSpace)`. A null pose means tracking `lost`; `emulatedPosition` true means `limited`; otherwise `tracking`.
  - `t = (performance.timeOrigin + frame time) / 1000`.
- Reticle:
  - Get a hit-test source from the `viewer` space.
  - Read `frame.getHitTestResults(src)[0]?.getPose(localSpace).transform.position`. Lock A/B/C send this hit, never the viewer pose.
  - The lock button is disabled when there is no hit or tracking is not `tracking`.
- Listen for the `reset` event on `localSpace`: send `reset: true`.
- On session `end`, page hidden or a server state of `unlocked`: show a persistent "unlocked — re-lock A and B" banner. Never resume the old lock.

**Chrome WebXR pitfalls (all need verification on the device)**

- **Reference space:** `local-floor` may be unsupported in AR, so use `local` (y is unused anyway). Request it after the session starts.
- **Hit-test source:** `requestHitTestSource({space: viewerSpace})` needs `hit-test` in the features. Results may be empty on featureless ground, and the hit may lag plane detection.
- **No explicit tracking-state enum is exposed to JS:** `emulatedPosition` and a null pose are proxies, to be verified.
- **Session end and backgrounding:**
  - The session ends when the tab is backgrounded or the screen locks.
  - `watchPosition` may throttle or keep running during immersive-ar.
  - DeviceMotion may stop.
- **Restricted context:**
  - immersive-ar requires HTTPS, so the self-signed LAN certificate must be accepted first.
  - The DOM overlay is the only UI available, and `alert()` and prompts do not show.

## 5. Risks

- **Tracking-state proxies:** `emulatedPosition`/null pose may not catch drift or a silent relocalisation. The 5 m/s jump rule is a heuristic.
- **Clock alignment:** pairing and the GNSS alert depend on the XRFrame-to-epoch clock conversion.
- **Hit-test on grass:** hit-test may return no plane, or a plane under the grass. The arm-offset removal is unverified.
- **Plan-point uncertainty:** `PLAN_POINT_SIGMA_M`, the 2 % scale tolerance and the fallback α are placeholders. No accuracy is implied (WP-M).
- **Fallback drift:** the yaw can creep below 10° per update. The 5-reject re-initialisation can accept a wrong yaw.
- **PDR:** the threshold and cadence are untested on a real gait and phone pocket placement. The compass heading near metal is unreliable.

Only a real phone can verify:
- WebXR availability and permissions;
- the frame rate, and battery/thermal throttling over a garden walk;
- whether geolocation keeps running in AR;
- `reset` events;
- whether `emulatedPosition` is meaningful in Chrome.