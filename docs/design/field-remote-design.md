# Field Remote and positioning: design

Status: proposal (design only, no production code). Builds on ADR-079 (georeferencing) and ADR-080
(field records sidecar). Becomes ADR-081 plus FR-GEO entries when the first work package lands.

Scope: a phone-facing server (Field Remote), a pure-Python positioning core, an optional RTCM relay,
a self-contained mobile UI, and an ordered delivery plan.

## 0. Ground truth this design stands on

- The Agent API is an MCP server on a daemon thread (`agent_api/server.py`, uvicorn + starlette,
  loopback only, DNS-rebinding protection, token read from header or `?token=` by
  `_bearer_token_middleware`, compared with `secrets.compare_digest`, write tools gated by the
  Preferences toggle plus the token). Main-thread work goes through `agent_api/bridge.py`
  (`run_on_main`, queued signal, timeout) and the providers in `agent_api/providers.py`.
- `services/field_records.py` owns `<plan>.fieldnotes.json` plus `<plan>.fieldnotes/photos/`.
  Its lock is `threading.RLock`, in-process only (the file says so). Writes are atomic.
- `core/georef.py` already has `GeoReference.gps_to_canvas/canvas_to_gps` and
  `fit_from_reference_points`. Scene is Y-up, centimetres. `rotation_deg == north_angle_deg`.
- `cryptography`, `uvicorn`, `starlette`, `numpy` are already in `uv.lock` (`cryptography`
  transitively). If the app imports `cryptography` directly it must be declared in `pyproject.toml`
  and added to the licence check (WP-A).

## 1. Server architecture

### 1.1 Shape

```
Field Remote (new, off by default)             Agent API (unchanged)
 https://<bind>:<port>/  (phone, LAN/Tailscale)   http://127.0.0.1:8765/mcp (agent)
        |                                                |
        +-----------> FieldRemoteHost (QObject, GUI process) <---+
                       - reuses AgentBridge.run_on_main
                       - reuses the write providers / command stack
                       - is the ONLY writer of the fieldnotes sidecar
```

- New package `field_remote/` (`server.py`, `auth.py`, `tls.py`, `routes.py`, `static/`), started and
  stopped by a new `app/field_remote_host.py` next to `app/agent_host.py`. Separate listener, separate
  port (default 8766), separate token. Never mounted on the Agent API app: that one stays
  loopback-only and its threat model does not change.
- Starlette app + uvicorn with `ssl_certfile`/`ssl_keyfile`, on its own daemon thread, same
  start/stop/port-fallback pattern as `agent_api/server.py`. No new runtime dependency beyond the
  explicit `cryptography` pin.
- Off by default. Preferences "Field Remote" group: enable (default off), bind choice, port,
  "allow phone to edit the plan" (default off, separate from the agent-edit toggle), show pairing QR,
  regenerate token, regenerate certificate. A running server shows a persistent status-bar badge with
  the bind address and the number of connected phones.

### 1.2 Bind

| Choice | Bind address | Notes |
|---|---|---|
| Off | none | default |
| This computer only | 127.0.0.1 | for testing the page in a desktop browser |
| Local network | the chosen LAN interface address (never `0.0.0.0` unless the user picks "all interfaces" and confirms a warning) | home Wi-Fi |
| Tailscale | the `tailscale0`/`100.x` interface address, if present | works away from home Wi-Fi; encrypted by Tailscale |

The interface list comes from `psutil`-free stdlib code (`socket.getaddrinfo` over
`socket.gethostname()` is unreliable on Windows; use `ipaddress` over `socket.if_nameindex` where
available, else a connect-UDP probe). Detection is advisory: the user can type an address.
Binding a specific address, not the wildcard, is the default so a laptop on café Wi-Fi does not
expose the listener on every interface.

### 1.3 TLS (decided: HTTPS)

Chrome exposes Geolocation, DeviceMotion/Orientation and WebXR only in a secure context, and
`http://192.168.x.x` is not one.

- **Self-signed (default).** On first enable, `field_remote/tls.py` generates an EC P-256 key and a
  certificate valid 397 days with SAN = the bind IP(s), `localhost`, and the machine name; stored in
  the per-user data dir (never in the plan, never in a repo), key file ACL'd to the user. Regenerated
  when the bind address changes or on demand. The cert's SHA-256 fingerprint is shown in the
  Preferences dialog and in the QR dialog.
- **Android Chrome trust UX (honest).** A self-signed cert gives a full-page warning. The user taps
  "Advanced", then "Proceed to <ip> (unsafe)". This is a one-time click per cert and Chrome does
  grant secure-context features after it. Installing the cert as a user CA does not help for sites
  on Chrome for Android unless the user also sets a screen lock, and is a bigger trust grant than
  needed; do not recommend it. The pairing dialog tells the user to compare the fingerprint on the
  warning page ("Advanced" shows it) with the one on the desktop, once.
- **Tailscale alternative.** If the machine has a `*.ts.net` name and the user has run
  `tailscale cert`, the user can point Preferences at the cert and key files; the server then serves
  a publicly trusted certificate and the warning disappears. We never run `tailscale` ourselves.
  Documented, not automated, in v1.

### 1.4 Pairing and auth

- Token: `secrets.token_urlsafe(32)`, kept (not hashed, because the QR must be shown again) in the
  existing secret store the Agent API token uses (same module, same file permissions), separate key.
- QR code encodes `https://<ip>:<port>/#token=<token>`. A fragment is never sent in a request, so it
  stays out of server logs, the Referer header and proxies. `static/app.js` reads
  `location.hash`, immediately `history.replaceState` to strip it, keeps the token in memory and
  `sessionStorage` only (cleared when the tab closes; not `localStorage`), and sends
  `Authorization: Bearer <token>` on every call. The server accepts the header only: no `?token=`
  query form (the Agent API accepts it for MCP client reasons; here there is no such client).
- Token compare: `secrets.compare_digest`, ASCII guard, same as `_require_write_auth`.
- Phone-visible session: one `POST /api/pair` with the header returns a short-lived (8 h) signed
  session id used only to bind SSE streams (EventSource cannot set headers; fetch-streaming can, so
  prefer fetch streaming and skip the session id if it works on target Chrome versions; open question 4).
- QR rendering: draw it with a small pure-Python QR encoder (vendored, MIT) or Qt's own drawing of a
  matrix from a 60-line encoder. No new dependency if the vendored encoder passes the licence check;
  otherwise `qrcode` (BSD) is acceptable. Decide in WP-A.

### 1.5 Threat model

| Threat | Mitigation | Residual risk |
|---|---|---|
| Other device on the LAN guesses the token | 256-bit token; constant-time compare; failures cost 250 ms and count toward a per-IP lockout (10 failures then 5 min); bind to one interface | A LAN attacker can still reach the TLS port and learn that it exists |
| Token leaks via QR photo or screenshot | Regenerate-token button; QR shown only on demand and hidden after 2 min; fragment keeps it out of logs | Anyone who photographs the QR while shown |
| Token in browser history/Referer | Fragment stripped on load; `Referrer-Policy: no-referrer`; no external links or resources | None found |
| CSRF / drive-by from a web page the phone also has open | No cookies are used for auth, so a cross-site request has no credential; all writes need the Authorization header, which a cross-origin page cannot set without a CORS preflight we refuse; strict `Host` check (bind IP, localhost, machine name) defeats DNS rebinding the way `agent_api/server.py` already does; no CORS headers at all | None expected |
| MITM on the LAN | TLS; fingerprint check once at pairing; Tailscale cert path | User clicks through a warning for a different host: mitigated by the fingerprint step, not eliminated |
| Content/XSS in served page | Same-origin only, strict CSP (`default-src 'self'; img-src 'self' blob: data:; connect-src 'self'; style-src 'self'`), no inline script, all strings set with `textContent`, JSON only | Plant names/notes are user text; they are escaped on the phone and on the desktop |
| Hostile or buggy phone uploads | Request body cap 12 MB for photos, 256 KB otherwise (checked from Content-Length and while streaming); the photo is re-validated by `field_records.sniff_photo` (Pillow decode) and renamed by the store; only `image/jpeg|png|webp`; field lengths use `field_records` limits | Decompression bombs: Pillow `MAX_IMAGE_PIXELS` stays at its default and a decode error is a 400 |
| Flooding | Token bucket per token: 20 req/s reads, 5 req/s writes, 1 concurrent photo upload, 1 position-stream per phone; at most 4 connected phones | A paired phone can still waste CPU within those limits |
| Writes without consent | Writes need "allow phone to edit the plan" AND the token; with the toggle off every write route returns 403 and the UI shows "view only" | None |
| Path traversal / arbitrary file read | No route takes a path; photos are addressed by record id matching `^[A-Za-z0-9_-]{1,64}$`; static files come from an in-memory dict built at startup | None |
| Plan leakage | Phone-facing JSON is a projection (§1.6): objects, layers, species names, georef; no file paths, no tokens, no other plans | The phone sees the plan; that is the point |

TLS-fingerprint pinning, mTLS and per-phone tokens are out of scope for v1 (open question 3).

### 1.6 Endpoints (all under `/api`, JSON unless stated; all require the header)

Reads (always allowed once paired):
- `GET /api/state`: plan name, georef summary (origin, scale, rotation, rms, source), layers
  (id, name, locked, visible), counts, server capabilities (`writes_enabled`, `ntrip_relay`),
  server time. Cheap; polled every 5 s, also ETag'd.
- `GET /api/scene?bbox=&layers=`: simplified geometry for drawing: per object `{id, type, layer,
  points|rect|circle, name, species_key, stroke/fill hint}`; no thumbnails; capped 5000 objects.
- `GET /api/basemap`: the plan background image as a downscaled JPEG (max 2048 px) with its canvas
  rectangle, so the phone overlays it correctly. `ETag` from the background's hash.
- `GET /api/species?q=`: search of `plant_species.json` plus the user's custom plants: `{species_key,
  common_name, latin_name, mature_spread_cm, type}`, max 25 rows.
- `GET /api/field-records` and `GET /api/field-records/{id}/photo`: from the in-process store.
- `GET /api/stream` (SSE or fetch streaming): desktop-to-phone events (scene revision, records added).

Position intake (allowed with writes off; they change nothing on disk):
- `POST /api/position` `{t, lat, lon, accuracy_m, alt, alt_accuracy_m, heading_deg, speed_mps,
  source: "geolocation", vio?: {...}, imu?: {...}}` -> telemetry (§2.7). Batchable (`[...]`, max 50).
- `POST /api/survey-in/{start|sample|finish}` or a pure client-side buffer posted once; see §2.5.

Writes (need `writes_enabled`):
- `POST /api/field-records` multipart: `meta` JSON (plant_name, species_key?, age?, notes?, lat, lon,
  accuracy_m, bearing_deg?, fix_mode, timestamp) + optional `photo` -> `add_record` -> returns the
  record. Does not touch the scene (sidecar only), so it has no undo step (same as `ingest_site_photo`).
- `POST /api/place` `{species_key|object_type, name?, x, y | lat, lon, field_record_id?}` -> one create
  command through the same path as `create_object`/`place_geotagged_feature`.
- `POST /api/irrigation-path` `{points: [[lat,lon]...] | [[x,y]...], diameter_mm?, flow_lph?,
  emitter_spacing_cm?, name?, request_id}` -> one polyline on the "Irrigation" layer.
- `POST /api/georef/reference-points` `{points: [{name, lat, lon, x, y}], allow_affine}` -> calls the
  same code as `set_georeference_from_reference_points` (one undo step).
- `POST /api/undo` (last phone-originated command only; see §1.7).

Optional, v2 (see §3): `PUT /api/rtcm`, `GET /api/rtcm/status`.

Idempotency: every write carries a client `request_id` (UUID); the server keeps the last 200 and
returns the previous result for a retry. This is what makes the offline outbox (§4.4) safe.

### 1.7 Phone writes through the command stack on the Qt thread

The phone never touches the scene. Each write route validates input in the server thread (pure
checks, bounds, lengths), then calls `bridge.run_on_main(fn)` exactly as the Agent API tools do, and
`fn` calls a provider on the GUI thread that builds the same `Command` the GUI would and pushes it on
the undo stack:

- Place: reuse `_do_agent_create_object` (the chokepoint `place_geotagged_feature` uses). Locked-layer
  and unknown-layer refusals come back as 409 with the message; the phone shows it.
- Irrigation polyline: needs a layer-ensure step. New provider `EnsureLayerProvider` in
  `providers.py` (find by name, case-insensitive; else create via the existing layer command so the
  layer creation and the polyline are **one macro** and one undo step). If "Irrigation" exists and is
  locked: refuse; never unlock.
- Georef: the existing undoable georef command from Package 1.
- Every command is tagged `origin="field_remote"` so the status bar says "Phone: added Lemon (undo
  with Ctrl+Z)" and the phone's `/api/undo` can only pop a command it created and only if it is
  still on top (otherwise 409; it never undoes a desktop edit).
- If the desktop is mid-modal-dialog, `run_on_main` times out (existing behaviour): the route returns
  503 `busy`, the phone retries with the same `request_id`.
- The plan must be saved to disk for sidecar writes (the sidecar sits beside the file). An unsaved
  plan returns 409 `plan_not_saved` and the UI says so.

### 1.8 Single writer for the sidecar (recommendation)

ADR-080 flags this. Options: (a) cross-process file lock; (b) separate Field Remote process; (c)
Field Remote in the GUI process as the sole writer. **Recommend (c).** The Field Remote server runs
inside the GUI process, so the existing `threading.RLock` and atomic replace already serialise it;
the Agent API tool `ingest_site_photo` also runs in-process, so there are two writers (agent thread,
phone thread) but one process and one lock. No second process exists, so a cross-process lock buys
nothing. Rules to keep this true:

1. `field_records` stays the only code that opens the sidecar; Field Remote routes call
   `add_record/update_record` and never write files themselves.
2. The server runs in a daemon thread of the GUI process; never run it from a console script or a
   second `python -m` process (a test asserts `os.getpid()` equality in the integration test).
3. A user who opens the same plan in two app instances is out of scope; the store already survives
   that (atomic replace, last writer wins) without corruption. Document it; do not engineer for it.
4. Read-modify-write inside one lock hold (already how `add_record` works); add a test that 8 threads
   adding 50 records each lose none.

## 2. Positioning core: `core/positioning/` (pure Python, numpy allowed)

Pure means no Qt, no network, deterministic given inputs. Time comes from the samples, never from
`time.time()`. Everything works in the **local ENU frame in metres** (east, north) around the plan's
georef origin, not in canvas centimetres, so noise models are in physical units. Conversion to canvas
happens once, at the edge, with `georef.gps_to_canvas` after the anchor transform of §2.6.

Module map: `types.py` (dataclasses, `FixMode`), `kalman.py`, `gating.py`, `zupt.py`,
`breadcrumbs.py`, `survey.py`, `calibration.py`, `fusion.py` (VIO/PDR alignment), `engine.py`
(`PositioningEngine`, the one object the server owns), `__init__.py`.

### 2.1 Honest capability statement

What a phone's browser can realistically give on Android Chrome:

- `navigator.geolocation.watchPosition` with `enableHighAccuracy`: about 1 Hz (often 0.5-1 Hz),
  reported `accuracy` typically 3-10 m in open sky, 10-30 m near a two-storey stucco house or under
  a tree canopy. Newer phones with dual-frequency GNSS (L1+L5) are better, commonly 1-4 m open sky.
  Chrome gives no HDOP, no satellite count, no raw measurements, no fix type.
- Walking-pace relative path: filtering removes the sawtooth, but absolute position stays at
  GNSS accuracy. Averaging N independent fixes improves error roughly as 1/sqrt(N) only for
  uncorrelated noise; GNSS errors (multipath, atmosphere) are strongly correlated over seconds, so
  3-5 s of Survey-In gives a modest gain, not a factor of 2.
- WebXR (`immersive-ar`, Android Chrome, ARCore-supported device): smooth 6-DoF relative motion, a
  few cm of drift per 10 m in good texture, worse on blank lawn/gravel or in low light. It needs a
  user gesture, camera permission, and an active full-screen AR session; it cannot run in the
  background and the screen stays on with the camera feed visible. Battery use is high.
- DeviceMotion/Orientation PDR: step-length-based, 5-10 % distance error, heading error of
  10-20 degrees near metal/rebar, drifts quickly. A fallback for smoothing only.
- **We do not claim sub-decimeter absolute accuracy anywhere.** Realistic outcomes:
  - `standard`: GNSS-limited, 2-10 m. Good for "which side of the yard".
  - `anchor_calibrated`: one point removes the slowly-varying bias, leaving about 1-3 m in the same
    session (bias drifts within minutes); two to four points also fix rotation/scale error of the
    satellite image. Best case 0.5-2 m, not decimetres.
  - `vio_fused`: relative shape of a walked line good to roughly 5-20 cm over tens of metres; its
    absolute placement is still the anchor/GNSS accuracy. That is useful for irrigation lines: they
    look straight and the right length, offset by the absolute error.
  - `rtk_float|rtk_fixed`: only if the phone's own RTK app feeds Mock Location and Chrome relays it.
    Chrome gives no fix type, so we **infer** it from the reported accuracy (<0.5 m sustained =
    float, <0.05 m = fixed) and label it "inferred". It is the only path to centimetres.
  - Plan geometry itself is traced from a satellite image at 30-60 cm accuracy, so a placement better
    than that is not meaningful in the plan anyway.

### 2.2 Types

```python
class FixMode(StrEnum):
    STANDARD = "standard"; ANCHOR_CALIBRATED = "anchor_calibrated"; VIO_FUSED = "vio_fused"
    RTK_FLOAT = "rtk_float"; RTK_FIXED = "rtk_fixed"

@dataclass(frozen=True) class GnssSample:
    t: float                  # seconds, phone clock (Date.now()/1000 or performance.timeOrigin based)
    lat: float; lon: float
    accuracy_m: float         # 1-sigma horizontal radius as reported; may be 68 % or ~95 %: see 2.3
    alt: float | None; heading_deg: float | None; speed_mps: float | None

@dataclass(frozen=True) class VioSample:      # camera pose in the AR session's own frame
    t: float; x: float; y: float; z: float; qx: float; qy: float; qz: float; qw: float
    tracking: Literal["tracking", "limited", "lost"]

@dataclass(frozen=True) class ImuSample:      # DeviceMotion + orientation at ~20-60 Hz
    t: float; ax: float; ay: float; az: float; alpha: float; beta: float; gamma: float
    compass_deg: float | None                 # webkitCompassHeading-like absolute, if available

@dataclass(frozen=True) class PositionEstimate:
    t: float; e_m: float; n_m: float; x_cm: float; y_cm: float; lat: float; lon: float
    sigma_m: float; heading_deg: float | None; speed_mps: float
    fix_mode: FixMode; stationary: bool; quality: str   # "good"|"fair"|"poor"
    gated: bool                                          # this sample was rejected as an outlier
```

### 2.3 Kalman filter (`kalman.py`)

State `x = [e, n, ve, vn]` (metres, m/s), covariance `P` 4x4. Constant-velocity model, time step
`dt` from sample times (clamped to [0.05, 5] s; a gap over 5 s reinitialises velocity, keeps position).

- Transition `F = [[1,0,dt,0],[0,1,0,dt],[0,0,1,0],[0,0,0,1]]`.
- Process noise: continuous white-noise acceleration, `q = sigma_a^2` with `Q = q * [[dt^4/4,0,dt^3/2,0],
  [0,dt^4/4,0,dt^3/2],[dt^3/2,0,dt^2,0],[0,dt^3/2,0,dt^2]]`. **`sigma_a = 0.8 m/s^2` walking** (gardener
  starting/stopping and turning; a pedestrian's acceleration is of order 0.5-1.5 m/s^2); `0.15 m/s^2`
  when ZUPT says stationary. Tunable constants in `types.py`, with the replay test (§5, WP-B) as the
  tuning harness.
- Measurement `z = [e, n]`, `H = [[1,0,0,0],[0,1,0,0]]`, `R = r^2 * I` with
  `r = max(accuracy_m * k, r_min)`. Chrome's `accuracy` is a 68 % radius in the spec; implementations
  vary, so `k = 1.0` and `r_min = 1.5 m` (a floor because reported accuracy is optimistic with
  correlated errors and Android sometimes reports 1 m with 5 m error). Inflate `r` by x2 when
  `accuracy_m` jumped up more than 3x in one second (degrading fix).
- Initial `P = diag(r0^2, r0^2, 2^2, 2^2)`.
- Update uses the Joseph form for numerical safety. Output is `x`, `sqrt(trace(P[:2,:2])/2)` as
  `sigma_m`.
- The filter is **forward only** and runs on every sample; a separate RTS smoother runs only on a
  finished recording (irrigation path) to get the best path, with the fixed-lag window equal to the
  whole recording.

### 2.4 Gating, ZUPT, breadcrumbs

**Gating (`gating.py`)**, in order:
1. Reject non-finite, `accuracy_m <= 0`, `accuracy_m > 50`, timestamps going backwards.
2. Mahalanobis: innovation `y = z - Hx`, `S = HPH^T + R`, `d2 = y^T S^-1 y`. Reject if
   `d2 > 9.21` (chi-square, 2 dof, 99 %). Three consecutive rejects within 5 s mean the filter, not the
   sample, is wrong: re-seed from the latest sample with `P` large (handles real jumps such as
   coming out of a tunnel of trees, or the user being driven).
3. Velocity-jump: implied speed `|z - x_prev| / dt > 3.0 m/s` (a fast gardener with a wheelbarrow
   runs about 2 m/s) rejects the sample even if Mahalanobis passes (large R can swallow a spike).
   Rejected samples are reported `gated=True` and are **not** used; they are kept for diagnostics.

**ZUPT (`zupt.py`)**: stationary when, over the last 3 s, (a) all reported `speed_mps` < 0.3 or
horizontal displacement of raw fixes < `max(1.0 m, 1.5 x median accuracy)`, and (b) IMU acceleration
variance < 0.05 (m/s^2)^2 when IMU samples exist. While stationary: apply a pseudo-measurement
`v = 0` (R_v = 0.05^2) and freeze the breadcrumb emitter. This cuts the "GPS dance" while the user
stands at a tree. Leaving stationary needs 2 consecutive samples above threshold (hysteresis) to avoid
chatter.

**Breadcrumbs (`breadcrumbs.py`)**, the irrigation-path recorder:
1. Emit a point only when the filtered position has moved >= `d_min = max(0.5 m, 1.0 x sigma_m)`
   from the last emitted point (so a poor fix does not produce more points than information).
2. At release, run the RTS smoother over the filtered track, then Ramer-Douglas-Peucker with
   `epsilon = max(0.15 m, 0.5 x median sigma_m)`, then drop vertices closer than 0.3 m, then
   enforce a hard cap of 2000 vertices (raise epsilon until it fits).
3. Open path; the first/last point are always kept. Output `list[(e, n)]` plus per-vertex `sigma_m`.
4. Live preview on the phone uses the unsimplified filtered track; only the commit is simplified.

### 2.5 Survey-In (`survey.py`)

For one critical point: stand still, collect `N >= 4` samples over 3-5 s (a 5 s hard stop; abort if
the user moves more than `max(1.5 m, 2 x accuracy)` from the first sample).
- Discard samples with `accuracy_m > max(2 x median, 1.5 x min)` over the window ("high accuracy
  radius").
- Weighted mean in ENU with `w_i = 1 / max(accuracy_m, 1.0)^2`; weighted standard error
  `sigma = sqrt(1 / sum(w_i))` **inflated by `sqrt(N_eff_correlation)`**: assume samples are
  correlated, so use `N_eff = max(1, N / 3)`; report `sigma = max(median accuracy / sqrt(N_eff), 0.5 m)`.
- Returns position, `sigma_m`, `n_used`, `n_discarded`, `duration_s`. The UI shows the sigma and says
  "averaging reduces random jitter, not systematic GPS offset".
- The result is a point estimate; it does not go through the Kalman filter (it is its own
  measurement) but is converted with the active anchor transform.

### 2.6 Calibration (`calibration.py`)

Two coordinate steps: GNSS lat/lon -> ENU (georef) -> **anchor transform** -> plan ENU.

- **1-point ("Calibrate here")**: user stands at (or taps on the map) a known plan point P (canvas
  x, y -> ENU `p`), and the engine takes the Survey-In estimate `g` of the same moment. Translation
  `t = p - g`; stored as an offset with the time and `sigma`. Because GNSS bias wanders over minutes,
  the offset carries an age; after 10 min the badge falls back to "stale anchor" and the UI suggests
  re-calibrating. Not persisted to the plan (session state), because it is a property of today's
  satellites, not of the garden.
- **2-4 point**: reuse `georef.fit_from_reference_points` (similarity by default, affine only when
  `allow_affine` and >= 3 points; refuse 2-point affine). Applied to the **device-frame** points
  (what the GNSS said) vs the **plan-frame** points (where they are on the plan), giving a transform
  `A: gnss_enu -> plan_enu` with `rms_m`. Show rms and refuse (with a message) if rms exceeds 3 m
  or a residual is over 2.5x the median (a mis-tapped point).
- **Corner recording is different**: "Record corner" captures the Survey-In position at a real
  property corner and the user picks which plan corner it is; these points are sent to
  `POST /api/georef/reference-points`, which updates the **plan's georeference** itself (persisted,
  one undo step) exactly like the existing MCP tool. That is the way to fix a satellite image that is
  offset or rotated. The 1-point session anchor is the way to fix today's GNSS bias. The UI keeps the
  two clearly labelled ("fix the map" vs "fix my position today").
- Ground-control markers (printed ArUco/QR) are **deferred**: the page would need to decode them in
  JS; open question 6.

### 2.7 VIO and PDR fusion (`fusion.py`)

Goal: smooth walked geometry. Not goal: better absolute position than the anchor/GNSS gives.

- The phone streams `VioSample` poses (about 30 Hz, decimated to 10 Hz on the phone). The VIO frame
  is arbitrary (origin where the AR session started, gravity-aligned, yaw arbitrary). Take its
  horizontal position `(vx, vy)` (floor plane).
- Keep a sliding window of the last `W = 30 s` or 40 m of travel of paired points: VIO position at the
  time of each filtered GNSS estimate (interpolated), plus the GNSS covariance.
- Estimate the 2D similarity (rotation `theta`, translation, scale fixed at 1: VIO is metric)
  from VIO frame to ENU using weighted **Umeyama** (weights `1/sigma^2`, translation-only until the
  window has >= 8 m of extent and >= 10 points, because heading is unobservable on a short or straight
  walk). Run it each second; low-pass `theta` and translation (`alpha = 0.1`) and reject updates whose
  `theta` changes by more than 10 degrees without a tracking loss.
- Output position `p_fused = T(vio_t)` between GNSS updates (so the puck moves smoothly at 10 Hz), and
  a **complementary correction**: the GNSS-filter position pulls `T`'s translation slowly (time
  constant 20 s). `sigma_m` of the fused estimate is the anchor/GNSS sigma, **not** the VIO sigma,
  because that is the honest absolute number; an additional `relative_sigma_m_per_10m = 0.1` field
  reports the smoothness.
- Tracking `limited`/`lost` for > 1 s: freeze `T`, drop to PDR/GNSS-only, set fix_mode back to the
  anchor/standard level, and discard VIO samples until `tracking` returns (then re-seed the window, do
  not splice across the gap).
- **PDR fallback** (no WebXR, or camera declined): step detection on vertical acceleration (peak in a
  low-passed |a| with a refractory 0.3 s), step length `0.7 m` (user-tunable, learned from GNSS over
  straight stretches), heading from the compass/orientation (absolute, noisy; trust it only through
  a low gain, relative changes from the gyro). Used as a **prediction** between GNSS fixes (replacing
  the constant-velocity prediction) with `sigma_a` raised to 1.5 m/s^2; it never overrides GNSS. In the
  UI it is called "motion-smoothed", and does not set `vio_fused`.
- WebXR availability must be probed on the device (`navigator.xr.isSessionSupported('immersive-ar')`)
  and requires a button tap to start. Entering AR mode takes over the screen; the map shows as a small
  inset (open question 5). This is the highest-risk feature and ships last.

### 2.8 `fix_mode` selection and telemetry

Precedence: `rtk_fixed` (inferred) > `rtk_float` (inferred) > `vio_fused` (tracking good, window
converged) > `anchor_calibrated` (anchor age < 10 min or a plan-georef fit with rms < 2 m done today) >
`standard`. Telemetry payload returned by `POST /api/position` and pushed on `/api/stream` (and shown
by the desktop puck):

```json
{"t": 1767000000.0, "lat": 33.43, "lon": -111.74, "x_cm": 1234.5, "y_cm": -230.1,
 "sigma_m": 2.1, "horizontal_accuracy_m": 3.4, "heading_deg": 87.0, "heading_source": "gps|compass|vio",
 "speed_mps": 0.9, "fix_mode": "anchor_calibrated", "fix_label": "Anchored 2.4 min ago",
 "stationary": false, "gated": false, "quality": "fair", "anchor_age_s": 144,
 "relative_sigma_m_per_10m": null, "samples_used": 5, "samples_gated": 0}
```

`horizontal_accuracy_m` is the reported raw value; `sigma_m` is the filtered, calibrated estimate.
The desktop canvas puck is an overlay item (not a scene object, never saved, not in undo) that listens
to the same telemetry through a Qt signal; toggled in the View menu.

## 3. NTRIP

A desktop NTRIP client cannot correct the phone's internal GNSS: the corrections must reach the GNSS
chip, which only the phone's own software (Mock Location apps) can do. So:

- **Real RTK path (documented, no code from us):** the phone runs SW Maps / RTKGPS+ / similar, connects
  to the caster itself (RTK2go, AZCORS), feeds Android Mock Location, and Chrome's geolocation then
  reports the corrected position. Field Remote just sees better `accuracy_m` and labels fix_mode
  "inferred". Field Remote needs no NTRIP code for this to work; the help page gets a short guide
  and the honest caveat that a phone's internal antenna limits results (decimetres, not centimetres, in
  most cases; a real external receiver is needed for the best results).
- **Optional RTCM relay/diagnostic (recommendation: defer to v2, not v1).** Its only legitimate uses are
  (a) a diagnostic "is the caster reachable, which mountpoints exist, what does the stream contain",
  and (b) forwarding RTCM to a phone app that accepts it over HTTPS or WebSocket, which no common app
  does. The value is low, the privacy surface is real (a desktop opening a TCP connection to a caster
  with user credentials, plus sending the phone's approximate position as NMEA GGA if the caster
  needs it), and honest UI wording must carry a long caveat. Defer.
- **If built (v2) the scope is:** off by default; `services/ntrip_client.py` (stdlib `socket` + `ssl`,
  NTRIP v1/v2 handshake, source-table fetch, RTCM3 frame detection, 1 MB/min cap); Preferences fields
  host, port, mountpoint, username, password (password in the secret store, never in settings
  files or logs); one connection; reconnect backoff; credentials never go to the phone. UI says
  "Diagnostic only: this does not correct your phone's built-in GPS." Disclosure in `PRIVACY.md`
  (what is sent: caster host, mountpoint, username, optional position; when: only while connected) and a
  new `DISCLOSED` row for `services/ntrip_client.py` with phrases that must appear in `PRIVACY.md`
  (`tests/unit/test_privacy_inventory.py`). The same test also needs a row now for
  `field_remote/server.py` (it binds a non-loopback address, so the test's loopback-only allowance
  does not cover it): WP-A must add it together with a "Field Remote" section in `PRIVACY.md`
  (listens only when enabled, on the chosen interface; sends nothing to the internet; the plan,
  photos and positions travel to the paired phone over TLS).

## 4. Mobile UI

### 4.1 Constraints

- Served from memory by the same server, files in `field_remote/static/`: `index.html`, `app.css`,
  `app.js` (ES modules, split into `map.js`, `api.js`, `position.js`, `capture.js`, `store.js`).
  No CDN, no framework, no build step (hand-written ES2020; Android Chrome 100+). Total budget 150 KB
  uncompressed; no webfonts (system UI font); SVG icons inline.
- Rendering: Canvas 2D. Basemap JPEG + scene polylines/polygons drawn from `/api/scene`; pan/zoom with
  pointer events (pinch via two pointers). Y flip and cm-to-metre are handled once in `map.js`.
- Accessibility: touch targets >= 48 px, high-contrast for sun glare, one-handed layout (controls
  at the bottom), Wake Lock API requested while recording to keep the screen on.
- i18n: user-visible phone strings come from a small JSON dictionary served for the desktop's current
  language (`/api/strings`) generated from `tr()` strings on the desktop, so the translation gate
  still covers them.

### 4.2 Screens

1. **Map** (home): puck + accuracy halo (radius = `sigma_m`, grey when poor), compass cone (heading,
   30 degree cone; source shown), fix badge (mode + sigma + anchor age), follow/center button, layer
   visibility toggles, view-only banner when writes are off.
2. **Tap-to-place**: tap puck = "place here" (offers Survey-In first when the user enables it); tap a
   map point = target; a crosshair appears with a **D-pad nudge** (steps 5 / 25 cm, toggle) and
   live coordinates; "Plant" opens the search sheet (§ species search: debounce 200 ms, results from
   `/api/species`, or "no species, just a marker"); confirm -> `POST /api/place`. Result toast with
   "Undo" (calls `/api/undo`).
3. **Survey-In**: a 5 s ring, live n/sigma, discard count, result card (position + sigma + the honest
   note), "Use for this placement" / "Calibrate here" (1-point anchor).
4. **Calibration**: list of up to 4 points (tap a known plan point, then stand there, then Survey-In);
   shows rms and residuals; "Apply to today's position" (session) vs "Fix the map" (plan georef,
   sends corners to `POST /api/georef/reference-points`).
5. **Irrigation**: big toggle-lock button (and optional press-and-hold) with a lock-screen
   guard: Chrome stops geolocation when the screen sleeps, so the page holds a wake lock and warns if
   the page is backgrounded. Live polyline preview on phone (stream also to the desktop). On release:
   preview of the simplified result, pipe diameter / flow / emitter fields (remembered), "Commit" ->
   `POST /api/irrigation-path` -> one undoable polyline on "Irrigation" (created if missing). "Discard"
   throws it away. A recording under 3 m or under 3 points is refused.
6. **Capture**: take a photo with `<input type="file" accept="image/*" capture="environment">` (the
   OS camera, so the photo keeps its EXIF GPS and bearing when the user has location tagging on; the
   camera app strips nothing, but Chrome's own canvas re-encoding would, so **never re-encode**).
   Then plant name (free text + species search), age, notes, position (current puck, with
   `fix_mode`), optional bearing (the compass reading at capture, also stored as `bearing_deg`) ->
   `POST /api/field-records`. Records appear in a list; tapping shows the photo. A record can later be
   placed by the agent (`place_geotagged_feature`) or by the user from this list (`/api/place` with
   `field_record_id`).
7. **Settings/Status**: connection, server time skew, fingerprint, storage use, outbox count.

### 4.3 Permissions flow

Geolocation prompt on first map open; DeviceOrientation permission (needs a tap on some browsers);
camera only for the file input (the OS handles it) and for WebXR; explain why before each. If denied,
the screen degrades (map only, manual tap placement still works).

### 4.4 Offline and spotty signal

- The page and the basemap are cached by a Service Worker (secure context allows it, scope `/`), so a
  dead Wi-Fi does not blank the app; cache versioned by the server build id.
- **Outbox**: every write goes first into IndexedDB (`outbox` store) with its `request_id`, then is
  sent; failures retry with exponential backoff (2, 4, 8 ... 60 s) and on `online`/focus events.
  Photos are stored as Blob. Banner "3 waiting to sync". The server's idempotency (§1.6) makes this
  safe. Entries older than 14 days or the server returning 4xx are shown to the user, not retried
  forever.
- Position keeps being collected on the phone while offline; breadcrumbs for a recording live in the
  page and are committed when the connection returns (recording is allowed offline, commit waits).
- The filter is on the **server**, so while offline the puck shows raw fixes labelled "offline,
  unfiltered"; a thin client-side fallback (exponential smoothing) keeps the puck stable. After
  reconnect the phone posts the buffered samples in time order (`/api/position` batch) so the server
  can build the track with the proper filter; a 50-sample cap per batch.
- Clock: the phone's timestamps are used for filtering; the server records the skew and rejects a
  batch whose clock differs by > 120 s from the server (status shown).

## 5. Delivery plan

Sizing: each package fits one Sonnet session (about 1-2 days of review, at most ~600 changed lines
plus tests). Opus reviews are marked **[Opus]**. Gates for every package: `ruff check src/`,
`bandit -r src/ --severity-level high`, translations if UI strings change, and the targeted tests.
Test command default: `venv/Scripts/python.exe -m pytest <paths> -v -n 0`.

| # | Package | Files and interfaces | Tests | Review |
|---|---|---|---|---|
| WP-A | Server skeleton, TLS, auth, prefs, privacy | `field_remote/{__init__,server,auth,tls,limits}.py`; `app/field_remote_host.py` (`FieldRemoteHost.start/stop/status`, signal `status_changed`); prefs keys `field_remote/{enabled,bind,port,writes,token_ref}`; `ui/dialogs/field_remote_dialog.py` (pairing QR); `PRIVACY.md` + `DISCLOSED["field_remote/server.py"]`; pin `cryptography` in `pyproject.toml`; ADR-081 | `tests/unit/test_field_remote_auth.py` (token, lockout, header-only, Host check), `test_field_remote_limits.py` (body caps, rate bucket), `test_field_remote_tls.py` (SAN, EC key, regenerate), `tests/integration/test_field_remote_lifecycle.py` (off by default, starts, serves 403 without token, binds only the chosen address), `tests/unit/test_privacy_inventory.py` | **[Opus]** security |
| WP-B | Positioning core | `core/positioning/*` as in §2 (`engine.py`: `PositioningEngine.add_gnss(s)->PositionEstimate`, `add_vio`, `add_imu`, `set_anchor`, `survey_in(samples)->SurveyResult`, `finish_track(track)->list[Vertex]`) | `tests/unit/test_positioning_{kalman,gating,zupt,breadcrumbs,survey,calibration}.py`; **replay harness** `tests/unit/test_positioning_replay.py` with synthetic walks (straight, L-shape, stand-still, multipath spike, accuracy ramp) with fixed seeds; assertions on RMS vs truth ratio (filtered < 0.8 x raw) not absolute metres; golden numbers for survey and calibration | **[Opus]** kalman/gating/survey math; Sonnet for plumbing |
| WP-C | Read API + live telemetry | `field_remote/routes.py` (`/state`, `/scene`, `/basemap`, `/species`, `/field-records`, `/position`, `/stream`), scene projection in `field_remote/projection.py` (Qt-free from the Agent API's existing serialisers), `FieldRemoteHost` hooks `position_estimate` signal, desktop puck overlay `ui/canvas/items/position_puck.py` (not in undo/saves) | route tests with `starlette.testclient`; projection golden; integration test: post positions -> puck item present; screenshot via `render_canvas_image` not needed | Sonnet |
| WP-D | Write API | providers `EnsureLayerProvider` in `agent_api/providers.py` (or `field_remote/providers.py`; do not widen the Agent API surface); `/place`, `/irrigation-path`, `/field-records` (multipart), `/georef/reference-points`, `/undo`; idempotency cache; origin tag on commands | `tests/integration/test_field_remote_writes.py`: toggle off -> 403 for each; locked layer -> 409; Irrigation layer created in one macro with one undo; photo cap; concurrent sidecar writes (8x50 lose none); idempotent retry; unsaved plan -> 409; `os.getpid()` single-process assertion | **[Opus]** write path and locks |
| WP-E | Mobile UI: map, place, species, capture | `field_remote/static/*` | `tests/unit/test_field_remote_static.py` (files exist, size budget, no `http://`/`https://` URLs except the page's own origin, no `eval`, CSP header, no inline script); headless smoke via the built-in browser pane/a desktop browser against `127.0.0.1` | JS review by Opus for CSP/XSS **[Opus, short]** |
| WP-F | Survey-In, calibration, irrigation recording UI | `static/{position,survey,calib,record}.js`; engine plumbing for `survey/start|finish` | unit tests of JS-free parts via the pure engine; integration test: posted synthetic walk -> committed polyline vertices (count, length within 5 % of truth, one undo) | Sonnet |
| WP-G | Offline outbox + service worker | `static/{sw.js,store.js}` | static tests for SW cache list; manual | Sonnet |
| WP-H | WebXR VIO fusion | `core/positioning/fusion.py` (already specified in WP-B but implemented here: `FusionAligner.add_vio/add_gnss -> Transform2D`); `static/xr.js` | unit tests with synthetic VIO frames (arbitrary rotation + drift + tracking loss); no on-device claim | **[Opus]** fusion, and **device-only validation** |
| WP-I | Docs, wiki sources, FR-GEO entries, glossary, roadmap row | docs per the project table | `scripts/sync_wiki.py --check`, `check_agent_context.py` | Sonnet |
| WP-J (v2, deferred) | NTRIP diagnostic/relay | `services/ntrip_client.py`, prefs, privacy rows | unit tests with a fake caster (local socket) | **[Opus]** |

Order: A, B (can run in parallel with A; disjoint files), C, D, E, F, G, H, I. A and D are the two
that must not be delegated without Opus review of the final diff (policy: security-sensitive and
single-writer logic).

### 5.1 What only the user can test, on the phone (Android Chrome)

- Cert warning and fingerprint step; the page loads over LAN and over Tailscale.
- Geolocation permission and real accuracy readings in the yard (near the house, under trees, open
  lawn). Record the reported accuracies; they set `r_min`.
- Compass direction source and screen-sleep behaviour with the page open (wake lock works?).
- Camera capture: does the photo keep EXIF GPS and bearing? (Depends on the camera app setting.)
- Survey-In sanity (stand 5 s at a known corner; compare against the plan).
- Walk a known 20 m straight path (hose reel to tap, say) and compare filtered length and straightness
  with the tape measure. This is the only way to tune `sigma_a`/`d_min`.
- WebXR AR session: does Chrome offer it on this phone, does tracking survive blank lawn/gravel, how
  much battery does 5 minutes use, can the user keep the phone out for a long walk.
- Spotty signal: toggle airplane mode in the middle of a placement and a recording; verify the outbox
  sends exactly one copy.
- RTK path (optional): SW Maps + external receiver feeding Mock Location, confirm the accuracy
  figures Chrome reports.

### 5.2 Risks

1. **Android Chrome stops geolocation when the screen sleeps or the page is backgrounded.** The wake
   lock helps; a PWA install does not remove it. The user must keep the screen on. Mitigation: warn,
   and keep the buffer client-side.
2. **Self-signed cert friction.** Mitigations in §1.3. A bad pairing experience will make the feature
   unused; budget a polished pairing dialog.
3. **Reported accuracy is not truth.** The filter weights by a number that can be optimistic;
   hence `r_min` and the replay harness, but real-world tuning needs the on-device walk.
4. **WebXR on a given phone/Chrome may not exist**; the feature must degrade silently to PDR/GNSS and
   the roadmap/FR text must call it "when supported".
5. **Single-thread GUI bridge latency.** Dozens of position posts per second must not each hop to the
   Qt thread; the engine runs on the server thread and only the puck signal and writes touch Qt.
6. **Scope creep in the phone page.** Hold the line at the screens in §4.2.
7. **Licensing/dependency**: `cryptography` is permissively licensed (Apache-2.0/BSD) and compatible
   with GPLv3; the vendored QR encoder (if any) needs its licence file; `ogp-external-positioning` check
   happens in WP-A. No public claim of centimetre accuracy anywhere.
8. **Exe packaging**: the static files and a TLS-only `ssl` need the PyInstaller spec's data list;
   `--selftest` already fails on this machine on an `ssl` loader error (0xc0000139), which would also
   break the TLS server in the exe: investigate the cause before WP-A closes, or HTTPS cannot ship.

### 5.3 Open questions for the user

1. Phone writes toggle: a separate "Allow the phone to edit the plan" (recommended) or reuse the
   agent-edit toggle?
2. Is the plan always saved to disk before field work (sidecar needs a path)? OK to refuse
   phone writes on an unsaved plan?
3. Per-phone tokens and fingerprint pinning (stronger, more UI) or one shared token (v1)?
4. Is Android Chrome the only phone browser to support? (Firefox/Samsung Internet change the
   WebXR/permissions story.)
5. WebXR full-screen AR takes the screen over: acceptable for irrigation walks, with the map as a
   small inset?
6. Ground-control QR/ArUco markers: defer? (Needs JS decoding; the project has no vendored decoder.)
7. NTRIP relay: confirm "defer to v2" and put the RTK how-to (SW Maps/RTKGPS+) in the wiki instead.
8. Tailscale: do you already have `tailscale cert` set up, so WP-A should document it rather than
   build around the self-signed flow?
9. Photo storage size cap and retention for the sidecar (the store has 25 MB per photo and 5000
   records): is that enough?
