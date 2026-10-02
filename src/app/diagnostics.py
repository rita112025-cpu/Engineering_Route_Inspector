"""Diagnostics: what is wrong, in words, and a bundle a person can send when asking for help.

Privacy: nothing about the *content* of a project leaves this module. No drawing, specification or rule text,
no file names, no absolute paths (the data folder, the home folder and the program folder are replaced by
placeholders in everything that is exported). The bundle is a ZIP the user downloads and decides about; the
program never sends it anywhere.
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
_FILE_IN_STORAGE = re.compile(r"(drawings|documents|exports)[\\/][^\s'\"<>|]+")


def scrubber(data_dir: Path):
    """A function that hides absolute paths and stored file names in text."""
    roots = {str(data_dir.resolve()): "<data>", str(REPO_ROOT): "<app>", str(Path.home()): "<home>"}
    variants = {}
    for k, v in roots.items():
        variants[k] = v
        variants[k.replace("\\", "/")] = v
        variants[k.replace("/", "\\")] = v
    ordered = sorted(variants, key=len, reverse=True)

    def scrub(text: str) -> str:
        for k in ordered:
            if k:
                text = text.replace(k, variants[k])
        return _FILE_IN_STORAGE.sub(lambda m: f"{m.group(1)}/<file>", text)
    return scrub


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
            recovery: dict | None = None, started_at: float | None = None) -> dict:
    """Facts and health checks. ``conn`` is optional so the offline command works on a folder with no server."""
    data_dir = Path(data_dir)
    scrub = scrubber(data_dir)
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
            info["write_error"] = scrub(str(exc))
        checks.append(_check("資料資料夾可以寫入", writable))
        free = shutil.disk_usage(data_dir).free / 1024 / 1024
        info["disk_free_mb"] = round(free)
        info["data_size_mb"] = round(_dir_size(data_dir) / 1024 / 1024, 1)
        checks.append(_check(f"磁碟可用空間 ≥ {MIN_FREE_MB} MB", free >= MIN_FREE_MB, f"剩 {free:.0f} MB"))
    # database
    db_file = data_dir / "eri.sqlite3"
    own = None
    if conn is None and db_file.is_file():
        own = conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
    if conn is not None:
        try:
            integrity = [r[0] for r in conn.execute("PRAGMA integrity_check")]
            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
            checks.append(_check("資料庫完整性", integrity == ["ok"], "ok" if integrity == ["ok"] else "; ".join(integrity[:3])))
            checks.append(_check("資料庫關聯一致", not fk, "" if not fk else f"{len(fk)} 筆關聯遺失"))
            info["schema_version"] = schema_version(conn)
            info["counts"] = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                              for t in ("projects", "drawings", "documents", "rules", "runs", "issues", "exports")}
            info["runs_by_status"] = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM runs GROUP BY status")}
            info["recent_failed_runs"] = [
                {"run": r["id"], "status": r["status"], "code": r["error_code"], "message": scrub(r["error_message"] or ""),
                 "created_at": r["created_at"]}
                for r in conn.execute("SELECT id, status, error_code, error_message, created_at FROM runs "
                                      "WHERE status = 'failed' ORDER BY created_at DESC LIMIT 10")]
            info["recent_activity"] = [{"ts": r["ts"], "operation": r["operation"], "result": r["result"]}
                                       for r in conn.execute("SELECT ts, operation, result FROM audit_log ORDER BY id DESC LIMIT 20")]
            stuck = conn.execute("SELECT COUNT(*) FROM runs WHERE status IN ('queued','running')").fetchone()[0]
            if manager_status is not None:
                checks.append(_check("背景分析程式運作中", manager_status.get("alive", False)))
            info["active_runs"] = stuck
        except sqlite3.Error as exc:
            checks.append(_check("資料庫可以讀取", False, scrub(str(exc))))
        finally:
            if own is not None:
                own.close()
    elif exists:
        checks.append(_check("資料庫存在", False, "找不到 eri.sqlite3（還沒有任何資料）"))
    if manager_status is not None:
        info["jobs"] = {"max_workers": manager_status.get("max_workers"), "running": manager_status.get("running"),
                        "recent_events": [dict(e, detail=scrub(str(e.get("detail", "")))) for e in manager_status.get("recent_events", [])]}
    if recovery is not None:
        info["startup_recovery"] = recovery
    info["checks"] = checks
    info["healthy"] = all(c["ok"] for c in checks)
    info["log_tail"] = log_tail(data_dir, scrub)
    return info


def log_tail(data_dir: Path, scrub=None, lines: int = LOG_TAIL_LINES) -> list[str]:
    scrub = scrub or scrubber(data_dir)
    f = data_dir / "logs" / "server.log"
    if not f.is_file():
        return []
    data = f.read_bytes()[-200_000:].decode("utf-8", errors="replace").splitlines()
    return [scrub(x) for x in data[-lines:]]


def build_bundle(data_dir: Path, info: dict) -> bytes:
    """ZIP: diagnostics.json, the scrubbed server log and the newest worker logs, plus a note on what is inside."""
    scrub = scrubber(data_dir)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", "這是「工程管線檢查」的診斷資料。\n"
                   "內容：版本與環境、健康檢查結果、資料庫統計、近期失敗的分析、程式記錄檔。\n"
                   "不包含：圖面、規範、規則的內容與檔名。路徑已用 <data>、<app>、<home> 取代。\n"
                   "本程式不會自動傳送這個檔案；要不要提供給別人，由你決定。\n")
        z.writestr("diagnostics.json", json.dumps(info, ensure_ascii=False, indent=2))
        logs = data_dir / "logs"
        if logs.is_dir():
            server = logs / "server.log"
            if server.is_file():
                z.writestr("server.log", scrub(server.read_bytes()[-BUNDLE_LOG_BYTES:].decode("utf-8", errors="replace")))
            runs = sorted(logs.glob("run_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]
            for p in runs:
                z.writestr(f"worker-logs/{p.name}", scrub(p.read_bytes()[-200_000:].decode("utf-8", errors="replace")))
    return buf.getvalue()
