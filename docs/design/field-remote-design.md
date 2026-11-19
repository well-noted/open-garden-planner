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
                       - the phone only WRITES RECORDS (sidecar); it never edits the plan
                       - is the ONLY writer of the fieldnotes sidecar; desktop/agent approve later
```

- New package `field_remote/` (`server.py`, `auth.py`, `tls.py`, `limits.py`, `routes.py`, `static/`),
  started by `app/field_remote_host.py` beside `app/agent_host.py`. Own port (default 8766), own token, own
  daemon thread, same start/stop/port-fallback pattern. Never mounted on the Agent API app, whose loopback
  threat model stays unchanged.
- Preferences "Field Remote": enable (default off), bind, port, "allow the phone to save records" (default
  off; separate from the agent-edit toggle), pairing QR, regenerate token/certificate. A running server
  shows a status-bar badge (bind address, connected phones).

### 1.2 Bind

Off (default) | this computer only (127.0.0.1) | local network (one chosen interface address; wildcard only
after an explicit warning) | Tailscale (the `100.x` interface). Detection is advisory; the user can type an
address. A specific address, not `0.0.0.0`, so café Wi-Fi does not expose it.

### 1.3 TLS (decided: HTTPS)

Chrome exposes Geolocation, DeviceMotion/Orientation and WebXR only in secure contexts.

- **Self-signed (default).** `field_remote/tls.py` generates an EC P-256 key and a cert valid 397 days
  (SAN: bind IPs, `localhost`, machine name) in the per-user data dir, key file ACL'd to the user;
  regenerated on bind change or on demand. The SHA-256 fingerprint is shown in Preferences and the QR dialog.
- **Android Chrome UX.** Full-page warning; "Advanced" then "Proceed" once per cert; Chrome then grants
  secure-context APIs. The pairing dialog asks the user to compare the fingerprint once.
- **What a self-signed cert cannot do (corrected).** Chrome refuses service-worker registration on an
  untrusted-certificate origin (`SecurityError`), so **no offline page reload and no PWA install** on the
  default path. IndexedDB, Geolocation, motion sensors, Wake Lock and a kept-open tab still work (§4.3).
  Offline reload needs a trusted cert: Tailscale `*.ts.net` (the user runs `tailscale cert` and points
  Preferences at the files; we never run `tailscale`) or a user-installed CA. A user CA is a larger trust
  grant than needed and Android needs a screen lock for it; offered only as an explicit opt-in with that
  warning.

### 1.4 Pairing and auth

- Token `secrets.token_urlsafe(32)` kept in the existing secret store (separate key; not hashed because
  the QR must be shown again). QR encodes `https://<ip>:<port>/#token=<token>&fp=<sha256-hex>`; a fragment is never sent in a
  request, so it stays out of logs and Referer. `fp` is the SHA-256 fingerprint of the server's
  self-signed certificate. It is **not a secret**: any TLS peer sees the certificate, and the fingerprint only
  lets a client check it is talking to the paired desktop; it grants no access without the token. The web page
  ignores `fp` (the browser handles trust, and the dialog shows it for the manual check); a native client
  pins it (§6). `app.js` reads `location.hash`, strips it with
  `history.replaceState`, keeps the token in memory and `sessionStorage` (not `localStorage`), and sends
  `Authorization: Bearer`. Header only: no `?token=` form here. Compare with `secrets.compare_digest` plus
  an ASCII guard.
- Streams use fetch-streaming (can set headers); QR via a vendored MIT encoder or `qrcode` (BSD): decide in WP-A/C.

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
| Writes without consent | Record writes need the phone-save toggle AND the token; off gives 403 and a view-only UI. The phone can never change the plan: a hostile or buggy phone can at worst add pending records the user rejects | spam of pending records (capped, 1.9) |
| Path traversal | No route takes a path; photos by record id `^[A-Za-z0-9_-]{1,64}$`; static files from an in-memory dict | none |
| Plan leakage | Phone JSON is a projection (objects, layers, species, georef); no file paths or tokens | phone sees the plan (intended) |

Per-phone tokens, fingerprint pinning and mTLS are out of scope for v1 (open question 3).

### 1.6 Endpoints (`/api`, JSON, all need the header)

Reads: `GET /state` (plan name, georef summary, layers with locked/visible, `records_enabled`, capabilities,
server time; ETag, polled every 5 s); `GET /scene?bbox=&layers=` (simplified geometry, max 5000 objects);
`GET /basemap` (background downscaled to 2048 px JPEG plus its canvas rectangle, ETag); `GET /species?q=`
(bundled plus custom, 25 rows); `GET /field-records`, `GET /field-records/{id}/photo`; `GET /stream`
(desktop-to-phone events); `GET /strings` (UI strings from desktop `tr()`).

Position intake (changes nothing on disk, allowed with writes off): `POST /position` (single or batch of at
most 50: `t, lat, lon, accuracy_m, alt, heading_deg, speed_mps, vio?, imu?`, plus optional client-reported
`fix_quality` (`none|gps|dgps|rtk_float|rtk_fixed`, NMEA GGA meaning), `hdop`, `sigma_m`, `source`
(`browser|nmea_usb|nmea_ble|native_fused`); a browser client omits them) returns telemetry (§2.9).

Writes (need `records_enabled`; **sidecar only, no plan change, no undo step**): `POST /records` (JSON, or
multipart with `meta` + `photo`) creates a pending record of any `kind` (§1.9); `PATCH /records/{id}` edits
the phone's own pending record (text, geometry nudge) and `DELETE /records/{id}` withdraws it, both only
while `status == pending`. There is **no** place, layer, georeference or undo endpoint: the plan changes only
through desktop approval or the agent (§1.7).

Every write carries a client `idempotency_key` (UUID) stored in the record; a retry returns the existing
record. This is what makes the outbox (§4.3) safe.

### 1.7 Approval path: the plan changes only on the desktop or by the agent

The phone has no Qt-thread command-stack writes, so no phone write needs `bridge.run_on_main`. Pending
records become plan objects in exactly two ways, both on existing paths:
- **Desktop "Field records" panel** (new, WP-R): list with filters (kind, status), map preview of the
  geometry and accuracy, photo, notes. **Approve** places on the plan through the existing undoable commands
  and rules: a plant/note/measurement point through the command behind `_do_agent_create_object` (species
  match, locked-layer refusal); an `irrigation_path` as one polyline macro that creates the "Irrigation"
  layer if absent and is **one undo step** (an existing locked Irrigation layer is refused, never unlocked);
  a `reference_point` is queued into the georeference calibration dialog (Package 1's fit, one undo) with
  the fit rms shown before the user confirms. **Reject** sets `status=rejected` (kept, hideable). Approving
  writes `status=placed` and `placed_object_id`. This panel is the only place the "Irrigation" layer is created.
- **Agent:** `list_field_records` (reads, now includes `status`) and `place_geotagged_feature` (existing,
  behind the agent-edit toggle + token) extended to polylines and reference points (`place_field_record` on a
  record id, same code as the panel), then marking the record `placed`. Rejecting is a gated agent write too.
**Placement requests:** the panel groups records by `group_id` and shows candidates side by side (photo,
  position on the plan with its accuracy halo, bearing, `fix_mode`, precision hint). **Approve one candidate**
  places it as above and sets the siblings to `superseded` in the same store call; **Reject all** sets every
  candidate `rejected`; "Reopen" (and undo of the placement) returns the siblings to `pending`. The agent
  path is `list_field_records` (grouped by `group_id`) + `place_geotagged_feature`/`place_field_record` on the
  chosen id, which supersedes the siblings identically.
Undoing a placement leaves `placed_object_id` dangling (ADR-080 consequence, unchanged).

**Unsaved plan.** The sidecar path derives from the plan's file path, so an unsaved plan has nowhere to hold
records. Recommendation: the server answers `409 plan_not_saved` and the phone shows "Save the plan on the
computer first". Rejected alternative: a temporary store migrated on first save (second location, crash window, migration to test).

### 1.8 Single writer for the sidecar (recommendation)

Run Field Remote **in the GUI process** (option c of ADR-080); with no plan writes from the phone it is simply the sole sidecar writer (the desktop panel and agent set `status` through the same store). The Agent API's `ingest_site_photo` and the
phone routes are then two threads of one process under one `RLock` with atomic replace; a cross-process lock
or second process buys nothing. Rules: only `field_records` opens the sidecar; the server is never started
from a second process (integration test asserts `os.getpid()` equality); two app instances on one plan are
out of scope (atomic replace avoids corruption, last writer wins); a test adds 8 threads x 50 records and
loses none.

### 1.9 Field-record schema extension (sidecar version 2, migration in `_migrate`)

New fields, all optional on read, so version 1 files load unchanged and unknown keys are kept (ADR-080):

| Field | Values |
|---|---|
| `kind` | `plant` (default, = version 1 records), `irrigation_path`, `reference_point`, `note`, `measurement`, `placement_request` (a client's "we want this here") |
| `status` | `pending` (default for phone writes), `approved`, `rejected`, `placed`, `superseded` (a sibling candidate whose group had another one approved; reversible) |
| `geometry` | `{"type":"point","lat","lon","x_cm","y_cm"}` or `{"type":"polyline","points":[[lat,lon,x_cm,y_cm],...]}`; plan x,y **at capture** (current georef) kept beside lat/lon so a later georef change does not silently move it |
| `accuracy_m`, `fix_mode`, `sigma_m` | telemetry at capture (`fix_mode` gains the §2.9 values; the existing `gps/exif/map_tap/manual/remote_position` stays valid) |
| `idempotency_key` | client UUID, unique per record |
| per kind | irrigation: `diameter_mm`, `flow_lph`, `emitter_spacing_cm`, `closes_on`; reference point: `name` + the plan x,y the user says it is; measurement: `distances` and trilateration `rms` |
| `group_id`, `what`, `requester` | placement requests: candidates are **separate records sharing a `group_id`** (simplest: each has its own photo, position, accuracy, bearing, `fix_mode`, status); `what` = free-text plant or feature (+ optional `species_key`); `requester` optional name; `precision_hint` = how the point was taken (`gps`, `survey_in`, `plan_locked`, `trilateration`) |
| `source` | `field_remote`, `agent`, `desktop` |

Limits: polyline <= 2000 vertices; the 5000-record and 25 MB-photo caps stay; **pending** records capped at
500 so a runaway phone cannot fill the review list. Rejected records are kept until the user purges them.
Every status change goes through `update_record`.

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
class FixMode(StrEnum): STANDARD, ANCHOR_CALIBRATED, VIO_FUSED, RTK_FLOAT, RTK_FIXED  # values = lowercase names
# GnssSample(t, lat, lon, accuracy_m, alt, heading_deg, speed_mps); VioSample(t, x, y, z, quat, tracking)
# ImuSample(t, accel, alpha, beta, gamma, heading_deg, absolute); PositionEstimate(t, e_m, n_m, x_cm, y_cm,
#   lat, lon, sigma_m, heading_deg, speed_mps, fix_mode, stationary, quality, gated)  -- all frozen dataclasses
# plan-locked VIO reports fix_mode vio_fused with anchor_source "plan_points".
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
- Forward filter live; RTS smoother only over a finished recording.

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
- **Control network:** a small least-squares adjustment of a few plan points from extra tape distances may be
  added in `closure.py` if it stays under ~150 lines; otherwise v2.

### 2.7 Bowditch loop closure (`closure.py`)

A recorded loop or a path that should end at a known point has a closure vector `c = p_end - p_target`.
Distribute it by cumulative path length: `p_i' = p_i - (s_i / s_total) c`. Applied after the RTS smoother and
before RDP, only when the user marks "closes on the start" or "ends at plan point". Report `|c|` and its ratio
to path length; refuse when the ratio is over 5 % (a mistake, not a drift).

### 2.8 VIO and PDR fusion (`fusion.py`)

Two alignment sources, primary first.

**Primary: plan-locked VIO (experimental, WebXR only, pending WP-M).** The user stands at two known plan points A and B and
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

**PDR fallback (no WebXR):** step detection on low-passed |a| (refractory 0.3 s), step length 0.7 m, heading
from §2.2; it only replaces the constant-velocity prediction (`sigma_a = 1.5`), never overrides GNSS, and is
labelled "motion-smoothed", not `vio_fused`.

WebXR support is probed on the device (`navigator.xr.isSessionSupported('immersive-ar')`) and needs a tap;
the AR session takes over the screen, the map is a small inset.

### 2.9 `fix_mode` and telemetry

A client-reported `fix_quality` (real NMEA) overrides inference and is shown as "reported"; a client
`sigma_m` replaces the `accuracy_m`-derived `r` in the filter (never below `r_min`). Otherwise precedence:
`rtk_fixed` > `rtk_float` (both inferred) > `vio_fused` (tracking good and locked) >
`anchor_calibrated` (anchor < 10 min or georef fit rms < 2 m today) > `standard`. Telemetry from
`POST /position` and `/stream`, also feeding the desktop puck:

Telemetry JSON: `t, lat, lon, x_cm, y_cm, sigma_m, horizontal_accuracy_m, heading_deg, heading_source
(gnss|compass|vio|none), speed_mps, fix_mode, anchor_source (gnss_point|plan_points|none), anchor_age_s,
stationary, gated, quality, samples_used`.

`horizontal_accuracy_m` is the raw reported value; `sigma_m` the filtered, calibrated estimate. The desktop
puck is a non-saved overlay item outside the undo stack, toggled in the View menu.

## 3. NTRIP

A desktop NTRIP client cannot correct the phone's internal GNSS: corrections must reach the chip, which only
the phone's own software can do. **Decided: deferred to v2.**
- **Real RTK path (documented, no code):** SW Maps or RTKGPS+ connects to the caster (RTK2go, AZCORS) and
  feeds Android Mock Location; Chrome then reports better accuracy and Field Remote labels the mode as
  inferred. A wiki how-to carries the caveat that a phone antenna limits results.
- **v2 relay/diagnostic (if built):** off by default; `services/ntrip_client.py` (stdlib `socket` + `ssl`,
  NTRIP v1/v2, source table, RTCM3 detection, 1 MB/min cap); password in the secret store, never to the
  phone; UI text "Diagnostic only: this does not correct your phone's built-in GPS"; `PRIVACY.md` section and
  a `DISCLOSED` row in `tests/unit/test_privacy_inventory.py`.
- **Needed now regardless:** WP-A adds `DISCLOSED["field_remote/server.py"]` and a "Field Remote" PRIVACY
  section (listens only when enabled, on the chosen interface, sends nothing to the internet), because the
  test's loopback-only allowance does not cover a non-loopback bind.

## 4. Mobile UI

### 4.1 Constraints

Served from memory from `field_remote/static/` (ES modules `app.js`, `map.js`, `api.js`, `position.js`,
`capture.js`, `store.js`); no CDN, framework or build step; Android Chrome 100+; 150 KB budget; Canvas 2D map;
targets >= 48 px, one-handed, Wake Lock while recording; strings from `/strings` (covered by the translation gate).

### 4.2 Screens

1. **Map:** puck, accuracy halo (`sigma_m`, grey when poor), compass cone (heading + source; hidden if no
   heading, §2.2), fix badge (mode, sigma, anchor age), follow button, layer toggles, view-only banner.
2. **Mark a plant/point:** tap the puck (optional Survey-In first) or any map point; crosshair with a
   **D-pad nudge** (5 / 25 cm); species search (debounced 200 ms, or "note only"); **Save as pending**
   (`POST /records`). Toast: "Saved for review on the computer"; no place or undo on the phone.
3. **Survey-In:** 5 s ring, n and sigma, discard count, result card with the honest note; "Use" / "Calibrate here".
4. **Calibration:** up to 4 points with rms/residuals; "fix my position today" (session) vs "send corner
   recordings for review" (saved as `reference_point` records; the georeference changes only when approved).
5. **Measure from 2-3 points (no GPS or camera):** pick known plan points, enter tape/laser distances, pick
   the side if ambiguous; shows the computed spot, residuals, rms and DOP warning; "Save" stores a
   `measurement` or `plant` record there.
6. **Irrigation:** toggle-lock (or hold) record; live preview on phone and desktop; at release a simplified
   preview, optional Bowditch closure, diameter/flow/emitter fields; Save stores a pending `irrigation_path`
   record (under 3 m or 3 points refused). Source is VIO when plan-locked, else the Kalman track.
7. **AR lock (WebXR, experimental, when supported):** guided lock of two plan points with the reticle, scale
   check shown, then the recorded path uses plan-locked VIO.
8. **Capture:** OS camera via `<input type="file" accept="image/*" capture="environment">` (keeps EXIF
   GPS/bearing; **never re-encode**), plant name + species search, age, notes, position with `fix_mode`,
   optional compass bearing; Save a pending `plant` record. A list shows each record's status (pending,
   placed, rejected).
9. **Client wish ("place request"):** photograph the spot, type what the client wants (plant/feature, optional
   species match, optional requester name), take the position (puck, with a **fine-nudge** of the captured
   point before submit); a hint says "if the exact spot matters, use Survey-In, plan-lock or tape
   measurement" and stores which was used (`precision_hint`). "Add another candidate" creates a sibling under
   the same `group_id`; Save submits all as pending.
10. **Status:** connection, clock skew, fingerprint, outbox count, storage use.

Permissions are explained before each prompt (geolocation, device orientation, camera for WebXR); a denial
degrades gracefully (map only; tap placement and tape measurement still work).

### 4.3 Offline and spotty signal (corrected)

- **Outbox (works on a self-signed cert):** every write goes first into IndexedDB (`idempotency_key`, photos as
  Blob), is sent with exponential backoff (2..60 s) and on `online`/focus; "N waiting to sync"; entries over
  14 days or rejected with 4xx are shown, not retried forever. Server idempotency (§1.6) makes this safe.
- **Kept-open tab:** the page, scene and basemap are already in memory; losing Wi-Fi does not blank them. A
  reload while offline fails on the default cert (no service worker, §1.3). The UI says so and tells the user
  not to reload; recordings live in the page (and IndexedDB) until the connection returns.
- **Offline reload and PWA install** only with a trusted cert (Tailscale `*.ts.net` or user-installed CA):
  then a service worker caches the shell and basemap, versioned by build id.
- Position keeps collecting offline (raw fixes labelled "offline, unfiltered"); on reconnect buffered samples
  go in time order (batch <= 50) to the server filter. A batch whose clock differs by > 120 s is rejected.

## 5. Delivery plan

Sizing: one Sonnet session per package (about 600 changed lines at most plus tests). **[Opus]** marks
required Opus review. Default gates: `ruff check src/`, `bandit -r src/ --severity-level high`, translations
when UI strings change. Default test command: `venv/Scripts/python.exe -m pytest <paths> -v -n 0`.

| # | Package | Files and interfaces | Tests | Review |
|---|---|---|---|---|
| **WP-0 (gate)** | **Frozen-exe `ssl` loader failure.** Reproduce `--selftest` exit 0xc0000139 and find the cause. Likely: a `libssl-3-x64.dll` / `libcrypto-3-x64.dll` pair mismatch between the copies the bundle ships (`dist/OpenGardenPlanner/_internal`) and ones found first on `PATH` or the app directory (Git, Python, Anaconda, other OpenSSL installs). Check which copies exist and their versions, run with a stripped `PATH`, compare against a clean `venv`. Fix in `installer/ogp.spec` (bundle the matching pair) and add a check to `installer.build_environment --strict` that `ssl` imports in the frozen exe | `--selftest` exit 0 on the dev box; TLS start-up covered in `tests/integration/test_field_remote_lifecycle.py` | **Blocks WP-A.** Opus for the diagnosis if not found in one pass |
| WP-A | Server skeleton, TLS, auth, prefs, privacy | `field_remote/{server,auth,tls,limits}.py`; `app/field_remote_host.py` (`start/stop/status`, signal `status_changed`); prefs `field_remote/{enabled,bind,port,records,token_ref}`; `ui/dialogs/field_remote_dialog.py` (QR with `fp`); `PRIVACY.md` + `DISCLOSED`; pin `cryptography`; ADR-081 | `tests/unit/test_field_remote_{auth,limits,tls}.py`; `tests/integration/test_field_remote_lifecycle.py` (off by default, 403 without token, binds only the chosen address); `tests/unit/test_privacy_inventory.py` | **[Opus]** security |
| WP-B | Positioning core | `core/positioning/{types,kalman,gating,zupt,breadcrumbs,survey,calibration,trilateration,closure,engine}.py`; `PositioningEngine.add_gnss/add_vio/add_imu/set_anchor/survey_in/finish_track`; `trilateration.solve(points, distances, *, side_hint=None) -> TriResult(pos, residuals, rms, cov)` | `tests/unit/test_positioning_*.py`; replay harness `test_positioning_replay.py` with seeded synthetic walks (straight, L, stand-still, multipath spike, accuracy ramp), asserting filtered/raw error ratio, never absolute metres; trilateration cases (2 circles, 3+ over-determined, collinear refusal, noise); Bowditch loop golden | **[Opus]** kalman, gating, survey, trilateration math |
| WP-C | Read API, telemetry, desktop puck | `field_remote/{routes,projection}.py`; `ui/canvas/items/position_puck.py` (not saved, not in undo) | `starlette.testclient` route tests; projection golden; integration: posted positions give a puck item | Sonnet |
| WP-D | Records API + schema v2 | `field_records.py` v2 (`kind/status/geometry/idempotency_key/group_id`, `supersede_siblings`/`reopen_group`, `_migrate`, pending cap, find by key); `POST/PATCH/DELETE /records` in `field_remote/routes.py` (no plan writes, no `run_on_main`) | `tests/unit/test_field_records_v2.py` (v1 file loads, unknown keys kept, idempotent retry, caps, 8x50 concurrent writes lose none); `tests/integration/test_field_remote_records.py` (toggle off 403, unsaved plan 409, photo cap, single-process `os.getpid()`) | **[Opus]** sidecar writer and locks |
| WP-R | Desktop **Field records review panel** + agent approver | `ui/panels/field_records_panel.py` (list, filters, map preview, photo, Approve/Reject); `services/field_record_placement.py` (Qt-free planner: record to create-spec / polyline / reference point); `EnsureLayerProvider` (Irrigation layer + polyline as one macro); candidate comparison view for `placement_request` groups; extend `agent_api/field_tools.py` (`place_field_record`, polylines, reference points, `status` in `list_field_records`) | `tests/integration/test_field_records_panel.py` (approve plant = one undo; approve one of 3 candidates supersedes the others, undo/reopen restores them; reject all; approve irrigation creates layer + polyline in one undo; locked layer refused; reject; reference point reaches the fit dialog); agent tool tests with toggle off/on | **[Opus]** placement and write path |
| WP-E | Mobile UI: map, mark, species, capture, client-wish candidates, measure-from-points | `field_remote/static/*` | `test_field_remote_static.py` (size budget, no external URLs, no `eval`, CSP header, no inline script); smoke in a desktop browser against 127.0.0.1 | **[Opus, short]** CSP/XSS |
| WP-F | Survey-In, calibration, irrigation recording UI, Bowditch option | `static/{position,survey,calib,record}.js`; engine plumbing | integration: a posted synthetic walk becomes a pending `irrigation_path` record (length within 5 % of truth, vertex cap) | Sonnet |
| WP-G1 | Outbox (works on self-signed) | `static/store.js` (IndexedDB, backoff, idempotent replay) | static tests; manual airplane-mode check | Sonnet |
| WP-G2 | Offline reload and PWA, **trusted cert only** | `static/sw.js`, cert-trust detection (register only if it succeeds), docs for `tailscale cert` | static tests; manual on a trusted cert | Sonnet |
| WP-H | WebXR fusion: plan-locked VIO, GNSS fallback, PDR | `core/positioning/fusion.py` (`PlanLockedAligner.lock(a_vio, a_plan, b_vio, b_plan) -> Transform2D`, `scale_check`, `FusionAligner` fallback); `static/xr.js` (hit-test reticle) | synthetic VIO frames (rotation, drift, tracking loss, bad scale); **no accuracy claim** | **[Opus]** fusion |
| **WP-M** | **On-device measurement protocol (required; gates any accuracy claim)** | `docs/design/field-accuracy-protocol.md` + a result-logging mode (`POST /api/diagnostics/run`, results into `<plan>.fieldnotes/diagnostics/`) + `scripts/summarize_field_runs.py` | summarizer unit test on synthetic runs | Sonnet to build, owner to run |
| WP-I | Docs, wiki, FR-GEO, glossary, roadmap row | project docs table | `scripts/sync_wiki.py --check`, `check_agent_context.py` | Sonnet |
| WP-J (v2) | NTRIP diagnostic/relay | `services/ntrip_client.py`, prefs, privacy rows | fake caster on a local socket | **[Opus]** |

Order: WP-0, then A (with B in parallel; disjoint files), C, D, R, E, F, G1, H, M, I; G2 after a trusted cert
exists. A, D and R are never merged without an Opus read of the final diff. The phone UI (E, F) may ship before R only if the agent can already read records (`list_field_records`).

### 5.1 Measurement protocol (WP-M) gating accuracy claims

On the owner's property with tape-measured baselines (5, 10, 20 m straight lines, one L, one closed loop; gravel
and lawn; shade and sun). For each mode (standard, anchor-calibrated, plan-locked VIO, GNSS-aligned VIO,
trilateration) do >= 10 runs per baseline, same walker, different times of day. Record reported sigma,
endpoint, length and lateral error, loop closure, tracking losses, battery drop; report median, P90 and max.
A number may appear in the UI, README, PRIVACY or wiki only as "median X, 90 % under Y, n runs, this
phone/Chrome version"; otherwise the UI shows reported sigma only. WP-M also sets `r_min`, `sigma_a` and the 1 %
VIO drift assumption.

### 5.2 Only the user can test on the phone (Android Chrome)

Cert warning and fingerprint; accuracy near the house, under trees, in open lawn; `deviceorientationabsolute`
and rotation compensation; wake lock; photo EXIF kept; Survey-In at a known corner; 20 m tape walk; tape
trilateration; WebXR availability, tracking on gravel/lawn, 5-minute battery; airplane mode mid-write and
mid-recording; offline reload fails as documented; optional RTK app via Mock Location.

### 5.3 Risks

Chrome stops geolocation when the screen sleeps (wake lock, warnings, client buffer); self-signed friction and
no service worker (pairing dialog, Tailscale path); reported accuracy is not truth (`r_min`, replay harness,
WP-M); WebXR may be missing on the phone (silent fallback, "when supported"); phone-page scope creep; a long
pending list if records are never reviewed (cap 500, panel badge, agent can approve); sidecar v2 must keep
version 1 files and photos intact (migration test); licensing (`cryptography`, QR encoder,
`ogp-external-positioning` check in WP-A); WP-0 may delay HTTPS in the exe.

### 5.4 Deferred to v2

BarcodeDetector QR fiducials (planar pose from `cornerPoints` needs camera intrinsics the page cannot get; 1-3
cm unproven); Web Bluetooth NMEA from an external receiver (see §6); desktop photogrammetry orthomosaic;
per-phone tokens and pinning.

### 5.5 Decided and open

Decided by the owner: separate phone-save toggle (off by default); one shared token; Android Chrome only;
NTRIP deferred to v2 (SW Maps / RTKGPS+ how-to in the wiki); caps 25 MB and 5000 records; offline reload only
with a trusted cert; the phone never edits the plan (records only, approved on the desktop or by the agent; client wishes included).

Open:
1. Unsaved plan: refuse with `plan_not_saved` (recommended, §1.7) or another policy?
2. Is a full-screen AR session acceptable for irrigation walks and the two-point lock?
3. Do you use `tailscale cert`, and is offline reload worth setting it up?
4. Do you own a tape or laser measure (trilateration and WP-M)?
5. Should approving an `irrigation_path` use a default pipe style, or always ask the diameter?

## 6. Native client (later)

Decision: **web client first; a native Android (Kotlin) client only after WP-M.** The §1.6 contract is
client-agnostic (HTTPS + JSON + header token); two hooks exist now: the certificate fingerprint `fp` in the
QR (§1.4) and the optional `fix_quality/hdop/sigma_m/source` on position posts (§1.6).

**Triggers (any one, from WP-M data):** measured plan-locked WebXR error above the owner's target (set before
the runs); Chrome WebXR unavailable or unreliable on the phone; an external RTK receiver wanted; screen-sleep
limits too disruptive on real irrigation walks.

**It would add:** foreground-service GNSS logging (raw `GnssMeasurement`), certificate pinning from `fp`,
ARCore Depth ground ranging, direct USB/Bluetooth NMEA ($GGA/$GST) with its own NTRIP client reporting
`fix_quality`/`hdop` honestly.

**Unproven:** 1-2 cm plan-local accuracy from ARCore Depth or AprilTags, and decimeter-class raw phone GNSS,
are hypotheses needing WP-M-style evidence before any claim.

**Costs:** a Kotlin/Gradle toolchain and CI, APK signing and distribution, on-device-only testing, ARCore
Depth device support, a second UI to keep in step.
