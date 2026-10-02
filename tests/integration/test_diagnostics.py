"""Diagnostics: real server, real database, real files. Nothing about project content or absolute paths may leak."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

from app import diagnostics

SECRET_NAME = "機密案名-秘密廠區.dxf"
SECRET_TEXT = "秘密規範內容XYZ"


def test_collect_reports_healthy_data_folder(live):
    live.http.post("/api/projects", json={"name": "診斷專案"})
    d = live.http.get("/api/diagnostics").json()
    assert d["healthy"] is True, d["checks"]
    assert d["schema_version"] >= 2
    assert d["counts"]["projects"] == 1
    names = [c["name"] for c in d["checks"]]
    assert "資料庫完整性" in names and "背景分析程式運作中" in names
    assert d["packages"]["ezdxf"]


def test_diagnostics_hides_paths_and_project_content(live, sample_dxf):
    pid = live.http.post("/api/projects", json={"name": "診斷專案"}).json()["id"]
    live.http.post(f"/api/projects/{pid}/drawings", files={"file": (SECRET_NAME, sample_dxf.read_bytes())})
    live.http.post(f"/api/projects/{pid}/documents", files={"file": ("spec.md", f"{SECRET_TEXT} 300 mm".encode())})
    body = live.http.get("/api/diagnostics").text
    data_dir = str(live.config.data_dir.resolve())
    assert data_dir not in body and data_dir.replace("\\", "/") not in body
    assert SECRET_NAME not in body and SECRET_TEXT not in body


def test_bundle_is_a_zip_without_paths_or_content(live, sample_dxf):
    pid = live.http.post("/api/projects", json={"name": "診斷專案"}).json()["id"]
    live.http.post(f"/api/projects/{pid}/drawings", files={"file": (SECRET_NAME, sample_dxf.read_bytes())})
    live.http.post(f"/api/projects/{pid}/documents", files={"file": ("spec.md", f"{SECRET_TEXT} 300 mm".encode())})
    r = live.http.get("/api/diagnostics/bundle")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert {"README.txt", "diagnostics.json"} <= set(z.namelist())
    assert z.testzip() is None
    data_dir = str(live.config.data_dir.resolve())
    for name in z.namelist():
        text = z.read(name).decode("utf-8", errors="replace")
        assert data_dir not in text, name
        assert SECRET_NAME not in text and SECRET_TEXT not in text, name
    json.loads(z.read("diagnostics.json"))


def test_scrubber_replaces_roots_in_every_spelling(tmp_path):
    scrub = diagnostics.scrubber(tmp_path / "data")
    root = str((tmp_path / "data").resolve())
    text = f"open {root}/drawings/abc_x.dxf and {root.replace('/', chr(92))}\\exports\\r.csv failed"
    out = scrub(text)
    assert root not in out and "<data>" in out
    assert "abc_x.dxf" not in out and "r.csv" not in out


def test_failed_run_message_is_scrubbed(live, sample_dxf):
    st = live.app.state.eri
    root = str(live.config.data_dir.resolve())
    pid = live.http.post("/api/projects", json={"name": "p"}).json()["id"]
    did = live.http.post(f"/api/projects/{pid}/drawings", files={"file": ("plan.dxf", sample_dxf.read_bytes())}).json()["id"]
    repo = st.repo()
    run = repo.create_run(pid, repo.get_drawing(did), repo.get_ruleset(pid), None)
    repo.finish_run(run["id"], "failed", error_code="ANALYSIS_ERROR",
                    error_message=f"cannot read {root}/drawings/s_secret.dxf")
    d = live.http.get("/api/diagnostics").json()
    msg = d["recent_failed_runs"][0]["message"]
    assert root not in msg and "secret.dxf" not in msg and "<data>" in msg


def test_offline_diagnose_on_missing_folder_reports_problem(tmp_path):
    info = diagnostics.collect(tmp_path / "nothing-here")
    assert info["healthy"] is False
    assert any(c["name"] == "資料資料夾存在" and not c["ok"] for c in info["checks"])


def test_cli_diagnose_exit_codes(tmp_path, live):
    src = str(Path(__file__).resolve().parents[2] / "src")
    ok = subprocess.run([sys.executable, "-m", "app", "--diagnose", "--json", "--data-dir", str(live.config.data_dir)],
                        capture_output=True, text=True, env={"PYTHONPATH": src, "PATH": ""}, timeout=60)
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["healthy"] is True
    bad = subprocess.run([sys.executable, "-m", "app", "--diagnose", "--data-dir", str(tmp_path / "missing")],
                         capture_output=True, text=True, env={"PYTHONPATH": src, "PATH": ""}, timeout=60)
    assert bad.returncode == 1 and "[問題] 資料資料夾存在" in bad.stdout and "有項目需要注意" in bad.stdout
    text = subprocess.run([sys.executable, "-m", "app", "--diagnose", "--data-dir", str(live.config.data_dir)],
                          capture_output=True, text=True, env={"PYTHONPATH": src, "PATH": ""}, timeout=60)
    assert text.returncode == 0 and "結果：一切正常" in text.stdout and "[正常] 資料庫完整性" in text.stdout
