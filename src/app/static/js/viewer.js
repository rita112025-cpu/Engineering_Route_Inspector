// Canvas 2D drawing viewer: pan, zoom, layers, issue markers and "locate this issue".
// World coordinates are CAD coordinates (y up); the screen is y down.

const PALETTE_LIGHT = ["#37474f", "#6d4c41", "#00695c", "#4527a0", "#6a1b9a", "#283593", "#00838f", "#827717", "#bf360c", "#33691e"];
const PALETTE_DARK = ["#90a4ae", "#bcaaa4", "#80cbc4", "#b39ddb", "#ce93d8", "#9fa8da", "#80deea", "#e6ee9c", "#ffab91", "#c5e1a5"];
const MARKER_PX = 8;
const PICK_PX = 12;
const MIN_ENTITY_PX = 0.4;

function hash(s) { let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return Math.abs(h); }

export class Viewer {
  constructor(canvas, hooks = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.hooks = hooks;
    this.entities = [];            // {h, l, t, g, box:[minx,miny,maxx,maxy]}
    this.layers = new Map();       // name -> {color, visible, count}
    this.extent = null;
    this.issues = [];
    this.selected = null;          // {issue, entities: [...], closest}
    this.view = { cx: 0, cy: 0, scale: 1 };
    this.w = 0; this.h = 0; this.dpr = 1;
    this.frame = 0;
    this.drag = null;
    this.stats = { drawn: 0, culled: 0, lastMs: 0 };
    this.readTheme();
    this.bind();
    new ResizeObserver(() => this.resize()).observe(canvas);
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { this.readTheme(); this.recolor(); this.draw(); });
    this.resize();
  }

  readTheme() {
    const cs = getComputedStyle(document.documentElement);
    const v = (n) => cs.getPropertyValue(n).trim();
    this.theme = { bg: v("--canvas-bg"), fail: v("--fail"), warn: v("--warn"), unk: v("--unk"), fg: v("--fg"),
                   dark: matchMedia("(prefers-color-scheme: dark)").matches };
  }

  statusColor(status) {
    return { FAIL: this.theme.fail, WARNING: this.theme.warn, UNKNOWN: this.theme.unk }[status] ?? this.theme.fg;
  }

  recolor() {
    const pal = this.theme.dark ? PALETTE_DARK : PALETTE_LIGHT;
    for (const [name, info] of this.layers) info.color = pal[hash(name) % pal.length];
  }

  // ---- data ------------------------------------------------------------------------------------------------
  setDrawing(geo) {
    this.entities = geo.entities.map((e) => ({ ...e, box: bboxOf(e.g) }));
    this.extent = geo.extent;
    const counts = new Map();
    for (const e of this.entities) counts.set(e.l, (counts.get(e.l) ?? 0) + 1);
    this.layers = new Map([...counts].sort().map(([name, count]) => [name, { color: "", visible: true, count }]));
    this.recolor();
    this.issues = [];
    this.selected = null;
    this.fit(false);
  }

  clearDrawing() { this.entities = []; this.layers = new Map(); this.extent = null; this.issues = []; this.selected = null; this.draw(); }
  setIssues(issues) { this.issues = issues.filter((i) => i.location && i.status !== "PASS"); this.draw(); }
  setLayerVisible(name, visible) { const l = this.layers.get(name); if (l) { l.visible = visible; this.draw(); } }
  hasDrawing() { return this.entities.length > 0; }

  select(issue, entities = [], closest = null) {
    this.selected = issue ? { issue, entities, closest } : null;
    this.draw();
  }

  // ---- view ------------------------------------------------------------------------------------------------
  resize() {
    const r = this.canvas.getBoundingClientRect();
    this.dpr = window.devicePixelRatio || 1;
    this.w = Math.max(1, Math.round(r.width)); this.h = Math.max(1, Math.round(r.height));
    this.canvas.width = Math.round(this.w * this.dpr); this.canvas.height = Math.round(this.h * this.dpr);
    this.draw();
  }

  fit(redraw = true) {
    const ex = this.extent;
    if (!ex) { this.view = { cx: 0, cy: 0, scale: 1 }; if (redraw) this.draw(); return; }
    this.fitBox(ex, 0.06, false);
    if (redraw) this.draw();
  }

  fitBox([x0, y0, x1, y1], pad = 0.25, animate = true, minSpan = 0) {
    const span = Math.max(x1 - x0, y1 - y0, minSpan, 1e-6);
    const w = Math.max(x1 - x0, span * 0.05), h = Math.max(y1 - y0, span * 0.05);
    const target = {
      cx: (x0 + x1) / 2, cy: (y0 + y1) / 2,
      scale: Math.min(this.w / (w * (1 + 2 * pad)), this.h / (h * (1 + 2 * pad))),
    };
    if (!animate || matchMedia("(prefers-reduced-motion: reduce)").matches) { this.view = target; this.draw(); return; }
    const from = { ...this.view }, t0 = performance.now(), dur = 260;
    const step = (now) => {
      const k = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - k, 3);
      // interpolate scale on a log axis so zooming feels even
      this.view = { cx: from.cx + (target.cx - from.cx) * e, cy: from.cy + (target.cy - from.cy) * e,
                    scale: Math.exp(Math.log(from.scale) + (Math.log(target.scale) - Math.log(from.scale)) * e) };
      this.draw();
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  zoomBy(factor, px = this.w / 2, py = this.h / 2) {
    const before = this.toWorld(px, py);
    this.view.scale = Math.min(Math.max(this.view.scale * factor, 1e-9), 1e9);
    const after = this.toWorld(px, py);
    this.view.cx += before[0] - after[0];
    this.view.cy += before[1] - after[1];
    this.draw();
  }

  toScreen(x, y) { return [(x - this.view.cx) * this.view.scale + this.w / 2, this.h / 2 - (y - this.view.cy) * this.view.scale]; }
  toWorld(px, py) { return [(px - this.w / 2) / this.view.scale + this.view.cx, this.view.cy - (py - this.h / 2) / this.view.scale]; }

  // ---- input -----------------------------------------------------------------------------------------------
  bind() {
    const c = this.canvas;
    c.addEventListener("pointerdown", (e) => {
      c.setPointerCapture(e.pointerId);
      this.drag = { x: e.clientX, y: e.clientY, cx: this.view.cx, cy: this.view.cy, moved: false };
    });
    c.addEventListener("pointermove", (e) => {
      if (!this.drag) return;
      const dx = e.clientX - this.drag.x, dy = e.clientY - this.drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) { this.drag.moved = true; c.classList.add("dragging"); }
      if (!this.drag.moved) return;
      this.view.cx = this.drag.cx - dx / this.view.scale;
      this.view.cy = this.drag.cy + dy / this.view.scale;
      this.schedule();
    });
    const end = (e) => {
      if (!this.drag) return;
      const wasClick = !this.drag.moved;
      this.drag = null;
      c.classList.remove("dragging");
      if (wasClick && e.type === "pointerup") this.click(e);
    };
    c.addEventListener("pointerup", end);
    c.addEventListener("pointercancel", end);
    c.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = c.getBoundingClientRect();
      this.zoomBy(Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });
    c.addEventListener("dblclick", () => this.fit());
    c.addEventListener("keydown", (e) => {
      const step = 60 / this.view.scale;
      if (e.key === "+" || e.key === "=") this.zoomBy(1.25);
      else if (e.key === "-") this.zoomBy(0.8);
      else if (e.key === "0") this.fit();
      else if (e.key === "ArrowLeft") { this.view.cx -= step; this.draw(); }
      else if (e.key === "ArrowRight") { this.view.cx += step; this.draw(); }
      else if (e.key === "ArrowUp") { this.view.cy += step; this.draw(); }
      else if (e.key === "ArrowDown") { this.view.cy -= step; this.draw(); }
      else return;
      e.preventDefault();
    });
  }

  click(e) {
    const r = this.canvas.getBoundingClientRect();
    const px = e.clientX - r.left, py = e.clientY - r.top;
    let best = null, bestD = PICK_PX;
    for (const issue of this.issues) {
      const [sx, sy] = this.toScreen(issue.location[0], issue.location[1]);
      const d = Math.hypot(sx - px, sy - py);
      if (d <= bestD) { best = issue; bestD = d; }
    }
    if (this.hooks.onPick) this.hooks.onPick(best);
  }

  // ---- drawing ---------------------------------------------------------------------------------------------
  schedule() { if (!this.frame) this.frame = requestAnimationFrame(() => { this.frame = 0; this.draw(); }); }

  draw() {
    const ctx = this.ctx, t0 = performance.now();
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.fillStyle = this.theme.bg;
    ctx.fillRect(0, 0, this.w, this.h);
    this.drawGeometry(ctx);
    this.drawSelection(ctx);
    this.drawMarkers(ctx);
    this.stats.lastMs = performance.now() - t0;
  }

  drawGeometry(ctx) {
    const { scale } = this.view;
    const [wx0, wy1] = this.toWorld(0, 0), [wx1, wy0] = this.toWorld(this.w, this.h);
    const dim = this.selected ? 0.35 : 0.8;
    let drawn = 0, culled = 0;
    ctx.lineWidth = 1;
    ctx.globalAlpha = dim;
    ctx.lineJoin = "round";
    const byLayer = new Map();
    for (const e of this.entities) {
      const L = this.layers.get(e.l);
      if (!L || !L.visible) continue;
      const b = e.box;
      if (b[2] < wx0 || b[0] > wx1 || b[3] < wy0 || b[1] > wy1) { culled++; continue; }
      if ((b[2] - b[0]) * scale < MIN_ENTITY_PX && (b[3] - b[1]) * scale < MIN_ENTITY_PX) { culled++; continue; }
      let list = byLayer.get(e.l);
      if (!list) byLayer.set(e.l, (list = []));
      list.push(e);
    }
    for (const [name, list] of byLayer) {
      const color = this.layers.get(name).color;
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.beginPath();
      for (const e of list) { this.path(ctx, e); drawn++; }
      ctx.stroke();
      for (const e of list) if (e.g.kind === "text" && e.g.height * scale >= 5) this.text(ctx, e);
    }
    ctx.globalAlpha = 1;
    this.stats.drawn = drawn; this.stats.culled = culled;
  }

  path(ctx, e) {
    const g = e.g;
    if (g.kind === "polyline") {
      const pts = g.points;
      const [x0, y0] = this.toScreen(pts[0][0], pts[0][1]);
      ctx.moveTo(x0, y0);
      for (let i = 1; i < pts.length; i++) { const [x, y] = this.toScreen(pts[i][0], pts[i][1]); ctx.lineTo(x, y); }
      if (g.closed) ctx.closePath();
    } else if (g.kind === "circle") {
      const [cx, cy] = this.toScreen(g.center[0], g.center[1]), r = g.radius * this.view.scale;
      ctx.moveTo(cx + r, cy);
      ctx.arc(cx, cy, r, 0, Math.PI * 2);
    }
  }

  text(ctx, e) {
    const g = e.g, [x, y] = this.toScreen(g.position[0], g.position[1]);
    ctx.font = `${Math.min(g.height * this.view.scale, 48)}px sans-serif`;
    ctx.fillText(g.text.split("\n")[0], x, y);
  }

  drawSelection(ctx) {
    const sel = this.selected;
    if (!sel) return;
    const color = this.statusColor(sel.issue.status);
    ctx.save();
    ctx.strokeStyle = color; ctx.lineWidth = 3.5; ctx.lineJoin = "round"; ctx.globalAlpha = 1;
    ctx.beginPath();
    for (const e of sel.entities) this.path(ctx, e);
    ctx.stroke();
    if (sel.closest) {
      const [a, b] = sel.closest.map(([x, y]) => this.toScreen(x, y));
      ctx.lineWidth = 2; ctx.setLineDash([6, 4]);
      ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
    }
    ctx.restore();
  }

  drawMarkers(ctx) {
    for (const issue of this.issues) {
      const [x, y] = this.toScreen(issue.location[0], issue.location[1]);
      if (x < -20 || y < -20 || x > this.w + 20 || y > this.h + 20) continue;
      const isSel = this.selected && this.selected.issue.issue_id === issue.issue_id;
      this.marker(ctx, issue.status, x, y, isSel ? MARKER_PX * 1.5 : MARKER_PX, isSel);
    }
  }

  marker(ctx, status, x, y, r, selected) {
    const color = this.statusColor(status);
    ctx.save();
    ctx.lineWidth = selected ? 3 : 2;
    ctx.strokeStyle = color;
    ctx.fillStyle = selected ? color : this.theme.bg;
    ctx.globalAlpha = 1;
    ctx.beginPath();
    if (status === "FAIL") ctx.arc(x, y, r, 0, Math.PI * 2);                                   // circle
    else if (status === "WARNING") { ctx.moveTo(x, y - r); ctx.lineTo(x + r, y + r * 0.8); ctx.lineTo(x - r, y + r * 0.8); ctx.closePath(); } // triangle
    else ctx.rect(x - r * 0.85, y - r * 0.85, r * 1.7, r * 1.7);                                // square
    ctx.globalAlpha = selected ? 0.9 : 0.75;
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.stroke();
    if (selected) { ctx.beginPath(); ctx.arc(x, y, r + 5, 0, Math.PI * 2); ctx.lineWidth = 1.5; ctx.stroke(); }
    ctx.restore();
  }

  // pixel probe used by the automated browser test: how much of the canvas differs from its background
  probe() {
    const w = this.canvas.width, h = this.canvas.height;
    const copy = document.createElement("canvas");               // read back from a copy so the visible canvas
    copy.width = w; copy.height = h;                             // keeps its fast GPU path
    const cctx = copy.getContext("2d", { willReadFrequently: true });
    cctx.drawImage(this.canvas, 0, 0);
    const data = cctx.getImageData(0, 0, w, h).data;
    const br = data[0], bgc = data[1], bb = data[2];            // top-left pixel is background
    let nonBg = 0, reds = 0;
    for (let i = 0; i < data.length; i += 4) {
      const r = data[i], g = data[i + 1], b = data[i + 2];
      if (Math.abs(r - br) + Math.abs(g - bgc) + Math.abs(b - bb) > 24) nonBg++;
      if (r > 150 && g < 110 && b < 110) reds++;
    }
    return { nonBg, reds, width: w, height: h, drawn: this.stats.drawn };
  }
}

function bboxOf(g) {
  if (g.kind === "polyline") {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const [x, y] of g.points) { if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; }
    return [x0, y0, x1, y1];
  }
  if (g.kind === "circle") return [g.center[0] - g.radius, g.center[1] - g.radius, g.center[0] + g.radius, g.center[1] + g.radius];
  const [x, y] = g.position, w = Math.max(g.text.length, 1) * g.height * 0.6;
  return [x, y, x + w, y + g.height];
}

export function entitiesBox(entities, extraPoints = []) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const e of entities) { const b = bboxOf(e.g); x0 = Math.min(x0, b[0]); y0 = Math.min(y0, b[1]); x1 = Math.max(x1, b[2]); y1 = Math.max(y1, b[3]); }
  for (const [x, y] of extraPoints) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
  return Number.isFinite(x0) ? [x0, y0, x1, y1] : null;
}
