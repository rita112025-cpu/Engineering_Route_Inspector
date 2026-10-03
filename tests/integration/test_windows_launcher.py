"""Execute the real Windows launcher against isolated databases, without installing packages."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from persistence.db import open_database

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="real Windows batch launcher")


@pytest.mark.parametrize("healthy", [True, False])
@pytest.mark.parametrize("flags", [("--diagnose", "--json"), ("--json", "--diagnose")])
def test_windows_diagnose_once_with_correct_exit_code(tmp_path, healthy, flags):
    data = tmp_path / "data"
    data.mkdir()
    if healthy:
        open_database(data / "eri.sqlite3").close()
    else:
        (data / "eri.sqlite3").write_bytes(b"not a database" * 200)
    result = subprocess.run(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(ROOT / "start-ui.bat"),
         *flags, "--data-dir", str(data)], cwd=ROOT, input="\n", capture_output=True,
        encoding="utf-8", timeout=30,
    )
    assert result.returncode == (0 if healthy else 1), result.stdout + result.stderr
    assert result.stdout.count('"healthy"') == 1, result.stdout
    assert "Running the self-check" not in result.stdout, result.stdout
    assert "Traceback" not in result.stderr
    info = json.loads(result.stdout[result.stdout.index("{"):])
    assert info["healthy"] is healthy


# -- the --diagnose decision is made in one place (has_diagnose) and never expands wildcards ------------------------

@pytest.fixture
def win_box(tmp_path):
    """A copy of start-ui.bat next to a private 'venv' that already has the packages (no network, no pip)."""
    import shutil
    import sysconfig
    if sys.prefix == sys.base_prefix:
        pytest.skip("the tests are not running inside a virtual environment")
    box = tmp_path / "box dir"                                    # a space in the path, like a real install folder
    (box / ".venv" / "Scripts").mkdir(parents=True)
    shutil.copy(ROOT / "start-ui.bat", box / "start-ui.bat")
    shutil.copy(ROOT / "requirements.txt", box / "requirements.txt")
    shutil.copy(ROOT / "requirements.txt", box / ".venv" / "requirements.installed")
    shutil.copy(Path(sys.prefix) / "pyvenv.cfg", box / ".venv" / "pyvenv.cfg")
    shutil.copy(Path(sys.prefix) / "Scripts" / "python.exe", box / ".venv" / "Scripts" / "python.exe")

    def junction(link: Path, target: Path):
        link.parent.mkdir(parents=True, exist_ok=True)
        done = subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True, text=True)
        if done.returncode != 0:
            pytest.skip(f"cannot create a directory junction: {done.stdout} {done.stderr}")
    junction(box / "src", ROOT / "src")
    junction(box / ".venv" / "Lib" / "site-packages", Path(sysconfig.get_paths()["purelib"]))
    return box


def run_box(box: Path, *args: str, data: Path | None = None):
    env = dict(os.environ, ERI_DATA_DIR=str(data or box / "data"))
    # cmd /s /c ""script" args": the documented way to run a script whose path contains spaces together with quoted arguments
    command = f'{os.environ.get("COMSPEC", "cmd.exe")} /d /s /c ""{box / "start-ui.bat"}" {subprocess.list2cmdline(list(args))}"'
    return subprocess.run(command, cwd=box.parent, env=env, input="\n", capture_output=True, encoding="utf-8", timeout=60)


def test_a_failing_app_gets_exactly_one_self_check_and_keeps_its_exit_code(win_box):
    r = run_box(win_box, "--host", "0.0.0.0", "--no-browser")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "The program stopped with error code 2" in r.stdout
    assert r.stdout.count("Running the self-check") == 1 and r.stdout.count("結果：") == 1, r.stdout
    assert "Traceback" not in r.stdout + r.stderr


def test_wildcard_arguments_are_not_expanded_into_file_names(win_box):
    """A file called --diagnose sits in the launcher folder. `for %%A in (%*)` expanded `--d*` to that name and took it
    for the flag, silently skipping the self-check after a failure."""
    (win_box / "--diagnose").write_text("decoy", encoding="utf-8")
    r = run_box(win_box, "--host", "0.0.0.0", "--d*")
    assert r.returncode == 2, r.stdout + r.stderr
    assert r.stdout.count("Running the self-check") == 1 and r.stdout.count("結果：") == 1, r.stdout


@pytest.mark.parametrize("position", ["last", "first", "middle"])
def test_diagnose_is_found_in_any_position_next_to_a_quoted_path_with_spaces(win_box, position):
    data = win_box / "my data folder"
    data.mkdir()
    open_database(data / "eri.sqlite3").close()
    args = {"last": ["--data-dir", str(data), "--diagnose"], "first": ["--diagnose", "--data-dir", str(data)],
            "middle": ["--json", "--diagnose", "--data-dir", str(data)]}[position]
    r = run_box(win_box, *args, data=data)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Running the self-check" not in r.stdout
    assert r.stdout.count("結果：") + r.stdout.count('"healthy"') == 1, r.stdout


def test_the_decision_has_a_single_source_in_the_script():
    code = [l for l in (ROOT / "start-ui.bat").read_bytes().decode("ascii").splitlines() if not l.lower().startswith("rem")]
    comparisons = [l for l in code if l.lower().startswith("if ") and "--diagnose" in l.lower()]
    assert len(comparisons) == 1, comparisons                          # one decision, in :has_diagnose
    assert not [l for l in code if l.lower().startswith("for ") and "%*" in l]
    assert [l for l in code if l.lower().startswith("call :has_diagnose")] == ["call :has_diagnose %*"]
