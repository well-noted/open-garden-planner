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

export function initXr(ctx) {
  const { S, $, toast, api, errorText, map, showFix } = ctx;
  const card = $('xr-card');
  const overlay = $('xr-overlay');
  const status = $('xr-status');
  const banner = $('xr-banner');
  let xr = null; // { session, local, hit, gl }
  let tracking = 'lost';
  let pose = null; // last viewer position {x, y, z}
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
    if (!r || r.status === 429) return false; // keep them; the next tick retries
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
  async function drain(untilT) {
    for (let i = 0; i < 8; i += 1) {
      if (inFlight) { await inFlight; continue; }
      if (lastVioT !== null && lastVioT >= untilT) return true;
      if (!vioQ.length || !(await flush())) return false;
    }
    return lastVioT !== null && lastVioT >= untilT;
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
      if (!ready() && xr) say(tracking !== 'tracking' ? 'Tracking ' + tracking + ': hold still and look around' : 'Point at the ground to place the reticle');
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

  async function start() {
    if (xr) return;
    if (S.target == null) { toast('Tap the known spot on the map first, then start AR.'); return; }
    let session = null;
    try {
      session = await navigator.xr.requestSession('immersive-ar', {
        requiredFeatures: ['hit-test', 'local'],
        optionalFeatures: ['dom-overlay'],
        domOverlay: { root: overlay },
      });
      const gl = document.createElement('canvas').getContext('webgl', { xrCompatible: true, alpha: true });
      if (!gl) { session.end().catch(() => null); toast('This browser cannot start AR here.'); return; }
      await session.updateRenderState({ baseLayer: new XRWebGLLayer(session, gl) });
      const local = await session.requestReferenceSpace('local');
      const viewer = await session.requestReferenceSpace('viewer');
      const src = await session.requestHitTestSource({ space: viewer });
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
      moveMap(true);
      setBanner(locked ? '' : 'Stand on a known spot, then lock A. Walk to a second spot, lock B.');
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
  $('xr-start').addEventListener('click', start);
  $('xr-end').addEventListener('click', () => { if (xr) xr.session.end().catch(() => null); });
  $('xr-a').addEventListener('click', () => lock('a'));
  $('xr-b').addEventListener('click', () => lock('b'));
  $('xr-c').addEventListener('click', () => lock('c'));
  $('xr-unlock').addEventListener('click', unlock);

  // Probe only: the card is shown when the browser reports immersive-ar support.
  if (navigator.xr && typeof navigator.xr.isSessionSupported === 'function') {
    navigator.xr.isSessionSupported('immersive-ar').then((ok) => { card.hidden = !ok; }).catch(() => { card.hidden = true; });
  }
  refreshButtons();
  return { startMotion, onTelemetry };
}
