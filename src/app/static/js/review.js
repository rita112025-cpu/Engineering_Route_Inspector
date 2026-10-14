// Rule review: the rules the specification scan found, grouped by what a person still has to do.
// Only "needs review" and "conflict" cards ask for anything; the others can be read, switched off, or traced to the
// original sentence. Nothing here runs an analysis or decides for the user.
import { api, ApiError } from "./api.js";
import { $, clear, debounce, el, toast } from "./util.js";

const UNITS = [["mm", "毫米 mm"], ["cm", "公分 cm"], ["m", "公尺 m"], ["ft", "英尺 ft"], ["in", "英吋 in"]];
const BADGE = {
  CONFIRMED: ["✓ CONFIRMED", "可直接使用"], NEEDS_REVIEW: ["⚠ NEEDS_REVIEW", "需要確認"],
  CONFLICTING: ["⚠ CONFLICTING", "規範衝突"], NOT_ENFORCEABLE: ["— NOT ENFORCEABLE", "無法自動檢查"],
};
const TABS = [["all", "全部", "total"], ["usable", "可使用", "usable"], ["review", "需確認", "review"],
              ["conflict", "衝突", "conflict"], ["not_enforceable", "不可自動檢查", "not_enforceable"], ["off", "已停用", "off"]];
const PAGE = 200;

let R = null;

// What "start analysis" will do, in words (also used under the run button).
export function gateLines(gate) {
  if (!gate) return [];
  const lines = [`${gate.will_run} 條規則會執行`];
  if (gate.review) lines.push(`${gate.review} 條尚未確認`);
  if (gate.conflict) lines.push(`${gate.conflict} 條存在衝突`);
  if (gate.not_enforceable) lines.push(`${gate.not_enforceable} 條僅供人工查核`);
  if (gate.review || gate.conflict) lines.push("本次未確認規則不會執行。");
  return lines;
}

export async function openReview({ project, drawing, systems, onChange, onRun }) {
  R = { project, onChange, onRun, bucket: "all", q: "", items: [], total: 0, summary: null, gate: null,
        layers: (drawing?.info?.layers ?? []).map((l) => l.name), systems: (systems ?? []).map((s) => s.name).filter(Boolean),
        shown: new Set() };
  const dlg = $("#review-dialog");
  $("#rv-search").value = "";
  $("#review-close").onclick = () => dlg.close();
  $("#rv-run").onclick = () => { dlg.close(); R.onRun?.(); };
  $("#rv-search").oninput = debounce(() => { R.q = $("#rv-search").value.trim(); reload().catch(report); }, 250);
  if (!dlg.open) dlg.showModal();
  await reload();
}

const report = (e) => toast(e instanceof ApiError ? e.message : String(e?.message ?? e), "error");

async function fetchPage(offset) {
  const q = new URLSearchParams({ offset: String(offset), limit: String(PAGE) });
  if (R.bucket !== "all") q.set("bucket", R.bucket);
  if (R.q) q.set("q", R.q);
  return api.get(`/api/projects/${R.project.id}/rule-candidates?${q}`);
}

async function reload() {
  const page = await fetchPage(0);
  R.items = page.items; R.total = page.total; R.summary = page.summary; R.gate = page.gate;
  render();
  R.onChange?.(page.summary, page.gate);
}

async function more() {
  const page = await fetchPage(R.items.length);
  R.items.push(...page.items);
  render();
}

async function decide(id, body, doneText) {
  try {
    const out = await api.patch(`/api/projects/${R.project.id}/rule-candidates/${id}`, body);
    if (out.merged) toast("這個選擇讓它和另一條規則完全相同，已合併為同一條（兩處來源都保留）。");
    else if (doneText) toast(doneText);
    await reload();
  } catch (e) { report(e); }
}

// ---- layout --------------------------------------------------------------------------------------------------------
function render() {
  renderTabs();
  const box = clear($("#rv-list"));
  if (!R.items.length) {
    box.append(el("p", { class: "rv-empty" }, R.summary?.total
      ? (R.q ? "找不到符合的規則。" : "這個分類目前沒有項目。")
      : "還沒有規則。請回到左側按「掃描規範」，或把單一規範檔拖進「進階設定」。"));
  }
  const seen = new Set();
  for (const c of R.items) {
    if (c.conflict && c.bucket === "conflict") {          // one card per disagreement, with every value side by side
      if (seen.has(c.conflict.group)) continue;
      seen.add(c.conflict.group);
      box.append(conflictCard(c));
    } else box.append(card(c));
  }
  if (R.total > R.items.length) {
    box.append(el("button", { type: "button", class: "btn more", onclick: () => more().catch(report) },
      `顯示更多（${R.items.length} / ${R.total}）`));
  }
  const lines = gateLines(R.gate);
  $("#rv-gate").textContent = lines.join("　");
  $("#rv-run").disabled = !R.gate?.runnable;
}

function renderTabs() {
  const box = clear($("#rv-tabs"));
  for (const [key, label, field] of TABS) {
    const n = R.summary?.[field] ?? 0;
    if (key === "off" && !n) continue;
    box.append(el("button", { type: "button", class: `chip tab-${key}`, "aria-pressed": String(R.bucket === key), dataset: { bucket: key },
      onclick: () => { R.bucket = key; reload().catch(report); } }, el("b", {}, String(n)), label));
  }
}

// ---- cards ---------------------------------------------------------------------------------------------------------
function source(c) {
  const ev = c.evidence;
  return [ev.section, ev.ref].filter(Boolean).join(" / ");
}

function evidenceBox(c, open) {
  const ev = c.evidence;
  const box = el("div", { class: "rv-evi", hidden: !open },
    el("div", { class: "rv-ref" }, `${ev.document}${ev.page != null ? `　第 ${ev.page} 頁` : ""}${ev.section ? `　${ev.section}` : ""}`),
    el("q", {}, ev.text));
  if (c.duplicates?.length) {
    box.append(el("div", { class: "rv-ref" }, `另有 ${c.duplicates.length} 處相同規定：` +
      c.duplicates.slice(0, 4).map((d) => `${d.document}${d.page != null ? ` 第 ${d.page} 頁` : ""}`).join("、")));
  }
  return box;
}

function toggleButton(box, label = "查看原文") {
  return el("button", { type: "button", class: "btn small", onclick: () => { box.hidden = !box.hidden; } }, label);
}

function partiesLine(c) {
  const bits = c.parties.map((p) => p.mapped ? p.selector_label : (p.text ? `${p.text}（尚未對應）` : "？"));
  return bits.join(c.template_id === "keep_out" || c.template_id === "inside_zone" ? " → " : " ↔ ");
}

function card(c) {
  const [en, zh] = BADGE[c.status];
  const evi = evidenceBox(c, false);
  const acts = el("div", { class: "rv-acts" });
  const node = el("article", { class: `rv-card ${c.status} bucket-${c.bucket}`, dataset: { id: c.id, status: c.status, bucket: c.bucket } },
    el("div", { class: "rv-head" },
      el("span", { class: `rv-badge ${c.status}` }, en), el("span", { class: "rv-zh" }, zh),
      c.confirmed_by === "user" ? el("span", { class: "rv-note" }, "已由你確認") : null,
      c.severity === "WARNING" && c.status === "CONFIRMED" ? el("span", { class: "rv-note" }, "不符合＝警告") : null,
      c.bucket === "off" ? el("span", { class: "rv-note" }, c.status === "CONFLICTING" ? "已選擇另一個數值" : "已停用") : null));
  if (c.status === "NOT_ENFORCEABLE") {
    node.append(el("p", { class: "rv-quote" }, `「${c.evidence.text}」`),
      el("p", { class: "rv-reason" }, c.reason || "目前無法由 2D DXF 自動量化。"),
      el("div", { class: "rv-src" }, `來源：${source(c)}`), evi);
    acts.append(toggleButton(evi));
    node.append(acts);
    return node;
  }
  node.append(el("div", { class: "rv-title" }, partiesLine(c)),
    el("div", { class: "rv-req" }, c.requirement || "（檢查內容未確定）"),
    el("div", { class: "rv-src" }, `來源：${source(c)}`));
  if (c.missing.length) {
    node.append(el("div", { class: "rv-missing" }, "缺少：", el("ul", {}, c.missing.map((m) => el("li", {}, m.label)))));
  }
  for (const w of c.warnings ?? []) node.append(el("div", { class: "rv-note" }, w));
  node.append(evi);
  if (c.status === "NEEDS_REVIEW") fixControls(c, acts);
  acts.append(toggleButton(evi));
  if (c.bucket === "off") {
    if (c.status === "CONFLICTING") {
      acts.append(el("button", { type: "button", class: "btn small", onclick: () => decide(c.id, { choose: true }, "已改選這個數值。") }, "改選這個數值"));
    } else {
      acts.append(el("button", { type: "button", class: "btn small", onclick: () => decide(c.id, { disabled: false }) }, "啟用"));
    }
  } else if (c.status === "NEEDS_REVIEW") {
    acts.append(el("button", { type: "button", class: "btn small", onclick: () => decide(c.id, { disabled: true }) }, "忽略"));
  } else {
    acts.append(el("button", { type: "button", class: "btn small", onclick: () => decide(c.id, { disabled: true }) }, "停用"));
  }
  if (Object.keys(c.decision ?? {}).length && c.bucket !== "off") {
    acts.append(el("button", { type: "button", class: "btn small", onclick: () => decide(c.id, { reset: true }, "已還原為擷取的結果。") }, "還原"));
  }
  node.append(acts);
  return node;
}

function selectorOptions() {
  const opts = [el("option", { value: "" }, "請選擇…")];
  if (R.systems.length) opts.push(el("optgroup", { label: "系統" }, R.systems.map((n) => el("option", { value: `system:${n}` }, n))));
  if (R.layers.length) opts.push(el("optgroup", { label: "單一圖層" }, R.layers.map((n) => el("option", { value: `layer:${n}` }, n))));
  return opts;
}

function fixControls(c, acts) {
  for (const slot of c.fix.slots) {
    const label = { subject: "檢查對象", target: "附近物件", zone: "區域" }[slot];
    const sel = el("select", { "aria-label": `選擇${label}`, dataset: { slot }, onchange: (e) => {
      const v = e.target.value;
      if (!v) return;
      const i = v.indexOf(":");
      decide(c.id, { selectors: { [slot]: { [v.slice(0, i)]: v.slice(i + 1) } } }, `已指定${label}。`);
    } }, selectorOptions());
    acts.append(el("label", { class: "rv-fix" }, `${label}：`, sel));
  }
  if (c.fix.axis) {
    acts.append(el("label", { class: "rv-fix" }, "方向：", el("select", { "aria-label": "水平或垂直", dataset: { fix: "axis" }, onchange: (e) => {
      if (e.target.value) decide(c.id, { template_id: e.target.value });
    } }, el("option", { value: "" }, "請選擇…"), el("option", { value: "clearance" }, "水平淨距"),
      el("option", { value: "vertical_clearance" }, "垂直淨距"))));
  }
  if (c.fix.direction && c.fix.direction_ops?.length) {
    const op = c.fix.direction_ops[0];
    acts.append(el("button", { type: "button", class: "btn small", dataset: { fix: "direction" },
      onclick: () => decide(c.id, { operator: op }, "已指定比較方向。") },
      op === ">=" ? "原意是「不得小於」（≥）" : "原意是「不得大於」（≤）"));
  }
  if (c.fix.unit) {
    acts.append(el("label", { class: "rv-fix" }, "原文沒有單位，請選：", el("select", { "aria-label": "單位", dataset: { fix: "unit" }, onchange: (e) => {
      if (e.target.value) decide(c.id, { unit: e.target.value });
    } }, el("option", { value: "" }, "請選擇…"), UNITS.map(([v, l]) => el("option", { value: v }, l)))));
  }
  if (c.fix.tolerance) {
    const input = el("input", { type: "number", min: "0", step: "any", "aria-label": "容許偏差（度）", dataset: { fix: "tolerance" } });
    acts.append(el("label", { class: "rv-fix" }, "容許偏差（度）：", input,
      el("button", { type: "button", class: "btn small", onclick: () => {
        if (input.value !== "") decide(c.id, { value: Number(input.value) });
      } }, "套用")));
  }
  if (c.fix.accept) {
    const soft = c.missing.some((m) => m.code === "ADVISORY" || m.code === "NO_MODAL");
    acts.append(el("button", { type: "button", class: "btn small", dataset: { fix: "accept" }, onclick: () => decide(c.id, { accept: true },
      soft ? "已接受，不符合時會以「警告」處理。" : "已確認。") }, soft ? "接受（以警告處理）" : "確認沿用"));
  }
}

function conflictCard(first) {
  const [en, zh] = BADGE.CONFLICTING;
  const options = first.conflict.options;
  const node = el("article", { class: "rv-card CONFLICTING bucket-conflict", dataset: { id: first.id, status: "CONFLICTING", bucket: "conflict" } },
    el("div", { class: "rv-head" }, el("span", { class: "rv-badge CONFLICTING" }, en), el("span", { class: "rv-zh" }, zh)),
    el("div", { class: "rv-title" }, partiesLine(first)),
    el("p", { class: "hint" }, "同一組對象在規範中有不同的要求。系統不會自動選較嚴格的值，也不會選較新的版本，請你決定。"));
  const row = el("div", { class: "rv-options" });
  for (const o of options) {
    const evi = el("div", { class: "rv-evi" }, el("div", { class: "rv-ref" }, `${o.source.document}${o.source.page != null ? `　第 ${o.source.page} 頁` : ""}${o.source.section ? `　${o.source.section}` : ""}`),
      el("q", {}, o.source.text));
    row.append(el("div", { class: "rv-option", dataset: { id: o.id } },
      el("div", { class: "rv-req" }, o.text), el("div", { class: "rv-src" }, `來源：${o.source.document}`), evi,
      el("div", { class: "rv-acts" },
        el("button", { type: "button", class: "btn small primary", dataset: { choose: String(o.value) },
          onclick: () => decide(o.id, { choose: true }, "已選擇這個數值。") }, `選 ${o.value}`),
        el("button", { type: "button", class: "btn small", onclick: () => decide(o.id, { disabled: true }) }, "停用"))));
  }
  node.append(row);
  return node;
}
