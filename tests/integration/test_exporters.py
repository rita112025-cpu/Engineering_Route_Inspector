from __future__ import annotations

import csv
import io
import json
import re

import ezdxf
import pytest

from exporters import service
from exporters.report import ExportError, build_report
from persistence.storage import sha256_file
from .conftest import ruleset, write_sample_dxf

EVIDENCE_QUOTE = "水平淨距不得小於 300 mm"


@pytest.fixture
def done(env, sample_dxf):
    """A completed run over the sample drawing, with a linked evidence chunk."""
    from core.models.results import EvidenceChunk
    p = env.repo.create_project("匯出測試專案")
    env.storage.project_dir(p["id"], create=True)
    d = env.import_dxf(p, sample_dxf, "plan.dxf")
    ev = EvidenceChunk("EV-TEST0001", "x", "spec.md", 1, 4, 5, "4.2 間距", EVIDENCE_QUOTE + "。", "h")
    env.repo.add_document(p["id"], "spec.md", "h_spec.md", "md", "e" * 64, 10, [ev], [], document_id="doc_0123456789abcdef")
    rule = {"id": "CLR", "name": "SCADA/POWER 水平淨距", "subject": {"system": "SCADA"}, "target": {"system": "POWER"},
            "measurement": "horizontal_clearance", "operator": ">=", "value": 300, "warn_margin": 50,
            "evidence_ref": {"document": "spec.md", "quote": EVIDENCE_QUOTE}}
    rs = ruleset(rule)
    env.repo.save_ruleset(p["id"], rs)
    run = env.run_in_process(p, d, rs)
    return p, d, run


def test_sample_run_has_all_four_outcomes(done):
    _, _, run = done
    assert run["summary"]["counts"] == {"FAIL": 1, "WARNING": 1, "PASS": 1, "UNKNOWN": 1}


def test_report_collects_evidence_and_rules(env, done):
    _, _, run = done
    rep = build_report(env.repo, run["id"])
    assert len(rep.issues) == 4 and set(rep.evidence) == {"EV-TEST0001"} and "CLR" in rep.rules
    fail = rep.issues_by_status("FAIL")[0]
    assert rep.measured_text(fail) == "250 mm" and rep.required_text(fail) == ">= 300 mm"
    assert rep.evidence_ref_text(fail) == "spec.md 第1頁 第4-5行"


def test_csv_export(env, done):
    p, d, run = done
    row = service.export_run(env.repo, env.storage, run["id"], "csv")
    path, name, ctype = service.export_file(env.repo, env.storage, row["id"])
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and ctype.startswith("text/csv")
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    assert len(rows) == 1 + 4 and rows[0][0] == "問題編號"
    head = rows[0]
    fail = next(r for r in rows[1:] if r[head.index("狀態")] == "不合格")
    assert fail[head.index("實測值")] == "250" and fail[head.index("規則要求")] == ">= 300 mm"
    assert fail[head.index("信心")] == "已確認" and fail[head.index("證據出處")] == "spec.md 第1頁 第4-5行"
    assert fail[head.index("圖層")] == "SCADA-CABLE" and len(fail[head.index("Handle")].split(",")) == 2
    unknown = next(r for r in rows[1:] if r[head.index("狀態")] == "無法判定")
    assert unknown[head.index("實測值")] == "" and unknown[head.index("信心")] == "無法判定"
    assert row["sha256"] == sha256_file(path) and row["format"] == "csv"
    assert path.parent == env.storage.area(p["id"], "exports")


def test_csv_neutralises_formula_injection(env, ef):
    hostile = '=HYPERLINK("http://x","y")'              # a cell starting with = / + / - / @ is a formula in Excel
    ents = [ef.line(hostile + "SCADA", (0, 0), (5000, 0)), ef.line("POWER-1", (0, 250), (5000, 250))]
    rule = {"id": "CLR", "name": "+cmd|' /C calc'!A0", "subject": {"layer_contains": "SCADA"},
            "target": {"layer_regex": "^POWER"}, "measurement": "horizontal_clearance", "operator": ">=", "value": 300}
    p, d, rs = env.project_with_drawing(ents, rules=ruleset(rule))
    run = env.run_in_process(p, d, rs)
    row = service.export_run(env.repo, env.storage, run["id"], "csv")
    text = service.export_file(env.repo, env.storage, row["id"])[0].read_text(encoding="utf-8-sig")
    cells = [c for r in csv.reader(io.StringIO(text)) for c in r]
    assert "'" + hostile + "SCADA" in cells and "'+cmd|' /C calc'!A0" in cells     # kept, but defused
    for cell in cells:
        assert not re.match(r"^[=+@\t\r]", cell), cell
    from exporters.csv_export import safe_cell
    assert safe_cell("=1+1") == "'=1+1" and safe_cell("-5 mm") == "'-5 mm" and safe_cell("普通") == "普通"
    assert safe_cell(None) == ""


def test_markdown_export(env, done):
    _, _, run = done
    row = service.export_run(env.repo, env.storage, run["id"], "markdown")
    path, name, ctype = service.export_file(env.repo, env.storage, row["id"])
    md = path.read_text(encoding="utf-8")
    assert name.endswith("_report.md") and ctype.startswith("text/markdown")
    assert "# 工程管線檢查報告：plan.dxf" in md
    assert "| 1 | 1 | 1 | 1 |" in md                                   # fail / warn / unknown / pass
    fail = build_report(env.repo, run["id"]).issues_by_status("FAIL")[0]
    assert f"#### {fail['issue_id']}" in md
    assert "| 實測值 | 250 mm |" in md and "| 規則要求 | &gt;= 300 mm |" in md      # ">" is escaped
    assert "> 水平淨距不得小於 300 mm。" in md and "spec.md 第1頁 第4-5行" in md
    assert "Handle" in md and "圖層" in md


def test_markdown_escapes_user_text():
    from exporters.markdown import esc
    assert esc("a|b") == "a\\|b" and "<script>" not in esc("<script>") and esc("x\ny") == "x<br>y"
    assert esc("[a](http://x)") == "\\[a\\](http://x)"


def test_html_export_is_standalone_and_safe(env, ef):
    evil = "</script><img src=x onerror=alert(1)>"
    p = env.repo.create_project("專案 </title><script>alert(1)</script>")
    ents = [ef.line("SCADA" + evil, (0, 0), (5000, 0)), ef.line("POWER-1", (0, 250), (5000, 250)),
            ef.line("SCADA-2", (0, 3000), (5000, 3000)), ef.line("POWER-2", (0, 3500), (5000, 3500))]
    rule = {"id": "CLR", "name": "規則 " + evil, "subject": {"system": "SCADA"}, "target": {"system": "POWER"},
            "measurement": "horizontal_clearance", "operator": ">=", "value": 300,
            "evidence_ref": {"quote": "300 mm"}}
    p, d, rs = env.project_with_drawing(ents, rules=ruleset(rule), logical_name="x.dxf", project=p)
    from core.models.results import EvidenceChunk
    env.repo.add_document(p["id"], "spec.txt", "h_spec.txt", "txt", "e" * 64, 5,
                          [EvidenceChunk("EV-EVIL", "x", "spec.txt", 1, 1, 1, "", "不得小於 300 mm " + evil, "h")], [])
    run = env.run_in_process(p, d, rs)
    row = service.export_run(env.repo, env.storage, run["id"], "html")
    path, _, ctype = service.export_file(env.repo, env.storage, row["id"])
    page = path.read_text(encoding="utf-8")
    assert ctype.startswith("text/html")
    # nothing hostile is live markup
    assert "<img" not in page and "<script>alert" not in page
    assert page.count("<script") == 2 and page.count("</script>") == 2         # data block + page script only
    assert "<title>工程管線檢查報告 – x.dxf</title>" in page
    # no external resources of any kind
    assert re.search(r"""(?:src|href|action)\s*=\s*["']?\s*(?:https?:)?//""", page) is None
    assert "@import" not in page and "url(" not in page and "fetch(" not in page and "XMLHttpRequest" not in page
    assert "default-src 'none'" in page
    data = json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>', page, re.S).group(1))
    assert len(data["issues"]) == 2
    assert any(evil in i["layer"] for i in data["issues"])                      # present only as data
    assert page.count("<polyline") >= 4                                         # drawing is embedded as SVG
    assert "textContent" in page and "innerHTML" not in page


def test_html_contains_required_fields_for_each_issue(env, done):
    _, _, run = done
    row = service.export_run(env.repo, env.storage, run["id"], "html")
    page = service.export_file(env.repo, env.storage, row["id"])[0].read_text(encoding="utf-8")
    data = json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>', page, re.S).group(1))
    fail = next(i for i in data["issues"] if i["status"] == "FAIL")
    assert fail["handles"] and fail["layer"] == "SCADA-CABLE" and fail["measured"] == "250 mm"
    assert fail["required"] == ">= 300 mm" and fail["rule"] == "CLR"
    assert fail["evidence"]["text"].startswith(EVIDENCE_QUOTE) and fail["x"] is not None


def test_annotated_dxf_export(env, done):
    p, d, run = done
    source = env.storage.file_path(p["id"], "drawings", d["stored_name"])
    before = sha256_file(source)
    row = service.export_run(env.repo, env.storage, run["id"], "dxf")
    out, name, _ = service.export_file(env.repo, env.storage, row["id"])
    assert sha256_file(source) == before and out != source                    # source untouched
    assert name.endswith("_annotated.dxf") and out.parent == env.storage.area(p["id"], "exports")
    orig, annotated = ezdxf.readfile(source), ezdxf.readfile(out)
    # every non-PASS issue adds a cross (2 lines) and, for pair issues, one closest-point line
    assert len(annotated.modelspace().query("LINE")) > len(orig.modelspace().query("LINE"))
    layers = {l.dxf.name for l in annotated.layers}
    assert {"ERI_FAIL", "ERI_WARNING", "ERI_UNKNOWN"} <= layers
    assert len(annotated.modelspace().query('CIRCLE[layer=="ERI_FAIL"]')) == 1
    assert len(annotated.modelspace().query('CIRCLE[layer=="ERI_WARNING"]')) == 1
    assert len(annotated.modelspace().query('CIRCLE[layer=="ERI_UNKNOWN"]')) == 1
    assert len(annotated.modelspace().query('CIRCLE[layer=="ERI_PASS"]')) == 0      # PASS is not marked
    texts = [t.dxf.text for t in annotated.modelspace().query("TEXT")]
    assert any("FAIL" in t and "250" in t and ">=300" in t for t in texts)
    assert all(ord(ch) < 128 for t in texts for ch in t)
    # original entities all still there
    assert {e.dxf.handle for e in orig.modelspace()} <= {e.dxf.handle for e in annotated.modelspace()}


def test_annotated_dxf_missing_source_is_a_readable_error(env, done):
    p, d, run = done
    env.storage.file_path(p["id"], "drawings", d["stored_name"]).unlink()
    with pytest.raises(ExportError, match="原始檔"):
        service.export_run(env.repo, env.storage, run["id"], "dxf")
    assert list(env.storage.area(p["id"], "exports").glob(".export-*")) == []     # no temp leftovers


def test_export_errors(env, done):
    p, d, run = done
    with pytest.raises(ExportError, match="格式"):
        service.export_run(env.repo, env.storage, run["id"], "pdf")
    with pytest.raises(ExportError, match="找不到"):
        service.export_run(env.repo, env.storage, "run_0000000000000000", "csv")
    queued = env.repo.create_run(p["id"], d, env.repo.get_ruleset(p["id"]), None)
    with pytest.raises(ExportError, match="尚未完成"):
        service.export_run(env.repo, env.storage, queued["id"], "csv")
    row = service.export_run(env.repo, env.storage, run["id"], "csv")
    service.export_file(env.repo, env.storage, row["id"])[0].unlink()
    with pytest.raises(ExportError, match="已不存在"):
        service.export_file(env.repo, env.storage, row["id"])
    with pytest.raises(ExportError, match="找不到"):
        service.export_file(env.repo, env.storage, "exp_0000000000000000")


def test_export_file_refuses_a_tampered_path(env, done):
    _, _, run = done
    row = service.export_run(env.repo, env.storage, run["id"], "csv")
    env.conn.execute("UPDATE exports SET path = ? WHERE id = ?", ("projects/../../eri.sqlite3", row["id"]))
    with pytest.raises(ExportError):
        service.export_file(env.repo, env.storage, row["id"])
    env.conn.execute("UPDATE exports SET path = ? WHERE id = ?", ("projects/p_0123456789abcdef/exports/../../../x", row["id"]))
    with pytest.raises(ExportError):
        service.export_file(env.repo, env.storage, row["id"])


def test_reexport_overwrites_the_same_file_and_is_audited(env, done):
    _, _, run = done
    a = service.export_run(env.repo, env.storage, run["id"], "csv")
    b = service.export_run(env.repo, env.storage, run["id"], "csv")
    assert a["path"] == b["path"] and a["id"] != b["id"]
    assert [e["format"] for e in env.repo.list_exports(run["id"])] == ["csv", "csv"]
    assert sum(1 for x in env.repo.recent_audit() if x["operation"] == "export.create") == 2


def test_export_name_is_safe():
    n = service.export_name("..\\..\\evil<>.dxf", "run_0123456789abcdef", "html")
    assert n == "evil__89abcdef_report.html" or n.endswith("_89abcdef_report.html")
    assert "/" not in n and "\\" not in n and ".." not in n
