"""Data access for projects, drawings, documents, rules, runs and issues.

All methods take/return plain dicts (or core model objects) so callers
never see SQL. Multi-row writes run inside one transaction.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Iterable, Sequence

from core.models.entities import GeometryEntity
from core.models.results import EvidenceChunk, RuleResult
from core.rules.schema import normalize_ruleset
from core.version import SOFTWARE_VERSION
from .db import transaction, utcnow
from .storage import new_id

RUN_ACTIVE = ("queued", "running")


class RunInProgress(Exception):
    def __init__(self, run_id: str):
        super().__init__(run_id)
        self.run_id = run_id

RUN_FINAL = ("completed", "failed", "cancelled")
ISSUE_SORT = {
    "severity": "CASE status WHEN 'FAIL' THEN 0 WHEN 'WARNING' THEN 1 WHEN 'UNKNOWN' THEN 2 ELSE 3 END, rule_id, seq",
    "rule": "rule_id, seq",
    "measured": "measured IS NULL, measured, seq",
}


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


def ruleset_sha256(ruleset: dict) -> str:
    return hashlib.sha256(json.dumps(ruleset, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class Repo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def q(self, sql: str, args: Sequence = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args)]

    def one(self, sql: str, args: Sequence = ()) -> dict | None:
        return _row(self.conn.execute(sql, args).fetchone())

    # -- audit ---------------------------------------------------------------
    def audit(self, operation: str, project_id: str | None = None, inputs: Any = None, result: str = "ok") -> None:
        self.conn.execute(
            "INSERT INTO audit_log(ts, operation, project_id, input_json, result, software_version) VALUES (?,?,?,?,?,?)",
            (utcnow(), operation, project_id, _j(inputs) if inputs is not None else None, result, SOFTWARE_VERSION))

    def recent_audit(self, limit: int = 50) -> list[dict]:
        return self.q("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))

    # -- projects ------------------------------------------------------------
    def create_project(self, name: str, is_demo: bool = False, project_id: str | None = None) -> dict:
        pid = project_id or new_id("p")
        now = utcnow()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO projects(id, name, root_path, export_dir, is_demo, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)", (pid, name, f"projects/{pid}", f"projects/{pid}/exports", int(is_demo), now, now))
            self.conn.execute("INSERT INTO project_settings(project_id, systems_json) VALUES (?, '[]')", (pid,))
            self.audit("project.create", pid, {"name": name, "is_demo": is_demo})
        return self.get_project(pid)

    def get_project(self, pid: str) -> dict | None:
        return self.one("SELECT * FROM projects WHERE id = ?", (pid,))

    def list_projects(self) -> list[dict]:
        return self.q("""SELECT p.*,
              (SELECT COUNT(*) FROM drawings d WHERE d.project_id = p.id) AS drawing_count,
              (SELECT COUNT(*) FROM documents d WHERE d.project_id = p.id) AS document_count,
              (SELECT COUNT(*) FROM rules r WHERE r.project_id = p.id) AS rule_count
            FROM projects p ORDER BY p.updated_at DESC""")

    def touch_project(self, pid: str) -> None:
        self.conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (utcnow(), pid))

    def rename_project(self, pid: str, name: str) -> None:
        self.conn.execute("UPDATE projects SET name = ?, updated_at = ? WHERE id = ?", (name, utcnow(), pid))

    def delete_project(self, pid: str) -> None:
        with transaction(self.conn):
            self.conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
            self.audit("project.delete", pid)

    def demo_project(self) -> dict | None:
        return self.one("SELECT * FROM projects WHERE is_demo = 1 ORDER BY created_at LIMIT 1")

    # -- drawings ------------------------------------------------------------
    def add_drawing(self, project_id: str, logical_name: str, stored_name: str, sha256: str, size: int,
                    entities: Sequence[GeometryEntity], *, unit_to_mm: float, units_assumed: bool,
                    info: dict) -> dict:
        did = new_id("d")
        with transaction(self.conn):
            same = self.one("SELECT id FROM drawings WHERE project_id = ? AND stored_name = ?",
                            (project_id, stored_name))
            if same:      # identical file (same content and name) imported again: keep the first import
                existing = self.get_drawing(same["id"])
                existing["already_imported"] = True
                return existing
            self.conn.execute(
                "INSERT INTO drawings(id, project_id, logical_name, stored_name, sha256, size_bytes, imported_at, "
                "entity_count, unit_to_mm, units_assumed, info_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (did, project_id, logical_name, stored_name, sha256, size, utcnow(), len(entities), unit_to_mm,
                 int(units_assumed), _j(info)))
            self.conn.executemany(
                "INSERT INTO entities(drawing_id, seq, eid, handle, layer, entity_type, minx, miny, maxx, maxy, "
                "geometry_json, metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ((did, i, e.id, e.handle, e.layer, e.entity_type, *e.bbox, _j(e.geometry), _j(e.metadata))
                 for i, e in enumerate(entities)))
            self.touch_project(project_id)
            self.audit("drawing.import", project_id, {"logical_name": logical_name, "sha256": sha256,
                                                      "entities": len(entities)})
        return self.get_drawing(did)

    def drawing_by_stored_name(self, project_id: str, stored_name: str) -> dict | None:
        r = self.one("SELECT id FROM drawings WHERE project_id = ? AND stored_name = ?", (project_id, stored_name))
        return self.get_drawing(r["id"]) if r else None

    def document_by_stored_name(self, project_id: str, stored_name: str) -> dict | None:
        r = self.one("SELECT id FROM documents WHERE project_id = ? AND stored_name = ?", (project_id, stored_name))
        return self.get_document(r["id"]) if r else None

    def get_drawing(self, did: str) -> dict | None:
        d = self.one("SELECT * FROM drawings WHERE id = ?", (did,))
        if d:
            d["info"] = json.loads(d.pop("info_json") or "{}")
            d["units_assumed"] = bool(d["units_assumed"])
        return d

    def list_drawings(self, project_id: str) -> list[dict]:
        out = []
        for d in self.q("SELECT * FROM drawings WHERE project_id = ? ORDER BY imported_at DESC", (project_id,)):
            d["info"] = json.loads(d.pop("info_json") or "{}")
            d["units_assumed"] = bool(d["units_assumed"])
            out.append(d)
        return out

    def delete_drawing(self, did: str) -> None:
        with transaction(self.conn):
            d = self.one("SELECT project_id, logical_name FROM drawings WHERE id = ?", (did,))
            self.conn.execute("DELETE FROM drawings WHERE id = ?", (did,))
            if d:
                self.audit("drawing.delete", d["project_id"], {"drawing_id": did, "logical_name": d["logical_name"]})

    def load_entities(self, did: str) -> list[GeometryEntity]:
        out = []
        for r in self.conn.execute("SELECT * FROM entities WHERE drawing_id = ? ORDER BY seq", (did,)):
            out.append(GeometryEntity(
                id=r["eid"], handle=r["handle"], source_file="", layer=r["layer"], entity_type=r["entity_type"],
                geometry=json.loads(r["geometry_json"]), bbox=(r["minx"], r["miny"], r["maxx"], r["maxy"]),
                metadata=json.loads(r["metadata_json"] or "{}")))
        src = self.one("SELECT logical_name FROM drawings WHERE id = ?", (did,))
        for e in out:
            e.source_file = src["logical_name"] if src else ""
        return out

    def drawing_extent(self, did: str) -> tuple[float, float, float, float] | None:
        r = self.conn.execute("SELECT MIN(minx), MIN(miny), MAX(maxx), MAX(maxy) FROM entities WHERE drawing_id = ?",
                              (did,)).fetchone()
        return tuple(r) if r and r[0] is not None else None

    def entities_by_handles(self, did: str, handles: Sequence[str], limit: int = 100) -> list[dict]:
        """Geometry of specific entities (used to locate an issue in the viewer)."""
        handles = list(dict.fromkeys(handles))[:limit]
        if not handles:
            return []
        marks = ",".join("?" * len(handles))
        return [{"handle": r["handle"], "layer": r["layer"], "type": r["entity_type"],
                 "g": json.loads(r["geometry_json"])}
                for r in self.conn.execute(
                    f"SELECT handle, layer, entity_type, geometry_json FROM entities WHERE drawing_id = ? "
                    f"AND handle IN ({marks}) ORDER BY seq", (did, *handles))]

    def entity_rows(self, did: str) -> list[dict]:
        """Compact geometry for the viewer."""
        return [{"handle": r["handle"], "layer": r["layer"], "type": r["entity_type"],
                 "g": json.loads(r["geometry_json"])}
                for r in self.conn.execute(
                    "SELECT handle, layer, entity_type, geometry_json FROM entities WHERE drawing_id = ? ORDER BY seq",
                    (did,))]

    # -- documents / evidence -------------------------------------------------
    def add_document(self, project_id: str, filename: str, stored_name: str, kind: str, sha256: str, size: int,
                     chunks: Sequence[EvidenceChunk], warnings: list[str], document_id: str | None = None) -> dict:
        doc_id = document_id or new_id("doc")
        with transaction(self.conn):
            same = self.one("SELECT id FROM documents WHERE project_id = ? AND stored_name = ?",
                            (project_id, stored_name))
            if same:
                existing = self.get_document(same["id"])
                existing["already_imported"] = True
                return existing
            self.conn.execute(
                "INSERT INTO documents(id, project_id, filename, stored_name, kind, sha256, size_bytes, imported_at, "
                "chunk_count, warnings_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (doc_id, project_id, filename, stored_name, kind, sha256, size, utcnow(), len(chunks), _j(warnings)))
            self.conn.executemany(
                "INSERT OR IGNORE INTO evidence(id, document_id, filename, page, line_start, line_end, section, text, hash) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ((c.id, doc_id, c.filename, c.page, c.line_start, c.line_end, c.section, c.text, c.hash)
                 for c in chunks))
            self.touch_project(project_id)
            self.audit("document.import", project_id, {"filename": filename, "sha256": sha256, "chunks": len(chunks)})
        return self.get_document(doc_id)

    def get_document(self, doc_id: str) -> dict | None:
        d = self.one("SELECT * FROM documents WHERE id = ?", (doc_id,))
        if d:
            d["warnings"] = json.loads(d.pop("warnings_json") or "[]")
        return d

    def list_documents(self, project_id: str) -> list[dict]:
        out = []
        for d in self.q("SELECT * FROM documents WHERE project_id = ? ORDER BY imported_at DESC", (project_id,)):
            d["warnings"] = json.loads(d.pop("warnings_json") or "[]")
            out.append(d)
        return out

    def delete_document(self, doc_id: str) -> None:
        with transaction(self.conn):
            d = self.one("SELECT project_id, filename FROM documents WHERE id = ?", (doc_id,))
            self.conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            if d:
                self.audit("document.delete", d["project_id"], {"document_id": doc_id, "filename": d["filename"]})

    def load_evidence(self, project_id: str) -> list[EvidenceChunk]:
        rows = self.conn.execute(
            "SELECT e.* FROM evidence e JOIN documents d ON d.id = e.document_id WHERE d.project_id = ? "
            "ORDER BY d.imported_at, e.page, e.line_start", (project_id,))
        return [EvidenceChunk(id=r["id"], document_id=r["document_id"], filename=r["filename"], page=r["page"],
                              line_start=r["line_start"], line_end=r["line_end"], section=r["section"] or "",
                              text=r["text"], hash=r["hash"]) for r in rows]

    def get_evidence(self, project_id: str, evidence_id: str) -> dict | None:
        return self.one("SELECT e.* FROM evidence e JOIN documents d ON d.id = e.document_id "
                        "WHERE d.project_id = ? AND e.id = ?", (project_id, evidence_id))

    # -- rules ---------------------------------------------------------------
    def get_ruleset(self, project_id: str) -> dict:
        s = self.one("SELECT systems_json FROM project_settings WHERE project_id = ?", (project_id,))
        rules = [json.loads(r["rule_json"]) for r in self.conn.execute(
            "SELECT rule_json FROM rules WHERE project_id = ? ORDER BY position, rule_id", (project_id,))]
        return {"format": "eri-rules", "version": 1,
                "systems": json.loads(s["systems_json"]) if s else [], "rules": rules}

    def save_ruleset(self, project_id: str, ruleset: dict, operation: str = "rules.save") -> None:
        """Validate (``RuleError`` on problems) and replace the project's systems and rules."""
        ruleset = normalize_ruleset(ruleset)
        now = utcnow()
        with transaction(self.conn):
            self.conn.execute("INSERT INTO project_settings(project_id, systems_json) VALUES (?, ?) "
                              "ON CONFLICT(project_id) DO UPDATE SET systems_json = excluded.systems_json",
                              (project_id, _j(ruleset.get("systems", []))))
            self.conn.execute("DELETE FROM rules WHERE project_id = ?", (project_id,))
            self.conn.executemany(
                "INSERT INTO rules(project_id, rule_id, position, name, severity, enabled, rule_json, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ((project_id, r["id"], i, r["name"], r["severity"], int(r.get("enabled", True)), _j(r), now)
                 for i, r in enumerate(ruleset.get("rules", []))))
            self.touch_project(project_id)
            self.audit(operation, project_id, {"rules": len(ruleset.get("rules", [])),
                                               "sha256": ruleset_sha256(ruleset)})

    # -- runs ----------------------------------------------------------------
    def create_run(self, project_id: str, drawing: dict, ruleset: dict, baseline_run_id: str | None,
                   index_kind: str = "grid", exclusive: bool = False) -> dict:
        """Create a queued run. With ``exclusive`` the "no other active run on this drawing" check and the
        insert happen in one write transaction, so two simultaneous requests cannot both succeed."""
        if drawing.get("project_id") != project_id:
            raise ValueError("圖面不屬於這個專案")
        if baseline_run_id:
            base = self.get_run(baseline_run_id)
            if base is None or base["project_id"] != project_id:
                raise ValueError("基準分析不屬於這個專案")
        rid = new_id("run")
        with transaction(self.conn):
            if exclusive:
                busy = self.one("SELECT id FROM runs WHERE drawing_id = ? AND status IN ('queued','running') LIMIT 1",
                                (drawing["id"],))
                if busy:
                    raise RunInProgress(busy["id"])
            self.conn.execute(
                "INSERT INTO runs(id, project_id, drawing_id, status, stage, progress, created_at, software_version, "
                "drawing_sha256, ruleset_json, ruleset_sha256, baseline_run_id, index_kind) "
                "VALUES (?,?,?,'queued','queued',0,?,?,?,?,?,?,?)",
                (rid, project_id, drawing["id"], utcnow(), SOFTWARE_VERSION, drawing["sha256"], _j(ruleset),
                 ruleset_sha256(ruleset), baseline_run_id, index_kind))
            self.audit("run.create", project_id, {"run_id": rid, "drawing_id": drawing["id"],
                                                  "baseline_run_id": baseline_run_id})
        return self.get_run(rid)

    def get_run(self, rid: str) -> dict | None:
        r = self.one("SELECT * FROM runs WHERE id = ?", (rid,))
        if r:
            r["summary"] = json.loads(r.pop("summary_json") or "null")
            r["cancel_requested"] = bool(r["cancel_requested"])
        return r

    def run_ruleset(self, rid: str) -> dict:
        r = self.one("SELECT ruleset_json FROM runs WHERE id = ?", (rid,))
        return json.loads(r["ruleset_json"]) if r and r["ruleset_json"] else {"systems": [], "rules": []}

    def list_runs(self, project_id: str, limit: int = 50) -> list[dict]:
        out = []
        for r in self.q("SELECT r.id, r.project_id, r.drawing_id, r.status, r.stage, r.progress, r.created_at, "
                        "r.started_at, r.finished_at, r.error_code, r.error_message, r.baseline_run_id, r.summary_json, "
                        "d.logical_name FROM runs r LEFT JOIN drawings d ON d.id = r.drawing_id "
                        "WHERE r.project_id = ? ORDER BY r.created_at DESC LIMIT ?", (project_id, limit)):
            r["summary"] = json.loads(r.pop("summary_json") or "null")
            out.append(r)
        return out

    def active_runs(self) -> list[dict]:
        return self.q("SELECT * FROM runs WHERE status IN ('queued','running') ORDER BY created_at")

    def latest_completed_run(self, project_id: str, logical_name: str, exclude: str | None = None) -> dict | None:
        """Most recent completed run on a drawing with the same logical name (baseline default)."""
        return self.one(
            "SELECT r.* FROM runs r JOIN drawings d ON d.id = r.drawing_id "
            "WHERE r.project_id = ? AND d.logical_name = ? AND r.status = 'completed' AND r.id != ? "
            "ORDER BY r.finished_at DESC, r.created_at DESC LIMIT 1", (project_id, logical_name, exclude or ""))

    def claim_run(self, rid: str, pid: int, create_time: float | None) -> bool:
        """queued -> running for the worker that owns the run. False if not claimable."""
        cur = self.conn.execute(
            "UPDATE runs SET status = 'running', stage = 'parse', started_at = ?, worker_pid = ?, "
            "worker_create_time = ?, heartbeat_at = ? WHERE id = ? AND status = 'queued' AND cancel_requested = 0",
            (utcnow(), pid, create_time, utcnow(), rid))
        return cur.rowcount == 1

    def update_progress(self, rid: str, stage: str, progress: float, note: str = "") -> None:
        self.conn.execute("UPDATE runs SET stage = ?, progress = ?, stage_note = ?, heartbeat_at = ? "
                          "WHERE id = ? AND status = 'running'", (stage, max(0.0, min(1.0, progress)), note,
                                                                   utcnow(), rid))

    def cancel_requested(self, rid: str) -> bool:
        r = self.one("SELECT cancel_requested FROM runs WHERE id = ?", (rid,))
        return bool(r and r["cancel_requested"])

    def request_cancel(self, rid: str) -> dict | None:
        """Queued runs are cancelled immediately; running runs are flagged for the worker."""
        with transaction(self.conn):
            self.conn.execute("UPDATE runs SET status = 'cancelled', cancel_requested = 1, finished_at = ?, "
                              "stage = 'cancelled', error_code = 'CANCELLED', error_message = '使用者取消' "
                              "WHERE id = ? AND status = 'queued'", (utcnow(), rid))
            self.conn.execute("UPDATE runs SET cancel_requested = 1 WHERE id = ? AND status = 'running'", (rid,))
        return self.get_run(rid)

    def finish_run(self, rid: str, status: str, *, error_code: str | None = None, error_message: str | None = None,
                   error_detail: str | None = None, only_if_active: bool = True) -> bool:
        assert status in RUN_FINAL
        sql = ("UPDATE runs SET status = ?, finished_at = ?, error_code = ?, error_message = ?, error_detail = ?, "
               "stage = ? WHERE id = ?")
        if only_if_active:
            sql += " AND status IN ('queued','running')"
        cur = self.conn.execute(sql, (status, utcnow(), error_code, error_message, error_detail, status, rid))
        return cur.rowcount == 1

    def complete_run(self, rid: str, results: Iterable[RuleResult], summary: dict,
                     baseline_states: dict[str, str]) -> bool:
        """Store all results and mark the run completed in one transaction.

        Returns False (and stores nothing) when the run is no longer
        running, e.g. cancelled or recovered as failed meanwhile.
        """
        with transaction(self.conn):
            r = self.one("SELECT status, cancel_requested FROM runs WHERE id = ?", (rid,))
            if not r or r["status"] != "running" or r["cancel_requested"]:
                return False
            issue_rows, link_rows = [], []
            for seq, res in enumerate(results):
                x, y = res.location if res.location else (None, None)
                issue_rows.append((
                    rid, res.issue_id, seq, res.status, res.severity, res.confidence, res.rule_id, res.rule_name,
                    res.measurement, res.layer, res.system, ",".join(res.handles), x, y, res.measured,
                    res.required_op, res.required_value, res.unit, res.evidence_id, res.message, res.reason, res.fix,
                    _j(res.details), baseline_states.get(res.issue_id)))
                for role, ids, handles in (("subject", res.subject_ids, res.subject_handles),
                                           ("target", res.target_ids, res.target_handles)):
                    for eid, h in zip(ids, handles):
                        link_rows.append((rid, res.issue_id, role, eid, h))
            self.conn.executemany(
                "INSERT INTO issues(run_id, issue_id, seq, status, severity, confidence, rule_id, rule_name, "
                "measurement, layer, system, handle, x, y, measured, required_op, required_value, unit, evidence_id, "
                "message, reason, fix, details_json, baseline_state) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", issue_rows)
            self.conn.executemany(
                "INSERT OR IGNORE INTO issue_entities(run_id, issue_id, role, eid, handle) VALUES (?,?,?,?,?)",
                link_rows)
            self.conn.execute(
                "UPDATE runs SET status = 'completed', stage = 'completed', progress = 1, finished_at = ?, "
                "summary_json = ? WHERE id = ?", (utcnow(), _j(summary), rid))
            r2 = self.one("SELECT project_id FROM runs WHERE id = ?", (rid,))
            self.audit("run.complete", r2["project_id"], {"run_id": rid, "counts": summary.get("counts")})
        return True

    # -- issues --------------------------------------------------------------
    @staticmethod
    def _issue(d: dict) -> dict:
        d["details"] = json.loads(d.pop("details_json") or "{}")
        d["handles"] = [h for h in (d.pop("handle") or "").split(",") if h]
        d["location"] = [d["x"], d["y"]] if d["x"] is not None else None
        return d

    def list_issues(self, rid: str, *, statuses: Sequence[str] = (), rule_id: str | None = None,
                    search: str | None = None, baseline_state: str | None = None, sort: str = "severity",
                    offset: int = 0, limit: int = 200) -> tuple[list[dict], int]:
        where, args = ["run_id = ?"], [rid]
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            args.extend(statuses)
        if rule_id:
            where.append("rule_id = ?")
            args.append(rule_id)
        if baseline_state:
            where.append("baseline_state = ?")
            args.append(baseline_state)
        if search:
            like = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(issue_id LIKE ? ESCAPE '\\' OR handle LIKE ? ESCAPE '\\' OR layer LIKE ? ESCAPE '\\' "
                         "OR message LIKE ? ESCAPE '\\' OR rule_name LIKE ? ESCAPE '\\')")
            args.extend([like] * 5)
        w = " AND ".join(where)
        total = self.conn.execute(f"SELECT COUNT(*) FROM issues WHERE {w}", args).fetchone()[0]
        order = ISSUE_SORT.get(sort, ISSUE_SORT["severity"])
        rows = self.q(f"SELECT * FROM issues WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?",
                      [*args, max(1, min(int(limit), 5000)), max(0, int(offset))])
        return [self._issue(r) for r in rows], int(total)

    def all_issues(self, rid: str) -> list[dict]:
        return [self._issue(r) for r in self.q(
            f"SELECT * FROM issues WHERE run_id = ? ORDER BY {ISSUE_SORT['severity']}", (rid,))]

    def get_issue(self, rid: str, issue_id: str) -> dict | None:
        r = self.one("SELECT * FROM issues WHERE run_id = ? AND issue_id = ?", (rid, issue_id))
        return self._issue(r) if r else None

    def issue_brief(self, rid: str) -> list[dict]:
        """id / status / measured for baseline comparison."""
        return self.q("SELECT issue_id AS id, status, measured FROM issues WHERE run_id = ?", (rid,))

    # -- exports -------------------------------------------------------------
    def add_export(self, run_id: str, fmt: str, rel_path: str, sha256: str) -> dict:
        eid = new_id("exp")
        with transaction(self.conn):
            self.conn.execute("INSERT INTO exports(id, run_id, format, path, sha256, created_at) VALUES (?,?,?,?,?,?)",
                              (eid, run_id, fmt, rel_path, sha256, utcnow()))
            r = self.one("SELECT project_id FROM runs WHERE id = ?", (run_id,))
            self.audit("export.create", r["project_id"] if r else None, {"run_id": run_id, "format": fmt,
                                                                         "path": rel_path, "sha256": sha256})
        return self.get_export(eid)

    def get_export(self, eid: str) -> dict | None:
        return self.one("SELECT x.*, r.project_id FROM exports x JOIN runs r ON r.id = x.run_id WHERE x.id = ?", (eid,))

    def list_exports(self, run_id: str) -> list[dict]:
        return self.q("SELECT * FROM exports WHERE run_id = ? ORDER BY created_at DESC", (run_id,))
