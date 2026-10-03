"""Review of a55bb3a: N12-1 launcher exit code, N12-2 request log lines kept, N12-3 quadratic redaction,
N12-5 do not delete a referenced file, N12-6 logged class. Each test failed before its fix."""
from __future__ import annotations

import shutil
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

import pytest

from app import diagnostics
from persistence.db import open_database

ROOT = Path(__file__).resolve().parents[2]


# -- N12-1 -------------------------------------------------------------------------------------------------

@pytest.fixture
def launcher_box(tmp_path):
    """A copy of start-ui.sh with a private 'venv' that already has the packages (no network, no pip)."""
    if sys.prefix == sys.base_prefix:
        pytest.skip("the tests are not running inside a virtual environment")
    if sys.platform == "win32":
        pytest.skip("start-ui.sh is the POSIX launcher; it needs symlinks and bash (start-ui.bat is covered statically)")
    box = tmp_path / "box"
    box.mkdir()
    shutil.copy(ROOT / "start-ui.sh", box / "start-ui.sh")
    shutil.copy(ROOT / "requirements.txt", box / "requirements.txt")
    (box / "src").symlink_to(ROOT / "src")
    venv = box / ".venv"
    (venv / "bin").mkdir(parents=True)
    shutil.copy(Path(sys.prefix) / "pyvenv.cfg", venv / "pyvenv.cfg")
    (venv / "bin" / "python").symlink_to(Path(sys.executable))
    site = Path(sysconfig.get_paths()["purelib"])
    link = venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    link.parent.mkdir(parents=True)
    link.symlink_to(site)
    shutil.copy(ROOT / "requirements.txt", venv / "requirements.installed")
    return box


def run_launcher(box: Path, *args: str):
    return subprocess.run(["bash", str(box / "start-ui.sh"), *args], capture_output=True, text=True, timeout=120,
                          env={"PATH": "/usr/bin:/bin"})


def healthy_folder(tmp_path) -> Path:
    data = tmp_path / "good"
    open_database(data / "eri.sqlite3").close()
    (data / "logs").mkdir(exist_ok=True)
    return data


def broken_folder(tmp_path) -> Path:
    data = tmp_path / "bad"
    data.mkdir()
    (data / "eri.sqlite3").write_bytes(b"this is not a database" * 200)
    return data


def test_n121_launcher_diagnose_exit_codes_follow_the_result(launcher_box, tmp_path):
    ok = run_launcher(launcher_box, "--diagnose", "--data-dir", str(healthy_folder(tmp_path)))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    bad = run_launcher(launcher_box, "--diagnose", "--data-dir", str(broken_folder(tmp_path)))
    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert "有項目需要注意" in bad.stdout
    assert "stopped with error code" not in bad.stdout            # the self-check is not repeated


def test_n121_diagnose_in_any_argument_position_is_not_repeated(launcher_box, tmp_path):
    bad = run_launcher(launcher_box, "--json", "--diagnose", "--data-dir", str(broken_folder(tmp_path)))
    assert bad.returncode == 1
    assert "stopped with error code" not in bad.stdout and bad.stdout.count('"healthy"') == 1, bad.stdout


# -- N12-2 -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "2026-10-02 12:00:00,000 INFO GET /api/drawings/d_c818b5dbefaab289/geometry -> 200 1 ms",
    "2026-10-02 12:00:00,000 INFO GET /api/exports/exp_0000000000000000/download -> 409 1 ms",
    "2026-10-02 12:00:00,000 INFO GET /api/projects/p_0123456789abcdef/documents/doc_3/chunks -> 500 3 ms",
    "2026-10-02 12:00:00,000 INFO POST /api/projects/p_0123456789abcdef/drawings -> 400 5 ms",
])
def test_n122_request_log_lines_keep_their_status_and_time(tmp_path, line):
    assert diagnostics.scrubber(tmp_path / "data")(line) == line


@pytest.mark.parametrize("sep", ["/", "\\"])
def test_n122_storage_paths_are_still_hidden_to_the_end_of_the_line(tmp_path, sep):
    root = str((tmp_path / "data").resolve())
    parts = ["projects", "p_0123456789abcdef", "documents", "old secret notes.docx failed"]
    text = f"cannot read {root}{sep}" + sep.join(parts)
    out = diagnostics.scrubber(tmp_path / "data")(text)
    assert "secret" not in out and "notes.docx" not in out and "<file>" in out, out
    rel = "x " + sep.join(parts)
    assert "secret" not in diagnostics.scrubber(tmp_path / "data")(rel)


# -- N12-3 -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("payload", ["a." * 32000, "aError(" * 32000, "aError:" * 20000, "File " * 30000], ids=["dots", "paren", "colon", "file"])
def test_n123_redaction_time_is_bounded_for_one_huge_line(payload):
    t = time.perf_counter()
    out = diagnostics.redact_exceptions("GET /" + payload + " -> 404 1 ms\n")
    assert time.perf_counter() - t < 1.0
    assert len(out) < 10_000


def test_n123_a_normal_line_is_not_truncated():
    line = "2026-10-02 12:00:00,000 INFO GET /api/projects -> 200 1 ms"
    assert diagnostics.redact_exceptions(line) == line


# -- N12-5 / N12-6 -----------------------------------------------------------------------------------------

def test_n125_a_failure_does_not_delete_a_file_another_row_refers_to(live, sample_dxf, monkeypatch):
    import app.api as api
    st = live.app.state.eri
    pid = live.http.post("/api/projects", json={"name": "p"}).json()["id"]
    real_load = api.load_dxf

    def other_request_commits_then_this_one_fails(path, name):
        imp = real_load(path, name)
        repo = st.repo()                                         # the 'other request' stores the same file
        repo.add_drawing(pid, name, Path(path).name, "a" * 64, 1, imp.entities, unit_to_mm=1.0, units_assumed=False,
                         info={"warnings": []})
        raise RuntimeError("lost the race")
    monkeypatch.setattr(api, "load_dxf", other_request_commits_then_this_one_fails)
    r = live.http.post(f"/api/projects/{pid}/drawings", files={"file": ("plan.dxf", sample_dxf.read_bytes())})
    assert r.status_code == 500
    row = st.repo().list_drawings(pid)[0]
    assert (live.config.data_dir / "projects" / pid / "drawings" / row["stored_name"]).is_file()


def test_n126_the_logged_class_is_the_exception_that_happened(live):
    import re
    pid = live.http.post("/api/projects", json={"name": "p"}).json()["id"]
    for body in (b"not a dxf at all", b"0\nSECTION\n2\nHEADER\n9\n$ACADVER\n1\nAC1032\nBAD\nfoo\n0\nENDSEC\n0\nEOF\n"):
        assert live.http.post(f"/api/projects/{pid}/drawings",
                              files={"file": ("bad.dxf", body)}).status_code == 400
    log = (live.config.data_dir / "logs" / "server.log").read_text(encoding="utf-8")
    classes = re.findall(r"drawing import failed: (\S+)", log)
    assert len(classes) == 2 and all(re.search(r"(Error|Exception)$", c) for c in classes), classes
