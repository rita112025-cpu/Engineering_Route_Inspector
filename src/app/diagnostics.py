"""Diagnostics: what is wrong, in words, and a bundle a person can send when asking for help.

Privacy: what is exported is built so that nothing from the *content* of a project is in it: no drawing,
specification or rule text, no file names, no absolute paths. Mechanisms (each has a test):
  * every file name the database knows (and the pattern ``drawings/<name>.<ext>`` for ones it no longer knows)
    is replaced by ``<file>``; the data, program and home folders by ``<data>``, ``<app>``, ``<home>``;
  * the message of an exception (which can quote file content or a rule pattern) is dropped from logs and failed
    runs: only its class is kept;
  * failure messages are free text only for the fixed messages the program itself writes.
The bundle is a ZIP the user downloads and decides about; the program never sends it anywhere.
"""
from __future__ import annotations

import io
import json
import os
import platform
import re
import shutil
import sqlite3
import sys
import time
import zipfile
from importlib import metadata
from pathlib import Path

from core.version import SOFTWARE_VERSION
from persistence.db import schema_version

PACKAGES = ("starlette", "uvicorn", "ezdxf", "pymupdf", "python-docx", "python-multipart", "psutil")
MIN_FREE_MB = 200
LOG_TAIL_LINES = 120
BUNDLE_LOG_BYTES = 400_000
REPO_ROOT = Path(__file__).resolve().parents[2]
# An unknown name inside a project's storage folder (the database no longer knows it) is hidden to the end of the
# line, because names can hold spaces, quotes and anything else. It is only recognised as a file-system path
# ('projects/<id>/drawings/...'), never as a request URL ('/api/projects/<id>/drawings/...' keeps its status and
# time). 'drawings/<file>' (already replaced) is left alone.
_FILE_IN_STORAGE = re.compile(
    r"(?<!/api/)(?P<pre>projects[\\/][a-z]{1,4}_[0-9a-f]{16}[\\/](?P<d>drawings|documents|exports))[\\/](?!<file>)[^\r\n]*",
    re.I)
# '<Class>:' inside a line, possibly module-qualified and possibly after a prefix ('worker said: ValueError: ...')
_COLON_NAME = re.compile(r"(?<![\w.])((?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*)\s*:")
_EXC_SUFFIX = re.compile(r"(?:[Ee]rror|Exception|Warning|Exit|Interrupt|Failure|Timeout|Fault)$")
_EXC_KNOWN = {"StopIteration", "StopAsyncIteration", "BadZipFile", "Cancelled", "UploadTooLarge", "RunGone",
              "RunInProgress", "KeyboardInterrupt", "SystemExit", "GeneratorExit"}
_EXC_REPR = re.compile(r"(?<![\w.])(?P<cls>[A-Za-z_][\w.]*?(?:[Ee]rror|Exception))\(.*$")
MAX_LINE = 2000       # longer lines (a hostile request path in the log) are cut before any pattern runs: bounded cost
# a line that starts something new: ends the (possibly multi-line) message of the exception before it
_RECORD_START = re.compile(r"^(?:\s*$|\d{4}-\d\d-\d\d|Traceback|\s*File \"|During handling|The above exception)")
OMITTED = "<內容已省略>"
# messages the program writes itself (fixed wording plus stage position / numbers): safe to export as text
FIXED_MESSAGE_CODES = {"WORKER_START_FAILED", "WORKER_CRASHED", "WORKER_STALLED", "WORKER_NO_PROGRESS", "WORKER_LOST",
                       "RUN_TIMEOUT", "INTERRUPTED", "OUT_OF_MEMORY", "CANCELLED"}


def _is_exception_name(name: str) -> bool:
    last = name.rsplit(".", 1)[-1]
    if _EXC_SUFFIX.search(last) or last in _EXC_KNOWN:
        return True
    return "." in name and last[:1].isupper()           # module.Class


def _redact_line(line: str) -> tuple[str, bool]:
    for m in _COLON_NAME.finditer(line):
        if _is_exception_name(m.group(1)):
            return f"{line[:m.end()]} {OMITTED}", True
    return _EXC_REPR.sub(lambda m: f"{m.group('cls')}({OMITTED})", line, count=1), False


def redact_exceptions(text: str) -> str:
    """Keep the exception class, drop its message (it may quote drawing content or a rule pattern).

    The message can run over several lines; those lines are dropped until the next record or traceback frame.
    """
    out: list[str] = []
    in_message = False
    for line in text.split("\n"):
        if len(line) > MAX_LINE:
            line = line[:MAX_LINE] + "…"
        if in_message and not _RECORD_START.match(line) and not any(
                _is_exception_name(m.group(1)) for m in _COLON_NAME.finditer(line)):
            continue
        in_message = False
        redacted, in_message = _redact_line(line)
        out.append(redacted)
    return "\n".join(out)


def _open_ro(db_file: Path) -> sqlite3.Connection:
    """Read-only connection; the path is URI-quoted so '#', '?' and '%' in a folder name are not special."""
    conn = sqlite3.connect(Path(db_file).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def known_names(conn: sqlite3.Connection | None) -> set[str]:
    """Every file name the database has: original and stored names of drawings and documents, export files."""
    names: set[str] = set()
    if conn is None:
        return names
    try:
        for sql in ("SELECT logical_name, stored_name FROM drawings", "SELECT filename, stored_name FROM documents"):
            for row in conn.execute(sql):
                names.update(x for x in row if x)
        for (path,) in conn.execute("SELECT path FROM exports"):
            names.add(Path(path).name)
    except sqlite3.Error:
        pass
    return names


def scrubber(data_dir: Path, names=()):
    """A function that hides absolute paths, known file names and stored file names in text."""
    roots = {str(data_dir.resolve()): "<data>", str(REPO_ROOT): "<app>"}
    try:
        home = Path.home()
    except RuntimeError:      # no home directory in a stripped-down environment: nothing to hide
        home = None
    # an empty USERPROFILE/HOME gives Path(".") and HOME=/ gives "/": scrubbing those would mangle every line
    if home is not None and home.is_absolute() and len(home.parts) > 1:
        roots[str(home)] = "<home>"
    variants = {}
    for k, v in roots.items():
        variants[k] = v
        variants[k.replace("\\", "/")] = v
        variants[k.replace("/", "\\")] = v
    ordered = sorted(variants, key=len, reverse=True)
    file_names = sorted({n for n in names if n and len(n) >= 3}, key=len, reverse=True)

    def scrub(text: str) -> str:
        for k in ordered:
            if k:
                text = text.replace(k, variants[k])
        for n in file_names:
            text = text.replace(n, "<file>")
        return _FILE_IN_STORAGE.sub(lambda m: f"{m.group('pre')}/<file>", text)
    return scrub


def scrubber_for(data_dir: Path, conn: sqlite3.Connection | None = None):
    own = None
    db_file = Path(data_dir) / "eri.sqlite3"
    if conn is None and db_file.is_file():
        try:
            own = conn = _open_ro(db_file)
        except sqlite3.Error:
            conn = None
    try:
        return scrubber(Path(data_dir), known_names(conn))
    finally:
        if own is not None:
            own.close()


def safe_run_message(code: str | None, message: str | None, scrub) -> str:
    """Free text only for the program's own fixed messages; otherwise the exception class alone."""
    message = message or ""
    if code in FIXED_MESSAGE_CODES:
        # runs recorded by an older version named the rule: 「執行規則：<id>」 -> 「執行規則」
        return re.sub(r"(執行規則)：[^」（]*", r"\1", scrub(message))
    m = re.match(r"分析過程發生錯誤：([\w.]+)", message)
    return f"分析過程發生錯誤：{m.group(1)}（{OMITTED}）" if m else OMITTED


def _check(name: str, ok: bool, detail: str = "") -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _dir_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def _packages() -> dict[str, str | None]:
    out = {}
    for name in PACKAGES:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = None
    return out


def collect(data_dir: Path, *, conn: sqlite3.Connection | None = None, manager_status: dict | None = None,
            recovery: dict | None = None, started_at: float | None = None, deep: bool = False) -> dict:
    """Facts and health checks. ``conn`` is optional so the offline command works on a folder with no server.

    ``deep`` runs the full SQLite integrity check (slow on a big database, so the dialog uses the quick check).
    """
    data_dir = Path(data_dir)
    scrub = scrubber_for(data_dir, conn)
    checks: list[dict] = []
    info: dict = {
        "software_version": SOFTWARE_VERSION, "python": platform.python_version(), "platform": platform.platform(),
        "packages": _packages(), "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "uptime_seconds": round(time.time() - started_at) if started_at else None,
    }
    checks.append(_check("Python 3.10 以上", sys.version_info >= (3, 10), f"目前 {platform.python_version()}"))
    missing = [k for k, v in info["packages"].items() if v is None]
    checks.append(_check("必要套件都已安裝", not missing, "缺少：" + "、".join(missing) if missing else "全部可用"))
    # data folder
    exists = data_dir.is_dir()
    checks.append(_check("資料資料夾存在", exists, "" if exists else "尚未建立（第一次啟動時會自動建立）"))
    if exists:
        try:
            probe = data_dir / ".write-test"
            probe.write_text("x")
            probe.unlink()
            writable = True
        except OSError as exc:
            writable = False
            info["write_error"] = scrub(redact_exceptions(f"{type(exc).__name__}: {exc}"))
        checks.append(_check("資料資料夾可以寫入", writable))
        free = shutil.disk_usage(data_dir).free / 1024 / 1024
        info["disk_free_mb"] = round(free)
        info["data_size_mb"] = round(_dir_size(data_dir) / 1024 / 1024, 1)
        checks.append(_check(f"磁碟可用空間 ≥ {MIN_FREE_MB} MB", free >= MIN_FREE_MB, f"剩 {free:.0f} MB"))
    # database
    db_file = data_dir / "eri.sqlite3"
    own = None
    if conn is None and db_file.is_file():
        own = conn = _open_ro(db_file)
        conn.row_factory = sqlite3.Row
    if conn is not None:
        try:
            pragma, kind = ("integrity_check", "完整檢查") if deep else ("quick_check", "快速檢查")
            integrity = [r[0] for r in conn.execute(f"PRAGMA {pragma}")]
            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
            checks.append(_check("資料庫完整性", integrity == ["ok"],
                                 f"{kind}：ok" if integrity == ["ok"] else f"{kind}：" + "; ".join(integrity[:3])))
            checks.append(_check("資料庫關聯一致", not fk, "" if not fk else f"{len(fk)} 筆關聯遺失"))
            info["schema_version"] = schema_version(conn)
            info["counts"] = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                              for t in ("projects", "drawings", "documents", "rules", "runs", "issues", "exports")}
            info["runs_by_status"] = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM runs GROUP BY status")}
            info["recent_failed_runs"] = [
                {"run": r["id"], "status": r["status"], "code": r["error_code"],
                 "message": safe_run_message(r["error_code"], r["error_message"], scrub), "created_at": r["created_at"]}
                for r in conn.execute("SELECT id, status, error_code, error_message, created_at FROM runs "
                                      "WHERE status = 'failed' ORDER BY created_at DESC LIMIT 10")]
            info["recent_activity"] = [{"ts": r["ts"], "operation": r["operation"], "result": r["result"]}
                                       for r in conn.execute("SELECT ts, operation, result FROM audit_log ORDER BY id DESC LIMIT 20")]
            stuck = conn.execute("SELECT COUNT(*) FROM runs WHERE status IN ('queued','running')").fetchone()[0]
            if manager_status is not None:
                checks.append(_check("背景分析程式運作中", manager_status.get("alive", False)))
            info["active_runs"] = stuck
        except sqlite3.Error as exc:
            checks.append(_check("資料庫可以讀取", False, type(exc).__name__))
        finally:
            if own is not None:
                own.close()
    elif exists:
        checks.append(_check("資料庫存在", False, "找不到 eri.sqlite3（還沒有任何資料）"))
    if manager_status is not None:
        info["jobs"] = {"max_workers": manager_status.get("max_workers"), "running": manager_status.get("running"),
                        "recent_events": [dict(e, detail=scrub(redact_exceptions(str(e.get("detail", ""))))) for e in manager_status.get("recent_events", [])]}
    if recovery is not None:
        info["startup_recovery"] = recovery
    info["checks"] = checks
    info["healthy"] = all(c["ok"] for c in checks)
    info["log_tail"] = log_tail(data_dir, scrub)
    return info


def clean_log_text(text: str, scrub) -> str:
    return scrub(redact_exceptions(text))


def log_tail(data_dir: Path, scrub=None, lines: int = LOG_TAIL_LINES) -> list[str]:
    scrub = scrub or scrubber_for(data_dir)
    f = data_dir / "logs" / "server.log"
    if not f.is_file():
        return []
    data = f.read_bytes()[-200_000:].decode("utf-8", errors="replace")
    return clean_log_text(data, scrub).splitlines()[-lines:]


def build_bundle(data_dir: Path, info: dict, conn: sqlite3.Connection | None = None) -> bytes:
    """ZIP: diagnostics.json, the scrubbed server log and the newest worker logs, plus a note on what is inside."""
    scrub = scrubber_for(data_dir, conn)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", "這是「工程管線檢查」的診斷資料。\n"
                   "內容：版本與環境、健康檢查結果、資料庫統計、近期失敗的分析、程式記錄檔。\n"
                   "不包含：圖面、規範、規則的內容與檔名。路徑已用 <data>、<app>、<home> 取代，"
                   "檔名以 <file> 取代，例外訊息只保留例外的類別。\n"
                   "本程式不會自動傳送這個檔案；要不要提供給別人，由你決定。\n")
        z.writestr("diagnostics.json", json.dumps(info, ensure_ascii=False, indent=2))
        logs = data_dir / "logs"
        if logs.is_dir():
            server = logs / "server.log"
            if server.is_file():
                text = server.read_bytes()[-BUNDLE_LOG_BYTES:].decode("utf-8", errors="replace")
                z.writestr("server.log", clean_log_text(text, scrub))
            runs = sorted(logs.glob("run_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]
            for p in runs:
                text = p.read_bytes()[-200_000:].decode("utf-8", errors="replace")
                z.writestr(f"worker-logs/{p.name}", clean_log_text(text, scrub))
    return buf.getvalue()
