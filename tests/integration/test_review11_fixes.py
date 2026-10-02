"""Review #11 (9638c90): R11-1 unknown DXF entity -> 500 and an orphan file, R11-2 dev requirements, R11-3 URI
quoting, R11-4 duplicate diagnose output, plus the redaction gaps the reviewer listed. Each test failed before its fix."""
from __future__ import annotations

import re
from pathlib import Path

import ezdxf
import pytest

from app import diagnostics
from persistence.db import open_database

ROOT = Path(__file__).resolve().parents[2]


def stored_files(live, pid):
    folder = live.config.data_dir / "projects" / pid / "drawings"
    return sorted(p.name for p in folder.glob("*")) if folder.is_dir() else []


def dxf_with_unknown_entity(path: Path, new_type: str) -> Path:
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_line((0, 0), (1000, 0), dxfattribs={"layer": "SCADA-CABLE"})
    msp.add_line((0, 200), (1000, 200), dxfattribs={"layer": "POWER-CABLE"})
    doc.saveas(path)
    text = path.read_text(encoding="utf-8")
    start = text.index("ENTITIES")
    i = text.index("\nLINE\n", start)                      # the first entity's type line
    path.write_text(text[:i] + f"\n{new_type}\n" + text[i + len("\nLINE\n"):], encoding="utf-8")
    return path


# -- R11-1 ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("new_type", ["FOOBAR", "ACAD_ZOMBIE_ENTITY"])
def test_r111_unknown_entity_type_is_skipped_not_a_server_error(live, tmp_path, new_type):
    pid = live.http.post("/api/projects", json={"name": "p"}).json()["id"]
    path = dxf_with_unknown_entity(tmp_path / "odd.dxf", new_type)
    r = live.http.post(f"/api/projects/{pid}/drawings", files={"file": ("odd.dxf", path.read_bytes())})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["entity_count"] == 1, body                      # the intact second line
    assert sum(body["info"]["skipped"].values()) == 1 and new_type in body["info"]["skipped"], body["info"]
    assert len(stored_files(live, pid)) == 1


def test_r111_any_failure_while_importing_leaves_no_orphan_file(live, sample_dxf, monkeypatch):
    import app.api as api
    pid = live.http.post("/api/projects", json={"name": "p"}).json()["id"]

    def boom(*a, **k):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(api, "load_dxf", boom)
    r = live.http.post(f"/api/projects/{pid}/drawings", files={"file": ("plan.dxf", sample_dxf.read_bytes())})
    assert r.status_code == 500
    assert stored_files(live, pid) == []
    assert live.app.state.eri.repo().list_drawings(pid) == []   # (not over HTTP: uvicorn closes the connection after an unhandled error)


# -- R11-2 ---------------------------------------------------------------------------------------------

def test_r112_dev_requirements_have_the_test_tools():
    lines = [x.strip() for x in (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()]
    names = {re.split(r"[<>=!~]", x)[0] for x in lines if x and not x.startswith(("#", "-"))}
    assert {"pytest", "httpx", "playwright"} <= names
    assert "-r requirements.txt" in lines
    for x in lines:
        if x.startswith(("pytest", "httpx", "playwright")):
            assert "<" in x, x


# -- R11-3 ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("folder", ["has#hash", "has?question", "has%percent", "空白 and 中文"])
def test_r113_offline_diagnose_works_in_any_folder_name(tmp_path, folder):
    data = tmp_path / folder
    conn = open_database(data / "eri.sqlite3")
    conn.execute("INSERT INTO projects(id, name, root_path, export_dir, is_demo, created_at, updated_at) "
                 "VALUES ('p_0123456789abcdef','n','r','e',0,'t','t')")
    conn.commit()
    conn.close()
    info = diagnostics.collect(data)
    assert info["healthy"] is True, info["checks"]
    assert info["counts"]["projects"] == 1
    ro = diagnostics._open_ro(data / "eri.sqlite3")
    try:
        assert ro.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
    finally:
        ro.close()


# -- R11-4 ---------------------------------------------------------------------------------------------

def test_r114_launchers_do_not_repeat_the_diagnosis_for_a_diagnose_run():
    sh = (ROOT / "start-ui.sh").read_text(encoding="ascii")
    bat = (ROOT / "start-ui.bat").read_bytes().decode("ascii")
    assert '[ "$a" = "--diagnose" ] && diagnosing=1' in sh and sh.rstrip().endswith('exit "$rc"')
    assert 'if /i "%~1"=="--diagnose" goto done_rc' in bat
    assert re.search(r"^:done_rc\r?$", bat, re.M)     # (the shell script's exit codes are run for real in test_review12_fixes)


# -- redaction gaps listed by the reviewer ---------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "ValueError: first line\nSECRET second line\nSECRET third line\n",
    "ValueError: a\n  SECRET indented continuation\n\nnext paragraph\n",
    "zipfile.BadZipFile: SECRET not a zip\n",
    "StopIteration: SECRET\n",
    "app.errors.Cancelled: SECRET\n",
    "/x/y.py:12: UserWarning: SECRET\n",
    "  | ValueError: SECRET\n",
    "worker said: ValueError: SECRET\n",
    "2026-10-02 12:00:00 WARNING ValueError: SECRET\n",
    "ValueError : SECRET\n",
    "ValueError: SECRET",
])
def test_redaction_gaps_closed(text):
    out = diagnostics.redact_exceptions(text)
    assert "SECRET" not in out, out
    cls = re.search(r"[A-Za-z_.]*(?:Error|Exception|Warning|StopIteration|BadZipFile|Cancelled)", text).group(0)
    assert cls.split(".")[-1] in out, out


def test_redaction_keeps_traceback_frames_and_following_records():
    text = ("Traceback (most recent call last):\n  File \"a.py\", line 3, in f\n    g(x)\nValueError: SECRET\n"
            "2026-10-02 12:00:01,000 INFO POST /api/x -> 200 5 ms\n")
    out = diagnostics.redact_exceptions(text)
    assert 'File "a.py", line 3, in f' in out and "g(x)" in out and "INFO POST /api/x -> 200 5 ms" in out
    assert "SECRET" not in out


def test_traceback_tail_for_the_log_starts_on_a_line_boundary():
    from app.errors import tail_lines
    text = "".join(f"line {i}\n" for i in range(1000))
    tail = tail_lines(text, 500)
    assert len(tail) <= 500 and tail.startswith("line ") and tail.endswith("line 999\n")


def test_old_failure_messages_with_a_rule_id_are_cleaned():
    scrub = diagnostics.scrubber(Path("/nonexistent"))
    old = "分析在「執行規則：RULE-SECRET-ID」超過 60 秒沒有回應，已停止。這通常是這條規則的比對規則（regex）太複雜，請簡化它。"
    out = diagnostics.safe_run_message("WORKER_STALLED", old, scrub)
    assert "RULE-SECRET-ID" not in out and "執行規則" in out and "60 秒" in out


@pytest.mark.parametrize("text", [
    "x /elsewhere/projects/p_0123456789abcdef/documents/ab12_notes.md secret v2.pdf failed",
    "x /elsewhere/projects/p_0123456789abcdef/drawings/1_a.dxf.bak secret",
    "x /elsewhere/projects/p_0123456789abcdef/drawings/old secret plan.dwg",
    "x C:\\elsewhere\\projects\\p_0123456789abcdef\\exports\\run secret.csv done",
])
def test_unknown_names_after_a_storage_folder_are_hidden_to_the_end_of_the_line(text):
    out = diagnostics.scrubber(Path("/data-root"))(text)
    assert "secret" not in out and "<file>" in out, out
