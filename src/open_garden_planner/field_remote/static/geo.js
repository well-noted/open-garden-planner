// Plan <-> GPS conversion matching core/geo_frame.py (tangent plane about the origin), and
// two/three-circle trilateration for "measure from points". Plan units are cm, Y up.
// Metres per degree of longitude at the origin (the server sends the ellipsoidal value).
function lonScale(g) {
  return g.m_per_deg_lon || g.m_per_deg * Math.cos(g.origin_lat * Math.PI / 180);
}

export function canvasToGps(g, x, y) {
  const k = g.scale_meters_per_unit, a = g.rotation_deg * Math.PI / 180;
  const xm = (x - (g.origin_x_cm || 0)) * k, ym = (y - (g.origin_y_cm || 0)) * k;
  const c = Math.cos(a), s = Math.sin(a);
  const east = xm * c - ym * s, north = xm * s + ym * c;
  return {
    lat: g.origin_lat + north / g.m_per_deg,
    lon: g.origin_lon + east / lonScale(g),
  };
}

export function gpsToCanvas(g, lat, lon) {
  const north = (lat - g.origin_lat) * g.m_per_deg;
  const east = (lon - g.origin_lon) * lonScale(g);
  const a = g.rotation_deg * Math.PI / 180, c = Math.cos(a), s = Math.sin(a), k = g.scale_meters_per_unit;
  return { x: (g.origin_x_cm || 0) + (east * c + north * s) / k, y: (g.origin_y_cm || 0) + (-east * s + north * c) / k };
}

// points: [{x, y}] in plan units; dist: metres; mPerUnit: georef scale. Returns
// {pos, alt, rms_m, ambiguous, shallow} (pos/alt in plan units) or {error}.
export function trilaterate(points, dist, mPerUnit) {
  if (points.length < 2) return { error: 'need2' };
  const p = points.map((q) => ({ x: q.x * mPerUnit, y: q.y * mPerUnit }));
  const a = p[0], b = p[1], dx = b.x - a.x, dy = b.y - a.y, L = Math.hypot(dx, dy);
  if (L < 0.5) return { error: 'close' };
  const t = (dist[0] * dist[0] - dist[1] * dist[1] + L * L) / (2 * L);
  const h2 = dist[0] * dist[0] - t * t;
  if (h2 < -0.25) return { error: 'noreach' };
  const h = Math.sqrt(Math.max(0, h2));
  const ux = dx / L, uy = dy / L;
  const base = { x: a.x + ux * t, y: a.y + uy * t };
  const s1 = { x: base.x - uy * h, y: base.y + ux * h };
  const s2 = { x: base.x + uy * h, y: base.y - ux * h };
  const rms = (s) => {
    const r = p.map((q, i) => Math.hypot(s.x - q.x, s.y - q.y) - dist[i]);
    return Math.sqrt(r.reduce((acc, v) => acc + v * v, 0) / r.length);
  };
  let ordered = [s1, s2];
  let ambiguous = h > 0.05;
  if (p.length >= 3) {
    const r1 = rms(s1), r2 = rms(s2);
    ambiguous = Math.abs(r1 - r2) < 0.15;
    ordered = r1 <= r2 ? [s1, s2] : [s2, s1];
  }
  const back = (s) => ({ x: s.x / mPerUnit, y: s.y / mPerUnit });
  return {
    pos: back(ordered[0]),
    alt: ambiguous ? back(ordered[1]) : null,
    rms_m: rms(ordered[0]),
    ambiguous,
    shallow: h < 0.3 * Math.max(dist[0], 0.01),
  };
}
