// Irrigation path recording (design 4.2 item 6). The desktop filters the walk (Kalman, gating,
// stand-still handling); the phone only starts and stops, shows a live preview, and saves the
// simplified result as a PENDING record. Nothing changes the plan until it is approved on the
// computer.
export function initRecord(ctx) {
  const { S, $, el, clear, toast, api, errorText, uuid, map } = ctx;
  let timer = 0;
  let wake = null;
  let result = null;
  let key = null;

  const lengthOf = (pts) => pts.reduce((acc, p, i) => (i ? acc + Math.hypot(p[0] - pts[i - 1][0], p[1] - pts[i - 1][1]) : 0), 0) / 100;

  async function pollPreview() {
    const r = await api('/api/track/preview');
    if (!r.ok) return;
    map.track = r.json.points_cm;
    map.draw();
    $('rec-live').textContent = 'Recording: about ' + lengthOf(map.track).toFixed(1) + ' m, ' + map.track.length + ' points';
  }

  async function start() {
    if (S.watch === null) { toast('Turn GPS on first (the GPS button on the map).'); return; }
    const r = await api('/api/track/start', { method: 'POST', body: {} });
    if (!r.ok) { toast(errorText(r)); return; }
    result = null; key = null; map.track = [];
    $('rec-idle').hidden = true; $('rec-live-box').hidden = false; $('rec-done').hidden = true;
    timer = setInterval(pollPreview, 2000);
    try { if ('wakeLock' in navigator) wake = await navigator.wakeLock.request('screen'); } catch (e) { wake = null; }
  }

  function stopTimers() {
    clearInterval(timer);
    if (wake) { wake.release().catch(() => {}); wake = null; }
  }

  async function stop() {
    stopTimers();
    const close = $('rec-close').checked ? 'start' : null;
    const r = await api('/api/track/finish', { method: 'POST', body: { close, smooth: true } });
    $('rec-live-box').hidden = true;
    if (!r.ok) { toast(errorText(r)); $('rec-idle').hidden = false; return; }
    result = r.json; key = uuid();
    map.track = result.points.map((p) => [p[2], p[3]]);
    map.draw();
    show();
  }

  function show() {
    const box = $('rec-summary');
    clear(box);
    $('rec-done').hidden = false;
    box.appendChild(el('div', 'Length ' + result.length_m.toFixed(1) + ' m in ' + result.n_vertices + ' points.'));
    if (result.closure) {
      box.appendChild(el('div', 'Loop misclosure ' + result.closure.closure_m.toFixed(2) + ' m (' + (result.closure.ratio * 100).toFixed(1) + ' % of the path), spread along the path.'));
    }
    if (result.truncated) box.appendChild(el('div', 'The recording was very long: only the first part was kept.', 'warn'));
    const why = { too_short: 'Under 3 m: too short to keep.', too_few_points: 'Fewer than 3 points: too few to keep.' };
    for (const p of result.problems) box.appendChild(el('div', why[p] || p, 'bad'));
    $('btn-rec-save').disabled = !result.saveable;
  }

  async function save() {
    if (!result || !result.saveable) return;
    if (!S.state || !S.state.records_enabled) { toast('Saving is turned off on the computer.'); return; }
    if (!(await ctx.ensurePlanId())) return;
    const num = (id) => { const v = Number($(id).value.replace(',', '.')); return Number.isFinite(v) && v > 0 ? v : undefined; };
    const body = {
      kind: 'irrigation_path', idempotency_key: key,
      geometry: { type: 'polyline', points: result.points },
      closes_on: Boolean(result.closure), precision_hint: S.calibrated ? 'survey_in' : 'gps',
    };
    for (const [id, field] of [['rec-diameter', 'diameter_mm'], ['rec-flow', 'flow_lph'], ['rec-spacing', 'emitter_spacing_cm']]) {
      const v = num(id); if (v !== undefined) body[field] = v;
    }
    const name = $('rec-name').value.trim();
    if (name) body.name = name;
    const r = await ctx.post(body, null);
    if (r.ok) { toast('Saved for review on the computer'); discard(); } else toast(errorText(r));
  }

  async function discard() {
    stopTimers();
    await api('/api/track/cancel', { method: 'POST', body: {} }).catch(() => null);
    result = null; key = null; map.track = []; map.draw();
    $('rec-idle').hidden = false; $('rec-live-box').hidden = true; $('rec-done').hidden = true;
  }

  $('btn-rec-start').addEventListener('click', start);
  $('btn-rec-stop').addEventListener('click', stop);
  $('btn-rec-save').addEventListener('click', save);
  $('btn-rec-discard').addEventListener('click', discard);
  document.addEventListener('visibilitychange', async () => {
    // the wake lock is released when the page is hidden; take it again if still recording
    if (document.visibilityState === 'visible' && timer && !$('rec-live-box').hidden && 'wakeLock' in navigator) {
      try { wake = await navigator.wakeLock.request('screen'); } catch (e) { wake = null; }
    }
  });
}
