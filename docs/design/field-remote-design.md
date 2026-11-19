# Field Remote and positioning: design

Status: proposal (design only, no production code), revision 2. Builds on ADR-079 (georeferencing) and
ADR-080 (field records sidecar). Becomes ADR-081 plus FR-GEO entries when the first package lands.

## 0. Ground truth

- The Agent API is an MCP server on a daemon thread (`agent_api/server.py`: uvicorn + starlette,
  loopback only, DNS-rebinding protection, token from header or `?token=` via `_bearer_token_middleware`,
  `secrets.compare_digest`, write tools gated by the Preferences toggle plus the token). Main-thread work
  goes through `agent_api/bridge.py` (`run_on_main`) and `agent_api/providers.py`.
- `services/field_records.py` owns `<plan>.fieldnotes.json` and `<plan>.fieldnotes/photos/`; its lock is an
  in-process `threading.RLock`; writes are atomic.
- `core/georef.py` has `GeoReference.gps_to_canvas/canvas_to_gps` and `fit_from_reference_points`. Scene is
  Y-up, centimetres; `rotation_deg == north_angle_deg`.
- `cryptography`, `uvicorn`, `starlette`, `numpy` are in `uv.lock` (`cryptography` transitively). Importing
  `cryptography` directly means declaring it in `pyproject.toml` and passing the licence check.

## 1. Server architecture

### 1.1 Shape

```
Field Remote (new, off by default)             Agent API (unchanged)
 https://<bind>:<port>/  (phone)                http://127.0.0.1:8765/mcp (agent)
        +-----------> FieldRemoteHost (QObject, GUI process) <---+
                       - reuses AgentBridge.run_on_main and the write providers / command stack
                       - is the ONLY writer of the fieldnotes sidecar
```

- New package `field_remote/` (`server.py`, `auth.py`, `tls.py`, `limits.py`, `routes.py`, `static/`),
  started by `app/field_remote_host.py` beside `app/agent_host.py`. Own port (default 8766), own token, own
  daemon thread, same start/stop/port-fallback pattern. Never mounted on the Agent API app, whose loopback
  threat model stays unchanged.
- Preferences "Field Remote": enable (default off), bind, port, "allow the phone to edit the plan" (default
  off, separate from the agent-edit toggle), pairing QR, regenerate token/certificate. A running server
  shows a status-bar badge (bind address, connected phones).

### 1.2 Bind

Off (default) | this computer only (127.0.0.1) | local network (one chosen interface address; the wildcard
only after an explicit warning) | Tailscale (the `100.x` interface, if present). Interface detection is
advisory; the user can type an address. A specific address, not `0.0.0.0`, so café Wi-Fi does not expose it.

### 1.3 TLS (decided: HTTPS)

Chrome exposes Geolocation, DeviceMotion/Orientation and WebXR only in secure contexts.

- **Self-signed (default).** `field_remote/tls.py` generates an EC P-256 key and a cert valid 397 days
  (SAN: bind IPs, `localhost`, machine name) in the per-user data dir, key file ACL'd to the user;
  regenerated on bind change or on demand. The SHA-256 fingerprint is shown in Preferences and the QR dialog.
- **Android Chrome UX.** Full-page warning; "Advanced" then "Proceed" once per cert; Chrome then grants
  secure-context APIs. The pairing dialog asks the user to compare the fingerprint once.
- **What a self-signed cert cannot do (corrected).** Chrome refuses service-worker registration on an
  untrusted-certificate origin (`SecurityError`), so **no offline page reload and no PWA install** on the
  default path. IndexedDB, Geolocation, motion sensors, Wake Lock and a kept-open tab still work (§4.4).
  Offline reload needs a trusted cert: Tailscale `*.ts.net` (the user runs `tailscale cert` and points
  Preferences at the files; we never run `tailscale`) or a user-installed CA. A user CA is a larger trust
  grant than needed and Android needs a screen lock for it; offered only as an explicit opt-in with that
  warning.

### 1.4 Pairing and auth

- Token `secrets.token_urlsafe(32)` kept in the existing secret store (separate key; not hashed because
  the QR must be shown again). QR encodes `https://<ip>:<port>/#token=<token>`; a fragment is never sent in a
  request, so it stays out of logs and Referer. `app.js` reads `location.hash`, strips it with
  `history.replaceState`, keeps the token in memory and `sessionStorage` (not `localStorage`), and sends
  `Authorization: Bearer`. Header only: no `?token=` form here. Compare with `secrets.compare_digest` plus
  an ASCII guard.
- Streams: prefer fetch-streaming (can set headers) over `EventSource`; decide in WP-C on the phone.
- QR drawing: small vendored MIT encoder or `qrcode` (BSD); decide in WP-A.

### 1.5 Threat model

| Threat | Mitigation | Residual |
|---|---|---|
| LAN device guesses token | 256-bit token, constant-time compare, 250 ms failure delay, per-IP lockout (10 failures, 5 min), one-interface bind | Port is discoverable |
| QR photographed / token leaked | Regenerate button; QR shown on demand, hidden after 2 min; fragment keeps it out of logs | Whoever sees the QR while shown |
| Token in history/Referer | Fragment stripped; `Referrer-Policy: no-referrer`; no external resources | none found |
| CSRF / DNS rebinding | No cookie auth, so a cross-site request carries no credential; writes need the Authorization header (cross-origin cannot set it, no CORS headers sent); strict `Host` check as in `agent_api/server.py` | none expected |
| LAN MITM | TLS; fingerprint check at pairing; Tailscale cert path | user clicks through for a wrong host |
| XSS in served page | Same-origin only; CSP `default-src 'self'; img-src 'self' blob: data:; connect-src 'self'`; no inline script; user text via `textContent` | none expected |
| Hostile uploads | Body caps 12 MB photo / 256 KB other (Content-Length and while streaming); `field_records.sniff_photo` (Pillow decode, rename by store); allowed types jpeg/png/webp; Pillow default pixel limit | CPU within limits |
| Flooding | Token bucket per token (reads 20/s, writes 5/s), one photo upload and one position stream at a time, max 4 phones | paired phone wastes some CPU |
| Writes without consent | Writes need the phone-edit toggle AND the token; toggle off gives 403 and a view-only UI | none |
| Path traversal | No route takes a path; photos by record id `^[A-Za-z0-9_-]{1,64}$`; static files from an in-memory dict | none |
| Plan leakage | Phone JSON is a projection (objects, layers, species, georef); no file paths or tokens | phone sees the plan (intended) |

Per-phone tokens, fingerprint pinning and mTLS are out of scope for v1 (open question 3).

### 1.6 Endpoints (`/api`, JSON, all need the header)

Reads: `GET /state` (plan name, georef summary, layers with locked/visible, `writes_enabled`, capabilities,
server time; ETag, polled every 5 s); `GET /scene?bbox=&layers=` (simplified geometry, max 5000 objects);
`GET /basemap` (background downscaled to 2048 px JPEG plus its canvas rectangle, ETag); `GET /species?q=`
(bundled plus custom, 25 rows); `GET /field-records`, `GET /field-records/{id}/photo`; `GET /stream`
(desktop-to-phone events); `GET /strings` (UI strings from desktop `tr()`).

Position intake (changes nothing on disk, allowed with writes off): `POST /position` (single or batch of at
most 50: `t, lat, lon, accuracy_m, alt, heading_deg, speed_mps, vio?, imu?`) returns telemetry (§2.9).

Writes (need `writes_enabled`): `POST /field-records` (multipart `meta` + `photo`; sidecar only, no undo step,
like `ingest_site_photo`); `POST /place` (`species_key|object_type, x,y | lat,lon, field_record_id?`);
`POST /irrigation-path` (`points`, `diameter_mm?, flow_lph?, emitter_spacing_cm?, name?`);
`POST /georef/reference-points` (same code as `set_georeference_from_reference_points`); `POST /undo`.

Every write carries a client `request_id`; the server keeps the last 200 results and replays one for a retry.
This is what makes the outbox (§4.4) safe.

### 1.7 Phone writes on the Qt thread

The server thread validates (pure checks), then `bridge.run_on_main(fn)` calls a provider that builds the same
`Command` the GUI would:
- Place: reuse `_do_agent_create_object` (the chokepoint `place_geotagged_feature` uses). Locked/unknown layer
  gives 409 with the message.
- Irrigation: a new `EnsureLayerProvider` (find "Irrigation" case-insensitively, else create it) so layer
  creation plus polyline is **one macro, one undo**. An existing locked layer is refused, never unlocked.
- Georef: the Package 1 undoable command.
- Commands carry `origin="field_remote"`; the status bar says "Phone: added X". `/undo` pops only a phone
  command that is still on top, else 409.
- Modal dialog open: `run_on_main` times out, route returns 503 `busy`, phone retries with the same
  `request_id`. Sidecar writes need a saved plan: unsaved gives 409 `plan_not_saved`.

### 1.8 Single writer for the sidecar (recommendation)

Run Field Remote **in the GUI process** (option c of ADR-080). The Agent API's `ingest_site_photo` and the
phone routes are then two threads of one process under one `RLock` with atomic replace; a cross-process lock
or second process buys nothing. Rules: only `field_records` opens the sidecar; the server is never started
from a second process (integration test asserts `os.getpid()` equality); two app instances on one plan are
out of scope (atomic replace avoids corruption, last writer wins); a test adds 8 threads x 50 records and
loses none.

## 2. Positioning core: `core/positioning/` (pure Python, numpy allowed)

No Qt, no network, deterministic; time comes from samples. Work in local ENU metres around the georef
origin; convert to canvas once at the edge (`georef.gps_to_canvas` after the anchor transform).
Modules: `types.py`, `kalman.py`, `gating.py`, `zupt.py`, `breadcrumbs.py`, `survey.py`, `calibration.py`,
`trilateration.py`, `closure.py`, `fusion.py`, `engine.py`.

### 2.1 What is realistic (honest)

Two different questions, kept apart everywhere in code, UI and docs:
**absolute WGS84** (where on Earth) and **plan-local relative** (how far apart two things are on the plan, how
straight a pipe is). For a garden plan traced at 30-60 cm from imagery, plan-local relative accuracy is what
matters; absolute WGS84 matters little.

- Android Chrome `watchPosition` (high accuracy): about 1 Hz; reported accuracy typically 3-10 m open sky,
  10-30 m near the stucco house or under canopy; dual-frequency phones often 1-4 m open sky. No HDOP,
  satellite count or fix type.
- Absolute position stays at GNSS or anchor accuracy. We make **no sub-decimeter absolute claim**.
  `standard`: 2-10 m. `anchor_calibrated`: a 1-point anchor removes slowly varying bias (bias wanders over
  minutes); 2-4 points also fix satellite-image rotation/scale; 0.5-2 m best case.
- **Plan-local VIO is a hypothesis, not a result.** If 2 known plan points pin yaw and translation (§2.8),
  relative positions might be good to a few centimetres over tens of metres. Unmeasured. Expected failure
  modes: ARCore VIO drift commonly around 1 % of path length; hit-test error on uneven ground; low-texture
  gravel/lawn and mixed lighting (tracking degrades or is lost); arm-offset between phone and ground;
  Chrome WebXR availability and behaviour on this phone; heavy battery use and a screen taken over by AR.
  **No accuracy figure for any mode may appear in the UI, README, PRIVACY or wiki until WP-M (the on-device
  measurement protocol) has produced an error distribution.** Until then the UI shows reported sigma only.
- Tape/laser trilateration (§2.6) needs no GPS or camera and is limited only by the measurement and the
  quality of the plan points; its residual is a computed, honest number.
- `rtk_float|rtk_fixed` only if the phone's own RTK app feeds Mock Location. Chrome gives no fix type, so the
  mode is **inferred** from sustained reported accuracy (<0.5 m, <0.05 m) and labelled so.

### 2.2 Types

```python
class FixMode(StrEnum):
    STANDARD="standard"; ANCHOR_CALIBRATED="anchor_calibrated"; VIO_FUSED="vio_fused"
    RTK_FLOAT="rtk_float"; RTK_FIXED="rtk_fixed"
# fix_mode is the public enum; "plan_locked" VIO reports vio_fused with anchor_source="plan_points".

@dataclass(frozen=True) class GnssSample: t, lat, lon, accuracy_m, alt|None, heading_deg|None, speed_mps|None
@dataclass(frozen=True) class VioSample:  t, x, y, z, qx, qy, qz, qw, tracking: "tracking"|"limited"|"lost"
@dataclass(frozen=True) class ImuSample:  t, ax, ay, az, alpha, beta, gamma, heading_deg|None, absolute: bool
@dataclass(frozen=True) class PositionEstimate:
    t, e_m, n_m, x_cm, y_cm, lat, lon, sigma_m, heading_deg|None, speed_mps,
    fix_mode, stationary, quality, gated
```

**Heading (corrected).** `webkitCompassHeading` is iOS-only; it does not exist on Android. On Android Chrome
listen to **`deviceorientationabsolute`** (not `deviceorientation`, whose `alpha` is relative to an arbitrary
start). Compass heading is derived from `alpha/beta/gamma` as the rotation of the device's back-camera
direction projected on the ground plane, then **compensated for `screen.orientation.angle`** (0/90/180/270).
`ImuSample.absolute` records whether the source was absolute. If `deviceorientationabsolute` never fires
(some devices), heading falls back to GNSS course over ground when moving faster than 0.5 m/s, else the
compass cone is hidden.

### 2.3 Kalman filter (`kalman.py`)

State `[e, n, ve, vn]`, covariance 4x4, constant velocity. `dt` from sample times, clamped [0.05, 5] s; a
longer gap re-seeds velocity, keeps position.
- Transition `F` standard; process noise `Q = sigma_a^2 x` white-noise-acceleration blocks
  (`dt^4/4, dt^3/2, dt^2`). `sigma_a = 0.8 m/s^2` walking (gardener stops/turns; pedestrian accelerations are
  of order 0.5-1.5), `0.15` when stationary. Constants in `types.py`, tuned in the replay harness.
- Measurement `[e, n]`, `R = r^2 I`, `r = max(accuracy_m, r_min)`, `r_min = 1.5 m` (reported accuracy is
  optimistic under correlated error); inflate `r` x2 when the reported accuracy rose more than 3x in 1 s.
- `P0 = diag(r0^2, r0^2, 2^2, 2^2)`; Joseph-form update; `sigma_m = sqrt(trace(P[:2,:2])/2)`.
- Forward filter live; an RTS smoother over a finished recording only.

### 2.4 Gating, ZUPT, breadcrumbs

**Gating:** (1) reject non-finite, `accuracy_m` <= 0 or > 50, backwards time; (2) Mahalanobis
`d2 = y^T S^-1 y > 9.21` (chi-square, 2 dof, 99 %); three consecutive rejects in 5 s mean the filter is wrong,
so re-seed from the latest sample with large `P`; (3) implied speed > 3.0 m/s rejects even if Mahalanobis
passes (large R can hide a spike). Rejected samples are flagged `gated`, kept for diagnostics.

**ZUPT:** stationary when over 3 s every reported speed < 0.3 m/s or raw displacement < `max(1 m, 1.5 x median
accuracy)`, and IMU acceleration variance < 0.05 (when present). Then apply pseudo-measurement `v=0`
(`R_v = 0.05^2`), raise nothing, freeze breadcrumbs; hysteresis of 2 samples on exit.

**Breadcrumbs:** emit when moved >= `max(0.5 m, sigma_m)`; at release: RTS smooth, **optional Bowditch
closure** (§2.7), Ramer-Douglas-Peucker with `epsilon = max(0.15 m, 0.5 x median sigma_m)`, drop vertices
< 0.3 m apart, hard cap 2000 vertices (raise epsilon until it fits). Endpoints always kept. Live preview on
the phone uses the unsimplified filtered track; only the commit is simplified.

### 2.5 Survey-In (`survey.py`) (corrected)

Stand still, collect >= 4 samples over 3-5 s (hard stop 5 s; abort if the user moves more than
`max(1.5 m, 2 x accuracy)`). Discard samples with `accuracy_m > max(2 x median, 1.5 x min)`. Weighted mean in
ENU with `w = 1/max(accuracy_m, 1)^2`.

GNSS errors at 1 Hz are strongly correlated over tens of seconds, so 3-5 samples are nearly one independent
measurement. The earlier `N_eff = N/3` was optimistic. Use **`N_eff = clamp(1.2 .. 1.5)`** (1.2 at N=4, 1.5 at
N>=8) and `sigma = max(median_accuracy / sqrt(N_eff), 0.5 m)`: averaging buys at most about 10-20 %. The UI
says "averaging reduces jitter, not systematic offset". Result carries position, sigma, `n_used`,
`n_discarded`, duration; it does not pass through the Kalman filter.

### 2.6 Calibration and trilateration (`calibration.py`, `trilateration.py`)

Chain: GNSS lat/lon, then ENU (georef), then **anchor transform**, then plan ENU.
- **1-point "Calibrate here":** at known plan point P with Survey-In estimate g, translation `t = p - g`,
  stored with its age; after 10 min the badge says "stale anchor". Session state, never persisted (it
  describes today's satellites, not the garden).
- **2-4 point:** reuse `fit_from_reference_points` (similarity; affine only with `allow_affine` and >= 3
  points; 2-point affine refused) on device-frame vs plan-frame points. Refuse if rms > 3 m or a residual
  > 2.5 x median (mis-tapped point).
- **Corner recording** updates the plan's georeference itself (persisted, one undo) through
  `/georef/reference-points`. UI wording: "fix the map" vs "fix my position today".
- **Trilateration (new, pure, no GPS or camera).** Input: >= 2 (3+ preferred) `(known plan point, measured
  distance)` pairs from a tape or laser, plus a coarse side hint for the 2-circle ambiguity. 2 points: circle
  intersection, two solutions, choose by the hint or nearest to the current estimate. 3+: 2D Gauss-Newton on
  `r_i = |p - a_i| - d_i`, weights `1/sigma_d^2` (default `sigma_d = 2 cm` + 0.2 % of distance, user-editable),
  start from the intersection of the two best-conditioned circles, <= 20 iterations; return position,
  residuals per point, rms, and the covariance `(J^T W J)^-1` (so the sigma shown is computed, and a small
  geometric dilution of precision warns when the circles are nearly collinear). Refuse when residual rms >
  `3 x sigma_d` or the geometry is degenerate (angle between bearings < 20 degrees).
- **Control network (only if it stays small).** `closure.py` may offer `solve_network(points, distances)` for a
  few known plan points with extra tape distances between them (least squares for adjusting plan points, with
  fixed-point and free-network modes). If it passes ~150 lines, defer to v2.

### 2.7 Bowditch loop closure (`closure.py`)

A recorded loop or a path that should end at a known point has a closure vector `c = p_end - p_target`.
Distribute it by cumulative path length: `p_i' = p_i - (s_i / s_total) c`. Applied after the RTS smoother and
before RDP, only when the user marks "closes on the start" or "ends at plan point". Report `|c|` and its ratio
to path length; refuse when the ratio is over 5 % (a mistake, not a drift).

### 2.8 VIO and PDR fusion (`fusion.py`)

Two alignment sources, primary first.

**Primary: plan-locked VIO (hypothesis, WebXR only).** The user stands at two known plan points A and B and
"locks" each (hit-test reticle on the ground to remove the arm-offset: the point recorded is the reticle hit,
not the phone). The two VIO positions vs the two plan positions give yaw and translation of a 2D rigid
transform (scale fixed at 1, VIO being metric). **Scale sanity check:** `|VIO_AB| / |plan_AB|` must be within
2 % of 1; else warn "plan points disagree with the measured distance" (bad plan point or tracking drift).
Positions in the AR session map to plan ENU through that transform, with **no pull toward GNSS**; a third
known point, when visited, is a free residual check, reported as "closure at C". Tracking `limited/lost` > 1 s
drops the transform to "unlocked", GNSS/PDR take over, and the user must re-lock (never splice).

**Fallback: GNSS alignment.** When there are no plan-locked anchors: sliding window (30 s or 40 m) of VIO
position vs filtered GNSS, weighted Umeyama (`1/sigma^2`), translation-only until >= 8 m extent and >= 10
points (heading is unobservable on a short or straight walk), low-pass (`alpha = 0.1`), reject a yaw jump over
10 degrees without a tracking loss. **The 20 s drag of the translation toward GNSS is applied only in this
fallback**: with plan-locked anchors, GNSS (1-10 m, correlated) would degrade a good local solution, so it
is used only as a gross-error check (alert if the locked frame and filtered GNSS disagree by > 3 x sigma).
Fused `sigma_m` reports the anchor/GNSS sigma as the absolute number plus `relative_sigma_m_per_10m` only
after WP-M supplies a measured value (null before).

**PDR fallback (no WebXR):** step detection on low-passed |a| (refractory 0.3 s), step length 0.7 m (learned
from GNSS on straight stretches), heading from §2.2. It only replaces the constant-velocity prediction
(`sigma_a = 1.5`) and never overrides GNSS; labelled "motion-smoothed", not `vio_fused`.

WebXR support is probed on the device (`navigator.xr.isSessionSupported('immersive-ar')`) and needs a tap;
the AR session takes over the screen, the map is a small inset.

### 2.9 `fix_mode` and telemetry

Precedence: `rtk_fixed` > `rtk_float` (both inferred) > `vio_fused` (tracking good and locked) >
`anchor_calibrated` (anchor < 10 min or georef fit rms < 2 m today) > `standard`. Telemetry from
`POST /position` and `/stream`, also feeding the desktop puck:

```json
{"t": 1767000000.0, "lat": 33.43, "lon": -111.74, "x_cm": 1234.5, "y_cm": -230.1, "sigma_m": 2.1,
 "horizontal_accuracy_m": 3.4, "heading_deg": 87.0, "heading_source": "gnss|compass|vio|none",
 "speed_mps": 0.9, "fix_mode": "anchor_calibrated", "anchor_source": "gnss_point|plan_points|none",
 "anchor_age_s": 144, "stationary": false, "gated": false, "quality": "fair",
 "relative_sigma_m_per_10m": null, "samples_used": 5, "samples_gated": 0}
```

`horizontal_accuracy_m` is the raw reported value; `sigma_m` the filtered, calibrated estimate. The desktop
puck is a non-saved overlay item outside the undo stack, toggled in the View menu.

## 3. NTRIP

A desktop NTRIP client cannot correct the phone's internal GNSS (corrections must reach the chip, which only
the phone's own software can do).
- **Real RTK path (documented, no code):** SW Maps or RTKGPS+ connects to the caster (RTK2go, AZCORS) and
  feeds Android Mock Location; Chrome then reports better accuracy and Field Remote labels the mode as
  inferred. The wiki gets a short how-to with the caveat that a phone antenna limits results.
- **RTCM relay/diagnostic: defer to v2.** Its legitimate uses (caster reachability and source-table check;
  forwarding RTCM to an app that accepts it, which common apps do not) are thin, while it adds a credentialed
  outbound connection and a privacy surface.
- **If built (v2):** off by default; `services/ntrip_client.py` (stdlib `socket` + `ssl`, NTRIP v1/v2,
  source table, RTCM3 frame detection, 1 MB/min cap); host/port/mountpoint/username in Preferences, password
  in the secret store, never to the phone; UI text "Diagnostic only: this does not correct your phone's
  built-in GPS"; `PRIVACY.md` section plus a `DISCLOSED` row in `tests/unit/test_privacy_inventory.py`.
  Independent of v2: WP-A must already add a `DISCLOSED["field_remote/server.py"]` row and a "Field Remote"
  PRIVACY section (listens only when enabled, on the chosen interface, sends nothing to the internet), since
  the test's loopback-only allowance does not cover a non-loopback bind.

## 4. Mobile UI

### 4.1 Constraints

Served from memory from `field_remote/static/` (`index.html`, `app.css`, `app.js` + ES modules `map.js`,
`api.js`, `position.js`, `capture.js`, `store.js`). No CDN, no framework, no build step; Android Chrome 100+;
budget 150 KB; system font; inline SVG icons. Canvas 2D map (basemap JPEG + scene polylines) with pointer
events and two-pointer pinch. Targets >= 48 px, high contrast, one-handed bottom controls, Wake Lock while
recording. Strings from `/strings` so the translation gate still covers them.

### 4.2 Screens

1. **Map:** puck, accuracy halo (`sigma_m`, grey when poor), compass cone (heading + source; hidden if no
   heading, §2.2), fix badge (mode, sigma, anchor age), follow button, layer toggles, view-only banner.
2. **Tap-to-place:** tap puck (optional Survey-In first) or any map point; crosshair with a **D-pad nudge**
   (5 / 25 cm); species search (debounced 200 ms, or "marker only"); confirm sends `/place`; toast with Undo.
3. **Survey-In:** 5 s ring, n and sigma, discard count, result card with the honest note; "Use" / "Calibrate here".
4. **Calibration:** up to 4 points with rms/residuals; "fix my position today" (session) vs "fix the map"
   (plan georef).
5. **Measure from 2-3 points (new, no GPS or camera):** pick known plan points on the map, enter the tape or
   laser distance for each, pick the side if ambiguous; shows the computed spot, per-point residuals, rms
   and DOP warning; "Place here" uses the same `/place`. Works with the screen off the sky.
6. **Irrigation:** toggle-lock (or hold) record button; live preview on phone and desktop; at release a
   simplified preview, optional "closes on start / ends at plan point" (Bowditch), pipe diameter, flow,
   emitter fields; Commit sends `/irrigation-path`; under 3 m or 3 points is refused. With a plan-locked AR
   session the track comes from VIO, else from the Kalman track.
7. **AR lock (WebXR, when supported):** guided lock of two plan points with the reticle, shows scale-check
   result, then the recorded path uses plan-locked VIO.
8. **Capture:** OS camera via `<input type="file" accept="image/*" capture="environment">` (keeps EXIF
   GPS/bearing; **never re-encode**), plant name + species search, age, notes, current position with
   `fix_mode`, optional compass bearing at capture; `POST /field-records`. Records list; tapping shows the
   photo; the agent or the user can place a record later.
9. **Status:** connection, clock skew, fingerprint, outbox count, storage use.

Permissions are explained before each prompt (geolocation, device orientation, camera for WebXR); a denial
degrades gracefully (map only; tap placement and tape measurement still work).

### 4.3 Offline and spotty signal (corrected)

- **Outbox (works on a self-signed cert):** every write goes first into IndexedDB (`request_id`, photos as
  Blob), is sent with exponential backoff (2..60 s) and on `online`/focus; "N waiting to sync"; entries over
  14 days or rejected with 4xx are shown, not retried forever. Server idempotency (§1.6) makes this safe.
- **Kept-open tab:** the page, scene and basemap are already in memory; losing Wi-Fi does not blank them. A
  reload while offline fails on the default cert (no service worker, §1.3). The UI says so and tells the user
  not to reload; recordings live in the page (and IndexedDB) until the connection returns.
- **Offline reload and PWA install** only with a trusted cert (Tailscale `*.ts.net` or user-installed CA):
  then a service worker caches the shell and basemap, versioned by build id.
- Position keeps collecting offline; the puck shows raw fixes labelled "offline, unfiltered" with a thin
  client smoothing. On reconnect the buffered samples go in time order (batch <= 50) to the server filter.
  A batch whose clock differs by > 120 s from the server is rejected with a visible message.

## 5. Delivery plan

Sizing: one Sonnet session per package (about 600 changed lines at most plus tests). **[Opus]** marks
required Opus review. Default gates: `ruff check src/`, `bandit -r src/ --severity-level high`, translations
when UI strings change. Default test command: `venv/Scripts/python.exe -m pytest <paths> -v -n 0`.

| # | Package | Files and interfaces | Tests | Review |
|---|---|---|---|---|
| **WP-0 (gate)** | **Frozen-exe `ssl` loader failure.** Reproduce `--selftest` exit 0xc0000139 and find the cause. Likely: a `libssl-3-x64.dll` / `libcrypto-3-x64.dll` pair mismatch between the copies the bundle ships (`dist/OpenGardenPlanner/_internal`) and ones found first on `PATH` or the app directory (Git, Python, Anaconda, other OpenSSL installs). Check which copies exist and their versions, run with a stripped `PATH`, compare against a clean `venv`. Fix in `installer/ogp.spec` (bundle the matching pair) and add a check to `installer.build_environment --strict` that `ssl` imports in the frozen exe | `--selftest` exit 0 on the dev box; TLS start-up covered in `tests/integration/test_field_remote_lifecycle.py` | **Blocks WP-A.** Opus for the diagnosis if not found in one pass |
| WP-A | Server skeleton, TLS, auth, prefs, privacy | `field_remote/{server,auth,tls,limits}.py`; `app/field_remote_host.py` (`start/stop/status`, signal `status_changed`); prefs `field_remote/{enabled,bind,port,writes,token_ref}`; `ui/dialogs/field_remote_dialog.py` (QR); `PRIVACY.md` + `DISCLOSED`; pin `cryptography`; ADR-081 | `tests/unit/test_field_remote_{auth,limits,tls}.py`; `tests/integration/test_field_remote_lifecycle.py` (off by default, 403 without token, binds only the chosen address); `tests/unit/test_privacy_inventory.py` | **[Opus]** security |
| WP-B | Positioning core | `core/positioning/{types,kalman,gating,zupt,breadcrumbs,survey,calibration,trilateration,closure,engine}.py`; `PositioningEngine.add_gnss/add_vio/add_imu/set_anchor/survey_in/finish_track`; `trilateration.solve(points, distances, *, side_hint=None) -> TriResult(pos, residuals, rms, cov)` | `tests/unit/test_positioning_*.py`; replay harness `test_positioning_replay.py` with seeded synthetic walks (straight, L, stand-still, multipath spike, accuracy ramp), asserting filtered/raw error ratio, never absolute metres; trilateration cases (2 circles, 3+ over-determined, collinear refusal, noise); Bowditch loop golden | **[Opus]** kalman, gating, survey, trilateration math |
| WP-C | Read API, telemetry, desktop puck | `field_remote/{routes,projection}.py`; `ui/canvas/items/position_puck.py` (not saved, not in undo) | `starlette.testclient` route tests; projection golden; integration: posted positions give a puck item | Sonnet |
| WP-D | Write API | `EnsureLayerProvider`; `/place`, `/irrigation-path`, `/field-records`, `/georef/reference-points`, `/undo`; idempotency cache; `origin` tag | `tests/integration/test_field_remote_writes.py`: toggle off gives 403 per route; locked layer 409; Irrigation layer plus polyline in one macro, one undo; photo cap; 8x50 concurrent sidecar writes lose none; idempotent retry; unsaved plan 409; single-process `os.getpid()` assertion | **[Opus]** write path and locks |
| WP-E | Mobile UI: map, place, species, capture, measure-from-points | `field_remote/static/*` | `test_field_remote_static.py` (size budget, no external URLs, no `eval`, CSP header, no inline script); smoke in a desktop browser against 127.0.0.1 | **[Opus, short]** CSP/XSS |
| WP-F | Survey-In, calibration, irrigation recording UI, Bowditch option | `static/{position,survey,calib,record}.js`; engine plumbing | integration: posted synthetic walk gives committed vertices (length within 5 % of truth, one undo) | Sonnet |
| WP-G1 | Outbox (works on self-signed) | `static/store.js` (IndexedDB, backoff, idempotent replay) | static tests; manual airplane-mode check | Sonnet |
| WP-G2 | Offline reload and PWA, **trusted cert only** | `static/sw.js`, cert-trust detection (register only if it succeeds), docs for `tailscale cert` | static tests; manual on a trusted cert | Sonnet |
| WP-H | WebXR fusion: plan-locked VIO, GNSS fallback, PDR | `core/positioning/fusion.py` (`PlanLockedAligner.lock(a_vio, a_plan, b_vio, b_plan) -> Transform2D`, `scale_check`, `FusionAligner` fallback); `static/xr.js` (hit-test reticle) | synthetic VIO frames (rotation, drift, tracking loss, bad scale); **no accuracy claim** | **[Opus]** fusion |
| **WP-M** | **On-device measurement protocol (required; gates any accuracy claim)** | `docs/design/field-accuracy-protocol.md` + a result-logging mode (`POST /api/diagnostics/run`, results into `<plan>.fieldnotes/diagnostics/`) + `scripts/summarize_field_runs.py` | summarizer unit test on synthetic runs | Sonnet to build, owner to run |
| WP-I | Docs, wiki, FR-GEO, glossary, roadmap row | project docs table | `scripts/sync_wiki.py --check`, `check_agent_context.py` | Sonnet |
| WP-J (v2) | NTRIP diagnostic/relay | `services/ntrip_client.py`, prefs, privacy rows | fake caster on a local socket | **[Opus]** |

Order: WP-0, then A (with B in parallel; disjoint files), C, D, E, F, G1, H, M, I; G2 after a trusted cert
exists. A and D are never merged without an Opus read of the final diff.

### 5.1 Measurement protocol (WP-M) gating accuracy claims

On the owner's property, with a tape-measured baseline set (e.g. 5, 10, 20 m straight lines between marked
points, one L-shaped and one closed loop, one over gravel and one over lawn, shade and full sun). For each
mode (standard, anchor-calibrated, plan-locked VIO, GNSS-aligned VIO, trilateration) do N >= 10 runs per
baseline, same walker, on different times of day. Record per run: reported sigma, endpoint error against the
tape, length error, lateral deviation from the line, closure error for loops, tracking-loss count, battery
drop. Report median, P90 and max, never only the mean. A mode may be described in the UI, README, PRIVACY or
wiki with a number only if it is quoted as "median X, 90 % under Y, n runs, on this phone/Chrome version"
by the measured result; otherwise the UI shows reported sigma only. The protocol is also what sets `r_min`,
`sigma_a`, and the 1 % VIO drift assumption.

### 5.2 Only the user can test on the phone (Android Chrome)

Cert warning and fingerprint step (LAN and Tailscale); geolocation permission and real accuracy readings
near the house, under trees, in open lawn; `deviceorientationabsolute` support and screen-orientation
compensation; screen-sleep behaviour with the wake lock; camera capture keeps EXIF GPS/bearing; Survey-In
sanity at a known corner; the 20 m straight tape walk; tape trilateration against two known points;
WebXR availability, tracking on gravel/lawn, battery over 5 minutes; airplane-mode mid-write and mid-
recording; confirm a reload while offline fails as documented; optional RTK app feeding Mock Location.

### 5.3 Risks

1. Chrome stops geolocation when the screen sleeps or the page is backgrounded: wake lock, warnings, client
   buffer.
2. Self-signed friction and no service worker: pairing dialog quality; Tailscale cert path for offline reload.
3. Reported accuracy is not truth: `r_min`, replay harness and WP-M.
4. WebXR may be missing or unstable on the owner's phone: silent fallback; "when supported" wording.
5. Bridge latency: the engine runs on the server thread; only the puck signal and writes touch Qt.
6. Scope creep on the phone page; hold the screen list in §4.2.
7. Licensing: `cryptography` (Apache/BSD, GPLv3-compatible), vendored QR encoder licence file;
   `ogp-external-positioning` check in WP-A; no public accuracy claim before WP-M.
8. WP-0 may fail to find the cause quickly; HTTPS cannot ship in the exe until it is fixed.

### 5.4 Deferred to v2 / open

- **BarcodeDetector QR fiducials:** a planar pose from `cornerPoints` needs camera intrinsics (not exposed to
  the page), so a 1-3 cm figure is unproven.
- **Web Bluetooth NMEA from an external receiver ($GPGGA/$GPGST):** gives real fix quality and could replace the
  Mock Location heuristic and be the base of a real NTRIP relay; needs hardware and a Chrome feature; v2.
- **Desktop photogrammetry orthomosaic:** large and unrelated to the field loop; v2.
- Per-phone tokens and fingerprint pinning; control-network solver beyond a small size.

### 5.5 Open questions for the owner

1. Separate "phone may edit the plan" toggle (recommended) or reuse the agent-edit toggle?
2. Refuse phone writes on an unsaved plan (the sidecar needs a path)?
3. One shared token for v1, or per-phone tokens?
4. Android Chrome only?
5. Is a full-screen AR session acceptable for irrigation walks and the two-point lock?
6. Do you already use `tailscale cert`, and do you want offline reload (WP-G2) enough to set it up?
7. Confirm NTRIP relay deferred to v2, with the SW Maps / RTKGPS+ how-to in the wiki.
8. Photo caps (25 MB each, 5000 records): enough?
9. Do you own a tape or laser measure for the trilateration mode and WP-M?
