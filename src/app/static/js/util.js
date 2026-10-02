// Small DOM helpers. Text is always inserted as text nodes (never as HTML).

export const STATUS = {
  FAIL: { zh: "不合格", order: 0 },
  WARNING: { zh: "警告", order: 1 },
  UNKNOWN: { zh: "無法判定", order: 2 },
  PASS: { zh: "合格", order: 3 },
};
export const CONFIDENCE_ZH = { CONFIRMED: "已確認", INFERRED: "推算", UNKNOWN: "無法判定" };
export const BASELINE_ZH = { NEW: "新增", CHANGED: "有變化", UNCHANGED: "未變" };

export function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k === "dataset") Object.assign(e.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2).toLowerCase(), v);
    else e.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return e;
}

export function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }
export const $ = (sel, root = document) => root.querySelector(sel);

// localStorage can be missing or throw (private windows, blocked storage): it is only a convenience.
export const store = {
  get(key) { try { return window.localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { window.localStorage.setItem(key, value); } catch { /* ignore */ } },
};

const MAX_TOASTS = 3;

export function toast(message, kind = "ok", ms) {
  const box = $("#toasts");
  const node = el("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : "status" },
    el("span", { class: "msg" }, message),
    el("button", { type: "button", "aria-label": "關閉", onclick: () => node.remove() }, "×"));
  while (box.children.length >= MAX_TOASTS) box.firstChild.remove();       // never pile up over the results
  box.append(node);
  const life = ms ?? (kind === "error" ? 14000 : kind === "warn" ? 9000 : 4500);
  setTimeout(() => node.remove(), life);
  return node;
}

function dialogReady(dlg) { if (!dlg.open) dlg.showModal(); }

// Promise-based replacements for confirm()/prompt() that look the same everywhere and can be tested.
export function ask({ title, text = "", input = null, ok = "確定", danger = false }) {
  const dlg = $("#ask-dialog"), form = $("#ask-form"), inp = $("#ask-input");
  $("#ask-title").textContent = title;
  $("#ask-text").textContent = text;
  inp.hidden = input === null;
  inp.value = input ?? "";
  const okBtn = $("#ask-ok");
  okBtn.textContent = ok;
  okBtn.classList.toggle("danger", danger);
  return new Promise((resolve) => {
    const done = (value) => { dlg.close(); form.onsubmit = null; $("#ask-cancel").onclick = null; resolve(value); };
    form.onsubmit = (e) => { e.preventDefault(); done(input === null ? true : inp.value.trim()); };
    $("#ask-cancel").onclick = () => done(input === null ? false : null);
    dlg.oncancel = (e) => { e.preventDefault(); done(input === null ? false : null); };
    dialogReady(dlg);
    (input === null ? okBtn : inp).focus();
    if (input !== null) inp.select();
  });
}

export function fmtNumber(v) {
  if (v == null) return "—";
  if (Math.abs(v - Math.round(v)) < 0.005) return String(Math.round(v));
  return v.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
}

export function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export function escapeRegex(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

// Layer lists <-> the regular expression the server stores. Only expressions made by layersToRegex are parsed back.
export function layersToRegex(layers) { return `^(?:${layers.map(escapeRegex).join("|")})$`; }
export function regexToLayers(rx) {
  const m = /^\^\(\?:(.*)\)\$$/s.exec(rx || "");
  if (!m) return null;
  const parts = [];
  let cur = "";
  for (let i = 0; i < m[1].length; i++) {
    const ch = m[1][i];
    if (ch === "\\") { cur += m[1][++i] ?? ""; }
    else if (ch === "|") { parts.push(cur); cur = ""; }
    else if (/[.*+?^${}()[\]]/.test(ch)) return null;      // a real regex feature: not ours
    else cur += ch;
  }
  parts.push(cur);
  return parts.filter(Boolean);
}
