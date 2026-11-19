// Same-origin API client. The token lives in memory and sessionStorage, never in a URL or localStorage.
let token = null;

export function initToken() {
  const m = /(?:^#|&)token=([A-Za-z0-9_-]+)/.exec(location.hash);
  if (m) {
    token = m[1];
    try { sessionStorage.setItem('ogp_token', token); } catch (e) { /* private mode */ }
    history.replaceState(null, '', location.pathname + location.search);
  } else {
    try { token = sessionStorage.getItem('ogp_token'); } catch (e) { token = null; }
  }
  return Boolean(token);
}

// One clock for every time the page sends that the server compares (GNSS, VIO, IMU, resets,
// lock points): epoch seconds on the high-resolution timeline. WebXR frame times share its base.
export function clockNow() {
  return (performance.timeOrigin + performance.now()) / 1000;
}

export async function api(path, opts = {}) {
  const headers = { Authorization: 'Bearer ' + token, ...(opts.headers || {}) };
  let body = opts.body;
  if (body !== undefined && !(body instanceof FormData) && typeof body !== 'string') {
    body = JSON.stringify(body);
    headers['Content-Type'] = 'application/json';
  }
  const res = await fetch(path, {
    method: opts.method || 'GET', headers, body, cache: 'no-store', referrerPolicy: 'no-referrer',
  });
  const type = res.headers.get('content-type') || '';
  const json = type.includes('json') ? await res.json().catch(() => null) : null;
  return { status: res.status, ok: res.ok, json, res };
}

export function errorText(r) {
  const j = r.json || {};
  return j.detail || j.error || ('HTTP ' + r.status);
}

async function blobOf(path) {
  // The bearer header only ever goes to a same-origin API path, never to a server-supplied host.
  if (typeof path !== 'string' || !path.startsWith('/api/') || path.startsWith('//')) return null;
  const r = await fetch(path, { headers: { Authorization: 'Bearer ' + token }, cache: 'no-store', referrerPolicy: 'no-referrer' });
  return r.ok ? r.blob() : null;
}

export async function fetchBasemap() {
  const blob = await blobOf('/api/basemap');
  if (!blob) return null;
  const img = new Image();
  const url = URL.createObjectURL(blob);
  await new Promise((ok, no) => { img.onload = ok; img.onerror = no; img.src = url; });
  URL.revokeObjectURL(url);
  return img;
}

export async function fetchPhoto(path) {
  const blob = await blobOf(path);
  return blob ? URL.createObjectURL(blob) : null;
}

export function uuid() {
  const a = new Uint8Array(16);
  crypto.getRandomValues(a);
  return Array.from(a, (b) => b.toString(16).padStart(2, '0')).join('');
}
