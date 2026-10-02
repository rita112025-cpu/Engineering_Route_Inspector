"""End-to-end through HTTP: upload -> rules -> analysis in a real worker process -> issues -> exports."""
from __future__ import annotations

import csv
import io
import sqlite3
import time

import ezdxf
import pytest

from .conftest import write_sample_dxf

SPEC = ("# 弱電管線規範\n\n4.2 間距\nSCADA 與電力電纜之水平淨距不得小於 300 mm，警示範圍 50 mm。\n\n"
        "4.3 禁設區\n電纜不得穿越機房禁設區。\n").encode("utf-8")
HANG = {"id": "HANG", "name": "hang", "subject": {"entity_type": "TEXT", "text_regex": "^(a|aa)+$"},
        "measurement": "entity_count", "operator": ">=", "value": 1}


def wait_run(live, rid, timeout=60, until=("completed", "failed", "cancelled")):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = live.http.get(f"/api/runs/{rid}").json()
        if run["status"] in until:
            return run
        time.sleep(0.05)
    raise AssertionError(f"run {rid} did not finish: {run}")


@pytest.fixture
def project(live, sample_dxf):
    """Project with the sample drawing, a specification, and one evidence-linked clearance rule."""
    pid = live.http.post("/api/projects", json={"name": "API 測試"}).json()["id"]
    d = live.http.post(f"/api/projects/{pid}/drawings", files={"file": ("plan.dxf", sample_dxf.read_bytes())})
    assert d.status_code == 201, d.text
    doc = live.http.post(f"/api/projects/{pid}/documents", files={"file": ("spec.md", SPEC)})
    assert doc.status_code == 201, doc.text
    ev = live.http.get(f"/api/projects/{pid}/evidence", params={"q": "水平淨距"}).json()["items"][0]
    built = live.http.post(f"/api/projects/{pid}/rules/build", json={
        "template_id": "clearance",
        "params": {"subject": {"system": "SCADA"}, "target": {"system": "POWER"}, "value": 300, "unit": "mm",
                   "warn_margin": 50, "evidence_id": ev["id"]}}).json()["rule"]
    rs = {"systems": [{"name": "SCADA", "layer_regex": "^SCADA"}, {"name": "POWER", "layer_regex": "^POWER"}],
          "rules": [built]}
    assert live.http.put(f"/api/projects/{pid}/rules", json=rs).status_code == 200
    return {"pid": pid, "did": d.json()["id"], "evidence": ev, "rule": built, "ruleset": rs}


def start(live, project, **body):
    return live.http.post(f"/api/projects/{project['pid']}/runs", json={"drawing_id": project["did"], **body})


def test_health_and_projects(live):
    h = live.http.get("/api/health").json()
    assert h["status"] == "ok" and h["schema_version"] >= 2 and h["job_manager"] is True
    p = live.http.post("/api/projects", json={"name": "專案一"})
    assert p.status_code == 201
    pid = p.json()["id"]
    assert [x["name"] for x in live.http.get("/api/projects").json()["projects"]] == ["專案一"]
    assert live.http.patch(f"/api/projects/{pid}", json={"name": "改名"}).json()["name"] == "改名"
    assert live.http.get(f"/api/projects/{pid}").json()["name"] == "改名"
    assert (live.config.data_dir / "projects" / pid / "drawings").is_dir()
    assert live.http.delete(f"/api/projects/{pid}").status_code == 200
    assert live.http.get(f"/api/projects/{pid}").status_code == 404
    assert not (live.config.data_dir / "projects" / pid).exists()


def test_upload_drawing_reports_what_was_read(live, sample_dxf):
    pid = live.http.post("/api/projects", json={"name": "P"}).json()["id"]
    r = live.http.post(f"/api/projects/{pid}/drawings", files={"file": ("plan.dxf", sample_dxf.read_bytes())})
    d = r.json()
    assert r.status_code == 201 and d["entity_count"] == 8 and d["logical_name"] == "plan.dxf"
    assert d["unit_to_mm"] == 1.0 and d["units_assumed"] is False
    assert {x["name"]: x["count"] for x in d["info"]["layers"]} == {"SCADA-CABLE": 4, "POWER-CABLE": 4}
    again = live.http.post(f"/api/projects/{pid}/drawings", files={"file": ("plan.dxf", sample_dxf.read_bytes())})
    assert again.status_code == 200 and again.json()["id"] == d["id"] and again.json()["already_imported"]
    assert len(live.http.get(f"/api/projects/{pid}/drawings").json()["drawings"]) == 1
    geo = live.http.get(f"/api/drawings/{d['id']}/geometry").json()
    assert len(geo["entities"]) == 8 and geo["extent"] == [0.0, 0.0, 7500.0, 4500.0]
    ents = live.http.get(f"/api/drawings/{d['id']}/entities", params={"handles": geo["entities"][0]["h"]}).json()
    assert [e["handle"] for e in ents["entities"]] == [geo["entities"][0]["h"]]


def test_documents_and_evidence_search(live):
    pid = live.http.post("/api/projects", json={"name": "P"}).json()["id"]
    r = live.http.post(f"/api/projects/{pid}/documents", files={"file": ("spec.md", SPEC)})
    doc = r.json()
    assert r.status_code == 201 and doc["kind"] == "md" and doc["chunk_count"] >= 3
    hit = live.http.get(f"/api/projects/{pid}/evidence", params={"q": "水平淨距"}).json()
    assert hit["total"] == 1
    item = hit["items"][0]
    assert item["section"] == "4.2 間距" and item["ref"] == "spec.md 第1頁 第4行"
    assert [(s["value"], s["unit"]) for s in item["suggest"]] == [(300.0, "mm"), (50.0, "mm")]
    full = live.http.get(f"/api/projects/{pid}/evidence/{item['id']}").json()
    assert "不得小於 300 mm" in full["text"]
    assert live.http.get(f"/api/projects/{pid}/evidence", params={"q": "不存在的字詞"}).json()["total"] == 0
    assert live.http.get(f"/api/projects/{pid}/evidence/EV-NOPE").status_code == 404
    assert live.http.delete(f"/api/documents/{doc['id']}").status_code == 200
    assert live.http.get(f"/api/projects/{pid}/evidence").json()["total"] == 0


def test_rule_templates_build_valid_rules(live):
    templates = live.http.get("/api/rule-templates").json()["templates"]
    assert {t["id"] for t in templates} >= {"clearance", "no_crossing", "keep_out", "inside_zone", "max_distance",
                                             "max_length", "orthogonal", "min_count", "vertical_clearance"}
    pid = live.http.post("/api/projects", json={"name": "P"}).json()["id"]
    params = {
        "clearance": {"subject": {"system": "A"}, "target": {"system": "B"}, "value": 300, "unit": "mm"},
        "vertical_clearance": {"subject": {"system": "A"}, "target": {"system": "B"}, "value": 0.2, "unit": "m"},
        "no_crossing": {"subject": {"system": "A"}, "target": {"layer": "WATER"}},
        "keep_out": {"subject": {"system": "A"}, "zone": {"layers": ["NOGO1", "NOGO2"]}},
        "inside_zone": {"subject": {"system": "A"}, "zone": {"layer": "TRENCH"}},
        "max_distance": {"subject": {"system": "A"}, "target": {"system": "SUP"}, "value": 1.5, "unit": "m"},
        "max_length": {"subject": {"system": "A"}, "value": 6, "unit": "m"},
        "orthogonal": {"subject": {"system": "A"}, "value": 1},
        "min_count": {"subject": {"layer": "TAG"}, "value": 2},
    }
    rules = []
    for t in templates:
        r = live.http.post(f"/api/projects/{pid}/rules/build",
                           json={"template_id": t["id"], "params": params[t["id"]], "taken_ids": [x["id"] for x in rules]})
        assert r.status_code == 200, (t["id"], r.text)
        rules.append(r.json()["rule"])
    assert len({r["id"] for r in rules}) == len(rules)
    names = {r["id"]: r["name"] for r in rules}
    assert any("不得小於 300 mm" in n for n in names.values())
    saved = live.http.put(f"/api/projects/{pid}/rules", json={"systems": [], "rules": rules})
    assert saved.status_code == 200 and saved.json()["enabled"] == len(rules)
    assert live.http.get(f"/api/projects/{pid}/rules").json()["ruleset"]["rules"] == saved.json()["ruleset"]["rules"]


def test_rule_builder_errors_are_readable(live):
    pid = live.http.post("/api/projects", json={"name": "P"}).json()["id"]
    bad = live.http.post(f"/api/projects/{pid}/rules/build", json={"template_id": "clearance", "params": {}})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "RULES_INVALID"
    assert any("檢查對象" in e for e in bad.json()["error"]["errors"])
    assert live.http.post(f"/api/projects/{pid}/rules/build", json={"template_id": "nope", "params": {}}
                          ).status_code == 400
    put = live.http.put(f"/api/projects/{pid}/rules", json={"rules": [
        {"id": "X", "subject": {"layer_regex": "(a+)+$"}, "measurement": "length", "operator": "<=", "value": 1}]})
    assert put.status_code == 400 and "回溯" in " ".join(put.json()["error"]["errors"])
    v = live.http.post(f"/api/projects/{pid}/rules/validate", json={"rules": [{"id": "bad id"}]}).json()
    assert v["valid"] is False and v["errors"]
    ok = live.http.post(f"/api/projects/{pid}/rules/validate", json={"rules": []}).json()
    assert ok == {"valid": True, "errors": [], "rules": 0}
    assert live.http.get(f"/api/projects/{pid}/rules/export").headers["content-disposition"].startswith("attachment")


def test_analysis_end_to_end(live, project):
    r = start(live, project)
    assert r.status_code == 202, r.text
    run = wait_run(live, r.json()["id"])
    assert run["status"] == "completed" and run["progress"] == 1
    assert run["summary"]["counts"] == {"FAIL": 1, "WARNING": 1, "PASS": 1, "UNKNOWN": 1}
    assert run["stage_label"] == "完成" and run["summary"]["comparison"] is None
    # the issue list: the four outcomes, worst first
    page = live.http.get(f"/api/runs/{run['id']}/issues").json()
    assert page["total"] == 4 and [i["status"] for i in page["items"]] == ["FAIL", "WARNING", "UNKNOWN", "PASS"]
    fail = page["items"][0]
    assert fail["measured_text"] == "250 mm" and fail["required_text"] == ">= 300 mm" and fail["layer"] == "SCADA-CABLE"
    assert fail["rule_id"] == project["rule"]["id"] and len(fail["handles"]) == 2 and fail["location"]
    only_fail = live.http.get(f"/api/runs/{run['id']}/issues", params={"status": "FAIL"}).json()
    assert only_fail["total"] == 1
    assert live.http.get(f"/api/runs/{run['id']}/issues", params={"status": "FAIL,WARNING"}).json()["total"] == 2
    assert live.http.get(f"/api/runs/{run['id']}/issues", params={"q": "250"}).json()["total"] == 1
    assert live.http.get(f"/api/runs/{run['id']}/issues", params={"sort": "measured", "limit": 2}).json()["items"][0][
        "measured"] == 250.0
    # clicking a red issue: everything needed to explain it and to locate it on the drawing
    d = live.http.get(f"/api/runs/{run['id']}/issues/{fail['issue_id']}").json()
    assert d["confidence"] == "CONFIRMED" and d["evidence"]["id"] == project["evidence"]["id"]
    assert "不得小於 300 mm" in d["evidence"]["text"] and d["evidence"]["ref"] == "spec.md 第1頁 第4行"
    assert d["reason"] and d["fix"] and "低於規則要求的 300 mm" in d["message"]
    assert {e["handle"] for e in d["entities"]} == set(d["handles"]) and all(e["g"]["points"] for e in d["entities"])
    assert d["details"]["closest_points"] and d["details"]["nearby"] is not None
    unknown = next(i for i in page["items"] if i["status"] == "UNKNOWN")
    assert unknown["confidence"] == "UNKNOWN" and unknown["measured"] is None
    assert live.http.get(f"/api/runs/{run['id']}/issues/ISS-NOPE").status_code == 404
    assert [x["id"] for x in live.http.get(f"/api/projects/{project['pid']}/runs").json()["runs"]] == [run["id"]]


def test_start_run_needs_drawing_and_rules(live, project):
    pid = project["pid"]
    assert live.http.post(f"/api/projects/{pid}/runs", json={}).json()["error"]["code"] == "NO_DRAWING"
    assert live.http.post(f"/api/projects/{pid}/runs", json={"drawing_id": "d_0000000000000000"}
                          ).json()["error"]["code"] == "NO_DRAWING"
    other = live.http.post("/api/projects", json={"name": "other"}).json()["id"]
    r = live.http.post(f"/api/projects/{other}/runs", json={"drawing_id": project["did"]})
    assert r.json()["error"]["code"] == "NO_DRAWING"                       # drawing of another project
    live.http.put(f"/api/projects/{pid}/rules", json={"systems": [], "rules": []})
    r = start(live, project)
    assert r.status_code == 400 and r.json()["error"]["code"] == "NO_RULES"


def test_second_run_on_a_busy_drawing_is_refused(live, project):
    conn = sqlite3.connect(live.state.storage.db_path)
    conn.execute("INSERT INTO runs(id, project_id, drawing_id, status, created_at, software_version) "
                 "VALUES ('run_bbbbbbbbbbbbbbbb', ?, ?, 'running', 'now', 'x')", (project["pid"], project["did"]))
    conn.commit()
    r = start(live, project)
    assert r.status_code == 409 and r.json()["error"]["code"] == "RUN_IN_PROGRESS"
    assert r.json()["error"]["run"]["id"] == "run_bbbbbbbbbbbbbbbb"
    assert live.http.delete(f"/api/drawings/{project['did']}").status_code == 409
    assert live.http.delete(f"/api/projects/{project['pid']}").status_code == 409
    conn.execute("UPDATE runs SET status = 'failed' WHERE id = 'run_bbbbbbbbbbbbbbbb'")
    conn.commit()
    conn.close()
    assert start(live, project).status_code == 202


def test_cancel_a_running_analysis(live, project):
    rs = dict(project["ruleset"], rules=[HANG])
    live.state.db.repo().save_ruleset(project["pid"], rs)      # the API refuses this pattern; the worker must still cope
    # a TEXT entity the hanging rule can chew on
    doc = ezdxf.new("R2018")
    doc.modelspace().add_text("a" * 40 + "!", dxfattribs={"layer": "NOTE"})
    buf = io.StringIO()
    doc.write(buf)
    up = live.http.post(f"/api/projects/{project['pid']}/drawings", files={"file": ("text.dxf", buf.getvalue().encode())})
    run = live.http.post(f"/api/projects/{project['pid']}/runs", json={"drawing_id": up.json()["id"]}).json()
    wait_run(live, run["id"], until=("running",))
    deadline = time.monotonic() + 20
    while live.http.get(f"/api/runs/{run['id']}").json()["stage"] != "rules" and time.monotonic() < deadline:
        time.sleep(0.05)
    c = live.http.post(f"/api/runs/{run['id']}/cancel")
    assert c.status_code == 200
    final = wait_run(live, run["id"], timeout=30)
    assert final["status"] == "cancelled" and "取消" in final["error_message"]
    assert live.http.get(f"/api/runs/{run['id']}/issues").json()["total"] == 0
    assert live.http.post(f"/api/runs/{run['id']}/cancel").json()["error"]["code"] == "NOT_ACTIVE"


def test_baseline_comparison_over_a_revised_drawing(live, project, tmp_path):
    first = wait_run(live, start(live, project).json()["id"])
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    for y, gap in ((0, 250), (2000, 600), (4000, 500)):           # first conflict stays, second is fixed
        msp.add_line((0, y), (5000, y), dxfattribs={"layer": "SCADA-CABLE"})
        msp.add_line((0, y + gap), (5000, y + gap), dxfattribs={"layer": "POWER-CABLE"})
    p = tmp_path / "plan.dxf"                                       # same logical name, different content
    doc.saveas(p)
    d2 = live.http.post(f"/api/projects/{project['pid']}/drawings", files={"file": ("plan.dxf", p.read_bytes())}).json()
    assert d2["id"] != project["did"] and not d2.get("already_imported")
    r = live.http.post(f"/api/projects/{project['pid']}/runs", json={"drawing_id": d2["id"]}).json()
    assert r["baseline_run_id"] == first["id"]                      # picked automatically
    second = wait_run(live, r["id"])
    cmp = second["summary"]["comparison"]
    assert cmp["available"] and cmp["drawing_changed"] and not cmp["ruleset_changed"]
    assert cmp["counts"]["RESOLVED"] >= 1
    states = {i["baseline_state"] for i in live.http.get(f"/api/runs/{second['id']}/issues").json()["items"]}
    assert states & {"UNCHANGED", "NEW", "CHANGED"}
    assert live.http.get(f"/api/runs/{second['id']}/issues", params={"baseline": "NEW"}).status_code == 200
    # no baseline at all when asked for none
    r3 = live.http.post(f"/api/projects/{project['pid']}/runs",
                        json={"drawing_id": d2["id"], "baseline_run_id": None}).json()
    assert r3["baseline_run_id"] is None
    wait_run(live, r3["id"])


def test_exports_over_http(live, project):
    run = wait_run(live, start(live, project).json()["id"])
    saved = {}
    for fmt, ctype in (("csv", "text/csv"), ("markdown", "text/markdown"), ("html", "text/html"),
                       ("dxf", "application/dxf")):
        r = live.http.post(f"/api/runs/{run['id']}/exports", json={"format": fmt})
        assert r.status_code == 201, (fmt, r.text)
        row = r.json()
        assert not row["saved_to"].startswith("/") and ":" not in row["saved_to"] and "/exports/" in row["saved_to"]
        assert (live.config.data_dir / row["saved_to"]).is_file()
        dl = live.http.get(row["download_url"])
        assert dl.status_code == 200 and dl.headers["content-type"].startswith(ctype)
        assert dl.headers["content-disposition"].startswith("attachment")
        saved[fmt] = dl.content
    rows = list(csv.reader(io.StringIO(saved["csv"].decode("utf-8-sig"))))
    assert len(rows) == 5 and "Handle" in rows[0]
    assert "# 工程管線檢查報告：plan.dxf" in saved["markdown"].decode()
    assert b"default-src 'none'" in saved["html"]
    out = io.StringIO(saved["dxf"].decode("utf-8", errors="replace"))
    assert ezdxf.read(out).modelspace().query('CIRCLE[layer=="ERI_FAIL"]')
    listed = live.http.get(f"/api/runs/{run['id']}/exports").json()["exports"]
    assert sorted(e["format"] for e in listed) == ["csv", "dxf", "html", "markdown"]
    assert live.http.post(f"/api/runs/{run['id']}/exports", json={"format": "pdf"}).json()["error"]["code"] == "BAD_FORMAT"
    queued_msg = live.http.post("/api/runs/run_0000000000000000/exports", json={"format": "csv"})
    assert queued_msg.status_code == 404


def test_export_of_unfinished_run_is_refused(live, project):
    conn = sqlite3.connect(live.state.storage.db_path)
    conn.execute("INSERT INTO runs(id, project_id, drawing_id, status, created_at, software_version) "
                 "VALUES ('run_cccccccccccccccc', ?, ?, 'running', 'now', 'x')", (project["pid"], project["did"]))
    conn.commit()
    conn.close()
    r = live.http.post("/api/runs/run_cccccccccccccccc/exports", json={"format": "csv"})
    assert r.status_code == 409 and "尚未完成" in r.json()["error"]["message"]


def test_requests_during_an_analysis_stay_responsive(live, project):
    run = start(live, project).json()
    t0 = time.monotonic()
    for _ in range(20):
        assert live.http.get("/api/health").status_code == 200
        assert live.http.get(f"/api/runs/{run['id']}").status_code == 200
    assert time.monotonic() - t0 < 10
    wait_run(live, run["id"])


def test_restart_recovers_runs_the_old_server_lost(tmp_path, sample_dxf):
    """A server killed mid-run leaves a 'running' row; the next start marks it failed and the app still works."""
    from tests.conftest import LiveServer
    a = LiveServer(tmp_path)
    pid = a.http.post("/api/projects", json={"name": "P"}).json()["id"]
    d = a.http.post(f"/api/projects/{pid}/drawings", files={"file": ("plan.dxf", sample_dxf.read_bytes())}).json()
    conn = sqlite3.connect(a.state.storage.db_path)
    conn.execute("INSERT INTO runs(id, project_id, drawing_id, status, created_at, software_version, worker_pid, "
                 "worker_create_time) VALUES ('run_dddddddddddddddd', ?, ?, 'running', 'now', 'x', 999999, 1.0)",
                 (pid, d["id"]))
    conn.commit()
    conn.close()
    a.close()
    b = LiveServer(tmp_path)
    try:
        run = b.http.get("/api/runs/run_dddddddddddddddd").json()
        assert run["status"] == "failed" and run["error_code"] == "WORKER_LOST" and "重新執行" in run["error_message"]
        assert b.state.recovery["marked_failed"] == ["run_dddddddddddddddd"]
    finally:
        b.close()
