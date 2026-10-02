"""The demo project: a real drawing, a specification and rules, loaded through the same code paths a user's
own files take (so the demo cannot drift from the product). Idempotent: loading twice returns the same project.

The files live in ``demo/`` (see scripts/make_demo_dxf.py). The specification text is written for the demo
and says so; it is not a real standard.
"""
from __future__ import annotations

from core.evidence.chunks import find_by_quote
from core.rules.schema import normalize_ruleset
from . import api, templates
from .errors import ApiError
from .state import AppState

DEMO_NAME = "示範專案（變電站電纜走廊）"
DRAWING_FILE = "demo_plan.dxf"
SPEC_FILE = "demo_spec.md"
SYSTEMS = [{"name": "弱電 SCADA", "layer_regex": "^(?:SCADA-CABLE)$"},
           {"name": "電力", "layer_regex": "^(?:POWER-CABLE)$"}]

# (template, parameters, a sentence of the specification that justifies it)
RULES = [
    ("clearance", {"subject": {"system": "弱電 SCADA"}, "target": {"system": "電力"}, "value": 300, "unit": "mm",
                   "warn_margin": 50}, "水平淨距不得小於 300 mm"),
    ("vertical_clearance", {"subject": {"system": "弱電 SCADA"}, "target": {"system": "電力"}, "value": 200,
                            "unit": "mm"}, "垂直淨距不得小於 200 mm"),
    ("keep_out", {"subject": {"system": "弱電 SCADA"}, "zone": {"layer": "ZONE-NOGO"}}, "弱電電纜不得進入機房禁設區"),
    ("no_crossing", {"subject": {"system": "弱電 SCADA"}, "target": {"layer": "WATER-PIPE"}},
     "弱電電纜不得與給水管在平面上交叉"),
    ("orthogonal", {"subject": {"system": "弱電 SCADA"}, "value": 1}, "偏差不得超過 1°"),
]


def load_demo(st: AppState) -> dict:
    """Create the demo project if it does not exist yet. Returns {project, drawing_id, created}."""
    repo = st.repo()
    existing = repo.demo_project()
    if existing:
        drawings = repo.list_drawings(existing["id"])
        return {"project": existing, "drawing_id": drawings[0]["id"] if drawings else None, "created": False}
    folder = st.config.demo_dir
    plan, spec = folder / DRAWING_FILE, folder / SPEC_FILE
    if not plan.is_file() or not spec.is_file():
        raise ApiError(500, "DEMO_MISSING", "找不到示範檔案（demo 資料夾）。請重新下載完整的程式。")
    project = repo.create_project(DEMO_NAME, is_demo=True)
    pid = project["id"]
    try:
        st.storage.project_dir(pid, create=True)
        with open(plan, "rb") as f:
            drawing = api._import_drawing(st, pid, DRAWING_FILE, f)
        with open(spec, "rb") as f:
            api._import_document(st, pid, SPEC_FILE, f)
        evidence = repo.load_evidence(pid)
        taken: set[str] = set()
        rules = []
        for template_id, params, quote in RULES:
            chunk = find_by_quote(evidence, quote, SPEC_FILE)
            if chunk is None:
                raise ApiError(500, "DEMO_MISSING", f"示範規範中找不到條文：{quote}")
            rule = templates.build_rule(template_id, dict(params, evidence_id=chunk.id), taken)
            taken.add(rule["id"])
            rules.append(rule)
        repo.save_ruleset(pid, normalize_ruleset({"systems": SYSTEMS, "rules": rules}), operation="demo.rules")
    except BaseException:
        repo.delete_project(pid)                    # never leave a half-built demo behind
        st.storage.delete_project_files(pid)
        raise
    return {"project": repo.get_project(pid), "drawing_id": drawing["id"], "created": True}
