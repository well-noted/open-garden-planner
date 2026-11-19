// Field Remote phone page. No framework, no build step. All user and plan text is written with
// textContent/createElement, never as markup; there is no eval and no inline script or style.
import { api, errorText, fetchBasemap, fetchPhoto, initToken, uuid } from './api.js';
import { canvasToGps, trilaterate } from './geo.js';
import { PlanMap } from './map.js';

const $ = (id) => document.getElementById(id);
const S = {
  state: null, etag: null, planId: null, geo: null,
  target: null, targetFrom: null, // {x, y} plan cm; 'gps' | 'trilateration' | 'map'
  fix: null, watch: null, queue: [], step: 5,
  kind: 'plant', species: null, candidates: [], group: null,
  pick: false, anchors: [], side: 0, measured: null,
  ownRecords: [],
};
const map = new PlanMap($('map'), (p) => onMapTap(p));

function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text != null) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
let toastTimer = 0;
function toast(msg) {
  const t = $('toast');
  t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 4000);
}
function show(name) {
  for (const s of document.querySelectorAll('.screen')) s.hidden = s.id !== 'screen-' + name;
  for (const b of document.querySelectorAll('#tabs .tab')) b.classList.toggle('on', b.dataset.screen === name);
  if (name === 'map') map.resize();
  if (name === 'records') loadRecords();
}
for (const b of document.querySelectorAll('#tabs .tab')) b.addEventListener('click', () => show(b.dataset.screen));

// ---- connection, state, scene -----------------------------------------------------------

function setConn(text, cls) {
  const c = $('conn');
  c.textContent = text; c.className = 'badge ' + (cls || '');
}

async function pollState() {
  try {
    const r = await api('/api/state', { headers: S.etag ? { 'If-None-Match': S.etag } : {} });
    if (r.status === 304) { setConn('connected', 'ok'); return; }
    if (r.status === 401) { setConn('not paired', 'bad'); $('banner').textContent = 'Not paired. Scan the QR code on the computer again.'; $('banner').hidden = false; return; }
    if (!r.ok) { setConn(errorText(r), 'bad'); return; }
    S.etag = r.res.headers.get('etag');
    const first = S.state === null;
    S.state = r.json;
    S.geo = r.json.georef.georeferenced ? { ...r.json.georef, m_per_deg: r.json.georef.m_per_deg || 111194.93 } : null;
    map.mPerUnit = S.geo ? S.geo.scale_meters_per_unit : 0.01;
    $('plan-name').textContent = r.json.plan_name || 'Field Remote';
    setConn('connected', 'ok');
    const b = $('banner');
    if (!r.json.records_enabled) { b.textContent = 'View only: saving from the phone is turned off on the computer.'; b.hidden = false; }
    else if (!r.json.georef.georeferenced) { b.textContent = 'This plan is not georeferenced: GPS position will not show on the map.'; b.hidden = false; }
    else b.hidden = true;
    map.setPlan(r.json.canvas);
    buildLayers();
    if (first || r.json.has_basemap !== Boolean(map.base)) await loadScene();
  } catch (e) {
    setConn('offline - do not reload', 'bad');
  }
}

async function loadScene() {
  const r = await api('/api/scene');
  if (r.ok) { map.objects = r.json.objects; if (r.json.truncated) toast('The plan is large: only part of it is drawn.'); }
  if (S.state && S.state.has_basemap) {
    const meta = await api('/api/basemap?meta=1');
    if (meta.ok) { map.rect = meta.json.rect; map.base = await fetchBasemap().catch(() => null); }
  }
  map.fitAll();
}

function buildLayers() {
  const box = $('layers');
  clear(box);
  for (const l of S.state.layers) {
    const row = el('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.checked = l.visible && !map.hidden.has(l.id);
    cb.addEventListener('change', () => { if (cb.checked) map.hidden.delete(l.id); else map.hidden.add(l.id); map.draw(); });
    row.appendChild(cb); row.appendChild(el('span', l.name));
    box.appendChild(row);
  }
  for (const l of S.state.layers) if (!l.visible) map.hidden.add(l.id);
}
$('btn-layers').addEventListener('click', () => { $('layers').hidden = !$('layers').hidden; });
$('btn-follow').addEventListener('click', () => { map.follow = !map.follow; if (S.fix && S.fix.x_cm != null) map.setPuck(S.fix); });

// ---- position ------------------------------------------------------------------------------

function startGps() {
  if (!('geolocation' in navigator)) { toast('This browser has no location access.'); return; }
  if (S.watch !== null) { navigator.geolocation.clearWatch(S.watch); S.watch = null; $('btn-locate').textContent = 'GPS'; return; }
  S.watch = navigator.geolocation.watchPosition((p) => {
    const c = p.coords, num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
    const item = { t: p.timestamp / 1000, lat: c.latitude, lon: c.longitude, accuracy_m: Math.max(0, c.accuracy || 0), source: 'browser' };
    for (const [k, v] of [['alt', num(c.altitude)], ['heading_deg', num(c.heading)], ['speed_mps', num(c.speed)]]) if (v !== null) item[k] = v;
    S.queue.push(item);
    if (S.queue.length > 200) S.queue.splice(0, S.queue.length - 200);
  }, (e) => {
    toast(e.code === 1 ? 'Location is blocked. The map still works; tap it to choose a spot.' : 'No location yet.');
  }, { enableHighAccuracy: true, maximumAge: 1000, timeout: 20000 });
  $('btn-locate').textContent = 'stop';
}
$('btn-locate').addEventListener('click', startGps);

async function flushPositions() {
  if (!S.queue.length) return;
  const batch = S.queue.slice(0, 50);
  const r = await api('/api/position', { method: 'POST', body: { positions: batch } }).catch(() => null);
  if (!r) return; // keep them; the next tick retries
  S.queue.splice(0, batch.length);
  if (!r.ok) { if (r.status !== 429) toast(errorText(r)); return; }
  S.fix = r.json.telemetry;
  map.setPuck(S.fix);
  const t = S.fix;
  const f = $('fixbadge');
  f.textContent = t.fix_mode + ' ±' + Number(t.sigma_m).toFixed(1) + ' m' + (t.heading_source === 'none' ? '' : ' · ' + t.heading_source);
  f.className = 'overlay badge ' + (t.quality === 'poor' ? 'bad' : 'ok');
}

// ---- choosing a spot -------------------------------------------------------------------------

function onMapTap(p) {
  if (S.pick) { addAnchor(p); return; }
  setTarget(p, 'map');
}

function setTarget(p, from) {
  S.target = { x: p.x, y: p.y }; S.targetFrom = from;
  map.target = S.target; map.draw();
  $('target-bar').hidden = false;
}
for (const b of document.querySelectorAll('.nudge')) {
  b.addEventListener('click', () => {
    if (!S.target) return;
    const cm = S.step / (S.geo ? S.geo.scale_meters_per_unit * 100 : 1); // plan units for the step
    setTarget({ x: S.target.x + Number(b.dataset.dx) * cm, y: S.target.y + Number(b.dataset.dy) * cm }, S.targetFrom);
  });
}
$('nudge-step').addEventListener('click', () => {
  S.step = S.step === 5 ? 25 : 5;
  $('nudge-step').textContent = S.step + ' cm';
});
$('btn-here').addEventListener('click', () => {
  if (!S.fix || S.fix.x_cm == null) { toast('No position yet. Tap GPS, or tap the map.'); return; }
  setTarget({ x: S.fix.x_cm, y: S.fix.y_cm }, 'gps');
});
$('btn-mark').addEventListener('click', () => {
  if (!S.target) return;
  show('add');
  describeSpot();
});

function describeSpot() {
  const w = $('add-where');
  if (!S.target) { w.textContent = 'No spot chosen yet. Tap the map, then "Mark here".'; return; }
  const how = { gps: 'from your position' + (S.fix ? ' (±' + S.fix.sigma_m.toFixed(1) + ' m)' : ''), trilateration: 'from tape distances', map: 'tapped on the map' }[S.targetFrom];
  w.textContent = 'Spot: ' + (S.target.x / 100).toFixed(2) + ' m, ' + (S.target.y / 100).toFixed(2) + ' m ' + how + '.';
}

// ---- add a record ---------------------------------------------------------------------------

function setKind(kind) {
  S.kind = kind;
  for (const k of ['plant', 'note', 'wish']) $('kind-' + k).classList.toggle('on', k === kind);
  const wish = kind === 'wish', note = kind === 'note';
  $('f-name').hidden = wish || note; $('f-species').hidden = note; $('species-list').hidden = note;
  $('f-age').hidden = wish || note; $('f-what').hidden = !wish; $('f-requester').hidden = !wish;
  $('wish-hint').hidden = !wish; $('btn-candidate').hidden = !wish;
  renderCandidates();
}
for (const k of ['plant', 'note', 'wish']) $('kind-' + k).addEventListener('click', () => setKind(k));

let speciesTimer = 0;
$('species-q').addEventListener('input', () => {
  clearTimeout(speciesTimer);
  speciesTimer = setTimeout(async () => {
    const q = $('species-q').value.trim();
    const list = $('species-list');
    clear(list);
    if (!q) return;
    const r = await api('/api/species?q=' + encodeURIComponent(q));
    if (!r.ok) return;
    for (const s of r.json.species) {
      const li = el('li', s.common_name || s.species_key, 'tap');
      if (s.scientific_name) li.appendChild(el('div', s.scientific_name, 'hint'));
      li.addEventListener('click', () => {
        S.species = s.species_key; clear(list);
        $('species-q').value = s.common_name || s.species_key;
        if (!$('plant-name').value) $('plant-name').value = s.common_name || '';
      });
      list.appendChild(li);
    }
  }, 200);
});

function geometryNow() {
  if (!S.target) return null;
  if (!S.geo) return null;
  const g = canvasToGps(S.geo, S.target.x, S.target.y);
  return { type: 'point', lat: g.lat, lon: g.lon, x_cm: Math.round(S.target.x * 10) / 10, y_cm: Math.round(S.target.y * 10) / 10 };
}

function currentCandidate() {
  const geometry = geometryNow();
  if (!geometry) return null;
  const photo = $('photo').files[0] || null;
  const hint = S.targetFrom === 'gps' ? 'gps' : S.targetFrom === 'trilateration' ? 'trilateration' : null;
  const c = { key: uuid(), geometry, photo, hint, from: S.targetFrom };
  if (S.targetFrom === 'gps' && S.fix) {
    c.accuracy_m = S.fix.horizontal_accuracy_m; c.sigma_m = S.fix.sigma_m; c.fix_mode = S.fix.fix_mode;
    if (S.fix.heading_deg != null) c.bearing_deg = S.fix.heading_deg;
  }
  if (S.targetFrom === 'trilateration' && S.measured) c.rms = S.measured.rms_m;
  return c;
}

function renderCandidates() {
  const ul = $('candidates');
  clear(ul);
  if (S.kind !== 'wish') return;
  S.candidates.forEach((c, i) => {
    const li = el('li', 'Candidate ' + (i + 1) + ': ' + (c.geometry.x_cm / 100).toFixed(2) + ' m, ' + (c.geometry.y_cm / 100).toFixed(2) + ' m' + (c.photo ? ' + photo' : ''));
    ul.appendChild(li);
  });
}
$('btn-candidate').addEventListener('click', () => {
  const c = currentCandidate();
  if (!c) { toast('Choose a spot on the map first.'); return; }
  S.candidates.push(c);
  $('photo').value = ''; S.target = null; map.target = null; map.draw();
  $('add-where').textContent = 'Candidate added. Pick the next spot on the map.';
  renderCandidates();
});

function recordBody(c, extra) {
  const body = { idempotency_key: c.key, geometry: c.geometry, ...extra };
  for (const k of ['accuracy_m', 'sigma_m', 'fix_mode', 'bearing_deg', 'rms']) if (c[k] != null) body[k] = c[k];
  if (c.hint) body.precision_hint = c.hint;
  return body;
}

async function ensurePlanId() {
  if (S.planId) return true;
  const r = await api('/api/field-records?limit=1');
  if (r.ok) { S.planId = r.json.plan_id; return true; }
  toast(errorText(r));
  return false;
}

async function post(body, photo) {
  let init;
  if (photo) {
    const form = new FormData();
    form.append('meta', JSON.stringify(body));
    form.append('photo', photo, photo.name || 'photo.jpg'); // the original file: never re-encoded
    init = { method: 'POST', body: form, headers: { 'X-Plan-Id': S.planId } };
  } else {
    init = { method: 'POST', body, headers: { 'X-Plan-Id': S.planId } };
  }
  const r = await api('/api/records', init);
  if (r.status === 409 && r.json && r.json.error === 'plan_changed') S.planId = null;
  return r;
}

$('btn-save').addEventListener('click', async () => {
  if (!S.state || !S.state.records_enabled) { toast('Saving is turned off on the computer.'); return; }
  if (!S.geo) { toast('The plan is not georeferenced, so a spot cannot be saved.'); return; }
  if (!(await ensurePlanId())) return;
  const save = $('btn-save');
  save.disabled = true;
  try {
    const list = [...S.candidates];
    if (S.target) { const c = currentCandidate(); if (c) list.push(c); }
    if (!list.length) { toast('Choose a spot on the map first.'); return; }
    const notes = $('notes').value.trim() || undefined;
    let extra, kind;
    if (S.kind === 'wish') {
      const what = $('what').value.trim();
      if (!what) { toast('Say what the client wants.'); return; }
      kind = 'placement_request';
      extra = { kind, what, notes, requester: $('requester').value.trim() || undefined, group_id: S.group || (S.group = uuid()) };
      if (S.species) extra.species_key = S.species;
    } else if (S.kind === 'note') {
      kind = 'note'; extra = { kind, notes: notes || ($('plant-name').value.trim() || undefined) };
    } else {
      kind = 'plant';
      extra = { kind, plant_name: $('plant-name').value.trim() || undefined, age: $('age').value.trim() || undefined, notes };
      if (S.species) extra.species_key = S.species;
    }
    let saved = 0;
    for (const c of list) {
      const r = await post(recordBody(c, Object.fromEntries(Object.entries(extra).filter(([, v]) => v !== undefined))), c.photo);
      if (!r.ok) { toast(errorText(r)); break; }
      saved += 1; c.done = true;
    }
    S.candidates = S.candidates.filter((c) => !c.done);
    if (saved === list.length) {
      toast('Saved for review on the computer');
      resetForm();
    } else if (saved) toast(saved + ' of ' + list.length + ' saved. Press Save again for the rest.');
  } finally {
    save.disabled = false;
  }
});

function resetForm() {
  S.candidates = []; S.group = null; S.species = null; S.target = null; map.target = null; map.draw();
  $('target-bar').hidden = true;
  for (const id of ['photo', 'plant-name', 'species-q', 'age', 'what', 'requester', 'notes']) $(id).value = '';
  clear($('species-list'));
  describeSpot(); renderCandidates();
}

// ---- measure from points ---------------------------------------------------------------------

$('btn-pick').addEventListener('click', () => {
  if (!S.geo) { toast('Needs a georeferenced plan.'); return; }
  S.pick = true; show('map');
  toast('Tap a known spot on the map.');
});

function addAnchor(p) {
  S.pick = false;
  if (S.anchors.length >= 3) S.anchors.shift();
  S.anchors.push({ x: p.x, y: p.y, d: null });
  map.anchors = S.anchors; map.draw();
  show('measure'); renderAnchors();
}

function renderAnchors() {
  const ul = $('anchors');
  clear(ul);
  S.anchors.forEach((a, i) => {
    const li = el('li', 'Spot ' + (i + 1) + ' (' + (a.x / 100).toFixed(2) + ', ' + (a.y / 100).toFixed(2) + ' m) distance:');
    const inp = document.createElement('input');
    inp.type = 'text'; inp.inputMode = 'decimal'; inp.className = 'dist'; inp.maxLength = 8;
    inp.value = a.d == null ? '' : String(a.d);
    inp.addEventListener('input', () => { const v = Number(inp.value.replace(',', '.')); a.d = Number.isFinite(v) && v > 0 ? v : null; solveMeasure(); });
    li.appendChild(inp); li.appendChild(el('span', ' m'));
    const rm = el('button', 'Remove');
    rm.addEventListener('click', () => { S.anchors.splice(i, 1); map.anchors = S.anchors; map.draw(); renderAnchors(); solveMeasure(); });
    li.appendChild(rm);
    ul.appendChild(li);
  });
  solveMeasure();
}

function setSide(i) { S.side = i; $('side-a').classList.toggle('on', i === 0); $('side-b').classList.toggle('on', i === 1); solveMeasure(); }
$('side-a').addEventListener('click', () => setSide(0));
$('side-b').addEventListener('click', () => setSide(1));

function solveMeasure() {
  const out = $('measure-result'), use = $('btn-measure-use');
  S.measured = null; out.hidden = true; use.hidden = true; $('side-row').hidden = true;
  const ready = S.anchors.filter((a) => a.d != null);
  if (ready.length < 2 || !S.geo) return;
  const r = trilaterate(ready, ready.map((a) => a.d), S.geo.scale_meters_per_unit);
  clear(out); out.hidden = false;
  const msgs = { need2: 'Need two spots.', close: 'The two spots are too close together.', noreach: 'These distances cannot meet: check the tape readings.' };
  if (r.error) { out.appendChild(el('div', msgs[r.error], 'bad')); return; }
  let pos = r.pos;
  if (r.ambiguous && r.alt) { $('side-row').hidden = false; if (S.side === 1) pos = r.alt; }
  S.measured = { ...r, pos };
  out.appendChild(el('div', 'Spot: ' + (pos.x / 100).toFixed(2) + ' m, ' + (pos.y / 100).toFixed(2) + ' m'));
  out.appendChild(el('div', 'Fit residual (rms): ' + r.rms_m.toFixed(2) + ' m' + (ready.length < 3 ? ' (two distances cannot check each other)' : '')));
  if (r.ambiguous) out.appendChild(el('div', 'Two spots fit these distances: choose the side that matches where you stand.', 'warn'));
  if (r.shallow) out.appendChild(el('div', 'The new spot is nearly in line with the two known spots: the result is weak. Use a third spot.', 'warn'));
  if (r.rms_m > 0.3) out.appendChild(el('div', 'The distances disagree by more than 30 cm. Re-measure before trusting this.', 'warn'));
  use.hidden = false;
}

$('btn-measure-use').addEventListener('click', () => {
  if (!S.measured) return;
  setTarget(S.measured.pos, 'trilateration');
  show('add'); describeSpot();
});

// ---- records list and status -----------------------------------------------------------------

const photoUrls = [];
async function loadRecords() {
  const r = await api('/api/field-records?limit=100');
  const st = $('status'), ul = $('records');
  for (const u of photoUrls.splice(0)) URL.revokeObjectURL(u);
  clear(st); clear(ul);
  if (!r.ok) { st.appendChild(el('div', r.status === 409 ? 'Save the plan on the computer first.' : errorText(r), 'bad')); return; }
  S.planId = r.json.plan_id;
  const skew = S.state ? Math.round(Date.now() / 1000 - Date.parse(S.state.server_time) / 1000) : 0;
  st.appendChild(el('div', 'Connection: ' + $('conn').textContent));
  st.appendChild(el('div', r.json.pending + ' pending of ' + r.json.total + ' saved'));
  st.appendChild(el('div', 'Clock difference to the computer: ' + skew + ' s' + (Math.abs(skew) > 100 ? ' (too large: positions will be rejected)' : '')));
  if (S.state && !S.state.records_enabled) st.appendChild(el('div', 'Saving is turned off on the computer.', 'warn'));
  for (const rec of r.json.records) {
    const title = rec.plant_name || rec.what || rec.name || rec.kind;
    const li = el('li', title);
    li.appendChild(el('div', rec.kind + ' · ' + rec.status + ' · ' + String(rec.timestamp || '').slice(0, 16).replace('T', ' '), 'hint'));
    if (rec.photo_url) {
      const img = document.createElement('img');
      img.className = 'thumb'; img.alt = '';
      fetchPhoto(rec.photo_url).then((u) => { if (u) { photoUrls.push(u); img.src = u; } });
      li.appendChild(img);
    }
    if (rec.status === 'pending' && S.state && S.state.records_enabled) {
      const del = el('button', 'Withdraw');
      del.addEventListener('click', async () => {
        const d = await api('/api/records/' + encodeURIComponent(rec.id), { method: 'DELETE', headers: { 'X-Plan-Id': S.planId } });
        toast(d.ok ? 'Withdrawn' : errorText(d)); loadRecords();
      });
      li.appendChild(del);
    }
    ul.appendChild(li);
  }
}
$('btn-reload').addEventListener('click', loadRecords);

// ---- start ------------------------------------------------------------------------------------

setKind('plant');
if (!initToken()) {
  setConn('not paired', 'bad');
  $('banner').textContent = 'Open this page by scanning the QR code on the computer.'; $('banner').hidden = false;
} else {
  pollState();
  setInterval(pollState, 5000);
  setInterval(flushPositions, 2000);
  window.addEventListener('online', pollState);
  window.addEventListener('focus', pollState);
}
