"""Review #10 (19cdd49 / 8d5b87c): U8-1 diagnostics privacy, U8-2 README command, U8-3 confidence wording,
S-3 cross-site downloads, S-4 quick check, S-5 version limits. Each test failed before its fix."""
from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import pytest

from app import diagnostics
from core.rules.schema import normalize_ruleset
from jobs import manager as M
from tests.integration.conftest import SYSTEMS

ROOT = Path(__file__).resolve().parents[2]
ODD_NAMES = ["ZZ's secret plan.dxf", "ZZ secret spec.md"]


def bundle_texts(live) -> dict[str, str]:
    r = live.http.get("/api/diagnostics/bundle")
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    return {n: z.read(n).decode("utf-8", errors="replace") for n in z.namelist()}


# -- U8-1 (a): file names with spaces and quotes -----------------------------------------------------

def test_u81a_scrubber_hides_whole_names_with_spaces_and_quotes(tmp_path):
    root = str((tmp_path / "data").resolve())
    names = ["ZZ's secret plan.dxf", "ab12cd34ef56ab12_ZZ's secret plan.dxf", "spec secret.pdf"]
    scrub = diagnostics.scrubber(tmp_path / "data", names)
    for n in names:
        out = scrub(f"cannot read {root}/drawings/{n} (also '{n}')")
        assert "secret" not in out and "ZZ" not in out and "plan.dxf" not in out, out
    # a name that is no longer in the database: the pattern still stops at the extension, not at the first space
    out = diagnostics.scrubber(tmp_path / "data")(f"x {root}/projects/p_0123456789abcdef/documents/old secret notes.docx failed")
    assert "secret" not in out and "notes.docx" not in out, out        # hidden to the end of the line (R11)


def test_u81a_bundle_has_no_part_of_a_stored_file_name(live, sample_dxf):
    pid = live.http.post("/api/projects", json={"name": "診斷專案"}).json()["id"]
    did = live.http.post(f"/api/projects/{pid}/drawings",
                         files={"file": (ODD_NAMES[0], sample_dxf.read_bytes())}).json()["id"]
    live.http.post(f"/api/projects/{pid}/documents", files={"file": (ODD_NAMES[1], b"4.2 distance 300 mm")})
    repo = live.app.state.eri.repo()
    root = str(live.config.data_dir.resolve())
    run = repo.create_run(pid, repo.get_drawing(did), repo.get_ruleset(pid), None)
    stored = repo.get_drawing(did)["stored_name"]
    repo.finish_run(run["id"], "failed", error_code="WORKER_CRASHED",
                    error_message=f"crashed reading {root}/drawings/{stored} twice")
    for name, text in bundle_texts(live).items():
        assert "secret" not in text.lower() and "plan.dxf" not in text, name


# -- U8-1 (b): drawing content through exception text ------------------------------------------------

BAD_DXF = b"0\nSECTION\n2\nHEADER\n9\n$ACADVER\n1\nAC1032\nABC-SECRET-GROUPCODE\nfoo\n0\nENDSEC\n0\nEOF\n"


def test_u81b_corrupt_drawing_content_does_not_reach_the_bundle(live):
    pid = live.http.post("/api/projects", json={"name": "p"}).json()["id"]
    r = live.http.post(f"/api/projects/{pid}/drawings", files={"file": ("bad.dxf", BAD_DXF)})
    assert r.status_code == 400
    texts = bundle_texts(live)
    for name, text in texts.items():
        assert "ABC-SECRET-GROUPCODE" not in text, name
    assert "drawing import failed" in texts["server.log"], "the failure itself must still be diagnosable"


def test_u81b_exception_lines_in_logs_keep_the_class_and_drop_the_message():
    text = ("Traceback (most recent call last):\n  File \"x.py\", line 1, in f\n    g()\n"
            "ezdxf.lldxf.const.DXFStructureError: Invalid group code \"ZZ-SECRET\" at line 9.\n"
            "KeyError: 'ZZ-LAYER'\n"
            "INFO eri drawing import failed: DXFStructureError('Invalid group code \"ZZ-SECRET\"')\n"
            "re.error: nothing to repeat at position 0 in 'ZZ-PATTERN'\n")
    out = diagnostics.redact_exceptions(text)
    assert "ZZ-" not in out
    for cls in ("DXFStructureError", "KeyError", "re.error"):
        assert cls in out
    assert "File \"x.py\", line 1, in f" in out


def test_u81b_worker_log_and_failed_run_text_are_redacted(live, sample_dxf):
    st = live.app.state.eri
    pid = live.http.post("/api/projects", json={"name": "p"}).json()["id"]
    did = live.http.post(f"/api/projects/{pid}/drawings", files={"file": ("plan.dxf", sample_dxf.read_bytes())}).json()["id"]
    repo = st.repo()
    run = repo.create_run(pid, repo.get_drawing(did), repo.get_ruleset(pid), None)
    repo.finish_run(run["id"], "failed", error_code="ANALYSIS_ERROR",
                    error_message="分析過程發生錯誤：KeyError: 'ZZ-SECRET-LAYER'")
    (live.config.data_dir / "logs" / f"{run['id']}.log").write_text(
        "Traceback (most recent call last):\nKeyError: 'ZZ-SECRET-LAYER'\n", encoding="utf-8")
    texts = bundle_texts(live)
    for name, text in texts.items():
        assert "ZZ-SECRET" not in text, name
    failed = json.loads(texts["diagnostics.json"])["recent_failed_runs"][0]["message"]
    assert "KeyError" in failed


# -- U8-1 (c): rule ids in failure messages ----------------------------------------------------------

def test_u81c_pipeline_reports_position_with_the_rule(ef):
    from core.analysis.pipeline import run_analysis
    notes = []
    rules = [{"id": f"R{i}", "name": "n", "subject": {"system": "SCADA"}, "measurement": "entity_count",
              "operator": ">=", "value": 1} for i in range(2)]
    rs = normalize_ruleset({"systems": SYSTEMS, "rules": rules})
    run_analysis([ef.line("SCADA", (0, 0), (1, 0))], rs, drawing_key="t.dxf",
                 progress=lambda stage, frac, note: notes.append((stage, note)))
    rule_notes = [n for s, n in notes if s == "rules" and n]
    assert "R0（第 1/2 條）" in rule_notes and "R1（第 2/2 條）" in rule_notes


@pytest.mark.parametrize("stage_note,expect_in", [("RULE-SECRET-ID（第 2/5 條）", "第 2/5 條"), ("", "")])
def test_u81c_failure_messages_name_the_position_not_the_rule_id(env, ef, stage_note, expect_in):
    from datetime import datetime, timedelta, timezone
    p, d, rs = env.project_with_drawing([ef.line("SCADA", (0, 0), (1, 0))])
    run = env.repo.create_run(p["id"], d, rs, None)
    import os
    env.repo.claim_run(run["id"], os.getpid(), None)
    env.conn.execute("UPDATE runs SET heartbeat_at = ?, stage = 'rules', stage_note = ? WHERE id = ?",
                     ((datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat(), stage_note, run["id"]))
    m = M.JobManager(env.storage.data_dir, stall_timeout=600, progress_timeout=30)

    class Fake(M.Tracked):
        def exit_code(self):
            return None

        def kill(self):
            pass
    m.tracked[run["id"]] = Fake(run["id"], 1, log_path=m.logs_dir / f"{run['id']}.log")
    try:
        stall = m._stall_message(run["id"])                # before the run is finished and its stage overwritten
        assert "SECRET" not in stall and expect_in in stall, stall
        m._enforce_cancel()
        msg = env.repo.get_run(run["id"])["error_message"]
        assert "RULE-SECRET-ID" not in msg and "沒有任何進度" in msg and expect_in in msg, msg
    finally:
        m.tracked.clear()
        m.stop()


# -- U8-2 / S-1 / S-2: README ------------------------------------------------------------------------

def test_u82_readme_commands_can_be_run_as_written():
    lines = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    for line in lines:
        if "--diagnose" in line and "python -m app" in line:
            assert "PYTHONPATH" in line or "start-ui" in line, line


def test_s1_readme_test_counts_match_the_suite():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"(\d+) passed（單元 (\d+)、整合 (\d+)、回歸 (\d+)、安全 (\d+)、效能 (\d+)、瀏覽器 (\d+)）", text)
    assert m, "README must state the counts in this exact shape"
    stated = list(map(int, m.groups()))
    import subprocess, sys
    counts = []
    for marker in ("unit", "integration", "regression", "security", "performance", "browser"):
        out = subprocess.run([sys.executable, "-m", "pytest", "-m", marker, "--collect-only", "-q", "-p", "no:logging"],
                             capture_output=True, text=True, cwd=ROOT, timeout=120).stdout
        counts.append(int(re.search(r"(\d+)/(\d+) tests collected", out).group(1)))
    assert stated[1:] == counts and stated[0] == sum(counts), (stated, counts)


def test_s2_readme_does_not_claim_automated_mutation_tests_that_do_not_exist():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "移除防護後測試必須失敗" not in text


# -- U8-3: confidence wording ------------------------------------------------------------------------

def test_u83_non_numeric_rules_do_not_claim_a_number_was_compared(ef):
    from core.analysis.pipeline import run_analysis
    from core.models.results import EvidenceChunk
    chunk = EvidenceChunk(id="ev_1", document_id="d", filename="s.md", page=1, line_start=1, line_end=1,
                          section="", text="弱電電纜不得進入機房禁設區。", hash="h")
    rules = [{"id": "ZONE", "name": "z", "subject": {"system": "SCADA"}, "zone": {"layer_equals": "ZONE"},
              "measurement": "outside_zone", "evidence_id": "ev_1"},
             {"id": "CLR", "name": "c", "subject": {"system": "SCADA"}, "target": {"system": "POWER"},
              "measurement": "horizontal_clearance", "operator": ">=", "value": 300, "unit": "mm", "evidence_id": "ev_2"}]
    chunk2 = EvidenceChunk(id="ev_2", document_id="d", filename="s.md", page=1, line_start=2, line_end=2,
                           section="", text="水平淨距不得小於 300 mm。", hash="h")
    ents = [ef.line("SCADA", (0, 0), (5000, 0)), ef.line("POWER", (0, 200), (5000, 200)),
            ef.rect("ZONE", 0, 5000, 100, 5100)]
    out = run_analysis(ents, normalize_ruleset({"systems": SYSTEMS, "rules": rules}), drawing_key="t.dxf",
                       evidence=[chunk, chunk2])
    by_rule = {r.rule_id: r for r in out.results}
    zone_reason = " ".join(by_rule["ZONE"].details["confidence_reasons"])
    clr_reason = " ".join(by_rule["CLR"].details["confidence_reasons"])
    assert "相同的數值與單位" not in zone_reason and "沒有數值可與原文比對" in zone_reason
    assert "相同的數值與單位" in clr_reason


# -- S-3 / S-4 / S-5 ---------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/api/diagnostics", "/api/diagnostics/bundle", "/api/projects"])
def test_s3_cross_site_navigation_to_api_is_refused(live, path):
    assert live.http.get(path, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert live.http.get(path, headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert live.http.get(path, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert live.http.get(path, headers={"Sec-Fetch-Site": "none"}).status_code == 200
    assert live.http.get(path).status_code == 200            # command-line clients send no such header


def test_s4_dialog_uses_the_quick_check_and_the_command_the_full_one(live, tmp_path):
    quick = live.http.get("/api/diagnostics").json()
    assert any(c["name"] == "資料庫完整性" and "快速檢查" in c["detail"] for c in quick["checks"])
    full = diagnostics.collect(live.config.data_dir, deep=True)
    assert any(c["name"] == "資料庫完整性" and "完整檢查" in c["detail"] for c in full["checks"])


def test_s5_every_requirement_has_an_upper_bound():
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            assert "<" in line, line
