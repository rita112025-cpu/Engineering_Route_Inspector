"""JSON API (all routes under /api). Handlers are plain ``def`` functions, which Starlette runs on a
thread pool, so blocking database and file work never stalls the event loop; each thread has its own
SQLite connection (``AppState.repo``). Nothing here accepts a file-system path: files are addressed by
generated IDs and resolved through ``Storage``.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from core.analysis.pipeline import STAGES
from core.evidence.chunks import search as search_chunks
from core.rules.regexcheck import check_ruleset
from core.rules.schema import RuleError, normalize_ruleset
from core.version import SOFTWARE_VERSION
from exporters import service as export_service
from exporters.report import evidence_ref_text, measured_text, required_text
from importers import docx as docx_importer
from importers import pdf as pdf_importer
from importers import text as text_importer
from importers.dxf import DrawingImportError, load_dxf
from persistence.db import schema_version
from persistence.repo import RunInProgress
from persistence.storage import StorageError, check_id, new_id, safe_filename
from . import diagnostics, templates
from .errors import ApiError, bad_request, not_found
from .state import AppState

STAGE_LABEL = dict(STAGES) | {"queued": "排隊中", "completed": "完成", "failed": "失敗", "cancelled": "已取消"}
DOC_KINDS = {".txt": "txt", ".md": "md", ".markdown": "md", ".pdf": "pdf", ".docx": "docx"}
MAX_RULES = 2000
MAX_NAME = 100
_CTRL = re.compile(r"[\x00-\x1f\x7f‪-‮⁦-⁩]")


def S(request: Request) -> AppState:
    return request.app.state.eri


def ident(request: Request, key: str) -> str:
    try:
        return check_id(request.path_params[key])
    except StorageError:
        raise not_found("資料") from None


def project_of(request: Request) -> dict:
    p = S(request).repo().get_project(ident(request, "pid"))
    if p is None:
        raise not_found("專案")
    return p


def run_of(request: Request) -> dict:
    r = S(request).repo().get_run(ident(request, "rid"))
    if r is None:
        raise not_found("分析結果")
    return r


def _no_constant(name: str):
    raise ValueError(f"不允許的數值 {name}")


async def json_body(request: Request) -> Any:
    """Parsed JSON body. NaN / Infinity (accepted by Python's json) are refused: they would poison stored rules."""
    try:
        return json.loads(await request.body(), parse_constant=_no_constant)
    except (ValueError, UnicodeDecodeError):
        raise bad_request("送出的資料不是有效的 JSON。", "BAD_JSON") from None


def clean_name(value: Any, label: str = "名稱") -> str:
    if not isinstance(value, str):
        raise bad_request(f"請輸入{label}。")
    name = _CTRL.sub("", value).strip()
    if not name:
        raise bad_request(f"請輸入{label}。")
    if len(name) > MAX_NAME:
        raise bad_request(f"{label}最多 {MAX_NAME} 個字。")
    return name


def run_view(run: dict) -> dict:
    out = {k: run.get(k) for k in ("id", "project_id", "drawing_id", "status", "stage", "stage_note", "progress",
                                    "cancel_requested", "created_at", "started_at", "finished_at", "error_code",
                                    "error_message", "baseline_run_id", "summary", "logical_name")
           if k in run}
    out["stage_label"] = STAGE_LABEL.get(run.get("stage") or run["status"], run.get("stage") or "")
    return out


def issue_row(i: dict, detail: bool = False) -> dict:
    out = {"issue_id": i["issue_id"], "status": i["status"], "severity": i["severity"],
           "confidence": i["confidence"], "rule_id": i["rule_id"], "rule_name": i["rule_name"],
           "measurement": i["measurement"], "layer": i["layer"], "system": i["system"], "handles": i["handles"],
           "location": i["location"], "measured": i["measured"], "measured_text": measured_text(i),
           "required_text": required_text(i), "unit": i["unit"], "evidence_id": i["evidence_id"],
           "message": i["message"], "baseline_state": i["baseline_state"]}
    if detail:
        out.update(reason=i["reason"], fix=i["fix"], details=i["details"], required_op=i["required_op"],
                   required_value=i["required_value"], seq=i["seq"])
    return out


# ---- health -----------------------------------------------------------------------------------------

def health(request: Request):
    st = S(request)
    return JSONResponse({"status": "ok", "version": SOFTWARE_VERSION,
                         "schema_version": schema_version(st.repo().conn),
                         "job_manager": st.manager.status()["alive"]})


# ---- diagnostics -----------------------------------------------------------------------------------

def _diagnose(request: Request) -> dict:
    st = S(request)
    return diagnostics.collect(st.config.data_dir, conn=st.db.conn(), manager_status=st.manager.status(),
                               recovery=st.recovery, started_at=st.started_at)


def get_diagnostics(request: Request):
    return JSONResponse(_diagnose(request))


def diagnostics_bundle(request: Request):
    st = S(request)
    data = diagnostics.build_bundle(st.config.data_dir, _diagnose(request), st.db.conn())
    name = f"eri-diagnostics-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    return Response(data, media_type="application/zip", headers={"Content-Disposition": _attachment(name)})


# ---- demo -------------------------------------------------------------------------------------------

def load_demo_project(request: Request):
    from . import demo
    return JSONResponse(demo.load_demo(S(request)))


# ---- projects ---------------------------------------------------------------------------------------

def list_projects(request: Request):
    return JSONResponse({"projects": S(request).repo().list_projects()})


async def create_project(request: Request):
    body = await json_body(request)
    name = clean_name(body.get("name") if isinstance(body, dict) else None, "專案名稱")
    return JSONResponse(await run_in_threadpool(_create_project, S(request), name), status_code=201)


def _create_project(st: AppState, name: str) -> dict:
    p = st.repo().create_project(name)
    st.storage.project_dir(p["id"], create=True)
    return p


def get_project(request: Request):
    return JSONResponse(project_of(request))


async def rename_project(request: Request):
    body = await json_body(request)
    name = clean_name(body.get("name") if isinstance(body, dict) else None, "專案名稱")

    def work():
        p = project_of(request)
        S(request).repo().rename_project(p["id"], name)
        return S(request).repo().get_project(p["id"])
    return JSONResponse(await run_in_threadpool(work))


def delete_project(request: Request):
    p = project_of(request)
    st, repo = S(request), S(request).repo()
    if any(r["project_id"] == p["id"] for r in repo.active_runs()):
        raise ApiError(409, "RUN_IN_PROGRESS", "這個專案還有分析進行中，請先取消分析再刪除。")
    repo.delete_project(p["id"])
    st.storage.delete_project_files(p["id"])
    return JSONResponse({"deleted": p["id"]})


# ---- drawings -----------------------------------------------------------------------------------------

def _drawing_of(request: Request) -> dict:
    d = S(request).repo().get_drawing(ident(request, "did"))
    if d is None:
        raise not_found("圖面")
    return d


def _import_drawing(st: AppState, pid: str, filename: str, fileobj) -> dict:
    name = safe_filename(filename or "", "drawing.dxf")
    lower = name.lower()
    if lower.endswith(".dwg"):
        raise bad_request("目前只支援 DXF 檔。請在 CAD 軟體中把 DWG 另存為 DXF 後再匯入。", "DWG_UNSUPPORTED")
    if not lower.endswith(".dxf"):
        raise bad_request("請選擇 .dxf 圖面檔。", "NOT_DXF")
    max_bytes = st.config.max_drawing_mb * 1024 * 1024
    stored = st.storage.save_stream(pid, "drawings", name, fileobj, max_bytes)
    repo = st.repo()
    existing = repo.drawing_by_stored_name(pid, stored.stored_name)
    if existing:
        existing["already_imported"] = True
        return existing
    with open(stored.path, "rb") as f:
        if f.read(4) == b"AC10":
            stored.path.unlink(missing_ok=True)
            raise bad_request("這是 DWG 檔（副檔名被改成 .dxf）。請在 CAD 軟體中另存為 DXF。", "DWG_UNSUPPORTED")
    try:
        imp = load_dxf(stored.path, name)
        counts = Counter(e.layer for e in imp.entities)
        info = {"layers": [{"name": k, "count": counts[k]} for k in sorted(counts)], "type_counts": imp.type_counts,
                "skipped": imp.skipped, "warnings": imp.warnings, "dxf_version": imp.dxf_version,
                "parse_seconds": round(imp.parse_seconds, 3), "units_code": imp.units_code}
        return repo.add_drawing(pid, name, stored.stored_name, stored.sha256, stored.size, imp.entities,
                                unit_to_mm=imp.unit_to_mm, units_assumed=imp.units_assumed, info=info)
    except BaseException:
        # whatever went wrong, do not leave a file nobody refers to (but never one a stored drawing refers to:
        # an identical upload may have committed its row while this one failed)
        if repo.drawing_by_stored_name(pid, stored.stored_name) is None:
            stored.path.unlink(missing_ok=True)
        raise


async def upload_drawing(request: Request):
    pid = project_of(request)["id"]
    async with request.form(max_files=1, max_fields=4) as form:
        up = form.get("file")
        if up is None or not hasattr(up, "file"):
            raise bad_request("請選擇要匯入的圖面檔。", "NO_FILE")
        d = await run_in_threadpool(_import_drawing, S(request), pid, up.filename, up.file)
    return JSONResponse(d, status_code=200 if d.get("already_imported") else 201)


def list_drawings(request: Request):
    return JSONResponse({"drawings": S(request).repo().list_drawings(project_of(request)["id"])})


def delete_drawing(request: Request):
    d = _drawing_of(request)
    st, repo = S(request), S(request).repo()
    if any(r["drawing_id"] == d["id"] for r in repo.active_runs()):
        raise ApiError(409, "RUN_IN_PROGRESS", "這份圖面還有分析進行中，請先取消分析。")
    repo.delete_drawing(d["id"])
    try:
        st.storage.file_path(d["project_id"], "drawings", d["stored_name"]).unlink(missing_ok=True)
    except StorageError:
        pass
    return JSONResponse({"deleted": d["id"]})


def drawing_geometry(request: Request):
    d = _drawing_of(request)
    repo = S(request).repo()
    return JSONResponse({"drawing_id": d["id"], "logical_name": d["logical_name"], "extent": repo.drawing_extent(d["id"]),
                         "layers": d["info"].get("layers", []),
                         "entities": [{"h": e["handle"], "l": e["layer"], "t": e["type"], "g": e["g"]}
                                      for e in repo.entity_rows(d["id"])]})


def drawing_entities(request: Request):
    d = _drawing_of(request)
    handles = [h for h in request.query_params.get("handles", "").split(",") if h][:100]
    return JSONResponse({"entities": S(request).repo().entities_by_handles(d["id"], handles)})


# ---- documents / evidence ---------------------------------------------------------------------------------

def _import_document(st: AppState, pid: str, filename: str, fileobj) -> dict:
    name = safe_filename(filename or "", "document.txt")
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in DOC_KINDS:
        raise bad_request("規範檔只支援 PDF、Word（.docx）、TXT、Markdown（.md）。", "DOC_UNSUPPORTED")
    kind = DOC_KINDS[ext]
    stored = st.storage.save_stream(pid, "documents", name, fileobj, st.config.max_document_mb * 1024 * 1024)
    repo = st.repo()
    existing = repo.document_by_stored_name(pid, stored.stored_name)
    if existing:
        existing["already_imported"] = True
        return existing
    doc_id = new_id("doc")
    warnings: list[str] = []
    try:
        if kind == "pdf":
            chunks, warnings = pdf_importer.load_pdf(stored.path, doc_id, stored.sha256, name)
        elif kind == "docx":
            chunks = docx_importer.load_docx(stored.path, doc_id, stored.sha256, name)
        else:
            chunks = text_importer.load_text(stored.path, doc_id, stored.sha256, name)
    except text_importer.DocumentImportError:
        stored.path.unlink(missing_ok=True)
        raise
    if not chunks:
        warnings.append("這份文件沒有讀到任何文字，無法作為規範證據。")
    return repo.add_document(pid, name, stored.stored_name, kind, stored.sha256, stored.size, chunks, warnings,
                             document_id=doc_id)


async def upload_document(request: Request):
    pid = project_of(request)["id"]
    async with request.form(max_files=1, max_fields=4) as form:
        up = form.get("file")
        if up is None or not hasattr(up, "file"):
            raise bad_request("請選擇要匯入的規範檔。", "NO_FILE")
        d = await run_in_threadpool(_import_document, S(request), pid, up.filename, up.file)
    return JSONResponse(d, status_code=200 if d.get("already_imported") else 201)


def list_documents(request: Request):
    return JSONResponse({"documents": S(request).repo().list_documents(project_of(request)["id"])})


def delete_document(request: Request):
    st, repo = S(request), S(request).repo()
    d = repo.get_document(ident(request, "doc"))
    if d is None:
        raise not_found("規範文件")
    repo.delete_document(d["id"])
    try:
        st.storage.file_path(d["project_id"], "documents", d["stored_name"]).unlink(missing_ok=True)
    except StorageError:
        pass
    return JSONResponse({"deleted": d["id"]})


def _chunk_view(c, full: bool = False) -> dict:
    d = c if isinstance(c, dict) else c.to_dict()
    text = d["text"]
    out = {"id": d["id"], "filename": d["filename"], "page": d["page"], "line_start": d["line_start"],
           "line_end": d["line_end"], "section": d["section"], "ref": evidence_ref_text(d),
           "text": text if full or len(text) <= 600 else text[:600] + "…",
           "suggest": templates.suggest_values(text)}
    return out


def list_evidence(request: Request):
    pid = project_of(request)["id"]
    q = (request.query_params.get("q") or "").strip()
    doc = request.query_params.get("document_id")
    chunks = S(request).repo().load_evidence(pid)
    if doc:
        chunks = [c for c in chunks if c.document_id == doc]
    chunks = search_chunks(chunks, q, limit=10**9) if q else chunks
    try:
        offset = max(0, int(request.query_params.get("offset", 0)))
        limit = max(1, min(int(request.query_params.get("limit", 100)), 500))
    except ValueError:
        raise bad_request("offset / limit 必須是整數。") from None
    return JSONResponse({"total": len(chunks), "items": [_chunk_view(c) for c in chunks[offset:offset + limit]]})


def get_evidence(request: Request):
    pid = project_of(request)["id"]
    row = S(request).repo().get_evidence(pid, request.path_params["eid"])
    if row is None:
        raise not_found("規範段落")
    return JSONResponse(_chunk_view(row, full=True))


# ---- rules --------------------------------------------------------------------------------------------------

def rule_templates(request: Request):
    return JSONResponse({"templates": templates.public_templates()})


def get_rules(request: Request):
    rs = S(request).repo().get_ruleset(project_of(request)["id"])
    return JSONResponse({"ruleset": rs, "enabled": sum(1 for r in rs["rules"] if r.get("enabled", True))})


async def put_rules(request: Request):
    body = await json_body(request)
    if isinstance(body, dict) and "ruleset" in body and "rules" not in body:
        body = body["ruleset"]
    if not isinstance(body, dict):
        raise bad_request("規則檔必須是 JSON 物件。", "BAD_JSON")
    if not isinstance(body.get("rules", []), list):
        raise bad_request("rules 必須是清單。", "RULES_INVALID", errors=["rules 必須是清單"])
    if len(body.get("rules", [])) > MAX_RULES:
        raise bad_request(f"規則最多 {MAX_RULES} 條。")

    def work():
        pid = project_of(request)["id"]
        norm = normalize_ruleset(body)
        slow = check_ruleset(norm)
        if slow:
            raise RuleError(slow)
        S(request).repo().save_ruleset(pid, norm)
        return norm
    norm = await run_in_threadpool(work)
    return JSONResponse({"ruleset": norm, "enabled": sum(1 for r in norm["rules"] if r.get("enabled", True))})


async def validate_rules(request: Request):
    body = await json_body(request)
    if isinstance(body, dict) and "ruleset" in body and "rules" not in body:
        body = body["ruleset"]
    try:
        norm = normalize_ruleset(body)
        slow = await run_in_threadpool(check_ruleset, norm)
        if slow:
            raise RuleError(slow)
    except RuleError as exc:
        return JSONResponse({"valid": False, "errors": exc.errors})
    return JSONResponse({"valid": True, "errors": [], "rules": len(norm["rules"])})


async def build_rule(request: Request):
    body = await json_body(request)
    if not isinstance(body, dict):
        raise bad_request("資料格式錯誤。", "BAD_JSON")

    def work():
        rs = S(request).repo().get_ruleset(project_of(request)["id"])
        extra = body.get("taken_ids")
        taken = {r["id"] for r in rs["rules"]} | {x for x in (extra if isinstance(extra, list) else [])
                                                  if isinstance(x, str)}
        return templates.build_rule(str(body.get("template_id")), body.get("params") or {}, taken)
    return JSONResponse({"rule": await run_in_threadpool(work)})


def export_rules(request: Request):
    p = project_of(request)
    rs = S(request).repo().get_ruleset(p["id"])
    data = json.dumps(rs, ensure_ascii=False, indent=2)
    name = safe_filename(f"{p['name']}_rules.json", "rules.json")
    return Response(data.encode("utf-8"), media_type="application/json; charset=utf-8",
                    headers={"Content-Disposition": _attachment(name)})


def _attachment(name: str) -> str:
    from urllib.parse import quote
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


# ---- runs ----------------------------------------------------------------------------------------------------

async def start_run(request: Request):
    body = await json_body(request)
    if not isinstance(body, dict):
        raise bad_request("資料格式錯誤。", "BAD_JSON")

    def work():
        st = S(request)
        repo = st.repo()
        p = project_of(request)
        did = body.get("drawing_id")
        try:
            d = repo.get_drawing(check_id(did)) if isinstance(did, str) else None
        except StorageError:
            d = None
        if d is None or d["project_id"] != p["id"]:
            raise bad_request("請先選擇要分析的圖面。", "NO_DRAWING")
        rs = repo.get_ruleset(p["id"])
        if not any(r.get("enabled", True) for r in rs["rules"]):
            raise bad_request("還沒有可用的規則。請先匯入規範並建立規則，或載入示範專案。", "NO_RULES")
        base = body.get("baseline_run_id", "auto")
        if base == "auto":
            prev = repo.latest_completed_run(p["id"], d["logical_name"])
            base = prev["id"] if prev else None
        elif base is not None and not isinstance(base, str):
            raise bad_request("基準分析格式錯誤。")
        try:
            run = repo.create_run(p["id"], d, rs, base, exclusive=True)
        except RunInProgress as busy:
            raise ApiError(409, "RUN_IN_PROGRESS", "這份圖面已經有分析進行中。", run=run_view(repo.get_run(busy.run_id))) from None
        except ValueError as exc:
            raise bad_request(str(exc)) from None
        st.manager.wake()
        return run_view(run)
    return JSONResponse(await run_in_threadpool(work), status_code=202)


def list_runs(request: Request):
    return JSONResponse({"runs": [run_view(r) for r in S(request).repo().list_runs(project_of(request)["id"])]})


def get_run(request: Request):
    return JSONResponse(run_view(run_of(request)))


def cancel_run(request: Request):
    st = S(request)
    run = run_of(request)
    if run["status"] not in ("queued", "running"):
        raise ApiError(409, "NOT_ACTIVE", "這次分析已經結束，無法取消。")
    out = st.repo().request_cancel(run["id"])
    st.manager.wake()
    return JSONResponse(run_view(out))


def list_issues(request: Request):
    run = run_of(request)
    q = request.query_params
    statuses = [s for s in q.get("status", "").split(",") if s in ("FAIL", "WARNING", "PASS", "UNKNOWN")]
    try:
        offset, limit = int(q.get("offset", 0)), int(q.get("limit", 200))
    except ValueError:
        raise bad_request("offset / limit 必須是整數。") from None
    items, total = S(request).repo().list_issues(
        run["id"], statuses=statuses, rule_id=q.get("rule") or None, search=(q.get("q") or "").strip() or None,
        baseline_state=q.get("baseline") or None, sort=q.get("sort", "severity"), offset=offset, limit=limit)
    return JSONResponse({"total": total, "counts": (run["summary"] or {}).get("counts"),
                         "items": [issue_row(i) for i in items]})


def get_issue(request: Request):
    run = run_of(request)
    repo = S(request).repo()
    i = repo.get_issue(run["id"], request.path_params["iid"])
    if i is None:
        raise not_found("問題")
    out = issue_row(i, detail=True)
    snap = (run["summary"] or {}).get("evidence_snapshot", {})
    ev = (snap.get(i["evidence_id"]) or repo.get_evidence(run["project_id"], i["evidence_id"])) \
        if i["evidence_id"] else None
    out["evidence"] = ({"id": ev["id"], "filename": ev["filename"], "page": ev["page"], "line_start": ev["line_start"],
                        "line_end": ev["line_end"], "section": ev["section"], "text": ev["text"],
                        "ref": evidence_ref_text(ev)} if ev else None)
    out["entities"] = repo.entities_by_handles(run["drawing_id"], i["handles"])
    out["run_id"], out["drawing_id"] = run["id"], run["drawing_id"]
    return JSONResponse(out)


# ---- exports -------------------------------------------------------------------------------------------------

async def create_export(request: Request):
    body = await json_body(request)
    fmt = body.get("format") if isinstance(body, dict) else None
    if fmt not in export_service.FORMATS:
        raise bad_request("請選擇匯出格式：csv、markdown、html 或 dxf。", "BAD_FORMAT")

    def work():
        st = S(request)
        run = run_of(request)
        row = export_service.export_run(st.repo(), st.storage, run["id"], fmt)
        path = export_service.export_file(st.repo(), st.storage, row["id"])[0]
        row.update(download_url=f"/api/exports/{row['id']}/download", saved_to=row["path"])   # relative to the data folder
        return row
    return JSONResponse(await run_in_threadpool(work), status_code=201)


def list_exports(request: Request):
    run = run_of(request)
    rows = S(request).repo().list_exports(run["id"])
    for r in rows:
        r["download_url"] = f"/api/exports/{r['id']}/download"
        r["name"] = r["path"].rsplit("/", 1)[-1]
    return JSONResponse({"exports": rows})


def download_export(request: Request):
    st = S(request)
    path, name, ctype = export_service.export_file(st.repo(), st.storage, ident(request, "eid"))
    return FileResponse(path, media_type=ctype, filename=name)   # always as an attachment


ROUTES = [
    Route("/api/health", health),
    Route("/api/diagnostics", get_diagnostics),
    Route("/api/diagnostics/bundle", diagnostics_bundle),
    Route("/api/demo", load_demo_project, methods=["POST"]),
    Route("/api/projects", list_projects, methods=["GET"]),
    Route("/api/projects", create_project, methods=["POST"]),
    Route("/api/projects/{pid}", get_project, methods=["GET"]),
    Route("/api/projects/{pid}", rename_project, methods=["PATCH"]),
    Route("/api/projects/{pid}", delete_project, methods=["DELETE"]),
    Route("/api/projects/{pid}/drawings", list_drawings, methods=["GET"]),
    Route("/api/projects/{pid}/drawings", upload_drawing, methods=["POST"]),
    Route("/api/drawings/{did}", delete_drawing, methods=["DELETE"]),
    Route("/api/drawings/{did}/geometry", drawing_geometry),
    Route("/api/drawings/{did}/entities", drawing_entities),
    Route("/api/projects/{pid}/documents", list_documents, methods=["GET"]),
    Route("/api/projects/{pid}/documents", upload_document, methods=["POST"]),
    Route("/api/documents/{doc}", delete_document, methods=["DELETE"]),
    Route("/api/projects/{pid}/evidence", list_evidence),
    Route("/api/projects/{pid}/evidence/{eid}", get_evidence),
    Route("/api/rule-templates", rule_templates),
    Route("/api/projects/{pid}/rules", get_rules, methods=["GET"]),
    Route("/api/projects/{pid}/rules", put_rules, methods=["PUT"]),
    Route("/api/projects/{pid}/rules/validate", validate_rules, methods=["POST"]),
    Route("/api/projects/{pid}/rules/build", build_rule, methods=["POST"]),
    Route("/api/projects/{pid}/rules/export", export_rules, methods=["GET"]),
    Route("/api/projects/{pid}/runs", list_runs, methods=["GET"]),
    Route("/api/projects/{pid}/runs", start_run, methods=["POST"]),
    Route("/api/runs/{rid}", get_run),
    Route("/api/runs/{rid}/cancel", cancel_run, methods=["POST"]),
    Route("/api/runs/{rid}/issues", list_issues),
    Route("/api/runs/{rid}/issues/{iid}", get_issue),
    Route("/api/runs/{rid}/exports", list_exports, methods=["GET"]),
    Route("/api/runs/{rid}/exports", create_export, methods=["POST"]),
    Route("/api/exports/{eid}/download", download_export),
]
