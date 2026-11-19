// Canvas 2D plan map. Plan units are cm with Y up; the screen flips Y. Drag pans, two fingers
// pinch-zoom, a tap without movement reports the plan point. Nothing here touches the DOM
// except the canvas it was given.
export class PlanMap {
  constructor(canvas, onTap) {
    this.c = canvas;
    this.ctx = canvas.getContext('2d');
    this.onTap = onTap;
    this.cx = 0; this.cy = 0; this.k = 0.1; // view centre (plan cm) and pixels per cm
    this.objects = []; this.hidden = new Set();
    this.base = null; this.rect = null;
    this.puck = null; this.target = null; this.anchors = []; this.extent = null;
    this.follow = false;
    this.pointers = new Map();
    this.moved = false; this.pinch = 0;
    canvas.addEventListener('pointerdown', (e) => this.down(e));
    canvas.addEventListener('pointermove', (e) => this.move(e));
    canvas.addEventListener('pointerup', (e) => this.up(e));
    canvas.addEventListener('pointercancel', (e) => this.pointers.delete(e.pointerId));
    new ResizeObserver(() => this.resize()).observe(canvas);
    this.resize();
  }

  resize() {
    const r = this.c.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    this.w = Math.max(1, r.width); this.h = Math.max(1, r.height); this.dpr = dpr;
    this.c.width = Math.round(this.w * dpr); this.c.height = Math.round(this.h * dpr);
    this.draw();
  }

  toScreen(x, y) { return [this.w / 2 + (x - this.cx) * this.k, this.h / 2 - (y - this.cy) * this.k]; }
  toPlan(sx, sy) { return { x: this.cx + (sx - this.w / 2) / this.k, y: this.cy - (sy - this.h / 2) / this.k }; }

  fitAll() {
    let box = this.extent;
    if (this.rect) box = { x0: this.rect.x_cm, y0: this.rect.y_cm, x1: this.rect.x_cm + this.rect.width_cm, y1: this.rect.y_cm + this.rect.height_cm };
    if (!this.rect) box = this.objectBox() || box;
    if (!box) return;
    this.cx = (box.x0 + box.x1) / 2; this.cy = (box.y0 + box.y1) / 2;
    this.k = 0.9 * Math.min(this.w / Math.max(1, box.x1 - box.x0), this.h / Math.max(1, box.y1 - box.y0));
    this.draw();
  }

  objectBox() {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    const add = (x, y) => { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); };
    for (const o of this.objects) {
      if (o.points) o.points.forEach((p) => add(p[0], p[1]));
      else if (o.kind === 'box') { add(o.x, o.y); add(o.x + o.w, o.y + o.h); }
      else if (o.cx !== undefined) { const rx = o.r || o.rx, ry = o.r || o.ry; add(o.cx - rx, o.cy - ry); add(o.cx + rx, o.cy + ry); }
    }
    return Number.isFinite(x0) ? { x0: x0 - 50, y0: y0 - 50, x1: x1 + 50, y1: y1 + 50 } : null;
  }

  setPlan(canvasSize) {
    if (canvasSize && canvasSize.width_cm > 0 && canvasSize.height_cm > 0) {
      this.extent = { x0: 0, y0: 0, x1: canvasSize.width_cm, y1: canvasSize.height_cm };
    }
  }

  down(e) {
    this.c.setPointerCapture(e.pointerId);
    this.pointers.set(e.pointerId, { x: e.offsetX, y: e.offsetY, sx: e.offsetX, sy: e.offsetY });
    this.moved = false; this.pinch = this.span();
  }

  span() {
    const p = [...this.pointers.values()];
    return p.length === 2 ? Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) : 0;
  }

  move(e) {
    const p = this.pointers.get(e.pointerId);
    if (!p) return;
    const dx = e.offsetX - p.x, dy = e.offsetY - p.y;
    p.x = e.offsetX; p.y = e.offsetY;
    if (Math.hypot(p.x - p.sx, p.y - p.sy) > 8) this.moved = true;
    if (!this.moved) return;
    if (this.pointers.size === 1) {
      this.cx -= dx / this.k; this.cy += dy / this.k; this.follow = false;
    } else if (this.pointers.size === 2) {
      const s = this.span();
      if (this.pinch > 0 && s > 0) this.k = Math.min(20, Math.max(0.005, this.k * s / this.pinch));
      this.pinch = s;
    }
    this.draw();
  }

  up(e) {
    const p = this.pointers.get(e.pointerId);
    this.pointers.delete(e.pointerId);
    if (p && !this.moved && this.pointers.size === 0) this.onTap(this.toPlan(p.x, p.y));
  }

  draw() {
    const g = this.ctx;
    g.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    g.clearRect(0, 0, this.w, this.h);
    if (this.base && this.rect) {
      const [x0, y0] = this.toScreen(this.rect.x_cm, this.rect.y_cm + this.rect.height_cm);
      g.drawImage(this.base, x0, y0, this.rect.width_cm * this.k, this.rect.height_cm * this.k);
    }
    g.lineWidth = 1.5;
    for (const o of this.objects) {
      if (this.hidden.has(o.layer_id)) continue;
      g.fillStyle = (o.fill_color || '#7aa874') + '66';
      g.strokeStyle = o.stroke_color || '#3b5b38';
      g.beginPath();
      this.path(g, o);
      if (o.kind !== 'polyline') g.fill();
      g.stroke();
    }
    for (const a of this.anchors) this.dot(a, '#1565c0', 6);
    if (this.target) this.cross(this.target);
    if (this.puck) this.drawPuck(g);
  }

  path(g, o) {
    if (o.kind === 'polygon' || o.kind === 'polyline') {
      o.points.forEach((p, i) => { const [x, y] = this.toScreen(p[0], p[1]); if (i) g.lineTo(x, y); else g.moveTo(x, y); });
      if (o.kind === 'polygon') g.closePath();
    } else if (o.kind === 'circle') {
      const [x, y] = this.toScreen(o.cx, o.cy); g.arc(x, y, o.r * this.k, 0, 6.2832);
    } else if (o.kind === 'ellipse') {
      const [x, y] = this.toScreen(o.cx, o.cy);
      g.ellipse(x, y, o.rx * this.k, o.ry * this.k, -(o.rot_deg || 0) * Math.PI / 180, 0, 6.2832);
    } else if (o.kind === 'box') {
      const [x, y] = this.toScreen(o.x, o.y + o.h); g.rect(x, y, o.w * this.k, o.h * this.k);
    }
  }

  dot(p, colour, r) {
    const [x, y] = this.toScreen(p.x, p.y), g = this.ctx;
    g.fillStyle = colour; g.beginPath(); g.arc(x, y, r, 0, 6.2832); g.fill();
  }

  cross(p) {
    const [x, y] = this.toScreen(p.x, p.y), g = this.ctx;
    g.strokeStyle = '#d81b60'; g.lineWidth = 2; g.beginPath();
    g.moveTo(x - 14, y); g.lineTo(x + 14, y); g.moveTo(x, y - 14); g.lineTo(x, y + 14); g.stroke();
    g.beginPath(); g.arc(x, y, 8, 0, 6.2832); g.stroke();
  }

  drawPuck(g) {
    const t = this.puck;
    if (t.x_cm == null) return;
    const [x, y] = this.toScreen(t.x_cm, t.y_cm);
    const halo = (t.sigma_m / (this.mPerUnit || 0.01)) * this.k;
    const poor = t.quality === 'poor';
    g.fillStyle = poor ? 'rgba(120,120,120,.25)' : 'rgba(21,101,192,.18)';
    g.beginPath(); g.arc(x, y, Math.max(8, halo), 0, 6.2832); g.fill();
    if (t.heading_canvas_deg != null) {
      const a = (t.heading_canvas_deg - 90) * Math.PI / 180; // clockwise from plan-up, as a canvas arc angle
      g.fillStyle = 'rgba(21,101,192,.35)'; g.beginPath(); g.moveTo(x, y);
      g.arc(x, y, 42, a - 0.5, a + 0.5); g.closePath(); g.fill();
    }
    g.fillStyle = poor ? '#757575' : '#1565c0'; g.strokeStyle = '#fff'; g.lineWidth = 2;
    g.beginPath(); g.arc(x, y, 8, 0, 6.2832); g.fill(); g.stroke();
  }

  setPuck(t) {
    this.puck = t;
    if (this.follow && t && t.x_cm != null) { this.cx = t.x_cm; this.cy = t.y_cm; }
    this.draw();
  }
}
