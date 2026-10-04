"""Specification folder -> scan -> rule candidates -> review -> apply -> analysis, through the real HTTP API."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.conftest import LiveServer, write_sample_dxf
from .test_api import wait_run

A = ("# 附錄 C 管線佈設\n\n"
     "C.1 淨距\n"
     "SCADA 線纜架與電力線纜架之水平淨距不得小於 300 mm。\n"
     "SCADA 線纜架與給水管之水平淨距不得小於 300 mm。\n"
     "通信線纜架與電力線纜架之水平淨距不得小於 300 mm。\n"
     "管線應保持適當距離。\n")
B = "# 補充規定\n\nSCADA 線纜架與給水管之水平淨距不得小於 500 mm。\n"
GLOSSARY = "給水管 = WATER-PIPE"


def write(folder: Path, name: str, text: str) -> Path:
    p = folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def specs(tmp_path):
    folder = tmp_path / "規範"
    write(folder, "A.md", A)
    write(folder, "sub/B.md", B)
    return folder


def plan_bytes(sample_dxf: Path) -> bytes:
    """The sample drawing plus a WATER-PIPE layer far away from everything (the results of the sample stay the same)."""
    import ezdxf
    doc = ezdxf.readfile(sample_dxf)
    doc.modelspace().add_line((0, 20000), (5000, 20000), dxfattribs={"layer": "WATER-PIPE"})
    out = sample_dxf.with_name("plan-with-water.dxf")
    doc.saveas(out)
    return out.read_bytes()


def make_project(live, sample_dxf, with_drawing=True, name="規範專案"):
    pid = live.http.post("/api/projects", json={"name": name}).json()["id"]
    did = None
    if with_drawing:
        d = live.http.post(f"/api/projects/{pid}/drawings", files={"file": ("plan.dxf", plan_bytes(sample_dxf))})
        assert d.status_code == 201, d.text
        did = d.json()["id"]
    return pid, did


def set_source(live, pid, folder, glossary=None):
    body = {"path": str(folder)}
    if glossary is not None:
        body["glossary_text"] = glossary
    r = live.http.put(f"/api/projects/{pid}/spec-source", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def scan(live, pid):
    r = live.http.post(f"/api/projects/{pid}/spec-scan", json={"wait": True})
    assert r.status_code == 200, r.text
    state = r.json()
    assert state["state"] == "done", state
    return state["summary"]


def candidates(live, pid, **params):
    r = live.http.get(f"/api/projects/{pid}/rule-candidates", params={"limit": 500, **params})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def scanned(live, sample_dxf, specs):
    pid, did = make_project(live, sample_dxf)
    set_source(live, pid, specs, GLOSSARY)
    summary = scan(live, pid)
    return {"pid": pid, "did": did, "summary": summary, "specs": specs}


def by_text(data, fragment, value=None):
    hits = [c for c in data["items"] if fragment in c["evidence"]["text"] and (value is None or c["value"] == value)]
    assert len(hits) == 1, [c["evidence"]["text"] for c in data["items"]]
    return hits[0]


# ---- the scan and what it finds ------------------------------------------------------------------------------------------
def test_scan_builds_candidates_with_evidence(live, scanned):
    pid = scanned["pid"]
    data = candidates(live, pid)
    s = data["summary"]
    assert (s["usable"], s["review"], s["conflict"], s["not_enforceable"]) == (1, 1, 2, 1)
    ok = by_text(data, "SCADA 線纜架與電力線纜架")
    assert ok["status"] == "CONFIRMED" and ok["enabled"] and ok["confirmed_by"] == "auto" and ok["severity"] == "FAIL"
    assert ok["requirement"] == "水平淨距 ≥ 300 mm" and ok["title"] == "「SCADA-CABLE」↔「POWER-CABLE」"
    ev = ok["evidence"]                                       # the way back to the original wording
    assert ev["document"] == "A.md" and ev["section"] == "C.1 淨距" and ev["evidence_id"].startswith("EV-")
    assert "不得小於 300 mm" in ev["text"] and "A.md" in ev["ref"]
    stored = live.http.get(f"/api/projects/{pid}/evidence/{ev['evidence_id']}").json()
    assert ev["text"].rstrip("。") in stored["text"]
    review = by_text(data, "通信線纜架")
    assert review["status"] == "NEEDS_REVIEW" and not review["enabled"] and review["fix"]["slots"] == ["subject"]
    assert any(m["code"] == "SUBJECT_UNMAPPED" for m in review["missing"])
    ne = by_text(data, "適當距離")
    assert ne["status"] == "NOT_ENFORCEABLE" and ne["bucket"] == "not_enforceable" and not ne["enabled"]
    assert ne["template_id"] is None and ne["evidence"]["text"]          # kept verbatim, never a rule
    assert data["gate"] == {"will_run": 1, "from_spec": 1, "manual": 0, "review": 1, "conflict": 2, "not_enforceable": 1,
                            "runnable": True}


def test_conflict_keeps_both_evidences_and_is_never_resolved_automatically(live, scanned):
    data = candidates(live, scanned["pid"], bucket="conflict")
    assert data["total"] == 2 and all(c["status"] == "CONFLICTING" and not c["enabled"] for c in data["items"])
    opts = data["items"][0]["conflict"]["options"]
    assert sorted(o["value"] for o in opts) == [300, 500]
    assert {o["source"]["document"] for o in opts} == {"A.md", "B.md"}
    assert all(o["source"]["text"] for o in opts)


def test_choosing_a_value_resolves_the_conflict_and_persists(live, scanned):
    pid = scanned["pid"]
    items = candidates(live, pid, bucket="conflict")["items"]
    pick = next(c for c in items if c["value"] == 300)
    out = live.http.patch(f"/api/projects/{pid}/rule-candidates/{pick['id']}", json={"choose": True}).json()
    assert out["candidate"]["status"] == "CONFIRMED" and out["candidate"]["enabled"]
    assert out["candidate"]["confirmed_by"] == "user" and out["summary"]["conflict"] == 0
    other = next(c for c in candidates(live, pid)["items"] if c["value"] == 500)
    assert other["status"] == "CONFLICTING" and other["bucket"] == "off" and not other["enabled"]
    assert out["gate"]["will_run"] == 2


def test_review_decisions_a_person_can_make(live, scanned):
    pid = scanned["pid"]
    review = by_text(candidates(live, pid), "通信線纜架")
    url = f"/api/projects/{pid}/rule-candidates/{review['id']}"
    bad = live.http.patch(url, json={"selectors": {"subject": {"layer": "NO-SUCH-LAYER"}}})
    assert bad.status_code == 400 and "不存在" in bad.json()["error"]["message"]
    assert live.http.patch(url, json={"bogus": 1}).status_code == 400
    assert live.http.patch(url, json={"selectors": {"zone": {"layer": "SCADA-CABLE"}}}).status_code == 200   # accepted, unused
    out = live.http.patch(url, json={"selectors": {"subject": {"layer": "COMM-X"}}})
    assert out.status_code == 400                                       # the drawing has no such layer
    live.http.patch(url, json={"selectors": {"zone": None}})
    out = live.http.patch(url, json={"selectors": {"subject": {"layer": "WATER-PIPE"}}}).json()
    assert out["candidate"]["status"] == "CONFIRMED" and out["candidate"]["confirmed_by"] == "user"
    assert out["summary"]["review"] == 0 and out["merged"] is False
    off = live.http.patch(url, json={"disabled": True}).json()["candidate"]
    assert off["bucket"] == "off" and not off["enabled"]
    assert live.http.patch(url, json={"reset": True}).json()["candidate"]["status"] == "NEEDS_REVIEW"
    assert live.http.patch(f"/api/projects/{pid}/rule-candidates/cand_0000000000000000", json={"disabled": True}
                           ).status_code == 404
    # picking the object that makes it the same rule as an existing one merges them (one rule, two sources)
    merged = live.http.patch(url, json={"selectors": {"subject": {"layer": "SCADA-CABLE"}}}).json()
    assert merged["merged"] is True and len(merged["candidate"]["duplicates"]) == 1
    assert merged["summary"]["usable"] == 1
    assert live.http.get(f"/api/projects/{pid}/rule-candidates/not-an-id").status_code == 404


# ---- apply and analysis ---------------------------------------------------------------------------------------------------
def test_apply_runs_only_confirmed_rules_and_the_result_keeps_the_evidence(live, scanned):
    pid, did = scanned["pid"], scanned["did"]
    out = live.http.post(f"/api/projects/{pid}/rule-candidates/apply").json()
    assert out["applied"] == 1 and out["changed"] and out["summary"]["review"] == 1
    rules = live.http.get(f"/api/projects/{pid}/rules").json()["ruleset"]["rules"]
    assert len(rules) == 1
    rule = rules[0]
    assert rule["origin"]["type"] == "spec-extraction" and rule["evidence_id"].startswith("EV-")
    assert rule["id"].startswith("SPEC-CLEARANCE-") and rule["severity"] == "FAIL" and rule["value"] == 300
    assert rule["subject"] == {"layer_equals": "SCADA-CABLE"} and rule["target"] == {"layer_equals": "POWER-CABLE"}
    again = live.http.post(f"/api/projects/{pid}/rule-candidates/apply").json()
    assert again["changed"] is False and again["applied"] == 1                  # idempotent

    run = live.http.post(f"/api/projects/{pid}/runs", json={"drawing_id": did})
    assert run.status_code == 202, run.text
    done = wait_run(live, run.json()["id"])
    assert done["status"] == "completed" and done["summary"]["counts"]["FAIL"] == 1
    fail = live.http.get(f"/api/runs/{done['id']}/issues", params={"status": "FAIL"}).json()["items"][0]
    assert fail["measured_text"] == "250 mm" and fail["required_text"] == ">= 300 mm" and fail["rule_id"] == rule["id"]
    detail = live.http.get(f"/api/runs/{done['id']}/issues/{fail['issue_id']}").json()
    assert detail["evidence"]["filename"] == "A.md" and "不得小於 300 mm" in detail["evidence"]["text"]
    assert detail["confidence"] == "CONFIRMED"


def test_starting_a_run_applies_the_confirmed_rules_by_itself(live, scanned):
    pid, did = scanned["pid"], scanned["did"]
    assert live.http.get(f"/api/projects/{pid}/rules").json()["ruleset"]["rules"] == []
    run = live.http.post(f"/api/projects/{pid}/runs", json={"drawing_id": did})
    assert run.status_code == 202
    wait_run(live, run.json()["id"])
    assert len(live.http.get(f"/api/projects/{pid}/rules").json()["ruleset"]["rules"]) == 1


def test_only_unconfirmed_candidates_do_not_start_an_analysis(live, sample_dxf, tmp_path):
    folder = tmp_path / "only-review"
    write(folder, "R.md", "通信線纜架與電力線纜架之水平淨距不得小於 300 mm。\n建議保持 300 mm。\n應儘量縮短路徑。\n")
    pid, did = make_project(live, sample_dxf)
    set_source(live, pid, folder)
    scan(live, pid)
    data = candidates(live, pid)
    assert data["summary"]["usable"] == 0 and data["gate"]["runnable"] is False
    assert all(not c["enabled"] for c in data["items"])
    r = live.http.post(f"/api/projects/{pid}/runs", json={"drawing_id": did})
    assert r.status_code == 400 and r.json()["error"]["code"] == "NO_RULES"
    assert "可直接使用" in r.json()["error"]["message"]
    assert live.http.get(f"/api/projects/{pid}/rules").json()["ruleset"]["rules"] == []


def test_manual_rules_are_untouched_by_apply(live, scanned):
    pid = scanned["pid"]
    manual = live.http.post(f"/api/projects/{pid}/rules/build", json={
        "template_id": "max_length", "params": {"subject": {"layer": "SCADA-CABLE"}, "value": 6, "unit": "m"}}).json()["rule"]
    live.http.put(f"/api/projects/{pid}/rules", json={"systems": [{"name": "SCADA", "layer_regex": "^SCADA"}],
                                                      "rules": [manual]})
    out = live.http.post(f"/api/projects/{pid}/rule-candidates/apply").json()
    assert out["applied"] == 1 and out["manual"] == 1 and out["gate"]["will_run"] == 2
    rs = live.http.get(f"/api/projects/{pid}/rules").json()["ruleset"]
    assert [r["id"] for r in rs["rules"]][0] == manual["id"] and len(rs["rules"]) == 2
    assert rs["systems"] == [{"name": "SCADA", "layer_regex": "^SCADA"}]
    assert "origin" not in rs["rules"][0] and "evidence_id" not in rs["rules"][0]     # manual: no evidence is claimed


def test_a_rule_that_is_no_longer_confirmed_is_removed_by_the_next_apply(live, scanned):
    pid = scanned["pid"]
    live.http.post(f"/api/projects/{pid}/rule-candidates/apply")
    ok = by_text(candidates(live, pid), "SCADA 線纜架與電力線纜架")
    live.http.patch(f"/api/projects/{pid}/rule-candidates/{ok['id']}", json={"disabled": True})
    out = live.http.post(f"/api/projects/{pid}/rule-candidates/apply").json()
    assert out["applied"] == 0 and out["changed"] and out["gate"]["runnable"] is False
    assert live.http.get(f"/api/projects/{pid}/rules").json()["ruleset"]["rules"] == []


def test_a_candidate_without_a_drawing_is_never_confirmed(live, sample_dxf, specs):
    pid, _ = make_project(live, sample_dxf, with_drawing=False)
    set_source(live, pid, specs, GLOSSARY)
    scan(live, pid)
    data = candidates(live, pid)
    assert data["summary"]["usable"] == 0
    assert any(m["code"] == "NO_DRAWING" for c in data["items"] for m in c["missing"])
    up = live.http.post(f"/api/projects/{pid}/drawings", files={"file": ("plan.dxf", plan_bytes(sample_dxf))})
    assert up.status_code == 201
    assert candidates(live, pid)["summary"]["usable"] == 1                  # mapped as soon as there is a drawing


# ---- persistence ------------------------------------------------------------------------------------------------------------
def test_candidates_and_decisions_survive_a_restart(tmp_path, sample_dxf, specs):
    first = LiveServer(tmp_path)
    try:
        pid, _ = make_project(first, sample_dxf)
        set_source(first, pid, specs, GLOSSARY)
        scan(first, pid)
        review = by_text(candidates(first, pid), "通信線纜架")
        first.http.patch(f"/api/projects/{pid}/rule-candidates/{review['id']}",
                         json={"selectors": {"subject": {"layer": "WATER-PIPE"}}})
        pick = next(c for c in candidates(first, pid)["items"] if c["conflict"] and c["value"] == 500)
        first.http.patch(f"/api/projects/{pid}/rule-candidates/{pick['id']}", json={"choose": True})
        before = {c["id"]: (c["status"], c["enabled"]) for c in candidates(first, pid)["items"]}
    finally:
        first.close()
    second = LiveServer(tmp_path)
    try:
        after = {c["id"]: (c["status"], c["enabled"]) for c in candidates(second, pid)["items"]}
        assert after == before and len(after) == 5
        assert after[review["id"]] == ("CONFIRMED", True)
        assert after[pick["id"]] == ("CONFIRMED", True)
        src = second.http.get(f"/api/projects/{pid}/spec-source").json()
        assert src["path"] == str(specs.resolve()) and src["origin"] == "project"
        assert src["glossary"]["project"] == {"給水管": ["WATER-PIPE"]} and src["last_scan"]["files"] == 2
    finally:
        second.close()


def test_ids_and_decisions_are_stable_across_rescans_and_edits(live, scanned):
    pid, specs_dir = scanned["pid"], scanned["specs"]
    ids = {c["id"] for c in candidates(live, pid)["items"]}
    review = by_text(candidates(live, pid), "通信線纜架")
    live.http.patch(f"/api/projects/{pid}/rule-candidates/{review['id']}", json={"disabled": True})
    scan(live, pid)
    assert {c["id"] for c in candidates(live, pid)["items"]} == ids          # unchanged files: nothing re-extracted
    # a paragraph is added in front: the file changes, the requirement does not
    write(specs_dir, "A.md", "# 附錄 C 管線佈設\n\n前言：本章說明管線佈設。\n\n" + A.split("\n\n", 1)[1])
    summary = scan(live, pid)
    assert summary["reparsed"] == 1 and summary["reused"] == 1
    new_ids = {c["id"] for c in candidates(live, pid)["items"]}
    assert len(new_ids) == len(ids)                                          # same requirements ...
    still = by_text(candidates(live, pid), "通信線纜架")
    assert still["decision"] == {"disabled": True} and still["bucket"] == "off"      # ... and the decision is still there


# ---- incremental scanning ------------------------------------------------------------------------------------------------------
def test_second_scan_reuses_unchanged_files_and_reparses_only_what_changed(live, sample_dxf, specs, monkeypatch):
    import importers.text as text_importer
    parsed: list[str] = []
    real = text_importer.load_text

    def counting(path, document_id, doc_hash, filename=None):
        parsed.append(filename or Path(path).name)
        return real(path, document_id, doc_hash, filename)
    monkeypatch.setattr(text_importer, "load_text", counting)

    pid, _ = make_project(live, sample_dxf)
    set_source(live, pid, specs, GLOSSARY)
    first = scan(live, pid)
    assert sorted(parsed) == ["A.md", "B.md"] and first["new"] == 2 and first["reused"] == 0
    parsed.clear()
    second = scan(live, pid)
    assert parsed == [] and second["reused"] == 2 and second["new"] == 0 and second["reparsed"] == 0
    assert second["extracted_documents"] == 0

    write(specs, "sub/B.md", B.replace("500", "600"))
    parsed.clear()
    third = scan(live, pid)
    assert parsed == ["B.md"] and third["reused"] == 1 and third["reparsed"] == 1
    assert {"path": "sub/B.md", "action": "reparsed", "status": "OK"} in third["files_listed"]
    values = sorted(c["value"] for c in candidates(live, pid)["items"] if c["conflict"])
    assert values == [300, 600]                                              # B's old candidates were replaced
    assert len(live.http.get(f"/api/projects/{pid}/documents").json()["documents"]) == 2   # the old B is gone


def test_touching_a_file_without_changing_it_does_not_reparse(live, sample_dxf, specs, monkeypatch):
    import importers.text as text_importer
    pid, _ = make_project(live, sample_dxf)
    set_source(live, pid, specs)
    scan(live, pid)
    future = time.time() + 100
    os.utime(specs / "A.md", (future, future))
    monkeypatch.setattr(text_importer, "load_text", lambda *a, **k: pytest.fail("a file with the same content was parsed again"))
    s = scan(live, pid)
    assert s["reused"] == 2 and s["reparsed"] == 0


def test_a_removed_file_takes_its_candidates_with_it(live, sample_dxf, specs):
    pid, _ = make_project(live, sample_dxf)
    set_source(live, pid, specs, GLOSSARY)
    scan(live, pid)
    (specs / "sub" / "B.md").unlink()
    s = scan(live, pid)
    assert s["removed"] == 1
    data = candidates(live, pid)
    assert data["summary"]["conflict"] == 0 and data["total"] == 4
    assert len(live.http.get(f"/api/projects/{pid}/documents").json()["documents"]) == 1


def test_the_same_content_under_two_names_is_ingested_once(live, sample_dxf, specs):
    write(specs, "copy-of-A.md", A)
    pid, _ = make_project(live, sample_dxf)
    set_source(live, pid, specs, GLOSSARY)
    scan(live, pid)
    docs = live.http.get(f"/api/projects/{pid}/documents").json()["documents"]
    assert len(docs) == 2                                                    # A (shared by two files) and B
    assert candidates(live, pid)["summary"]["conflict"] == 2


def test_a_document_imported_by_hand_is_part_of_the_source(live, sample_dxf, tmp_path):
    pid, _ = make_project(live, sample_dxf)
    up = live.http.post(f"/api/projects/{pid}/documents", files={"file": ("manual.md", A.encode("utf-8"))})
    assert up.status_code == 201
    empty = tmp_path / "empty"
    empty.mkdir()
    set_source(live, pid, empty, GLOSSARY)
    scan(live, pid)
    assert candidates(live, pid)["summary"]["usable"] == 2


# ---- documents that cannot be read -------------------------------------------------------------------------------------------------
def test_pdf_without_a_text_layer_is_reported_not_pretended(live, sample_dxf, tmp_path):
    fitz = pytest.importorskip("fitz")
    folder = tmp_path / "pdfs"
    folder.mkdir()
    doc = fitz.open()
    doc.new_page()
    doc.save(str(folder / "scan.pdf"))
    doc.close()
    pid, _ = make_project(live, sample_dxf)
    set_source(live, pid, folder)
    s = scan(live, pid)
    assert s["not_extractable"] == 1 and s["candidates"] == 0
    assert {"path": "scan.pdf", "action": "new", "status": "NOT_EXTRACTABLE"} in s["files_listed"]
    src = live.http.get(f"/api/projects/{pid}/spec-source").json()
    assert src["file_status"] == {"NOT_EXTRACTABLE": 1} and "OCR" in src["reasons"]["PDF_NO_TEXT_LAYER"]
    assert live.http.get(f"/api/projects/{pid}/documents").json()["documents"] == []     # nothing empty is kept


def test_a_file_over_the_size_limit_is_skipped(live_factory, sample_dxf, tmp_path):
    live = live_factory(max_document_mb=1)
    folder = tmp_path / "big"
    write(folder, "small.md", A)
    (folder / "huge.txt").write_bytes(b"x" * (2 * 1024 * 1024))
    pid, _ = make_project(live, sample_dxf)
    set_source(live, pid, folder, GLOSSARY)
    s = scan(live, pid)
    assert s["too_large"] == 1 and s["new"] == 2
    assert {"path": "huge.txt", "action": "new", "status": "TOO_LARGE"} in s["files_listed"]


def test_unsupported_and_temporary_files_are_ignored(live, sample_dxf, tmp_path):
    folder = tmp_path / "mixed"
    write(folder, "notes.md", A)
    write(folder, "image.png", "x")
    write(folder, "~$lock.docx", "x")
    write(folder, ".hidden.md", A)
    write(folder, ".git/config.md", A)
    write(folder, "readme.rtf", A)
    pid, _ = make_project(live, sample_dxf)
    set_source(live, pid, folder)
    assert scan(live, pid)["files"] == 1


# ---- the source folder: safety --------------------------------------------------------------------------------------------------------
def put_path(live, pid, path):
    return live.http.put(f"/api/projects/{pid}/spec-source", json={"path": path})


def test_the_source_path_is_checked(live, sample_dxf, specs, tmp_path):
    pid, _ = make_project(live, sample_dxf)
    cases = {
        "": None,
        f"{specs}{os.sep}..{os.sep}規範": "SOURCE_INVALID",                      # traversal is refused outright
        "relative/folder": "SOURCE_INVALID",
        str(tmp_path / "missing"): "SOURCE_NOT_FOUND",
        str(specs / "A.md"): "SOURCE_NOT_DIR",
        "x" * 600: "SOURCE_INVALID",
        str(Path(specs.anchor)): "SOURCE_TOO_BROAD",
        str(live.config.data_dir): "SOURCE_INVALID",
        str(live.config.data_dir / "projects"): "SOURCE_INVALID",
    }
    for path, code in cases.items():
        r = put_path(live, pid, path)
        if code is None:
            assert r.status_code == 200 and r.json()["origin"] == "none"
            continue
        assert r.status_code == 400, (path, r.text)
        err = r.json()["error"]
        assert err["code"] == code and any("一" <= ch <= "鿿" for ch in err["message"]), (path, err)
    assert put_path(live, pid, 5).status_code == 400
    ok = put_path(live, pid, f' "{specs}" ')
    assert ok.status_code == 200 and ok.json()["path"] == str(specs.resolve())


def test_scan_needs_a_source_and_a_token(live, sample_dxf, specs):
    pid, _ = make_project(live, sample_dxf)
    r = live.http.post(f"/api/projects/{pid}/spec-scan", json={"wait": True})
    assert r.status_code == 400 and r.json()["error"]["code"] == "NO_SOURCE"
    set_source(live, pid, specs)
    assert live.raw.post(f"/api/projects/{pid}/spec-scan", json={}).status_code == 403
    assert live.raw.put(f"/api/projects/{pid}/spec-source", json={"path": str(specs)}).status_code == 403
    assert live.raw.patch(f"/api/projects/{pid}/rule-candidates/cand_0000000000000000", json={"reset": True}).status_code == 403
    assert live.raw.post(f"/api/projects/{pid}/rule-candidates/apply").status_code == 403
    evil = {"Origin": "http://evil.example"}
    assert live.http.put(f"/api/projects/{pid}/spec-source", json={"path": str(specs)}, headers=evil).status_code == 403


def test_the_source_folder_can_disappear_after_it_was_set(live, sample_dxf, tmp_path):
    folder = tmp_path / "gone"
    write(folder, "A.md", A)
    pid, _ = make_project(live, sample_dxf)
    set_source(live, pid, folder)
    for p in folder.iterdir():
        p.unlink()
    folder.rmdir()
    assert live.http.get(f"/api/projects/{pid}/spec-source").json()["exists"] is False
    r = live.http.post(f"/api/projects/{pid}/spec-scan", json={"wait": True})
    assert r.status_code == 400 and r.json()["error"]["code"] == "SOURCE_NOT_FOUND"


def test_an_unreadable_folder_is_refused(live, sample_dxf, specs, monkeypatch):
    pid, _ = make_project(live, sample_dxf)
    real = os.scandir

    def denied(path="."):
        if str(path).startswith(str(specs.resolve())):
            raise PermissionError(13, "denied")
        return real(path)
    monkeypatch.setattr(os, "scandir", denied)
    r = put_path(live, pid, str(specs))
    assert r.status_code == 400 and r.json()["error"]["code"] == "SOURCE_DENIED"


def test_too_many_files_stop_the_scan(live_factory, sample_dxf, tmp_path):
    live = live_factory(max_spec_files=3)
    folder = tmp_path / "many"
    for i in range(5):
        write(folder, f"{i}.md", f"通信線纜架與電力線纜架之水平淨距不得小於 {300 + i} mm。\n")
    pid, _ = make_project(live, sample_dxf)
    set_source(live, pid, folder)
    state = live.http.post(f"/api/projects/{pid}/spec-scan", json={"wait": True}).json()
    assert state["state"] == "failed" and state["code"] == "TOO_MANY_FILES" and "3" in state["error"]


def _make_link(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":                                              # a junction needs no privilege
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        return r.returncode == 0
    return False


def test_links_that_point_out_of_the_folder_are_not_followed(live, sample_dxf, tmp_path):
    outside = tmp_path / "outside"
    write(outside, "secret.md", "SCADA 線纜架與電力線纜架之水平淨距不得小於 999 mm。\n")
    folder = tmp_path / "root"
    write(folder, "inside.md", A)
    if not _make_link(folder / "escape", outside):
        pytest.skip("this machine cannot create a directory link")
    try:
        os.symlink(outside / "secret.md", folder / "secret-link.md")
    except (OSError, NotImplementedError):
        pass
    pid, _ = make_project(live, sample_dxf)
    set_source(live, pid, folder, GLOSSARY)
    s = scan(live, pid)
    assert s["files"] == 1 and s["skipped"]["links"] >= 1
    assert all(c["evidence"]["document"] == "A.md" or c["evidence"]["document"] == "inside.md"
               for c in candidates(live, pid)["items"])
    assert not any("999" in c["evidence"]["text"] for c in candidates(live, pid)["items"])


def test_the_users_files_are_never_modified(live, sample_dxf, specs):
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in specs.rglob("*") if p.is_file()}
    names_before = sorted(str(p) for p in specs.rglob("*"))
    pid, _ = make_project(live, sample_dxf)
    set_source(live, pid, specs, GLOSSARY)
    scan(live, pid)
    scan(live, pid)
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in specs.rglob("*") if p.is_file()} == before
    assert sorted(str(p) for p in specs.rglob("*")) == names_before


def test_the_default_source_is_used_only_when_the_project_has_none(live_factory, sample_dxf, specs):
    live = live_factory(default_spec_source=str(specs))
    pid, _ = make_project(live, sample_dxf)
    src = live.http.get(f"/api/projects/{pid}/spec-source").json()
    assert src["path"] == str(specs) and src["origin"] == "default" and src["exists"] is True
    assert scan(live, pid)["files"] == 2
    other = specs.parent / "other"
    other.mkdir()
    assert put_path(live, pid, str(other)).json()["origin"] == "project"
    assert put_path(live, pid, None).json()["origin"] == "default"


def test_glossary_text_round_trips_and_changes_the_mapping(live, sample_dxf, specs):
    pid, _ = make_project(live, sample_dxf)
    set_source(live, pid, specs)
    scan(live, pid)
    before = candidates(live, pid)
    assert before["summary"]["usable"] == 1                                 # 給水管 is unknown without a glossary entry ...
    water = [c for c in before["items"] if c["evidence"]["text"].find("給水管") >= 0]
    assert water and all(any(m["code"] == "TARGET_UNMAPPED" for m in c["missing"]) for c in water)
    r = live.http.put(f"/api/projects/{pid}/spec-source", json={"glossary_text": "給水管 = WATER-PIPE, WATER-*\n# comment"})
    assert r.json()["glossary"]["project"] == {"給水管": ["WATER-PIPE", "WATER-*"]}
    assert r.json()["glossary"]["effective"]["電力"]                          # built-in words stay
    after = candidates(live, pid)
    assert after["summary"]["usable"] == 1 and all(not any(m["code"] == "TARGET_UNMAPPED" for m in c["missing"])
                                                   for c in after["items"] if c["conflict"])
    assert live.http.put(f"/api/projects/{pid}/spec-source", json={"glossary_text": 5}).status_code == 400
    assert live.http.put(f"/api/projects/{pid}/spec-source", json={}).status_code == 400


def test_deleting_a_project_removes_its_candidates(live, scanned):
    pid = scanned["pid"]
    assert live.http.delete(f"/api/projects/{pid}").status_code == 200
    conn = live.state.db.conn()
    for table in ("rule_candidates", "candidate_reviews", "spec_files", "spec_settings", "doc_extraction"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


# ---- hand-made rules and candidates -----------------------------------------------------------------------------------------------
def test_a_hand_made_rule_that_says_the_same_runs_once(live, scanned):
    pid = scanned["pid"]
    systems = [{"name": "SCADA", "layer_regex": "^SCADA"}, {"name": "POWER", "layer_regex": "^POWER"}]
    manual = live.http.post(f"/api/projects/{pid}/rules/build", json={
        "template_id": "clearance",
        "params": {"subject": {"system": "SCADA"}, "target": {"system": "POWER"}, "value": 0.3, "unit": "m"}}).json()["rule"]
    live.http.put(f"/api/projects/{pid}/rules", json={"systems": systems, "rules": [manual]})
    out = live.http.post(f"/api/projects/{pid}/rule-candidates/apply").json()
    assert out["applied"] == 0 and out["manual"] == 1 and "已經涵蓋" in out["skipped"][0]["reason"]
    assert out["gate"]["will_run"] == 1 and out["gate"]["from_spec"] == 0       # 0.3 m = 300 mm, same objects: not counted twice
    assert [r["id"] for r in live.http.get(f"/api/projects/{pid}/rules").json()["ruleset"]["rules"]] == [manual["id"]]


def test_a_different_hand_made_rule_does_not_hide_the_candidate(live, scanned):
    pid = scanned["pid"]
    manual = live.http.post(f"/api/projects/{pid}/rules/build", json={
        "template_id": "clearance",
        "params": {"subject": {"layer": "SCADA-CABLE"}, "target": {"layer": "POWER-CABLE"}, "value": 400, "unit": "mm"}}).json()["rule"]
    live.http.put(f"/api/projects/{pid}/rules", json={"systems": [], "rules": [manual]})
    out = live.http.post(f"/api/projects/{pid}/rule-candidates/apply").json()
    assert out["applied"] == 1 and out["skipped"] == [] and out["gate"]["will_run"] == 2


def test_importing_a_file_by_hand_does_not_create_candidates_until_the_scan(live, sample_dxf):
    pid, _ = make_project(live, sample_dxf)
    live.http.post(f"/api/projects/{pid}/documents", files={"file": ("manual.md", A.encode("utf-8"))})
    assert candidates(live, pid)["total"] == 0
    r = live.http.post(f"/api/projects/{pid}/spec-scan", json={"wait": True}).json()     # no folder: reads what was imported
    assert r["state"] == "done" and r["summary"]["extracted_documents"] == 1 and r["summary"]["files"] == 0
    assert candidates(live, pid)["total"] == 4


def test_the_demo_project_is_not_changed_by_any_of_this(live):
    pid = live.http.post("/api/demo").json()["project"]["id"]
    assert candidates(live, pid)["total"] == 0
    rules_before = live.http.get(f"/api/projects/{pid}/rules").json()["ruleset"]
    assert live.http.post(f"/api/projects/{pid}/rule-candidates/apply").json()["changed"] is False
    assert live.http.get(f"/api/projects/{pid}/rules").json()["ruleset"] == rules_before
