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
