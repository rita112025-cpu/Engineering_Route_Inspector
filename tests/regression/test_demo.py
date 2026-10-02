"""The demo project must give the same, explainable result every time it is loaded and analysed."""
from __future__ import annotations

import time

import pytest

# (rule, status, handles, measured) reviewed against the drawing built by scripts/make_demo_dxf.py:
#  S1/P1 250 mm FAIL, S2/P2 320 mm WARNING, S3/P3 500 mm PASS; S6/P4 cross without elevations -> UNKNOWN;
#  S4 and S5 run into the no-go room; S2 crosses the water pipe; S5 is 1.718 degrees off horizontal;
#  S7/P5 cross 100 mm apart vertically.
EXPECTED = [
    ("CLEARANCE-01", "FAIL", ["34", "35"], "250 mm"), ("CLEARANCE-01", "WARNING", ["36", "37"], "320 mm"),
    ("CLEARANCE-01", "PASS", ["38", "39"], "500 mm"), ("CLEARANCE-01", "PASS", ["3A"], ""),
    ("CLEARANCE-01", "PASS", ["3B"], ""), ("CLEARANCE-01", "UNKNOWN", ["3E", "3F"], ""),
    ("CLEARANCE-01", "PASS", ["40"], ""),
    ("KEEP-OUT-01", "FAIL", ["3A", "3C"], "進入了區域"), ("KEEP-OUT-01", "FAIL", ["3B", "3C"], "進入了區域"),
    ("NO-CROSSING-01", "FAIL", ["36", "3D"], "有交叉"),
    ("ORTHOGONAL-01", "FAIL", ["3B"], "1.718°"),
    ("VERTICAL-CLEARANCE-01", "UNKNOWN", ["3E", "3F"], ""), ("VERTICAL-CLEARANCE-01", "FAIL", ["40", "41"], "100 mm"),
]


def run_demo(live):
    out = live.http.post("/api/demo")
    assert out.status_code == 200, out.text
    info = out.json()
    pid = info["project"]["id"]
    run = live.http.post(f"/api/projects/{pid}/runs", json={"drawing_id": info["drawing_id"]}).json()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        r = live.http.get(f"/api/runs/{run['id']}").json()
        if r["status"] in ("completed", "failed", "cancelled"):
            return info, r
        time.sleep(0.1)
    raise AssertionError("demo run did not finish")


def test_demo_result_is_exactly_what_was_designed(live):
    info, run = run_demo(live)
    assert run["status"] == "completed", run
    assert run["summary"]["counts"] == {"FAIL": 6, "WARNING": 1, "PASS": 26, "UNKNOWN": 2}
    items = live.http.get(f"/api/runs/{run['id']}/issues", params={"limit": 200, "sort": "rule"}).json()["items"]
    interesting = [(i["rule_id"], i["status"], i["handles"], i["measured_text"]) for i in items
                   if i["status"] != "PASS" or i["measured_text"] and i["rule_id"] == "CLEARANCE-01"]
    assert interesting == [e for e in EXPECTED if e[1] != "PASS" or e[3]]
    assert {i["status"] for i in items} == {"FAIL", "WARNING", "PASS", "UNKNOWN"}      # all four outcomes on screen
    assert run["summary"]["units_assumed"] is False


def test_demo_issue_ids_are_the_same_on_every_load(live_factory):
    ids = []
    for _ in range(2):
        srv = live_factory()
        _, run = run_demo(srv)
        items = srv.http.get(f"/api/runs/{run['id']}/issues", params={"limit": 200}).json()["items"]
        ids.append(sorted(i["issue_id"] for i in items))
    assert ids[0] == ids[1] and len(ids[0]) == 35


def test_every_demo_fail_can_be_explained(live):
    """Click any red issue: Handle / Layer / Measured / Required / Rule / Evidence are all there."""
    _, run = run_demo(live)
    fails = live.http.get(f"/api/runs/{run['id']}/issues", params={"status": "FAIL"}).json()["items"]
    assert len(fails) == 6
    for f in fails:
        d = live.http.get(f"/api/runs/{run['id']}/issues/{f['issue_id']}").json()
        assert d["handles"] and d["layer"] and d["required_text"] and d["rule_id"] and d["rule_name"]
        assert d["measured_text"], d["rule_id"]
        assert d["evidence"] and d["evidence"]["filename"] == "demo_spec.md" and d["evidence"]["text"]
        assert d["confidence"] == "CONFIRMED", (d["rule_id"], d["details"]["confidence_reasons"])
        assert d["reason"] and d["fix"] and d["entities"] and d["location"]


def test_loading_the_demo_twice_gives_the_same_project(live):
    a = live.http.post("/api/demo").json()
    b = live.http.post("/api/demo").json()
    assert a["created"] is True and b["created"] is False
    assert a["project"]["id"] == b["project"]["id"] and a["drawing_id"] == b["drawing_id"]
    assert a["project"]["is_demo"] == 1
    assert len([p for p in live.http.get("/api/projects").json()["projects"] if p["is_demo"]]) == 1


def test_demo_can_be_deleted_and_loaded_again(live):
    pid = live.http.post("/api/demo").json()["project"]["id"]
    assert live.http.delete(f"/api/projects/{pid}").status_code == 200
    again = live.http.post("/api/demo").json()
    assert again["created"] is True and again["project"]["id"] != pid


def test_demo_rules_are_ordinary_editable_rules(live):
    pid = live.http.post("/api/demo").json()["project"]["id"]
    rs = live.http.get(f"/api/projects/{pid}/rules").json()["ruleset"]
    assert [s["name"] for s in rs["systems"]] == ["弱電 SCADA", "電力"]
    assert len(rs["rules"]) == 5 and all(r["builder"]["template_id"] for r in rs["rules"])
    assert all(r.get("evidence_id") for r in rs["rules"])                       # every rule cites the specification
    docs = live.http.get(f"/api/projects/{pid}/documents").json()["documents"]
    assert [d["filename"] for d in docs] == ["demo_spec.md"]
    assert "示範" in live.http.get(f"/api/projects/{pid}/evidence", params={"q": "示範用"}).json()["items"][0]["text"]


def test_missing_demo_files_are_reported_and_leave_nothing_behind(live_factory, tmp_path):
    srv = live_factory(demo_dir=tmp_path / "nothing-here")
    r = srv.http.post("/api/demo")
    assert r.status_code == 500 and r.json()["error"]["code"] == "DEMO_MISSING"
    assert srv.http.get("/api/projects").json()["projects"] == []


def test_demo_spec_says_it_is_not_a_real_standard():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / "demo" / "demo_spec.md").read_text(encoding="utf-8")
    assert "示範" in text and "並非任何實際法規或標準" in text
