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

// ---- Offline outbox (WP-G1). A record save that cannot reach the computer is kept in IndexedDB and
// sent again, oldest first, when the connection returns. The server's idempotency_key makes a
// repeat harmless: a second POST of the same key answers 200 with the stored record, never a copy.
// The queue logic takes its storage and sender as parameters so it runs under Node in the tests.

const RETRYABLE = new Set([0, 401, 403, 408, 429]);
export function isRetryable(status) { return RETRYABLE.has(status) || status >= 500; }

export function memoryStore() {
  const rows = new Map();
  let seq = 0;
  return {
    async put(e) { if (!e.seq) e.seq = ++seq; rows.set(e.seq, { ...e }); return e.seq; },
    async all() { return [...rows.values()].sort((a, b) => a.seq - b.seq); },
    async remove(seq) { rows.delete(seq); },
  };
}

export function idbStore(factory) {
  let db = null;
  const open = () => new Promise((ok, no) => {
    const req = factory.open('ogp_outbox', 1);
    req.onupgradeneeded = () => req.result.createObjectStore('q', { keyPath: 'seq', autoIncrement: true });
    req.onsuccess = () => ok(req.result);
    req.onerror = () => no(req.error);
  });
  // one transaction per call; resolves with the request's result once the transaction commits
  const run = async (mode, fn) => {
    db = db || await open();
    return new Promise((ok, no) => {
      const tx = db.transaction('q', mode);
      const req = fn(tx.objectStore('q'));
      tx.oncomplete = () => ok(req.result);
      tx.onerror = () => no(tx.error);
      tx.onabort = () => no(tx.error);
    });
  };
  return {
    put: (e) => run('readwrite', (s) => s.put(e)),
    all: () => run('readonly', (s) => s.getAll()),
    remove: (seq) => run('readwrite', (s) => s.delete(seq)),
  };
}

// send(entry) -> {status, ok, json}; throws when the network is down.
export class Outbox {
  constructor({ store, send, onChange = () => {}, online = () => true, setTimer = setTimeout, clearTimer = clearTimeout, random = Math.random }) {
    Object.assign(this, { store, send, onChange, online, setTimer, clearTimer, random });
    this.timer = 0; this.flushing = false; this.attempts = 0;
    this.pending = 0; this.rejected = [];
  }

  async refresh() {
    const all = await this.store.all();
    this.pending = all.filter((e) => !e.rejected).length;
    this.rejected = all.filter((e) => e.rejected);
    this.onChange(this);
  }

  // Queue one save. Returns false when the browser would not store it (the caller reports the failure).
  async enqueue(entry) {
    try { await this.store.put({ ...entry, rejected: null, queued_at: Date.now() }); } catch (e) { return false; }
    await this.refresh();
    this.schedule();
    return true;
  }

  delay() { return Math.min(60000, 2000 * 2 ** Math.min(this.attempts, 5)) * (0.75 + this.random() * 0.5); }

  schedule() {
    this.clearTimer(this.timer);
    if (!this.pending) { this.timer = 0; return; }
    this.timer = this.setTimer(() => this.flush(), this.delay());
  }

  // Send the queue oldest first. A transient failure stops the run (order is kept) and backs off;
  // a record the server refuses for good is set aside so it cannot block the rest.
  async flush({ manual = false } = {}) {
    if (this.flushing) return;
    if (manual) this.attempts = 0;
    this.flushing = true;
    let sent = 0;
    try {
      for (const e of (await this.store.all()).filter((x) => !x.rejected)) {
        let r;
        try { r = await this.send(e); } catch (err) { r = { status: 0, ok: false, json: null }; }
        if (r.ok) { await this.store.remove(e.seq); sent += 1; this.attempts = 0; continue; }
        if (isRetryable(r.status)) { this.attempts += 1; break; }
        const j = r.json || {};
        await this.store.put({ ...e, rejected: j.detail || j.error || ('HTTP ' + r.status) });
      }
    } finally {
      this.flushing = false;
      await this.refresh();
      this.schedule();
    }
    return sent;
  }

  async discard(seq) { await this.store.remove(seq); await this.refresh(); }
  async discardRejected() { for (const e of this.rejected) await this.store.remove(e.seq); await this.refresh(); }
}
