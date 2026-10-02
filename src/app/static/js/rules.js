// Rule Builder: group layers into systems, build rules from sentence templates, drag specification
// paragraphs onto rules. Nobody edits JSON here (an import/export is tucked away under "進階").
import { api, ApiError } from "./api.js";
import { $, ask, clear, debounce, el, layersToRegex, regexToLayers, toast } from "./util.js";

const UNITS = [["mm", "毫米 mm"], ["cm", "公分 cm"], ["m", "公尺 m"], ["ft", "英尺 ft"], ["in", "英吋 in"]];
const EVIDENCE_MIME = "text/x-eri-evidence";
const BLANK_LABEL = { subject: "檢查對象", target: "附近物件", zone: "區域" };

let S = null;   // builder state for the open dialog

export async function openRuleBuilder({ project, drawing, onSaved }) {
  const [rules, templates] = await Promise.all([
    api.get(`/api/projects/${project.id}/rules`), api.get("/api/rule-templates")]);
  const layers = (drawing?.info?.layers ?? []).map((l) => l.name);
  S = {
    project, onSaved, layers, templates: templates.templates, dirty: false, evi: new Map(),
    systems: rules.ruleset.systems.map((s) => {
      const list = regexToLayers(s.layer_regex);
      return { name: s.name, layers: list ?? [], advanced: list === null, regex: s.layer_regex };
    }),
    rules: rules.ruleset.rules,
    form: newForm(templates.templates[0].id),
    query: "",
  };
  const dlg = $("#rules-dialog");
  $("#rb-errors").textContent = "";
  dlg.oncancel = (e) => { e.preventDefault(); requestClose(); };
  $("#rules-close").onclick = requestClose;
  $("#rb-cancel").onclick = requestClose;
  $("#rb-save").onclick = save;
  renderAll();
  if (!dlg.open) dlg.showModal();
  loadSpec();
}

function newForm(templateId) { return { templateId, params: {}, editingId: null, evidenceId: null, errors: [] }; }
const dirty = () => { S.dirty = true; };
const template = (id) => S.templates.find((t) => t.id === id);

async function requestClose() {
  if (S.dirty && !(await ask({ title: "放棄尚未儲存的變更？", text: "規則的修改還沒有儲存。", ok: "放棄變更", danger: true }))) return;
  $("#rules-dialog").close();
}

// ---- layout -------------------------------------------------------------------------------------------------
function renderAll() {
  const body = clear($("#rb-body"));
  const main = el("div", { class: "rb-main" },
    el("h3", {}, "① 系統（把圖層分組）"),
    el("p", { class: "hint" }, "例如把 SCADA 開頭的圖層歸成「SCADA」。規則就可以直接說「SCADA 與電力」，不必管圖層名稱。"),
    el("div", { id: "rb-systems" }),
    el("div", { class: "rb-row" },
      el("button", { type: "button", class: "btn small", id: "rb-add-system", onclick: addSystem }, "＋ 新增系統"),
      el("button", { type: "button", class: "btn small", id: "rb-auto", onclick: autoGroup }, "依圖層名稱自動分組")),
    el("h3", {}, "② 規則"),
    el("div", { id: "rb-rules" }),
    el("div", { class: "rb-new", id: "rb-new" }),
    advanced());
  const aside = el("aside", { class: "rb-spec" },
    el("h3", {}, "規範段落"),
    el("p", { class: "hint" }, "把段落拖到規則上，就會連結成規範依據；拖到「新增規則」會自動帶入數值。"),
    el("input", { type: "search", id: "rb-q", placeholder: "搜尋規範內容", "aria-label": "搜尋規範內容",
                  oninput: debounce((e) => { S.query = e.target.value.trim(); loadSpec(); }, 250) }),
    el("div", { id: "rb-paras" }));
  body.append(main, aside);
  renderSystems(); renderRules(); renderNew();
}

function advanced() {
  const file = el("input", { type: "file", accept: ".json,application/json", hidden: true, id: "rb-import",
    onchange: importJson });
  return el("details", { class: "adv" }, el("summary", {}, "進階：規則檔（JSON）"),
    el("p", { class: "hint" }, "只有需要在多個專案之間共用規則時才用到。"),
    el("div", { class: "rb-row" },
      el("a", { class: "btn small", href: `/api/projects/${S.project.id}/rules/export`, download: "" }, "匯出規則檔"),
      el("button", { type: "button", class: "btn small", onclick: () => file.click() }, "匯入規則檔…"), file));
}

// ---- systems -------------------------------------------------------------------------------------------------
function allLayers(sys) { return [...new Set([...S.layers, ...sys.layers])]; }

function renderSystems() {
  const box = clear($("#rb-systems"));
  if (!S.systems.length) box.append(el("p", { class: "hint" }, "還沒有系統。可以按「依圖層名稱自動分組」，或手動新增。"));
  S.systems.forEach((sys, idx) => {
    const name = el("input", { type: "text", value: sys.name, maxlength: 40, "aria-label": "系統名稱", placeholder: "系統名稱",
      oninput: (e) => { sys.name = e.target.value; dirty(); renderNewSelectors(); } });
    let content;
    if (sys.advanced) {
      content = el("div", {}, el("input", { type: "text", value: sys.regex, "aria-label": "圖層比對規則（進階）",
        oninput: (e) => { sys.regex = e.target.value; dirty(); } }),
        el("p", { class: "hint" }, "這是自訂的圖層比對規則（進階）。"));
    } else {
      const names = allLayers(sys);
      content = el("div", { class: "rb-layers", role: "group", "aria-label": `${sys.name || "系統"}包含的圖層` },
        names.length ? names.map((l) => el("label", {},
          el("input", { type: "checkbox", checked: sys.layers.includes(l), onchange: (e) => {
            sys.layers = e.target.checked ? [...sys.layers, l] : sys.layers.filter((x) => x !== l); dirty(); } }), l))
          : el("span", { class: "hint" }, "（圖面沒有圖層資料）"));
    }
    box.append(el("div", { class: "rb-sys" }, name, content,
      el("button", { type: "button", class: "btn small danger", "aria-label": `刪除系統 ${sys.name}`, onclick: () => {
        S.systems.splice(idx, 1); dirty(); renderSystems(); renderNewSelectors(); } }, "刪除")));
  });
}

function addSystem() { S.systems.push({ name: "", layers: [], advanced: false, regex: "" }); dirty(); renderSystems(); }

function autoGroup() {
  const taken = new Set(S.systems.flatMap((s) => s.layers));
  const groups = new Map();
  for (const l of S.layers) {
    if (taken.has(l) || /^(0|defpoints)$/i.test(l)) continue;
    const key = l.split(/[-_. ]/)[0] || l;
    groups.set(key, [...(groups.get(key) ?? []), l]);
  }
  let made = 0;
  for (const [key, list] of groups) {
    if (S.systems.some((s) => s.name.toLowerCase() === key.toLowerCase())) continue;
    S.systems.push({ name: key, layers: list, advanced: false, regex: "" });
    made++;
  }
  if (made) { dirty(); renderSystems(); renderNewSelectors(); }
  toast(made ? `已建立 ${made} 個系統，請確認每個系統包含的圖層。` : "沒有可以自動分組的圖層。", made ? "ok" : "warn");
}

// ---- rules -----------------------------------------------------------------------------------------------------
function sentence(r) { return r.description || r.name || r.id; }

async function evidenceOf(id) {
  if (!S.evi.has(id)) {
    S.evi.set(id, api.get(`/api/projects/${S.project.id}/evidence/${encodeURIComponent(id)}`).catch(() => null));
  }
  return S.evi.get(id);
}

async function fillEvidenceLine(box, id) {
  const ev = await evidenceOf(id);
  clear(box);
  if (!ev) { box.append("規範依據：找不到這段規範（文件可能已被刪除），分析時會視為沒有連結。"); return; }
  const text = ev.text.length > 90 ? ev.text.slice(0, 90) + "…" : ev.text;
  box.append("規範依據：", el("b", {}, ev.ref), "　", el("q", {}, text));
}

function renderRules() {
  const box = clear($("#rb-rules"));
  if (!S.rules.length) box.append(el("p", { class: "hint" }, "還沒有規則。從下方挑一個句型，或把規範段落拖到「新增規則」。"));
  S.rules.forEach((r, idx) => {
    const evId = r.evidence_id;
    const evLine = el("div", { class: "rb-evi" });
    if (evId) fillEvidenceLine(evLine, evId); else evLine.append("尚未連結規範依據（結果會標示為「推算」）。可把規範段落拖到這裡。");
    const card = el("div", { class: `rb-card${r.enabled === false ? " off" : ""}`, dataset: { ruleId: r.id } },
      el("div", { class: "rb-row" },
        el("input", { type: "checkbox", checked: r.enabled !== false, "aria-label": `啟用規則 ${sentence(r)}`, onchange: (e) => {
          r.enabled = e.target.checked; dirty(); card.classList.toggle("off", !r.enabled); } }),
        el("span", { class: "rb-sentence" }, sentence(r)),
        el("select", { "aria-label": "不符合時的結果", onchange: (e) => { r.severity = e.target.value; dirty(); } },
          el("option", { value: "FAIL", selected: r.severity === "FAIL" }, "不符合＝不合格"),
          el("option", { value: "WARNING", selected: r.severity === "WARNING" }, "不符合＝警告")),
        r.builder ? el("button", { type: "button", class: "btn small", onclick: () => editRule(idx) }, "編輯") : null,
        el("button", { type: "button", class: "btn small danger", "aria-label": `刪除規則 ${sentence(r)}`, onclick: () => {
          S.rules.splice(idx, 1); dirty(); renderRules(); } }, "刪除")),
      evLine);
    wireDrop(card, (id) => { r.evidence_id = id; delete r.evidence_ref; dirty(); renderRules(); });
    if (evId) {
      evLine.append(" ", el("button", { type: "button", class: "btn small", onclick: () => {
        delete r.evidence_id; dirty(); renderRules(); } }, "移除連結"));
    }
    box.append(card);
  });
}

function wireDrop(node, onId) {
  node.addEventListener("dragover", (e) => {
    if ([...e.dataTransfer.types].includes(EVIDENCE_MIME)) { e.preventDefault(); node.classList.add("over"); }
  });
  node.addEventListener("dragleave", () => node.classList.remove("over"));
  node.addEventListener("drop", (e) => {
    node.classList.remove("over");
    const raw = e.dataTransfer.getData(EVIDENCE_MIME);
    if (!raw) return;
    e.preventDefault();
    const para = JSON.parse(raw);
    onId(para.id, para);
  });
}

// ---- new / edit rule form ------------------------------------------------------------------------------------------
function selectorValue(sel) {
  if (!sel) return "";
  const [k, v] = Object.entries(sel)[0];
  return `${k}:${Array.isArray(v) ? v[0] : v}`;
}
function selectorFrom(text) {
  if (!text) return null;
  const i = text.indexOf(":");
  return { [text.slice(0, i)]: text.slice(i + 1) };
}

function selectorSelect(key) {
  const f = S.form;
  const cur = selectorValue(f.params[key]);
  const systemNames = S.systems.map((s) => s.name).filter(Boolean);
  const sel = el("select", { "aria-label": BLANK_LABEL[key], dataset: { blank: key }, onchange: (e) => {
    f.params[key] = selectorFrom(e.target.value); } },
    el("option", { value: "" }, "請選擇…"),
    systemNames.length ? el("optgroup", { label: "系統" }, systemNames.map((n) => el("option", { value: `system:${n}`, selected: cur === `system:${n}` }, n))) : null,
    S.layers.length ? el("optgroup", { label: "單一圖層" }, S.layers.map((n) => el("option", { value: `layer:${n}`, selected: cur === `layer:${n}` }, n))) : null);
  return sel;
}

function renderNewSelectors() {
  const box = $("#rb-blanks");
  if (box) renderBlanks(box);
}

function renderBlanks(box) {
  const f = S.form, t = template(f.templateId);
  clear(box);
  for (const key of ["subject", "target", "zone"]) {
    if (t.needs.includes(key)) box.append(el("label", {}, BLANK_LABEL[key], selectorSelect(key)));
  }
  if (t.needs.includes("value")) {
    const unitLess = !t.needs.includes("unit");
    box.append(el("label", {}, unitLess ? (t.id === "orthogonal" ? "最大偏差（度）" : "數量") : "數值",
      el("input", { type: "number", step: "any", min: "0", dataset: { blank: "value" }, "aria-label": "數值",
        value: f.params.value ?? t.default_value ?? "", oninput: (e) => {
          f.params.value = e.target.value === "" ? undefined : Number(e.target.value); } })));
  }
  if (t.needs.includes("unit")) {
    box.append(el("label", {}, "單位", el("select", { dataset: { blank: "unit" }, "aria-label": "單位", onchange: (e) => { f.params.unit = e.target.value; } },
      UNITS.map(([v, label]) => el("option", { value: v, selected: (f.params.unit ?? t.default_unit) === v }, label)))));
  }
  if (t.optional.includes("warn_margin")) {
    box.append(el("label", {}, "警示範圍（選填，同單位）",
      el("input", { type: "number", step: "any", min: "0", dataset: { blank: "warn_margin" }, "aria-label": "警示範圍",
        value: f.params.warn_margin ?? "", oninput: (e) => {
          f.params.warn_margin = e.target.value === "" ? undefined : Number(e.target.value); } })));
  }
  box.append(el("label", {}, "不符合時的結果", el("select", { dataset: { blank: "severity" }, "aria-label": "不符合時的結果", onchange: (e) => { f.params.severity = e.target.value; } },
    el("option", { value: "FAIL", selected: (f.params.severity ?? "FAIL") === "FAIL" }, "不合格"),
    el("option", { value: "WARNING", selected: f.params.severity === "WARNING" }, "警告"))));
  box.append(el("label", {}, "規則名稱（選填）", el("input", { type: "text", maxlength: 100, dataset: { blank: "name" }, "aria-label": "規則名稱",
    placeholder: "不填會自動用句子當名稱", value: f.params.name ?? "", oninput: (e) => { f.params.name = e.target.value; } })));
}

function renderNew() {
  const box = clear($("#rb-new")), f = S.form, t = template(f.templateId);
  const blanks = el("div", { class: "rb-blanks", id: "rb-blanks" });
  const evLine = el("div", { class: "rb-evi", id: "rb-new-evi" });
  if (f.evidenceId) {
    fillEvidenceLine(evLine, f.evidenceId);
    evLine.append(" ", el("button", { type: "button", class: "btn small", onclick: () => { f.evidenceId = null; renderNew(); } }, "移除連結"));
  } else evLine.append("規範依據：把右邊的規範段落拖到這個框，會自動帶入數值（也可以不連結）。");
  box.append(
    el("h4", {}, f.editingId ? "編輯規則" : "新增規則"),
    el("div", { class: "rb-row" },
      el("label", { for: "rb-template" }, "句型"),
      el("select", { id: "rb-template", class: "grow", onchange: (e) => {
        f.templateId = e.target.value; f.params = {}; f.errors = []; renderNew(); } },
        S.templates.map((x) => el("option", { value: x.id, selected: x.id === f.templateId }, x.name)))),
    el("p", { class: "hint" }, "句型：" + t.sentence.replace(/\{(\w+)\}/g, (_, k) => ({ subject: "○○", target: "△△", zone: "□□", value: "N", unit: "單位" }[k] ?? k))),
    blanks, evLine,
    el("div", { class: "rb-row" },
      el("button", { type: "button", class: "btn primary", id: "rb-add-rule", onclick: submitRule }, f.editingId ? "更新規則" : "加入規則"),
      f.editingId ? el("button", { type: "button", class: "btn", onclick: () => { S.form = newForm(f.templateId); renderNew(); } }, "取消編輯") : null),
    el("div", { class: "errors", id: "rb-form-errors", role: "alert" }, f.errors.join("\n")));
  renderBlanks(blanks);
  wireDrop($("#rb-new"), (id, para) => useParagraph(para));
}

function editRule(idx) {
  const r = S.rules[idx];
  S.form = { templateId: r.builder.template_id, params: structuredClone(r.builder.params), editingId: r.id,
             evidenceId: r.evidence_id ?? null, errors: [] };
  renderNew();
  $("#rb-new").scrollIntoView({ block: "center" });
}

async function submitRule() {
  const f = S.form;
  const params = { ...f.params };
  if (f.evidenceId) params.evidence_id = f.evidenceId;
  if (f.editingId) params.id = f.editingId;
  const taken = S.rules.filter((r) => r.id !== f.editingId).map((r) => r.id);
  try {
    const out = await api.post(`/api/projects/${S.project.id}/rules/build`, { template_id: f.templateId, params, taken_ids: taken });
    const rule = out.rule;
    const old = S.rules.findIndex((r) => r.id === f.editingId);
    if (old >= 0) {
      rule.enabled = S.rules[old].enabled;
      S.rules[old] = rule;
    } else S.rules.push(rule);
    dirty();
    S.form = newForm(f.templateId);
    renderRules(); renderNew();
  } catch (e) {
    if (!(e instanceof ApiError)) throw e;
    f.errors = e.extra?.errors?.length ? e.extra.errors : [e.message];
    $("#rb-form-errors").textContent = f.errors.join("\n");
  }
}

// ---- specification paragraphs -----------------------------------------------------------------------------------------
async function loadSpec() {
  const box = $("#rb-paras");
  if (!box) return;
  const q = S.query ? `&q=${encodeURIComponent(S.query)}` : "";
  const data = await api.get(`/api/projects/${S.project.id}/evidence?limit=100${q}`);
  clear(box);
  if (!data.items.length) {
    box.append(el("p", { class: "hint" }, S.query ? "找不到符合的段落。" : "還沒有規範。請先把規範檔拖到左側「規範」區。"));
    return;
  }
  if (data.total > data.items.length) box.append(el("p", { class: "hint" }, `共 ${data.total} 段，先顯示 ${data.items.length} 段；可用搜尋縮小範圍。`));
  for (const p of data.items) box.append(paragraph(p));
}

function highlight(text, suggest) {
  const needles = [...new Set(suggest.map((s) => s.text))].filter(Boolean);
  if (!needles.length) return [text];
  const rx = new RegExp(`(${needles.map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "g");
  return text.split(rx).map((part, i) => (i % 2 ? el("mark", {}, part) : part));
}

function paragraph(p) {
  const node = el("div", { class: "para", draggable: "true", dataset: { evidenceId: p.id } },
    el("div", { class: "ref" }, p.ref + (p.section ? `　${p.section}` : "")),
    el("div", { class: "txt" }, ...highlight(p.text, p.suggest)),
    el("div", { class: "acts" },
      el("button", { type: "button", class: "btn small", onclick: () => useParagraph(p) }, "用這段建立規則"),
      el("select", { "aria-label": "連結到既有規則", onchange: (e) => {
        const r = S.rules.find((x) => x.id === e.target.value);
        if (r) { r.evidence_id = p.id; delete r.evidence_ref; dirty(); renderRules(); }
        e.target.value = "";
      } }, el("option", { value: "" }, "連結到規則…"), S.rules.map((r) => el("option", { value: r.id }, sentence(r).slice(0, 40))))));
  node.addEventListener("dragstart", (e) => {
    e.dataTransfer.setData(EVIDENCE_MIME, JSON.stringify({ id: p.id, suggest: p.suggest }));
    e.dataTransfer.setData("text/plain", p.text);
    e.dataTransfer.effectAllowed = "copy";
  });
  return node;
}

function useParagraph(p) {
  const f = S.form, t = template(f.templateId);
  f.evidenceId = p.id;
  const first = p.suggest.find((s) => s.unit) ?? p.suggest[0];
  if (first && t.needs.includes("value") && f.params.value === undefined) {
    f.params.value = first.value;
    if (t.needs.includes("unit") && first.unit && ["mm", "cm", "m", "ft", "in"].includes(first.unit)) f.params.unit = first.unit;
  }
  renderNew();
  $("#rb-new").scrollIntoView({ block: "center" });
}

// ---- import / save --------------------------------------------------------------------------------------------------------
async function importJson(e) {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  let data;
  try { data = JSON.parse(await file.text()); } catch { toast("這個檔案不是有效的規則檔（JSON）。", "error"); return; }
  const check = await api.post(`/api/projects/${S.project.id}/rules/validate`, data);
  if (!check.valid) { toast("規則檔有問題：\n" + check.errors.slice(0, 5).join("\n"), "error"); return; }
  const rs = data.ruleset ?? data;
  S.systems = (rs.systems ?? []).map((s) => {
    const list = regexToLayers(s.layer_regex);
    return { name: s.name, layers: list ?? [], advanced: list === null, regex: s.layer_regex };
  });
  S.rules = rs.rules ?? [];
  dirty(); renderSystems(); renderRules(); renderNew();
  toast(`已讀入 ${S.rules.length} 條規則。按「儲存規則」才會生效。`);
}

function collect() {
  const systems = S.systems.filter((s) => s.name.trim() || s.layers.length).map((s) => ({
    name: s.name.trim(), layer_regex: s.advanced ? s.regex : layersToRegex(s.layers) }));
  return { systems, rules: S.rules };
}

async function save() {
  const err = $("#rb-errors");
  err.textContent = "";
  const empty = S.systems.find((s) => !s.advanced && s.name.trim() && !s.layers.length);
  if (empty) { err.textContent = `系統「${empty.name}」還沒有選任何圖層。`; return; }
  const unnamed = S.systems.find((s) => !s.name.trim() && s.layers.length);
  if (unnamed) { err.textContent = "有系統還沒有填名稱。"; return; }
  try {
    await api.put(`/api/projects/${S.project.id}/rules`, collect());
  } catch (e) {
    if (!(e instanceof ApiError)) throw e;
    err.textContent = [e.message, ...(e.extra?.errors ?? [])].join("\n");
    return;
  }
  S.dirty = false;
  $("#rules-dialog").close();
  toast("規則已儲存。");
  if (S.onSaved) S.onSaved();
}

