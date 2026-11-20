import { clockNow } from './api.js';

// Survey-In and calibration (design 2.5, 2.6, 4.2 items 3 and 4). Stand still, average the
// phone's own fixes for a few seconds, then either fix today's position to a known plan spot
// (session only) or send corner recordings that change the georeference only once approved.
const SURVEY_SECONDS = 5;
const MAX_POINTS = 4;

export function initSurvey(ctx) {
  const { S, $, el, clear, toast, api, errorText, uuid, map } = ctx;
  let last = null; // the latest survey result
  let busy = false;
  const points = []; // {survey_id, x, y, label}

  function note(text, cls) {
    const box = $('survey-result');
    clear(box); box.hidden = false;
    box.appendChild(el('div', text, cls));
    return box;
  }

  async function run() {
    if (busy) return;
    if (S.watch === null) { toast('Turn GPS on first (the GPS button on the map).'); return; }
    busy = true;
    const btn = $('btn-survey');
    const t0 = clockNow();
    S.raw.length = 0;
    try {
      for (let i = SURVEY_SECONDS; i > 0; i -= 1) {
        btn.textContent = 'Stand still... ' + i;
        await new Promise((ok) => setTimeout(ok, 1000));
      }
      const samples = S.raw.filter((s) => s.t >= t0 - 1);
      if (samples.length < 4) { note('Too few fixes arrived (' + samples.length + '). Keep GPS on and try again.', 'warn'); return; }
      const r = await api('/api/survey', { method: 'POST', body: { samples } });
      if (!r.ok) { note(errorText(r), 'bad'); return; }
      last = r.json;
      showResult();
    } finally {
      btn.textContent = 'Survey here (' + SURVEY_SECONDS + ' s)';
      busy = false;
    }
  }

  function showResult() {
    const box = note('Averaged ' + last.n_used + ' fixes (' + last.n_discarded + ' discarded) over ' + last.duration_s + ' s.');
    box.appendChild(el('div', 'Spread: ±' + last.sigma_m.toFixed(1) + ' m. ' + last.note, 'hint'));
    if (last.sigma_m > 3) box.appendChild(el('div', 'That is a wide spread: move away from walls and trees and survey again.', 'warn'));
    $('survey-actions').hidden = false;
    $('target-state').textContent = S.target
      ? 'Known plan spot: ' + (S.target.x / 100).toFixed(2) + ', ' + (S.target.y / 100).toFixed(2) + ' m (the spot you last marked on the map).'
      : 'To calibrate, first tap the known spot on the map, then come back and survey while standing on it.';
  }

  function needSpot() {
    if (!last) { toast('Survey first.'); return false; }
    if (!S.target) { toast('Tap the known spot on the map first.'); return false; }
    return true;
  }

  async function fixToday() {
    if (!needSpot()) return;
    const r = await api('/api/calibrate', { method: 'POST', body: { points: [{ survey_id: last.survey_id, plan_x_cm: S.target.x, plan_y_cm: S.target.y }] } });
    toast(r.ok ? 'Position corrected for this session.' : errorText(r));
    if (r.ok) renderCal({ kind: 'gnss_point' });
  }

  function addPoint() {
    if (!needSpot()) return;
    if (points.length >= MAX_POINTS) { toast('At most ' + MAX_POINTS + ' points.'); return; }
    points.push({ survey_id: last.survey_id, x: S.target.x, y: S.target.y });
    last = null; $('survey-actions').hidden = true;
    renderPoints();
  }

  function renderPoints() {
    const ul = $('cal-points');
    clear(ul);
    points.forEach((p, i) => {
      const li = el('li', 'Point ' + (i + 1) + ': plan ' + (p.x / 100).toFixed(2) + ', ' + (p.y / 100).toFixed(2) + ' m');
      const rm = el('button', 'Remove');
      rm.addEventListener('click', () => { points.splice(i, 1); renderPoints(); });
      li.appendChild(rm);
      ul.appendChild(li);
    });
    $('btn-fit').hidden = points.length < 2;
  }

  async function fit() {
    const body = { points: points.map((p) => ({ survey_id: p.survey_id, plan_x_cm: p.x, plan_y_cm: p.y })) };
    const r = await api('/api/calibrate', { method: 'POST', body });
    if (!r.ok) { toast(errorText(r)); return; }
    renderCal(r.json);
  }

  function renderCal(c) {
    const box = $('cal-result');
    clear(box); box.hidden = false;
    if (c.kind === 'gnss_point') box.appendChild(el('div', 'One-point correction active for this session.'));
    else {
      box.appendChild(el('div', c.n_points + '-point fit, rms ' + c.fit_rms_m.toFixed(2) + ' m'));
      box.appendChild(el('div', 'Residuals: ' + c.residuals_m.map((v) => v.toFixed(2)).join(', ') + ' m', 'hint'));
      if (c.fit_rms_m > 1) box.appendChild(el('div', 'A residual that large suggests a mis-tapped point.', 'warn'));
    }
    S.calibrated = true;
    $('btn-uncal').hidden = false;
  }

  async function clearCal() {
    const r = await api('/api/calibrate', { method: 'DELETE' });
    if (r.ok) { $('cal-result').hidden = true; $('btn-uncal').hidden = true; S.calibrated = false; points.length = 0; renderPoints(); toast('Correction removed.'); }
  }

  async function sendCorner() {
    if (!needSpot()) return;
    if (!S.state || !S.state.records_enabled) { toast('Saving is turned off on the computer.'); return; }
    if (!(await ctx.ensurePlanId())) return;
    const name = $('corner-name').value.trim();
    if (!name) { toast('Name the spot (for example "NE corner").'); return; }
    const body = {
      kind: 'reference_point', idempotency_key: uuid(), name,
      geometry: { type: 'point', lat: last.lat, lon: last.lon, x_cm: Math.round(S.target.x * 10) / 10, y_cm: Math.round(S.target.y * 10) / 10 },
      sigma_m: last.sigma_m, precision_hint: 'survey_in',
    };
    const r = await ctx.post(body, null);
    toast(r.ok ? 'Saved for review on the computer' : errorText(r));
    if (r.ok) $('corner-name').value = '';
  }

  $('btn-survey').addEventListener('click', run);
  $('btn-fix-today').addEventListener('click', fixToday);
  $('btn-add-point').addEventListener('click', addPoint);
  $('btn-fit').addEventListener('click', fit);
  $('btn-uncal').addEventListener('click', clearCal);
  $('btn-corner').addEventListener('click', sendCorner);
  void map;
}

// ---- AR lock (experimental) and motion samples (design 2.8). WebXR is probed, never assumed: the
// card stays hidden unless the browser says immersive-ar is supported, and nothing starts without
// a tap. No frame, camera image or pixel is ever sent anywhere; only pose numbers are.
// Unverified on a real phone: see docs/design/wph-fusion-notes.md (WebXR pitfalls).
const SEND_EVERY_MS = 1000;
const VIO_MIN_DT_S = 0.1; // at most 10 pose samples per second
const MAX_QUEUE = 150;
const UNLOCKED_TEXT = 'Unlocked: re-lock A and B';
const DRAIN_SPACING_MS = 400; // a drain posts at most 2.5/s, so the 5/s write limit keeps room for GNSS fixes
const RETRY_AFTER_MAX_MS = 5000;

// How long a 429 asks the page to wait (its Retry-After, in seconds), as ms; 1 s when absent.
export function retryAfterMs(r) {
  const h = r && r.res && r.res.headers ? r.res.headers.get('retry-after') : null;
  const s = h == null ? NaN : Number(h);
  return Number.isFinite(s) && s > 0 ? Math.min(s * 1000, RETRY_AFTER_MAX_MS) : 1000;
}
const wait = (ms) => new Promise((ok) => setTimeout(ok, ms));

// Runs `step` up to `tries` times and never posts back to back: after each step that posted
// without finishing ({ wait: ms }) it sleeps the drain spacing, or longer when a 429 asked for it.
// step() resolves to true (done), false (failed) or { wait: ms }. Resolves to true or false.
export async function paced(step, tries = 8, sleep = wait) {
  for (let i = 0; i < tries; i += 1) {
    const got = await step();
    if (got === true || got === false) return got;
    if (i + 1 < tries) await sleep(Math.max(DRAIN_SPACING_MS, got.wait || 0));
  }
  return false;
}


// ---- Tree height (design 4.2, "Tree height"). Every number is computed on the computer
// (POST /api/ar/height, core/positioning/tree_height.py); this page sends raw poses and shows the
// answer. The helpers below are pure so a Node test can pin them.
export const HEIGHT_WARNINGS = {
  crown_pitch_steep: 'The crown is steeply above you (over 70 degrees): step back and measure again.',
  base_pitch_not_down: 'The base tap was not aimed down at the foot of the trunk.',
  roll_high: 'The phone was tilted sideways (over 25 degrees): hold it upright.',
  taps_too_close_in_time: 'The two taps came less than half a second apart.',
  tracking_not_good: 'AR tracking was not steady at a tap: look around slowly and retake.',
  distance_too_short: 'Under 2 m from the trunk: step back.',
  distance_too_long: 'Over 40 m from the trunk: move closer.',
  ground_off_plane: 'The ground point looks far from where the phone is held: aim the cross at the soil at the trunk foot.',
  ground_geometry_mismatch: 'The ground point and the aim at it disagree about the distance: retake the base tap.',
  distance_disagreement: 'The typed distance and the AR distance differ by more than 10 %; the typed one is used.',
  orientation_only_coarse: 'Phone tilt only: coarser than AR.',
};
const MODE_TEXT = {
  hit_test: 'AR ground point',
  manual: 'Typed distance',
  both: 'Typed distance (AR distance also taken)',
  manual_orientation: 'Typed distance, phone tilt',
};

// "12,5" and "12.5" are both 12.5 m; anything else (or a non-positive number) is null.
export function parseMetres(text) {
  const t = String(text == null ? '' : text).trim().replace(',', '.');
  if (!/^\d{1,4}(\.\d{1,3})?$/.test(t)) return null;
  const v = Number(t);
  return v > 0 && v <= 1000 ? v : null;
}

// One tap: the viewer pose at that moment, as the server's TapIn model wants it.
export function tapSample(pose, tracking, t) {
  return { position: [pose.x, pose.y, pose.z], quaternion: [pose.qx, pose.qy, pose.qz, pose.qw], tracking, t };
}

// The request body from what was captured; only the parts that exist are sent.
export function heightRequest({ tap1, tap2, ground, tapeM, tilt1, tilt2 }) {
  const body = {};
  if (tilt1 && tilt2) {
    body.tilt1 = { beta_deg: tilt1.beta, gamma_deg: tilt1.gamma };
    body.tilt2 = { beta_deg: tilt2.beta, gamma_deg: tilt2.gamma };
  } else {
    if (tap1) body.tap1 = tap1;
    if (tap2) body.tap2 = tap2;
    if (ground) body.ground = { x: ground.x, y: ground.y, z: ground.z };
  }
  if (tapeM != null) body.manual_distance_m = tapeM;
  return body;
}

export const m1 = (v) => Number(v).toFixed(1) + ' m';

// What the result card shows: a headline, the +/- range with its honest label, details, warnings.
export function describeHeight(r) {
  const lines = [];
  lines.push('Distance to the trunk: ' + m1(r.d_m) + (r.eye_height_m != null ? '; phone held ' + m1(r.eye_height_m) + ' above the ground point' : ''));
  lines.push('Aimed ' + Number(r.pitch_base_deg).toFixed(0) + ' degrees at the base and ' + Number(r.pitch_crown_deg).toFixed(0) + ' degrees at the crown');
  if (r.hit_test) lines.push('AR distance ' + m1(r.hit_test.d_m) + ', height ' + m1(r.hit_test.height_m) + ' (they differ by ' + (r.distance_disagreement_rel * 100).toFixed(0) + ' %)');
  lines.push('Method: ' + (MODE_TEXT[r.mode] || r.mode));
  return {
    headline: 'Height about ' + m1(r.height_m),
    range: 'Between ' + m1(r.range_m[0]) + ' and ' + m1(r.range_m[1]) + ' if each tap is off by ' + r.assumed.angle_error_deg + ' degree and the distance by ' + Math.round(r.assumed.distance_rel_error * 100) + ' %. These are assumed errors, not a measured accuracy.',
    lines,
    warnings: (r.warnings || []).map((w) => HEIGHT_WARNINGS[w] || w),
  };
}

// The pending measurement record for a result. Raw inputs and the computed answer sit in
// height_data (small, flat); the trunk's plan spot is the geometry when an AR lock gave one.
export function heightRecordBody(result, req, { key, name, link }) {
  const inputs = {};
  if (req.tap1) Object.assign(inputs, { tap1_position: req.tap1.position, tap1_quaternion: req.tap1.quaternion, tap1_tracking: req.tap1.tracking, tap1_t: req.tap1.t });
  if (req.tap2) Object.assign(inputs, { tap2_position: req.tap2.position, tap2_quaternion: req.tap2.quaternion, tap2_tracking: req.tap2.tracking, tap2_t: req.tap2.t });
  if (req.ground) inputs.ground = [req.ground.x, req.ground.y, req.ground.z];
  if (req.tilt1) inputs.tilt1 = [req.tilt1.beta_deg, req.tilt1.gamma_deg];
  if (req.tilt2) inputs.tilt2 = [req.tilt2.beta_deg, req.tilt2.gamma_deg];
  if (req.manual_distance_m != null) inputs.manual_distance_m = req.manual_distance_m;
  const data = {
    mode: result.mode, uncertainty_m: result.uncertainty_m, range_m: result.range_m, d_m: result.d_m,
    pitch_base_deg: result.pitch_base_deg, pitch_crown_deg: result.pitch_crown_deg,
    warnings: result.warnings, assumed: result.assumed, inputs,
  };
  if (result.eye_height_m != null) data.eye_height_m = result.eye_height_m;
  const body = { kind: 'measurement', idempotency_key: key, height_m: result.height_m, height_data: data };
  if (name) body.plant_name = name;
  if (link) body.linked_object_id = link;
  const tp = result.trunk_plan;
  if (tp) {
    body.geometry = { type: 'point', lat: tp.lat, lon: tp.lon, x_cm: tp.x_cm, y_cm: tp.y_cm };
    body.fix_mode = 'vio_fused';
    body.precision_hint = 'plan_locked';
  }
  return body;
}

export function initXr(ctx) {
  const { S, $, toast, api, errorText, map, showFix } = ctx;
  const card = $('xr-card');
  const overlay = $('xr-overlay');
  const status = $('xr-status');
  const banner = $('xr-banner');
  let xr = null; // { session, local, hit, gl }
  let tracking = 'lost';
  let pose = null; // last viewer position {x, y, z}
  let poseT = 0; // frame time of that pose, epoch seconds
  let mode = 'lock'; // what the overlay shows: 'lock' or 'height' (the tree-height taps)
  let tree = null; // the tree-height controller, created below
  let hit = null; // last reticle hit {x, y, z}
  let lastSent = 0;
  let lastT = 0;
  let flushTimer = 0;
  let locked = false;
  let mapHome = null;
  let nextId = 1;
  let inFlight = null; // the request now on its way: never two at once, never the same slice twice
  let lastVioT = null; // t of the last pose the server acknowledged
  let alertText = ''; // the server's GNSS disagreement alert, shown beside the banner text
  let bannerText = '';
  let retryMs = 0; // set by a 429 answer: how long the server asked the page to wait
  let vioQ = []; // { id, s }: the id survives queue trimming, so an ack removes exactly what was sent
  let imuQ = [];
  function enqueue(q, s) {
    q.push({ id: nextId++, s });
    if (q.length > MAX_QUEUE) q.splice(0, q.length - MAX_QUEUE);
  }

  function say(text, cls) {
    status.textContent = text;
    status.className = 'hint ' + (cls || '');
  }
  function setBanner(text) {
    bannerText = text || '';
    const shown = [bannerText, alertText].filter(Boolean).join(' · ');
    banner.textContent = shown; // textContent only: server text never becomes markup
    banner.hidden = !shown;
  }
  function ready() { return Boolean(hit) && tracking === 'tracking'; }
  function refreshButtons() {
    for (const id of ['xr-a', 'xr-b', 'xr-c']) $(id).disabled = !xr || !ready();
    $('xr-unlock').disabled = !xr;
    if (tree) tree.refresh();
  }
  function setMode(next) {
    mode = next;
    overlay.dataset.mode = next;
    for (const node of overlay.querySelectorAll('[data-for]')) node.hidden = node.dataset.for !== next;
    $('xr-th').textContent = next === 'height' ? 'Plan lock' : 'Tree height';
    if (next === 'height' && tree) tree.begin();
    refreshButtons();
  }

  // ---- sending -----------------------------------------------------------------------------

  function onTelemetry(t) {
    if (!t) return;
    const next = t.fused && t.fused.gnss_alert ? String(t.fused.gnss_alert) : '';
    if (next !== alertText) { alertText = next; setBanner(bannerText); }
    const state = t.fused ? t.fused.state : 'unlocked';
    if (locked && state === 'unlocked') {
      locked = false;
      setBanner(UNLOCKED_TEXT); // the server dropped the lock; never resumed, only re-locked
      say(UNLOCKED_TEXT, 'warn');
    }
  }

  async function sendOnce() {
    if (!vioQ.length && !imuQ.length) return true;
    const vio = vioQ.slice(0, 50), imu = imuQ.slice(0, 50);
    const body = { vio_batch: vio.map((i) => i.s), imu_batch: imu.map((i) => i.s) };
    const r = await api('/api/position', { method: 'POST', body }).catch(() => null);
    if (!r || r.status === 429) { if (r) retryMs = retryAfterMs(r); return false; } // keep them; retried later
    const vMax = vio.length ? vio[vio.length - 1].id : 0, iMax = imu.length ? imu[imu.length - 1].id : 0;
    vioQ = vioQ.filter((i) => i.id > vMax); // remove what was sent, by id, after the answer
    imuQ = imuQ.filter((i) => i.id > iMax);
    if (vio.length) lastVioT = vio[vio.length - 1].s.t;
    if (!r.ok) { if (r.status !== 409) toast(errorText(r)); return false; }
    const t = r.json.telemetry;
    if (t.x_cm != null) showFix(t);
    onTelemetry(t);
    return true;
  }
  function flush() {
    if (!inFlight) inFlight = sendOnce().finally(() => { inFlight = null; });
    return inFlight;
  }
  // Before a lock: everything queued (a reset or a loss included) must reach the server first.
  // Posts are spread by paced(): a burst of drain posts must not starve the GNSS fixes of the limit.
  async function drainStep(untilT) {
    if (inFlight) { await inFlight; return { wait: 0 }; }
    if (lastVioT !== null && lastVioT >= untilT) return true;
    if (!vioQ.length) return false;
    retryMs = 0;
    if (await flush()) return { wait: 0 };
    return retryMs ? { wait: retryMs } : false; // a 429 waits its Retry-After; no answer gives up
  }
  async function drain(untilT) {
    return (await paced(() => drainStep(untilT))) || (lastVioT !== null && lastVioT >= untilT);
  }
  function startFlush() { if (!flushTimer) flushTimer = setInterval(flush, SEND_EVERY_MS); }

  // ---- motion (phone sensors, for the step counter when there is no AR) --------------------

  let motionOn = false;
  async function startMotion() {
    if (motionOn || typeof window.DeviceMotionEvent === 'undefined') return;
    try {
      if (typeof DeviceMotionEvent.requestPermission === 'function' && (await DeviceMotionEvent.requestPermission()) !== 'granted') return;
    } catch (e) { return; }
    motionOn = true;
    let heading = null;
    window.addEventListener('deviceorientation', (e) => {
      // Only iOS gives a compass heading the system has tilt-compensated; elsewhere none is sent.
      heading = typeof e.webkitCompassHeading === 'number' && Number.isFinite(e.webkitCompassHeading) ? e.webkitCompassHeading : null;
    });
    let last = 0;
    window.addEventListener('devicemotion', (e) => {
      const a = e.accelerationIncludingGravity;
      const now = clockNow();
      if (!a || now - last < 0.04) return; // about 25 per second
      last = now;
      const v = (n) => (typeof n === 'number' && Number.isFinite(n) ? n : 0);
      const item = { t: now, ax: v(a.x), ay: v(a.y), az: v(a.z) };
      if (heading !== null) { item.heading_deg = heading; item.absolute = true; }
      enqueue(imuQ, item);
    });
    startFlush();
  }

  // ---- the AR session ----------------------------------------------------------------------

  function onFrame(time, frame) {
    if (!xr) return;
    xr.session.requestAnimationFrame(onFrame);
    const vp = frame.getViewerPose(xr.local);
    const t = (performance.timeOrigin + time) / 1000; // frame time: the same timeline as clockNow()
    if (vp) {
      const p = vp.transform.position, q = vp.transform.orientation;
      pose = { x: p.x, y: p.y, z: p.z, qx: q.x, qy: q.y, qz: q.z, qw: q.w };
      poseT = t;
      tracking = vp.emulatedPosition ? 'limited' : 'tracking';
    } else {
      tracking = 'lost'; // no pose: the last position is sent so the server sees the loss
    }
    const results = xr.hit ? frame.getHitTestResults(xr.hit) : [];
    const hp = results.length ? results[0].getPose(xr.local) : null;
    hit = hp ? { x: hp.transform.position.x, y: hp.transform.position.y, z: hp.transform.position.z } : null;
    if (pose && t - lastT >= VIO_MIN_DT_S) {
      lastT = t;
      enqueue(vioQ, { t, ...pose, tracking });
    }
    if (t - lastSent > 0.25) {
      lastSent = t;
      refreshButtons();
      if (tree) tree.frame();
      if (mode === 'lock' && !ready() && xr) say(tracking !== 'tracking' ? 'Tracking ' + tracking + ': hold still and look around' : 'Point at the ground to place the reticle');
    }
  }

  function moveMap(into) {
    const canvas = $('map');
    if (into) {
      mapHome = { parent: canvas.parentNode, next: canvas.nextSibling };
      overlay.insertBefore(canvas, overlay.firstChild);
    } else if (mapHome) {
      mapHome.parent.insertBefore(canvas, mapHome.next);
      mapHome = null;
    }
    setTimeout(() => map.resize(), 0);
  }

  function onEnd() {
    const wasActive = Boolean(xr);
    xr = null; hit = null; pose = null; tracking = 'lost';
    if (tree) tree.ended();
    overlay.hidden = true;
    document.body.classList.remove('xr-on');
    moveMap(false);
    refreshButtons();
    if (wasActive) {
      locked = false;
      setBanner(UNLOCKED_TEXT);
      api('/api/ar/unlock', { method: 'POST', body: {} }).catch(() => null);
      say(UNLOCKED_TEXT + ' (the AR session ended)', 'warn');
    }
  }

  function openSession(required) {
    return navigator.xr.requestSession('immersive-ar', {
      requiredFeatures: required, optionalFeatures: ['dom-overlay'], domOverlay: { root: overlay },
    });
  }

  async function start(which) {
    if (xr) return;
    const heightOnly = which === 'height';
    if (!heightOnly && S.target == null) { toast('Tap the known spot on the map first, then start AR.'); return; }
    let session = null;
    let noHit = false;
    try {
      try {
        session = await openSession(['hit-test', 'local']);
      } catch (e) {
        // A phone without hit-test can still give poses: the tree height then needs a tape distance.
        if (!heightOnly || !e || e.name !== 'NotSupportedError') throw e;
        session = await openSession(['local']);
        noHit = true;
      }
      const gl = document.createElement('canvas').getContext('webgl', { xrCompatible: true, alpha: true });
      if (!gl) { session.end().catch(() => null); toast('This browser cannot start AR here.'); return; }
      await session.updateRenderState({ baseLayer: new XRWebGLLayer(session, gl) });
      const local = await session.requestReferenceSpace('local');
      const viewer = await session.requestReferenceSpace('viewer');
      const src = noHit ? null : await session.requestHitTestSource({ space: viewer });
      xr = { session, local, hit: src, gl };
      local.addEventListener('reset', () => {
        const t = Math.max(clockNow(), lastT); // never behind the last queued pose
        lastT = t;
        enqueue(vioQ, { t, ...(pose || { x: 0, y: 0, z: 0, qx: 0, qy: 0, qz: 0, qw: 1 }), tracking: 'limited', reset: true });
        locked = false; setBanner(UNLOCKED_TEXT);
      });
      session.addEventListener('end', onEnd);
      overlay.hidden = false;
      document.body.classList.add('xr-on');
      if (!heightOnly) moveMap(true);
      setMode(heightOnly ? 'height' : 'lock');
      setBanner(heightOnly ? '' : locked ? '' : 'Stand on a known spot, then lock A. Walk to a second spot, lock B.');
      startFlush();
      session.requestAnimationFrame(onFrame);
      refreshButtons();
    } catch (e) {
      if (e && e.name === 'NotAllowedError') toast('Camera denied: GNSS and step mode continue.');
      else toast('AR could not start on this phone.');
      xr = null;
      if (session) session.end().catch(() => null);
    }
  }

  async function lock(which) {
    if (!xr || !ready()) { toast('Wait for a steady reticle on the ground.'); return; }
    if (S.target == null) { toast('Tap the known spot on the map first.'); return; }
    const taken = { x: hit.x, y: hit.y, z: hit.z };
    const takenT = lastT; // the last pose queued when the hit was taken: the hit is in its frame
    // Send the queued poses up to that one first, then name it: the server refuses the lock
    // until its stream has reached it, and if a reset or loss broke the frame after it.
    if (!takenT || !(await drain(takenT))) { say('Sending tracking data... try again in a moment.', 'warn'); return; }
    const body = { plan: { x_cm: S.target.x, y_cm: S.target.y }, hit: taken, vio_t: takenT };
    const r = await api(which === 'c' ? '/api/ar/check' : '/api/ar/lock/' + which, { method: 'POST', body });
    if (!r.ok) { say(errorText(r), 'bad'); return; }
    if (which === 'a') { say('A captured. Walk to the second known spot, tap it on the map, then lock B.'); return; }
    if (which === 'c') { say('closure at C: ' + Number(r.json.closure_m).toFixed(2) + ' m'); return; }
    locked = true;
    setBanner('');
    const sc = r.json.scale;
    say((sc && !sc.ok ? 'Locked, but ' : 'Locked. ') + (sc ? sc.message : ''), sc && !sc.ok ? 'warn' : '');
  }

  async function unlock() {
    await api('/api/ar/unlock', { method: 'POST', body: {} }).catch(() => null);
    locked = false;
    setBanner(UNLOCKED_TEXT);
    say(UNLOCKED_TEXT, 'warn');
  }

  document.addEventListener('visibilitychange', () => { if (document.hidden && locked) { locked = false; setBanner(UNLOCKED_TEXT); } });
  $('xr-start').addEventListener('click', () => start('lock'));
  $('xr-th').addEventListener('click', () => setMode(mode === 'height' ? 'lock' : 'height'));
  $('xr-end').addEventListener('click', () => { if (xr) xr.session.end().catch(() => null); });
  $('xr-a').addEventListener('click', () => lock('a'));
  $('xr-b').addEventListener('click', () => lock('b'));
  $('xr-c').addEventListener('click', () => lock('c'));
  $('xr-unlock').addEventListener('click', unlock);

  // Probe only: the card is shown when the browser reports immersive-ar support.
  if (navigator.xr && typeof navigator.xr.isSessionSupported === 'function') {
    navigator.xr.isSessionSupported('immersive-ar').then((ok) => { card.hidden = !ok; }).catch(() => { card.hidden = true; });
  }
  tree = initHeight(ctx, {
    sample: () => (xr && pose ? { pose, tracking, t: poseT } : null),
    hit: () => hit,
    hasHitTest: () => Boolean(xr && xr.hit),
    inXr: () => Boolean(xr),
    startAr: () => start('height'),
    showReadout: (prompt, read) => { $('xr-th-prompt').textContent = prompt; $('xr-th-read').textContent = read; },
  });
  if (navigator.xr && typeof navigator.xr.isSessionSupported === 'function') {
    navigator.xr.isSessionSupported('immersive-ar').then((ok) => { $('th-ar').hidden = !ok; }).catch(() => null);
  }
  refreshButtons();
  return { startMotion, onTelemetry };
}

// The tree-height controller: the two taps, the live readout, the result and the save. `host`
// is the AR session of initXr (poses, the reticle hit); without AR the phone's tilt stands in.
const LIVE_EVERY_S = 1;
function initHeight(ctx, host) {
  const { S, $, el, clear, toast, api, errorText, uuid } = ctx;
  let step = 'idle'; // idle -> base -> crown -> done
  let tap1 = null, tap2 = null, ground = null, tilt1 = null, tilt2 = null;
  let result = null, req = null, key = null, saved = false;
  let busy = false; // one request at a time
  let lastLive = 0;
  let tilt = null; // latest deviceorientation {beta, gamma}
  let listening = false;

  const tape = () => ($('th-use-tape').checked ? parseMetres($('th-dist').value) : null);
  const wantTape = () => $('th-use-tape').checked;
  const status = (text, cls) => { const n = $('th-status'); n.textContent = text; n.className = 'hint ' + (cls || ''); };

  function prompt() {
    if (step === 'base') return 'Aim the circle at the foot of the trunk, then tap.';
    if (step === 'crown') return 'Now aim at the top of the crown, then tap.';
    return step === 'done' ? 'Done. Save it or retake.' : '';
  }
  function tapLabel() {
    return step === 'crown' ? 'Tap: top of the crown' : step === 'done' ? 'Measured' : 'Tap: trunk base';
  }

  function render() {
    const box = $('th-result');
    clear(box);
    $('th-save-box').hidden = !result;
    box.hidden = !result;
    $('th-save').disabled = !result || saved;
    $('xr-th-save').disabled = !result || saved;
    if (!result) return;
    const d = describeHeight(result);
    box.appendChild(el('div', d.headline));
    box.appendChild(el('div', d.range, 'hint'));
    for (const line of d.lines) box.appendChild(el('div', line, 'hint'));
    for (const w of d.warnings) box.appendChild(el('div', w, 'warn'));
    const more = d.warnings.length ? ' - ' + d.warnings.length + ' warning(s)' : '';
    host.showReadout(prompt(), d.headline + ' (' + m1(result.range_m[0]) + ' to ' + m1(result.range_m[1]) + ')' + more + (d.warnings.length ? ': ' + d.warnings[0] : ''));
  }

  function refresh() {
    const s = host.sample();
    const good = Boolean(s) && s.tracking === 'tracking';
    const aimedAtGround = Boolean(host.hit()) || !host.hasHitTest() || wantTape();
    $('xr-tap').textContent = tapLabel();
    $('xr-tap').disabled = !host.inXr() || !good || step === 'done' || step === 'idle' || (step === 'base' && !aimedAtGround);
    $('th-tilt-base').disabled = !wantTape();
    $('th-tilt-crown').disabled = !wantTape() || !tilt1;
  }

  function reset() {
    tap1 = tap2 = ground = tilt1 = tilt2 = null;
    result = req = null; key = null; saved = false;
    render();
  }
  function begin() {
    reset();
    step = 'base';
    status('');
    host.showReadout(prompt(), '');
    refresh();
  }
  function ended() {
    if (step !== 'done') step = 'idle';
    refresh();
  }

  // One request at a time. Returns { ok: answer }, { error: text }, or null (busy, 429, no answer).
  async function ask(body, final) {
    if (busy) return null;
    busy = true;
    try {
      const r = await api('/api/ar/height', { method: 'POST', body }).catch(() => null);
      if (!r) return null;
      if (!r.ok) {
        if (r.status === 429) return null;
        if (final) status(errorText(r), 'bad');
        return { error: errorText(r) };
      }
      return { ok: r.json };
    } finally {
      busy = false;
    }
  }

  async function onTap() {
    const s = host.sample();
    if (!s || s.tracking !== 'tracking') { toast('Wait for steady AR tracking.'); return; }
    if (wantTape() && tape() === null) { toast('Type the tape distance in metres first (or untick "Use tape distance").'); return; }
    if (!wantTape() && !host.hasHitTest()) { toast('This phone gave no ground point: tick "Use tape distance" and type it.'); return; }
    if (step === 'base') {
      const h = host.hit();
      if (!wantTape() && !h) { toast('Aim the circle at the ground until it holds steady.'); return; }
      tap1 = tapSample(s.pose, s.tracking, s.t);
      ground = h ? { x: h.x, y: h.y, z: h.z } : null;
      step = 'crown';
      lastLive = 0;
      host.showReadout(prompt(), '');
    } else if (step === 'crown') {
      tap2 = tapSample(s.pose, s.tracking, s.t);
      const body = heightRequest({ tap1, tap2, ground, tapeM: tape() });
      const got = await ask(body, true);
      if (got && got.ok) {
        result = got.ok; req = body; key = uuid(); saved = false; step = 'done';
        status('');
      } else if (got && got.error) toast(got.error); // stay on the crown tap: aim again and tap again
    }
    render();
    refresh();
  }

  // The live estimate: while aiming at the crown, the pose of now is sent about once a second.
  async function frame() {
    if (step !== 'crown' || !tap1 || busy) return;
    const s = host.sample();
    if (!s || s.t - lastLive < LIVE_EVERY_S) return;
    lastLive = s.t;
    const body = heightRequest({ tap1, tap2: tapSample(s.pose, s.tracking, s.t), ground, tapeM: tape() });
    const got = await ask(body, false);
    if (step !== 'crown') return; // the taps moved on while the answer was on its way
    if (got && got.ok) host.showReadout(prompt(), 'Now: about ' + m1(got.ok.height_m) + ' (' + m1(got.ok.range_m[0]) + ' to ' + m1(got.ok.range_m[1]) + ')');
    else if (got && got.error) host.showReadout(prompt(), got.error);
  }

  // ---- no AR: the phone's own tilt, with the tape distance
  function listen() {
    if (listening || typeof window.DeviceOrientationEvent === 'undefined') return;
    listening = true;
    window.addEventListener('deviceorientation', (e) => {
      if (Number.isFinite(e.beta) && Number.isFinite(e.gamma)) tilt = { beta: e.beta, gamma: e.gamma };
    });
  }
  async function tiltTap(which) {
    if (tape() === null) { toast('Type the tape distance in metres and tick "Use tape distance".'); return; }
    try {
      if (typeof DeviceOrientationEvent !== 'undefined' && typeof DeviceOrientationEvent.requestPermission === 'function') await DeviceOrientationEvent.requestPermission();
    } catch (e) { toast('Motion sensors were refused.'); return; }
    listen();
    await new Promise((ok) => setTimeout(ok, 150)); // the first reading after the permission
    if (!tilt) { toast('This phone gave no tilt reading.'); return; }
    if (which === 'base') {
      tilt1 = { ...tilt }; tilt2 = null; result = req = null; saved = false;
      status('Base reading taken. Now aim at the top of the crown and take the second reading.');
    } else {
      tilt2 = { ...tilt };
      const body = heightRequest({ tilt1, tilt2, tapeM: tape() });
      const got = await ask(body, true);
      if (got && got.ok) { result = got.ok; req = body; key = uuid(); saved = false; step = 'done'; status(''); }
    }
    render();
    refresh();
  }

  async function save() {
    if (!result || saved) return;
    if (!S.state || !S.state.records_enabled) { toast('Saving is turned off on the computer.'); return; }
    if (!(await ctx.ensurePlanId())) return;
    const body = heightRecordBody(result, req, { key, name: $('th-name').value.trim(), link: $('th-link').value.trim() });
    const r = await ctx.post(body, null);
    if (r.ok) { saved = true; toast('Saved for review on the computer'); } else toast(errorText(r));
    render();
  }
  function retake() {
    if (host.inXr()) begin();
    else { step = 'idle'; reset(); status('Cleared. Take the base reading again.'); }
    refresh();
  }

  $('xr-tap').addEventListener('click', onTap);
  $('xr-th-retake').addEventListener('click', retake);
  $('xr-th-save').addEventListener('click', save);
  $('th-save').addEventListener('click', save);
  $('th-retake').addEventListener('click', retake);
  $('th-ar').addEventListener('click', () => host.startAr());
  $('th-tilt-base').addEventListener('click', () => tiltTap('base'));
  $('th-tilt-crown').addEventListener('click', () => tiltTap('crown'));
  $('th-use-tape').addEventListener('change', refresh);
  refresh();
  return { begin, ended, frame, refresh };
}
