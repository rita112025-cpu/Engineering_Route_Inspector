"""scripts/package_release.py: the release ZIP is made from Git's tracked files, is clean and is reproducible."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "package_release.py"


@pytest.fixture(scope="module")
def pkg():
    spec = importlib.util.spec_from_file_location("package_release", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_AUTHOR_DATE": "2026-01-02T03:04:05Z", "GIT_COMMITTER_DATE": "2026-01-02T03:04:05Z"}
    out = subprocess.run(["git", "-C", str(repo), "-c", "core.autocrlf=false", *args], capture_output=True, text=True,
                         env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


@pytest.fixture
def repo(tmp_path, pkg, monkeypatch):
    """A throw-away Git repository laid out like the project, with every kind of junk lying around untracked."""
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git is not available")
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    files = {name: f"content of {name}\n".encode() for name in pkg.REQUIRED}
    files["start-ui.bat"] = b"@echo off\r\necho hi\r\n"
    files["src/app/__init__.py"] = b""
    files["docs/guide.md"] = b"# guide\n"
    for name, data in files.items():
        (r / name).parent.mkdir(parents=True, exist_ok=True)
        (r / name).write_bytes(data)
    _git(r, "add", "--", *files)
    _git(r, "update-index", "--chmod=+x", "start-ui.sh")
    _git(r, "commit", "-q", "-m", "release content")
    # junk that exists in a developer's folder but is not tracked
    junk = [".venv/Lib/site-packages/x.py", "data/projects/p1/database/eri.sqlite3", "data/projects/p1/database/eri.sqlite3-wal",
            "data/projects/p1/database/eri.sqlite3-shm", "data/logs/server.log", "__pycache__/m.cpython-312.pyc",
            "src/app/__pycache__/server.cpython-312.pyc", ".pytest_cache/v/cache/lastfailed", "benchmark_out/benchmark.json",
            "dist/old.zip", "uploads/tmp_upload.part", ".git/hooks/not-a-release"]
    for name in junk:
        (r / name).parent.mkdir(parents=True, exist_ok=True)
        (r / name).write_bytes(b"junk")
    monkeypatch.setattr(pkg, "ROOT", r)
    return r


def _names(zip_path: Path) -> list[str]:
    with zipfile.ZipFile(zip_path) as z:
        return z.namelist()


def test_zip_holds_exactly_the_tracked_files_and_none_of_the_junk(repo, pkg, tmp_path):
    out = tmp_path / "out" / "release.zip"
    assert pkg.main(["--out", str(out)]) == 0
    names = _names(out)
    tracked = [n for n in _git(repo, "ls-files").splitlines()]
    assert names == sorted(tracked)
    for part in (".git/", ".venv", "data/", "__pycache__", ".pytest_cache", "benchmark_out", "dist/", ".pyc", ".sqlite", "server.log", ".part"):
        assert not any(part in n for n in names), part
    assert not any(n.startswith("/") or "\\" in n or ".." in n.split("/") or ":" in n for n in names)
    for required in pkg.REQUIRED:
        assert required in names


def test_zip_is_reproducible_and_independent_of_file_times_and_junk(repo, pkg, tmp_path):
    a, b = tmp_path / "a.zip", tmp_path / "b.zip"
    assert pkg.main(["--out", str(a)]) == 0
    for p in repo.rglob("*"):
        if p.is_file():
            os.utime(p, (1_000_000_000, 1_000_000_000))               # different mtimes
    (repo / "data" / "projects" / "p1" / "more.sqlite3").write_bytes(b"new junk")   # different junk
    assert pkg.main(["--out", str(b)]) == 0
    assert a.read_bytes() == b.read_bytes()
    with zipfile.ZipFile(a) as z:
        assert {i.date_time for i in z.infolist()} == {(2026, 1, 2, 3, 4, 4)}      # the commit time (ZIP: even seconds), not "now"
        assert all(i.compress_type == zipfile.ZIP_DEFLATED for i in z.infolist())


def test_bytes_permissions_and_line_endings_are_those_committed(repo, pkg, tmp_path):
    out = tmp_path / "r.zip"
    assert pkg.main(["--out", str(out)]) == 0
    with zipfile.ZipFile(out) as z:
        assert z.read("start-ui.bat") == b"@echo off\r\necho hi\r\n"
        assert z.getinfo("start-ui.sh").external_attr >> 16 == 0o100755
        assert z.getinfo("README.md").external_attr >> 16 == 0o100644
        assert z.testzip() is None


def test_uncommitted_edits_are_not_shipped_and_a_warning_says_so(repo, pkg, tmp_path, capsys):
    (repo / "README.md").write_text("EDITED AFTER THE COMMIT\n", encoding="utf-8")
    out = tmp_path / "r.zip"
    assert pkg.main(["--out", str(out)]) == 0
    assert zipfile.ZipFile(out).read("README.md") == b"content of README.md\n"
    assert "uncommitted" in capsys.readouterr().err


def test_a_tracked_forbidden_file_fails_the_build_instead_of_being_dropped(repo, pkg, tmp_path, capsys):
    (repo / "data").mkdir(exist_ok=True)
    (repo / "data" / "secret.sqlite3").write_bytes(b"db")
    _git(repo, "add", "-f", "--", "data/secret.sqlite3")
    _git(repo, "commit", "-q", "-m", "oops")
    out = tmp_path / "r.zip"
    assert pkg.main(["--out", str(out)]) == 1
    assert "secret.sqlite3" in capsys.readouterr().err and not out.exists()


def test_missing_required_file_fails_the_build(repo, pkg, tmp_path, capsys):
    _git(repo, "rm", "-q", "--", "start-ui.bat")
    _git(repo, "commit", "-q", "-m", "drop launcher")
    assert pkg.main(["--out", str(tmp_path / "r.zip")]) == 1
    assert "start-ui.bat" in capsys.readouterr().err


def test_packaging_an_older_ref_uses_that_commit(repo, pkg, tmp_path):
    first = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "docs" / "new.md").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "--", "docs/new.md")
    _git(repo, "commit", "-q", "-m", "later")
    old, new = tmp_path / "old.zip", tmp_path / "new.zip"
    assert pkg.main(["--ref", first, "--out", str(old)]) == 0 and pkg.main(["--out", str(new)]) == 0
    assert "docs/new.md" not in _names(old) and "docs/new.md" in _names(new)


@pytest.mark.parametrize("name,bad", [
    ("README.md", False), ("src/app/server.py", False), ("demo/demo_plan.dxf", False), (".gitignore", False),
    ("tests/fixtures/golden_project/sample.dxf", False), ("docs/data/readme.md", False),
    (".git/config", True), (".venv/Scripts/python.exe", True), ("data/eri.sqlite3", True), ("a/__pycache__/x.pyc", True),
    ("x.pyc", True), ("x.PYO", True), ("db/eri.sqlite3-wal", True), ("eri.sqlite3-shm", True), ("benchmark_out/b.json", True),
    ("dist/Engineering_Route_Inspector.zip", True), ("logs/server.log", True), ("old/release.zip", True),
    (".pytest_cache/README.md", True), ("Thumbs.db", True), ("sub/.DS_Store", True),
])
def test_forbidden_reason_table(pkg, name, bad):
    assert (pkg.forbidden_reason(name) is not None) is bad, name


@pytest.mark.parametrize("name,bad", [
    ("a/b.txt", False), ("/etc/passwd", True), ("C:/Windows/x", True), ("a\\b.txt", True), ("../x", True),
    ("a/../x", True), ("a//b", True), ("./a", True), ("", True), ("//server/share/x", True),
])
def test_path_problem_table(pkg, name, bad):
    assert (pkg.path_problem(name) is not None) is bad, name


# -- the real repository: HEAD must make a usable, clean release -------------------------------------------------

@pytest.fixture(scope="module")
def real_zip(tmp_path_factory):
    if subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"], capture_output=True).returncode != 0:
        pytest.skip("not a Git checkout")
    out = tmp_path_factory.mktemp("rel") / "Engineering_Route_Inspector.zip"
    proc = subprocess.run([sys.executable, str(SCRIPT), "--out", str(out)], capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    return out


def test_real_head_zip_has_what_a_user_needs_and_nothing_else(real_zip, pkg):
    names = _names(real_zip)
    for required in ("README.md", "requirements.txt", "start-ui.bat", "start-ui.sh", "src/app/server.py",
                     "demo/demo_plan.dxf", "demo/demo_spec.md"):
        assert required in names
    assert [n for n in names if pkg.forbidden_reason(n) or pkg.path_problem(n)] == []
    assert not [n for n in names if n.split("/")[0] in (".git", ".venv", "data", "dist", "benchmark_out")]
    assert not [n for n in names if n.endswith((".pyc", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".log"))]
    with zipfile.ZipFile(real_zip) as z:
        bat = z.read("start-ui.bat")
        assert bat.count(b"\r\n") == bat.count(b"\n") > 0 and bat.isascii()
        # tests legitimately contain fake paths for the redaction tests; look at what a user actually runs and reads
        text = b"".join(z.read(n) for n in names if n.endswith((".py", ".md", ".bat", ".sh", ".txt"))
                        and not n.startswith("tests/"))
    for private in (b"Users/user", b"D:/github", b"/home/user", ("Users"+chr(92)+"user").encode(), ("D:"+chr(92)+"github").encode()):  # no developer machine paths
        assert private not in text, private


def test_extracted_release_imports_and_diagnoses_itself(real_zip, tmp_path):
    target = tmp_path / "unzipped"
    with zipfile.ZipFile(real_zip) as z:
        z.extractall(target)
    env = {"PYTHONPATH": str(target / "src"), "PATH": ""}
    for k in ("SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP"):
        if k in os.environ:
            env[k] = os.environ[k]
    imp = subprocess.run([sys.executable, "-W", "error", "-c",
                          "import app.server, core.analysis.pipeline, importers.dxf, exporters.service, persistence.db, jobs.manager"],
                         capture_output=True, text=True, env=env, cwd=target, timeout=60)
    assert imp.returncode == 0, imp.stderr
    # a data folder / database that do not exist yet are reported as problems, so create them with the released code
    made = subprocess.run([sys.executable, "-c", "import sys; from pathlib import Path; from persistence.db import open_database; "
                           "d = Path(sys.argv[1]); d.mkdir(); open_database(d / 'eri.sqlite3').close()", str(tmp_path / "d")],
                          capture_output=True, text=True, env=env, cwd=target, timeout=60)
    assert made.returncode == 0, made.stderr
    diag = subprocess.run([sys.executable, "-m", "app", "--diagnose", "--json", "--data-dir", str(tmp_path / "d")],
                          capture_output=True, text=True, env=env, cwd=target, timeout=60)
    assert diag.returncode == 0, diag.stderr
    assert json.loads(diag.stdout)["healthy"] is True
    assert not (target / ".git").exists() and not (target / "data").exists()


def test_zip_does_not_depend_on_the_users_autocrlf_setting(repo, pkg, tmp_path, monkeypatch):
    """The same commit must give the same bytes whatever core.autocrlf the person building it has configured."""
    a = tmp_path / "a.zip"
    assert pkg.main(["--out", str(a)]) == 0
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    b = tmp_path / "b.zip"
    assert pkg.main(["--out", str(b)]) == 0
    assert a.read_bytes() == b.read_bytes()
    assert zipfile.ZipFile(b).read("README.md") == b"content of README.md\n"


def test_everything_is_taken_from_the_resolved_commit_even_if_the_branch_moves(repo, pkg, tmp_path, monkeypatch):
    """HEAD moves between resolving the ref and archiving it (another session commits): the ZIP, its timestamp and the
    reported commit must still be those of the commit that was resolved."""
    first = _git(repo, "rev-parse", "HEAD").strip()
    calls, real_git, moved = [], pkg.git, []

    def spying_git(*args, **kw):
        calls.append(args)
        out = real_git(*args, **kw)
        if args[:2] == ("rev-parse", "--verify") and not moved:        # the branch moves right after it was resolved
            (repo / "docs" / "late.md").write_text("committed during the build\n", encoding="utf-8")
            _git(repo, "add", "--", "docs/late.md")
            _git(repo, "commit", "-q", "-m", "late", "--date=2027-05-05T05:05:05Z")
            moved.append(True)
        return out
    monkeypatch.setattr(pkg, "git", spying_git)
    out = tmp_path / "r.zip"
    info = pkg.build("HEAD", out)
    assert moved and _git(repo, "rev-parse", "HEAD").strip() != first
    assert info["commit"] == first
    assert "docs/late.md" not in _names(out)
    archive = [c for c in calls if c[0] == "archive"][0]
    log = [c for c in calls if c[0] == "log"][0]
    assert first in archive and "HEAD" not in archive and first in log and "HEAD" not in log
    with zipfile.ZipFile(out) as z:
        assert {i.date_time for i in z.infolist()} == {(2026, 1, 2, 3, 4, 4)}       # the resolved commit's time


@pytest.mark.parametrize("ref", ["--output=evil.zip", "-h", "--exec=x"])
def test_a_ref_that_looks_like_an_option_is_refused(repo, pkg, tmp_path, capsys, ref):
    assert pkg.main([f"--ref={ref}", "--out", str(tmp_path / "r.zip")]) == 1      # the only way a "-x" value reaches git
    assert "invalid --ref" in capsys.readouterr().err and not (tmp_path / "r.zip").exists()
    assert not (repo / "evil.zip").exists()
