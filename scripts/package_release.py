"""Build the release ZIP from what Git tracks - never from the working directory.

    python scripts/package_release.py                    # dist/Engineering_Route_Inspector.zip from HEAD
    python scripts/package_release.py --ref v0.2.0 --out build/eri.zip

Why Git and not "zip the folder": the development folder holds .git, .venv, data (projects, drawings, SQLite
databases with their -wal/-shm files, logs), caches and benchmark output. None of that belongs in a release, and
none of it is tracked. The ZIP is made from ``git archive <ref>`` (the committed bytes, so a .bat keeps its CRLF
and a .dxf stays byte for byte as ``.gitattributes`` says) and re-packed with Python's ``zipfile``:

  * members sorted by path, '/' separators, fixed per-file permissions (0644, 0755 where Git marks the exec bit);
  * every timestamp is the commit time of ``--ref``, so the same commit always yields the same bytes
    (same Python/zlib build);
  * the finished ZIP is re-opened and checked: no forbidden member, no absolute or '..' path, no backslash,
    every required file present, CRC test. A tracked file that is forbidden in a release makes the build FAIL
    instead of being dropped silently.

Uncommitted changes are NOT in the ZIP (a warning says so). Nothing is pushed, merged or modified.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import posixpath
import stat
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = Path("dist") / "Engineering_Route_Inspector.zip"        # relative to the repository root

# directories that never ship at any depth, directories that never ship at the top level (the application's own
# data/, build and benchmark output: a nested docs/data/ is ordinary content), files by name and by suffix
FORBIDDEN_DIRS = {".git", ".venv", "venv", ".pytest_cache", "__pycache__", "node_modules", ".idea", ".vscode"}
FORBIDDEN_TOP_DIRS = {"data", "dist", "benchmark_out"}
FORBIDDEN_NAMES = {"server.log", "app.log", ".DS_Store", "Thumbs.db", "desktop.ini"}
FORBIDDEN_SUFFIXES = (".pyc", ".pyo", ".sqlite", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".db", ".db-wal",
                      ".db-shm", ".log", ".zip", ".7z", ".tar", ".gz", ".tmp")
REQUIRED = ("README.md", "requirements.txt", "start-ui.bat", "start-ui.sh", "src/app/server.py",
            "demo/demo_plan.dxf", "demo/demo_spec.md")


class PackageError(Exception):
    pass


def forbidden_reason(name: str) -> str | None:
    """Why ``name`` (a '/'-separated archive path) must not be in a release, or None."""
    parts = name.split("/")
    if len(parts) > 1 and parts[0] in FORBIDDEN_TOP_DIRS:
        return f"inside forbidden directory '{parts[0]}/'"
    for part in parts[:-1]:
        if part in FORBIDDEN_DIRS:
            return f"inside forbidden directory '{part}/'"
    if parts[-1] in FORBIDDEN_NAMES:
        return f"forbidden file name '{parts[-1]}'"
    low = parts[-1].lower()
    for suffix in FORBIDDEN_SUFFIXES:
        if low.endswith(suffix):
            return f"forbidden file type '{suffix}'"
    return None


def path_problem(name: str) -> str | None:
    """Archive member names must be plain relative '/' paths."""
    if not name or name.startswith("/") or "\\" in name or ":" in name.split("/")[0]:
        return "absolute path, backslash or drive letter"
    if any(p in ("", ".", "..") for p in name.split("/")):
        return "empty, '.' or '..' path component"
    if posixpath.normpath(name) != name:
        return "path is not normalised"
    return None


def git(*args: str, check: bool = True) -> bytes:
    try:
        # core.autocrlf is pinned off: `git archive` would otherwise convert line endings according to the
        # building user's own Git settings, and the same commit would give different bytes on different machines.
        # (Per-file rules in .gitattributes still apply: they are part of the repository.)
        proc = subprocess.run(["git", "-c", "core.autocrlf=false", "-C", str(ROOT), *args], capture_output=True, timeout=300)
    except FileNotFoundError as exc:
        raise PackageError("git was not found on PATH; the release ZIP is built from Git's tracked files") from exc
    if check and proc.returncode != 0:
        raise PackageError(f"git {' '.join(args)} failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


def commit_time(ref: str) -> time.struct_time:
    ts = int(git("log", "-1", "--format=%ct", ref).decode().strip() or "0")
    return time.gmtime(max(ts, 315532800))                       # ZIP cannot hold dates before 1980


def collect(ref: str) -> list[tuple[str, bytes, bool]]:
    """(name, content, executable) of every tracked file at ``ref``, sorted by name."""
    raw = git("archive", "--format=tar", ref)
    files: dict[str, tuple[bytes, bool]] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
        for m in tar:
            if m.isdir():
                continue
            if not m.isfile():
                raise PackageError(f"tracked entry '{m.name}' is not a regular file (symlink/special): not shipped")
            problem = path_problem(m.name)
            if problem:
                raise PackageError(f"tracked path '{m.name}': {problem}")
            reason = forbidden_reason(m.name)
            if reason:
                raise PackageError(f"tracked file '{m.name}' must not be in a release ({reason}); "
                                   f"remove it from Git or from the forbidden list")
            files[m.name] = (tar.extractfile(m).read(), bool(m.mode & 0o111))
    return [(n, c, x) for n, (c, x) in sorted(files.items())]


def write_zip(entries: list[tuple[str, bytes, bool]], out: Path, stamp: time.struct_time) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    date_time = tuple(stamp)[:5] + (stamp.tm_sec - stamp.tm_sec % 2,)      # ZIP/DOS time has 2-second resolution
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for name, content, executable in entries:
                zi = zipfile.ZipInfo(name, date_time=date_time)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.create_system = 3                              # Unix: keeps the permission bits below
                zi.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
                z.writestr(zi, content, compresslevel=9)
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)


def verify(out: Path) -> list[str]:
    """Re-open the finished ZIP and check it. Returns the member names."""
    with zipfile.ZipFile(out) as z:
        bad = z.testzip()
        if bad:
            raise PackageError(f"ZIP integrity check failed at '{bad}'")
        names = z.namelist()
    for n in names:
        for check in (path_problem, forbidden_reason):
            reason = check(n)
            if reason:
                raise PackageError(f"member '{n}' in the finished ZIP: {reason}")
    if names != sorted(names) or len(set(names)) != len(names):
        raise PackageError("members are not unique and sorted")
    missing = [r for r in REQUIRED if r not in names]
    if missing:
        raise PackageError(f"required files missing from the release: {missing}")
    return names


def build(ref: str, out: Path) -> dict:
    if ref.startswith("-"):                                       # never let git read the ref as an option
        raise PackageError(f"invalid --ref {ref!r}")
    top = git("rev-parse", "--show-toplevel").decode().strip()
    if Path(top).resolve() != ROOT.resolve():
        raise PackageError(f"{ROOT} is not the top of a Git repository")
    sha = git("rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    dirty = git("status", "--porcelain", "--untracked-files=no").decode().strip()
    # from here on only the resolved commit id is used: if the branch moves while the ZIP is built, the content,
    # the timestamp and the reported commit still all describe the same commit
    entries = collect(sha)
    write_zip(entries, out, commit_time(sha))
    names = verify(out)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    return {"ref": ref, "commit": sha, "path": out, "files": len(names), "bytes": out.stat().st_size,
            "sha256": digest, "uncommitted_changes_ignored": bool(dirty)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ref", default="HEAD", help="commit, tag or branch to package (default HEAD)")
    ap.add_argument("--out", default=None, help=f"ZIP to write (default {DEFAULT_OUT.as_posix()} in the repository)")
    args = ap.parse_args(argv)
    try:
        info = build(args.ref, Path(args.out).resolve() if args.out else (ROOT / DEFAULT_OUT))
    except PackageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if info["uncommitted_changes_ignored"]:
        print("WARNING: tracked files have uncommitted changes; the ZIP contains the committed version "
              f"({info['commit'][:10]}) only.", file=sys.stderr)
    print(f"release ZIP: {info['path']}")
    print(f"  commit  {info['commit']}  ({info['ref']})")
    print(f"  files   {info['files']}   size {info['bytes']:,} bytes")
    print(f"  sha256  {info['sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
