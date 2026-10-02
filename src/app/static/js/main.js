// Page logic. The flow it serves:
//   drag a drawing -> drag the specification -> start analysis -> click a red issue -> read why -> export
import { api, ApiError } from "./api.js";
import { openRuleBuilder } from "./rules.js";
import { Viewer, entitiesBox } from "./viewer.js";
import { $, BASELINE_ZH, CONFIDENCE_ZH, STATUS, ask, clear, debounce, el, fmtBytes, fmtNumber, store, toast } from "./util.js";

const DRAWING_EXT = [".dxf"];
const SPEC_EXT = [".pdf", ".docx", ".txt", ".md", ".markdown"];
const PAGE = 200;
const ACTIVE = ["queued", "running"];

const app = {
  projects: [], project: null, drawings: [], documents: [], ruleset: { systems: [], rules: [] }, drawing: null,
  run: null, filters: new Set(["FAIL", "WARNING", "UNKNOWN"]), search: "", sort: "severity",
  issues: [], total: 0, markers: [], selectedId: null, detail: null, pollTimer: null, exporting: false,
};
let viewer;

const ext = (name) => (name.includes(".") ? "." + name.split(".").pop().toLowerCase() : "");
const enabledRules = () => app.ruleset.rules.filter((r) => r.enabled !== false);
const guard = (fn) => async (...a) => {
  try { return await fn(...a); } catch (e) {
    if (e instanceof ApiError) toast(e.message + (e.extra?.errors ? "\n" + e.extra.errors.slice(0, 4).join("\n") : ""), "error");
    else { console.error(e); toast("發生未預期的錯誤：" + (e?.message ?? e), "error"); }
  }
};

// ---- projects -------------------------------------------------------------------------------------------------------
async function loadProjects(selectId) {
  let { projects } = await api.get("/api/projects");
  if (!projects.length) {
    await api.post("/api/projects", { name: "我的專案" });
    ({ projects } = await api.get("/api/projects"));
  }
  app.projects = projects;
  const wanted = selectId ?? store.get("eri.project");
  const pick = projects.find((p) => p.id === wanted) ?? projects[0];
  const sel = clear($("#project-select"));
  for (const p of projects) sel.append(el("option", { value: p.id, selected: p.id === pick.id }, p.name));
  await openProject(pick.id);
}

async function openProject(id) {
  app.project = app.projects.find((p) => p.id === id);
  store.set("eri.project", id);
  stopPolling();
  app.run = null; resetResults();
  await refreshProject();
}

async function refreshProject() {
  const pid = app.project.id;
  const [dr, docs, rules, runs] = await Promise.all([
    api.get(`/api/projects/${pid}/drawings`), api.get(`/api/projects/${pid}/documents`),
    api.get(`/api/projects/${pid}/rules`), api.get(`/api/projects/${pid}/runs`)]);
  app.drawings = dr.drawings; app.documents = docs.documents; app.ruleset = rules.ruleset; app.runs = runs.runs;
  if (!app.drawing || !app.drawings.some((d) => d.id === app.drawing.id)) app.drawing = app.drawings[0] ?? null;
  else app.drawing = app.drawings.find((d) => d.id === app.drawing.id);
  renderDrawings(); renderDocs(); renderRulesSummary();
  await showDrawing();
}

async function showDrawing() {
  resetResults();
  const empty = $("#viewer-empty");
  if (!app.drawing) { viewer.clearDrawing(); empty.hidden = false; updateRunUi(); return; }
  empty.hidden = true;
  const geo = await api.get(`/api/drawings/${app.drawing.id}/geometry`);
  viewer.setDrawing(geo);
  renderLayers();
  updateRunUi();
  // show the newest result of this drawing, or follow a run still in progress
  const runs = (app.runs ?? []).filter((r) => r.drawing_id === app.drawing.id);
  const active = runs.find((r) => ACTIVE.includes(r.status));
  if (active) { app.run = active; poll(active.id); return; }
  const done = runs.find((r) => r.status === "completed");
  if (done) { app.run = done; await loadResults(); }
}

// ---- lists -----------------------------------------------------------------------------------------------------------
function renderDrawings() {
  const ul = clear($("#drawing-list"));
  for (const d of app.drawings) {
    const cur = app.drawing && d.id === app.drawing.id;
    ul.append(el("li", { class: cur ? "current" : "" },
      el("label", {}, el("input", { type: "radio", name: "drawing", checked: cur, "aria-label": `使用圖面 ${d.logical_name}`,
        onchange: guard(async () => { app.drawing = d; renderDrawings(); await showDrawing(); }) }),
        el("span", { class: "name", title: d.logical_name }, d.logical_name)),
      el("span", { class: "meta" }, `${d.entity_count} 物件`),
      el("button", { type: "button", class: "x", "aria-label": `刪除圖面 ${d.logical_name}`, onclick: guard(() => removeDrawing(d)) }, "✕")));
  }
}

function renderDocs() {
  const ul = clear($("#doc-list"));
  for (const d of app.documents) {
    ul.append(el("li", {},
      el("span", { class: "name", title: d.filename }, d.filename),
      el("span", { class: "meta" }, `${d.chunk_count} 段`),
      el("button", { type: "button", class: "x", "aria-label": `刪除規範 ${d.filename}`, onclick: guard(() => removeDoc(d)) }, "✕")));
  }
}

function renderRulesSummary() {
  const n = app.ruleset.rules.length, on = enabledRules().length;
  $("#rules-summary-text").textContent = n ? `已建立 ${n} 條規則${on < n ? `（${on} 條啟用）` : ""}` : "尚未建立規則";
}

async function removeDrawing(d) {
  if (!(await ask({ title: `刪除圖面「${d.logical_name}」？`, text: "這份圖面的分析結果也會一起刪除。", ok: "刪除", danger: true }))) return;
  await api.del(`/api/drawings/${d.id}`);
  if (app.drawing?.id === d.id) app.drawing = null;
  await refreshProject();
}

async function removeDoc(d) {
  if (!(await ask({ title: `刪除規範「${d.filename}」？`, text: "已建立的規則不會被刪除，但它們連結到這份規範的依據會失效。", ok: "刪除", danger: true }))) return;
  await api.del(`/api/documents/${d.id}`);
  await refreshProject();
}

function renderLayers() {
  const panel = clear($("#layers-panel"));
  for (const [name, info] of viewer.layers) {
    const sw = el("span", { class: "sw" });
    sw.style.background = info.color;
    panel.append(el("label", {}, el("input", { type: "checkbox", checked: info.visible,
      onchange: (e) => viewer.setLayerVisible(name, e.target.checked) }), sw, name, el("span", { class: "cnt" }, String(info.count))));
  }
}

// ---- importing ---------------------------------------------------------------------------------------------------------
async function importFiles(files) {
  for (const file of files) {
    const e = ext(file.name);
    if (DRAWING_EXT.includes(e)) await importDrawing(file);
    else if (SPEC_EXT.includes(e)) await importDocument(file);
    else if (e === ".dwg") toast(`「${file.name}」是 DWG 檔，目前只支援 DXF。請在 CAD 軟體中另存為 DXF 後再匯入。`, "error");
    else toast(`不支援的檔案：「${file.name}」。\n圖面請用 DXF；規範可用 PDF、Word（.docx）、TXT、Markdown。`, "error");
  }
}

async function withBusy(zone, label, fn) {
  const node = $(zone);
  node.classList.add("busy");
  const note = toast(label, "ok", 120000);
  try { return await fn(); } finally { node.classList.remove("busy"); note.remove(); }
}

const importDrawing = guard(async (file) => {
  const d = await withBusy("#drop-drawing", `匯入圖面「${file.name}」…`, () =>
    api.upload(`/api/projects/${app.project.id}/drawings`, file));
  if (d.already_imported) toast(`「${d.logical_name}」已經匯入過了，直接使用既有的資料。`);
  else {
    const layers = d.info.layers.length;
    toast(`已匯入「${d.logical_name}」：${d.entity_count} 個物件、${layers} 個圖層。`);
    if (d.units_assumed) toast("這份圖面沒有宣告單位，已用毫米（mm）計算。若尺寸不對，請在 CAD 軟體設定單位後重新匯出。", "warn", 12000);
    for (const w of d.info.warnings.filter((x) => !x.includes("INSUNITS")).slice(0, 3)) toast(w, "warn", 10000);
    if (Object.keys(d.info.skipped).length) {
      toast("有些物件類型目前不會被檢查：" + Object.entries(d.info.skipped).map(([k, v]) => `${k}×${v}`).join("、"), "warn", 10000);
    }
  }
  app.drawing = d;
  await refreshProject();
});

const importDocument = guard(async (file) => {
  const d = await withBusy("#drop-spec", `匯入規範「${file.name}」…`, () =>
    api.upload(`/api/projects/${app.project.id}/documents`, file));
  if (d.already_imported) toast(`「${d.filename}」已經匯入過了。`);
  else toast(`已匯入規範「${d.filename}」：${d.chunk_count} 段文字。`);
  for (const w of d.warnings ?? []) toast(w, "warn", 12000);
  await refreshProject();
});

function wireDrop(zone, input, accept) {
  const z = $(zone), inp = $(input);
  z.addEventListener("click", () => inp.click());
  z.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); inp.click(); } });
  inp.addEventListener("change", () => { const f = [...inp.files]; inp.value = ""; importFiles(f); });
  for (const t of ["dragenter", "dragover"]) z.addEventListener(t, (e) => { e.preventDefault(); z.classList.add("over"); });
  for (const t of ["dragleave", "drop"]) z.addEventListener(t, () => z.classList.remove("over"));
  z.addEventListener("drop", (e) => { e.preventDefault(); e.stopPropagation(); importFiles([...e.dataTransfer.files]); });
}

// ---- analysis ------------------------------------------------------------------------------------------------------------
function updateRunUi() {
  const btn = $("#run-btn"), hint = $("#run-hint");
  const active = app.run && ACTIVE.includes(app.run.status);
  const rules = enabledRules().length;
  btn.disabled = !app.drawing || !rules || !!active;
  hint.textContent = !app.drawing ? "請先匯入圖面。"
    : !rules ? "還沒有規則：按上方「編輯規則…」，用句型建立規則（可以把規範段落拖進去）。"
    : active ? "分析進行中…" : `會用 ${rules} 條規則檢查「${app.drawing.logical_name}」。`;
  $("#progress-box").hidden = !active;
}

const startRun = guard(async () => {
  try {
    app.run = await api.post(`/api/projects/${app.project.id}/runs`, { drawing_id: app.drawing.id });
  } catch (e) {
    if (e instanceof ApiError && e.code === "RUN_IN_PROGRESS" && e.extra.run) app.run = e.extra.run;
    else throw e;
  }
  resetResults();
  updateRunUi(); showProgress(app.run);
  poll(app.run.id);
});

function showProgress(run) {
  $("#progress").value = Math.round((run.progress ?? 0) * 100);
  const note = run.stage_note ? `（${run.stage_note}）` : "";
  $("#progress-label").textContent = run.status === "queued" ? "排隊中…" : `${run.stage_label}${note} ${Math.round((run.progress ?? 0) * 100)}%`;
}

function stopPolling() { clearTimeout(app.pollTimer); app.pollTimer = null; }

function poll(runId) {
  stopPolling();
  const tick = async () => {
    try {
      const run = await api.get(`/api/runs/${runId}`);
      if (!app.run || app.run.id !== runId) return;
      app.run = run;
      showProgress(run); updateRunUi();
      if (ACTIVE.includes(run.status)) { app.pollTimer = setTimeout(tick, 400); return; }
      await finishRun(run);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return;
      toast(e instanceof ApiError ? e.message : String(e), "error");
    }
  };
  tick();
}

async function finishRun(run) {
  const runs = await api.get(`/api/projects/${app.project.id}/runs`);
  app.runs = runs.runs;
  updateRunUi();
  if (run.status === "completed") { await loadResults(); const c = run.summary.counts;
    toast(`分析完成：不合格 ${c.FAIL}、警告 ${c.WARNING}、無法判定 ${c.UNKNOWN}、合格 ${c.PASS}。`); }
  else if (run.status === "cancelled") toast("分析已取消。", "warn");
  else toast(`分析失敗：${run.error_message ?? "原因不明"}`, "error", 20000);
}

const cancelRun = guard(async () => { if (app.run) { await api.post(`/api/runs/${app.run.id}/cancel`); $("#progress-label").textContent = "取消中…"; } });

// ---- results -------------------------------------------------------------------------------------------------------------
function resetResults() {
  app.issues = []; app.total = 0; app.markers = []; app.selectedId = null; app.detail = null;
  clear($("#issue-list")); clear($("#chips")); $("#detail").hidden = true; $("#export-bar").hidden = true;
  $("#baseline").hidden = true; $("#results-empty").hidden = false; $("#export-status").textContent = "";
  $("#results-tools").hidden = true;
  if (viewer) { viewer.setIssues([]); viewer.select(null); }
}

async function loadResults() {
  const run = app.run;
  $("#results-empty").hidden = true;
  $("#results-tools").hidden = false;
  $("#export-bar").hidden = false;
  renderChips(run.summary.counts);
  renderBaseline(run.summary.comparison);
  const all = await api.get(`/api/runs/${run.id}/issues?status=FAIL,WARNING,UNKNOWN&limit=5000`);
  app.allNonPass = all.items;
  applyMarkers();
  await loadIssues(true);
  const first = app.issues.find((i) => i.status === "FAIL") ?? app.issues[0];
  if (first) await selectIssue(first.issue_id);
}

function applyMarkers() {
  viewer.setIssues((app.allNonPass ?? []).filter((i) => app.filters.has(i.status)));
}

function renderChips(counts) {
  const box = clear($("#chips"));
  for (const s of ["FAIL", "WARNING", "UNKNOWN", "PASS"]) {
    box.append(el("button", { type: "button", class: `chip ${s}`, "aria-pressed": String(app.filters.has(s)), dataset: { status: s },
      onclick: guard(async () => {
        if (app.filters.has(s)) app.filters.delete(s); else app.filters.add(s);
        renderChips(counts); applyMarkers(); await loadIssues(true);
      }) }, el("b", {}, String(counts[s])), STATUS[s].zh));
  }
}

function renderBaseline(cmp) {
  const p = $("#baseline");
  if (!cmp) { p.hidden = true; return; }
  p.hidden = false;
  if (!cmp.available) { p.textContent = cmp.note; return; }
  const c = cmp.counts;
  p.textContent = `與上次分析比較：新增 ${c.NEW}、已解決 ${c.RESOLVED}、有變化 ${c.CHANGED}、未變 ${c.UNCHANGED}` +
    (cmp.ruleset_changed ? "（規則已修改）" : "") + (cmp.drawing_changed ? "（圖面已修改）" : "");
}

async function loadIssues(reset) {
  const run = app.run;
  if (reset) { app.issues = []; clear($("#issue-list")); }
  const statuses = [...app.filters].join(",");
  if (!statuses) { app.total = 0; renderIssueList(); return; }
  const q = new URLSearchParams({ status: statuses, sort: app.sort, offset: String(app.issues.length), limit: String(PAGE) });
  if (app.search) q.set("q", app.search);
  const page = await api.get(`/api/runs/${run.id}/issues?${q}`);
  app.issues.push(...page.items); app.total = page.total;
  renderIssueList();
}

function renderIssueList() {
  const box = clear($("#issue-list"));
  if (!app.issues.length) {
    box.append(el("div", { class: "results-empty" }, app.filters.size ? "沒有符合條件的項目。" : "請在上方選擇要顯示的結果類型。"));
    return;
  }
  for (const i of app.issues) {
    const sub = [`Handle ${i.handles.join(", ")}`, `圖層 ${i.layer || "—"}`];
    if (i.measured_text) sub.push(`實測 ${i.measured_text}`);
    if (i.required_text) sub.push(`要求 ${i.required_text}`);
    if (i.baseline_state) sub.push(BASELINE_ZH[i.baseline_state]);
    box.append(el("div", { class: "issue", role: "option", "aria-selected": String(i.issue_id === app.selectedId), tabindex: "-1",
      dataset: { issueId: i.issue_id, status: i.status }, onclick: guard(() => selectIssue(i.issue_id)) },
      el("span", {}, el("span", { class: `badge ${i.status}` }, STATUS[i.status].zh)),
      el("div", {}, el("div", { class: "t" }, i.rule_name), el("div", { class: "sub" }, sub.join(" · ")))));
  }
  if (app.total > app.issues.length) {
    box.append(el("button", { type: "button", class: "btn more", onclick: guard(() => loadIssues(false)) },
      `顯示更多（${app.issues.length} / ${app.total}）`));
  }
}

async function selectIssue(issueId) {
  const d = await api.get(`/api/runs/${app.run.id}/issues/${issueId}`);
  app.selectedId = issueId; app.detail = d;
  for (const row of document.querySelectorAll(".issue")) {
    const on = row.dataset.issueId === issueId;
    row.setAttribute("aria-selected", String(on));
    if (on) row.scrollIntoView({ block: "nearest" });
  }
  const marker = (app.allNonPass ?? []).find((i) => i.issue_id === issueId) ?? d;
  viewer.select({ ...marker, location: d.location }, d.entities, d.details.closest_points ?? null);
  const box = entitiesBox(d.entities, d.location ? [d.location] : []);
  const ext = viewer.extent;
  if (box) viewer.fitBox(box, 0.8, true, ext ? Math.max(ext[2] - ext[0], ext[3] - ext[1]) * 0.03 : 0);
  renderDetail(d);
}

function deselect() {
  app.selectedId = null; app.detail = null; viewer.select(null);
  $("#detail").hidden = true;
  for (const row of document.querySelectorAll(".issue")) row.setAttribute("aria-selected", "false");
}

function row(label, value) {
  if (value == null || value === "" || (Array.isArray(value) && !value.length)) return null;
  return el("tr", {}, el("th", { scope: "row" }, label), el("td", {}, value));
}

function renderDetail(d) {
  const box = clear($("#detail"));
  box.hidden = false;
  const ev = d.evidence
    ? el("div", {}, `${d.evidence.ref}${d.evidence.section ? `（${d.evidence.section}）` : ""}`,
        el("blockquote", { class: "ev" }, d.evidence.text))
    : "沒有連結規範依據";
  const nearby = (d.details.nearby ?? []).slice(0, 5).map((n) =>
    `${n.system ? n.system + " " : ""}${n.entity_type} · Handle ${n.handle} · 圖層 ${n.layer} · 距離 ${fmtNumber(n.distance)}`);
  const reasons = d.details.confidence_reasons ?? [];
  const sources = (d.details.subject ?? []).concat(d.details.targets ?? [])
    .map((c) => `${c.handle}：${c.classification_source}`);
  box.append(
    el("h3", {}, el("span", { class: `badge ${d.status}` }, STATUS[d.status].zh), d.rule_name),
    el("div", { class: "sub" }, d.issue_id),
    el("table", { class: "kv" }, el("tbody", {},
      row("Handle", d.handles.join(", ")),
      row("圖層 Layer", d.layer + (d.system ? `（系統：${d.system}）` : "")),
      row("實測值 Measured", d.measured_text || "—（資料不足，無法量測）"),
      row("規則要求 Required", d.required_text || "—"),
      row("規則 Rule", `${d.rule_id}　${d.rule_name}`),
      row("規範證據 Evidence", ev),
      row("說明", d.message),
      row("判定依據", d.reason),
      row("信心", `${CONFIDENCE_ZH[d.confidence]}${reasons.length ? "：" + reasons.join("；") : ""}`),
      row("位置", d.location ? `X ${fmtNumber(d.location[0])}，Y ${fmtNumber(d.location[1])}` : null),
      row("與上次比較", d.baseline_state ? BASELINE_ZH[d.baseline_state] : null),
      row("系統判定依據", sources.join("\n")),
      row("附近物件", nearby.join("\n")))),
    d.fix ? el("div", { class: "fixbox" }, el("b", {}, "建議處理："), d.fix) : null);
  box.scrollTop = 0;
}

// ---- export ----------------------------------------------------------------------------------------------------------------
const exportAs = guard(async (fmt) => {
  if (!app.run || app.exporting) return;
  app.exporting = true;
  const buttons = document.querySelectorAll("[data-export]");
  buttons.forEach((b) => { b.disabled = true; });
  const status = $("#export-status");
  status.textContent = "匯出中…";
  try {
    const row = await api.post(`/api/runs/${app.run.id}/exports`, { format: fmt });
    clear(status);
    status.append("已儲存（資料夾內的位置）：", row.saved_to, " ", el("a", { href: row.download_url, download: row.name, id: "export-link" }, "再下載一次"));
    $("#export-link").click();
  } finally {
    app.exporting = false;
    buttons.forEach((b) => { b.disabled = false; });
  }
});

// ---- wiring ------------------------------------------------------------------------------------------------------------------
function bind() {
  $("#project-select").addEventListener("change", guard((e) => openProject(e.target.value)));
  $("#project-new").addEventListener("click", guard(async () => {
    const name = await ask({ title: "新增專案", text: "請輸入專案名稱。", input: "", ok: "建立" });
    if (!name) return;
    const p = await api.post("/api/projects", { name });
    await loadProjects(p.id);
  }));
  $("#project-rename").addEventListener("click", guard(async () => {
    const name = await ask({ title: "專案改名", input: app.project.name, ok: "儲存" });
    if (!name || name === app.project.name) return;
    await api.patch(`/api/projects/${app.project.id}`, { name });
    await loadProjects(app.project.id);
  }));
  $("#project-delete").addEventListener("click", guard(async () => {
    if (!(await ask({ title: `刪除專案「${app.project.name}」？`, text: "專案內的圖面、規範、規則與所有分析結果都會刪除，無法復原。", ok: "刪除專案", danger: true }))) return;
    await api.del(`/api/projects/${app.project.id}`);
    store.set("eri.project", "");
    await loadProjects();
  }));
  $("#demo-load").addEventListener("click", guard(async () => {
    const out = await api.post("/api/demo");
    toast(out.created ? "已載入示範專案。接著按左側的「開始分析」。" : "示範專案已經存在，已切換過去。");
    if (out.drawing_id) app.drawing = { id: out.drawing_id };
    await loadProjects(out.project.id);
  }));
  wireDrop("#drop-drawing", "#file-drawing");
  wireDrop("#drop-spec", "#file-spec");
  window.addEventListener("dragover", (e) => { if (e.dataTransfer?.types.includes("Files")) e.preventDefault(); });
  window.addEventListener("drop", (e) => { if (e.dataTransfer?.files.length) { e.preventDefault(); importFiles([...e.dataTransfer.files]); } });
  $("#rules-open").addEventListener("click", guard(() => openRuleBuilder({
    project: app.project, drawing: app.drawing, onSaved: guard(async () => { await refreshRules(); }) })));
  $("#run-btn").addEventListener("click", startRun);
  $("#cancel-btn").addEventListener("click", cancelRun);
  $("#view-fit").addEventListener("click", () => viewer.fit());
  $("#view-in").addEventListener("click", () => viewer.zoomBy(1.4));
  $("#view-out").addEventListener("click", () => viewer.zoomBy(1 / 1.4));
  $("#layers-toggle").addEventListener("click", (e) => {
    const panel = $("#layers-panel");
    panel.hidden = !panel.hidden;
    e.currentTarget.setAttribute("aria-expanded", String(!panel.hidden));
  });
  $("#issue-search").addEventListener("input", debounce(guard(async (e) => { app.search = e.target.value.trim(); if (app.run?.status === "completed") await loadIssues(true); }), 250));
  $("#issue-sort").addEventListener("change", guard(async (e) => { app.sort = e.target.value; if (app.run?.status === "completed") await loadIssues(true); }));
  document.querySelectorAll("[data-export]").forEach((b) => b.addEventListener("click", () => exportAs(b.dataset.export)));
  $("#issue-list").addEventListener("keydown", guard(async (e) => {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key) || !app.issues.length) return;
    e.preventDefault();
    const idx = app.issues.findIndex((i) => i.issue_id === app.selectedId);
    const next = e.key === "Home" ? 0 : e.key === "End" ? app.issues.length - 1
      : Math.min(app.issues.length - 1, Math.max(0, idx + (e.key === "ArrowDown" ? 1 : -1)));
    await selectIssue(app.issues[next].issue_id);
  }));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !document.querySelector("dialog[open]")) deselect(); });
}

async function refreshRules() {
  const r = await api.get(`/api/projects/${app.project.id}/rules`);
  app.ruleset = r.ruleset;
  renderRulesSummary(); updateRunUi();
}

async function init() {
  viewer = new Viewer($("#viewer"), { onPick: guard(async (issue) => { if (issue) await selectIssue(issue.issue_id); else deselect(); }) });
  window.eri = { app, viewer };       // read-only probe for the automated browser test
  bind();
  await guard(loadProjects)();
}

init();
