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
    const t0 = Date.now() / 1000;
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
    toast(r.queued ? 'Kept on this phone; it is sent when the computer is reachable' : r.ok ? 'Saved for review on the computer' : errorText(r));
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
