"""start-ui.bat cannot be executed on the Linux test machine, so its text is checked statically here.
(The Linux twin start-ui.sh has been run for real; a human double-click on Windows is still PENDING.)"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BAT = ROOT / "start-ui.bat"
SH = ROOT / "start-ui.sh"
FORBIDDEN = ("0.0.0.0", "curl ", "wget", "powershell", "bitsadmin", "certutil", "invoke-webrequest", "ftp ",
             "http://", "del ", "erase ", "rmdir", "rd /", "format ", "reg ", "schtasks", "netsh", "git push")


def bat_text() -> str:
    return BAT.read_bytes().decode("ascii")        # raises if any non-ASCII byte slipped in


def test_bat_is_ascii_with_crlf_only():
    raw = BAT.read_bytes()
    raw.decode("ascii")
    assert raw.count(b"\r\n") == raw.count(b"\n") == raw.count(b"\r")


def test_bat_gotos_have_labels():
    text = bat_text()
    labels = {m.group(1).lower() for m in re.finditer(r"^:([A-Za-z_]\w*)\s*$", text, re.M)}
    targets = {m.group(1).lower() for m in re.finditer(r"\bgoto\s+([A-Za-z_]\w*)", text, re.I)}
    assert targets and targets <= labels, targets - labels


def test_bat_starts_the_app_locally_with_the_private_environment():
    text = bat_text()
    assert '"%VPY%" -m app %*' in text and 'set "PYTHONPATH=%~dp0src"' in text
    assert "--diagnose" in text and "requirements.txt" in text
    assert "--host" not in text                    # the host is never overridden: the program default is 127.0.0.1


def test_launchers_have_no_network_or_destructive_commands():
    for path in (BAT, SH):
        low = path.read_text(encoding="ascii").lower()
        for word in FORBIDDEN:
            assert word not in low, (path.name, word)
        # the only thing installed is requirements.txt, into the private .venv
        pips = re.findall(r"pip install[^\n]*", low)
        assert pips and all("-r " in p and "requirements.txt" in p for p in pips), pips


def test_sh_is_executable_and_lf():
    if sys.platform == "win32":
        # NTFS has no exec bit: check the mode recorded in the git index instead
        import subprocess
        out = subprocess.run(["git", "ls-files", "-s", "start-ui.sh"], cwd=ROOT, capture_output=True, text=True)
        if out.returncode != 0 or not out.stdout.strip():
            pytest.skip("git index not available")
        assert out.stdout.split()[0] == "100755", out.stdout
    else:
        assert SH.stat().st_mode & 0o111
    assert b"\r" not in SH.read_bytes()


def test_gitattributes_pins_line_endings():
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.bat -text" in attrs and "*.sh text eol=lf" in attrs and "*.dxf -text" in attrs
