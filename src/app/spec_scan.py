"""Scanning the specification folder a person chose: find the supported files, ingest what is new or changed,
extract rule candidates, and leave everything else alone.

Safety:
* only the one folder the user set (project setting, else the app default) is read; never anything outside it
* symbolic links, junctions / reparse points (also OneDrive placeholders) are not followed
* every file's real path is checked to lie inside that folder
* the user's files are only read; copies go to the project's own data folder
* sizes, depth and file counts are bounded
"""
from __future__ import annotations

import logging
import os
import re
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from core.rule_extraction.candidate import stable_candidate_id
from core.rule_extraction.extractor import EXTRACTOR_VERSION, extract_from_chunks
from importers.text import DocumentImportError
from persistence.storage import StorageError, sha256_file
from .errors import ApiError, bad_request

if TYPE_CHECKING:
    from .state import AppState

log = logging.getLogger("eri")

SCAN_EXTS = {".pdf": "pdf", ".docx": "docx", ".txt": "txt", ".md": "md", ".markdown": "md"}
MAX_DEPTH = 12
SKIP_DIRS = {"node_modules", "__pycache__", "$recycle.bin", "system volume information"}
MAX_LISTED = 300                      # files named in a scan report
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

REASON_ZH = {
    "PDF_NO_TEXT_LAYER": "PDF 沒有文字層（可能是掃描影像），本工具不做 OCR，無法擷取規則。",
    "EMPTY_DOCUMENT": "文件裡沒有讀到任何文字。",
    "TOO_LARGE": "檔案超過大小上限，已略過。",
}


def validate_source_path(raw, data_dir: Path) -> Path:
    """The folder a person typed -> its real path, or a Chinese error. No traversal, no whole-disk scans."""
    if not isinstance(raw, str) or not raw.strip():
        raise bad_request("請輸入規範資料夾的完整路徑。", "SOURCE_INVALID")
    text = raw.strip().strip('"')
    if len(text) > 500 or "\x00" in text:
        raise bad_request("路徑格式不正確。", "SOURCE_INVALID")
    if ".." in re.split(r"[\\/]+", text):
        raise bad_request("路徑不可包含「..」（上層資料夾），請輸入完整路徑。", "SOURCE_INVALID")
    p = Path(text)
    if not p.is_absolute():
        raise bad_request("請輸入完整路徑（例如 F:\\SCADA_主題導覽）。", "SOURCE_INVALID")
    try:
        resolved = p.resolve(strict=True)
    except (OSError, RuntimeError):
        raise bad_request("找不到這個資料夾，請確認路徑是否正確、磁碟是否已連接。", "SOURCE_NOT_FOUND") from None
    if not resolved.is_dir():
        raise bad_request("這個路徑不是資料夾。", "SOURCE_NOT_DIR")
    if resolved.parent == resolved:
        raise bad_request("不能掃描整個磁碟，請指定放規範的資料夾。", "SOURCE_TOO_BROAD")
    data = Path(data_dir).resolve()
    if resolved == data or data in resolved.parents:
        raise bad_request("不能把本程式自己的資料夾當成規範來源。", "SOURCE_INVALID")
    try:
        with os.scandir(resolved):
            pass
    except PermissionError:
        raise bad_request("沒有權限讀取這個資料夾。", "SOURCE_DENIED") from None
    except OSError:
        raise bad_request("無法讀取這個資料夾。", "SOURCE_NOT_FOUND") from None
    return resolved


@dataclass
class Found:
    rel: str
    path: Path
    size: int
    mtime_ns: int
    kind: str


def _is_link(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & REPARSE_POINT)


def walk_source(root: Path, data_dir: Path, limit: int) -> tuple[list[Found], dict[str, int]]:
    """Supported files below ``root`` (sorted), without following links, and what was skipped."""
    root_r = root.resolve()
    data_r = Path(data_dir).resolve()
    skip_data = data_r if root_r in data_r.parents else None
    skipped = {"links": 0, "escaped": 0, "unreadable_dirs": 0}
    found: list[Found] = []
    stack: list[tuple[Path, int]] = [(root_r, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name.casefold())
        except OSError:
            skipped["unreadable_dirs"] += 1
            continue
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if _is_link(st):
                skipped["links"] += 1
                continue
            path = Path(e.path)
            if stat.S_ISDIR(st.st_mode):
                if e.name.startswith(".") or e.name.casefold() in SKIP_DIRS or path == skip_data:
                    continue
                if depth < MAX_DEPTH:
                    stack.append((path, depth + 1))
                continue
            kind = SCAN_EXTS.get(os.path.splitext(e.name)[1].lower())
            if kind is None or e.name.startswith(("~$", ".")) or not stat.S_ISREG(st.st_mode):
                continue
            try:
                real = path.resolve()
            except OSError:
                continue
            if root_r not in real.parents:                      # containment: never read outside the chosen folder
                skipped["escaped"] += 1
                continue
            found.append(Found(path.relative_to(root_r).as_posix(), path, st.st_size, st.st_mtime_ns, kind))
            if len(found) > limit:
                raise ApiError(409, "TOO_MANY_FILES", f"這個資料夾有超過 {limit} 個規範檔，請指定更精確的子資料夾。")
    found.sort(key=lambda f: f.rel.casefold())
    return found, skipped


def _discard_document(st: AppState, pid: str, doc: dict) -> None:
    st.repo().delete_document(doc["id"])
    try:
        st.storage.file_path(pid, "documents", doc["stored_name"]).unlink(missing_ok=True)
    except (StorageError, OSError):
        pass


def _ingest(st: AppState, pid: str, f: Found) -> tuple[str, dict | None, str | None, bool]:
    """-> (status, document, reason, created). The file is only read."""
    from . import api                                               # imported late: api imports this module's routes
    spec = st.repo().spec
    max_bytes = st.config.max_document_mb * 1024 * 1024
    if f.size > max_bytes:
        return "TOO_LARGE", None, "TOO_LARGE", False
    try:
        sha = sha256_file(f.path)
    except OSError:
        return "UNREADABLE", None, "無法讀取檔案（可能被其他程式鎖定或沒有權限）。", False
    existing = spec.document_by_sha(pid, sha)
    if existing:                                                    # the same content already ingested: reuse it
        return "OK", existing, None, False
    try:
        with open(f.path, "rb") as fh:
            doc = api._import_document(st, pid, f.path.name, fh)
    except DocumentImportError as exc:
        return "UNREADABLE", None, exc.reason, False
    except (StorageError, OSError) as exc:
        return "UNREADABLE", None, str(exc) if isinstance(exc, StorageError) else "無法讀取檔案。", False
    if doc.get("chunk_count", 0) == 0:                              # nothing to extract from: do not keep an empty document
        _discard_document(st, pid, doc)
        return "NOT_EXTRACTABLE", None, "PDF_NO_TEXT_LAYER" if f.kind == "pdf" else "EMPTY_DOCUMENT", False
    return "OK", doc, None, not doc.get("already_imported")


def extract_pending(repo, pid: str) -> int:
    """Read the rule candidates out of every document that has none yet (or whose reading rules are outdated)."""
    spec = repo.spec
    n = 0
    for doc in spec.documents_to_extract(pid, EXTRACTOR_VERSION):
        raws = extract_from_chunks(spec.document_chunks(doc["id"]), doc["id"], doc["filename"])
        spec.save_extraction(pid, doc["id"], EXTRACTOR_VERSION,
                             [(stable_candidate_id(doc["sha256"], r.semantic_key), r) for r in raws])
        n += 1
    return n


def scan_project(st: AppState, pid: str, root: Path | None, progress=None) -> dict:
    repo = st.repo()
    spec = repo.spec
    started = time.time()
    root_key = str(root) if root else ""
    found, skipped = walk_source(root, st.config.data_dir, st.config.max_spec_files) if root else \
        ([], {"links": 0, "escaped": 0, "unreadable_dirs": 0})
    if progress:
        progress(total=len(found), done=0, current="")
    prev = spec.files(pid, root_key)
    stats = {"files": len(found), "new": 0, "reparsed": 0, "reused": 0, "removed": 0, "not_extractable": 0,
             "unreadable": 0, "too_large": 0, "extracted_documents": 0}
    listed: list[dict] = []
    stale_docs: set[str] = set()

    def note(rel: str, action: str, status: str) -> None:
        if len(listed) < MAX_LISTED and (action != "reused" or status != "OK"):
            listed.append({"path": rel, "action": action, "status": status})

    for i, f in enumerate(found):
        if progress:
            progress(done=i, current=f.rel)
        row = prev.get(f.rel)
        unchanged = row and row["size_bytes"] == f.size and row["mtime_ns"] == f.mtime_ns
        usable = row and (row["document_id"] or row["status"] in ("NOT_EXTRACTABLE", "TOO_LARGE"))
        if unchanged and usable:                                    # size + mtime say it is the same file
            stats["reused"] += 1
            note(f.rel, "reused", row["status"])
            continue
        status, doc, reason, created = _ingest(st, pid, f)
        sha = doc["sha256"] if doc else None
        if row and row["sha256"] and sha and row["sha256"] == sha and row["document_id"]:
            stats["reused"] += 1                                    # touched, not changed: the SHA-256 is the same
            created = bool(row["created_document"])
            note(f.rel, "reused", status)
        else:
            stats["reparsed" if row else "new"] += 1
            note(f.rel, "reparsed" if row else "new", status)
        if row and row["document_id"] and doc and row["document_id"] != doc["id"] and row["created_document"]:
            stale_docs.add(row["document_id"])
        if status == "NOT_EXTRACTABLE":
            stats["not_extractable"] += 1
        elif status == "UNREADABLE":
            stats["unreadable"] += 1
        elif status == "TOO_LARGE":
            stats["too_large"] += 1
        spec.upsert_file(pid, root_key, f.rel, size=f.size, mtime_ns=f.mtime_ns, sha256=sha, kind=f.kind, status=status,
                         reason=reason, document_id=doc["id"] if doc else None, created_document=created)

    seen = {f.rel for f in found}
    for rel, row in prev.items():                                   # files that are gone
        if rel in seen:
            continue
        spec.delete_file(pid, root_key, rel)
        stats["removed"] += 1
        note(rel, "removed", row["status"])
        if row["document_id"] and row["created_document"]:
            stale_docs.add(row["document_id"])
    for doc_id in stale_docs:                                       # a document the scan made, no longer used by any file
        doc = repo.get_document(doc_id)
        if doc and spec.document_refs(pid, doc_id) == 0:
            _discard_document(st, pid, doc)

    if progress:
        progress(done=len(found), current="擷取規則")
    stats["extracted_documents"] = extract_pending(repo, pid)       # also documents imported by hand

    from . import spec_rules
    counts = spec_rules.refresh_statuses(repo, pid)
    summary = {"root": root_key, **stats, "skipped": skipped, "candidates": counts["total"], "counts": counts,
               "files_listed": listed, "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "seconds": round(time.time() - started, 2)}
    spec.set_last_scan(pid, summary)
    repo.audit("spec.scan", pid, {k: v for k, v in summary.items() if k not in ("files_listed", "root")})
    return summary


class ScanJobs:
    """One background scan per project; the page polls its progress."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._threads: dict[str, threading.Thread] = {}

    def status(self, pid: str) -> dict:
        with self._lock:
            return dict(self._jobs.get(pid) or {"state": "idle"})

    def _set(self, pid: str, **kw) -> None:
        with self._lock:
            self._jobs.setdefault(pid, {"state": "idle"}).update(kw)

    def start(self, st: AppState, pid: str, root: Path | None, wait: bool = False) -> dict:
        with self._lock:
            if (self._jobs.get(pid) or {}).get("state") == "running":
                return dict(self._jobs[pid])
            self._jobs[pid] = {"state": "running", "total": 0, "done": 0, "current": "", "started_at": time.time()}

        def work():
            try:
                summary = scan_project(st, pid, root, progress=lambda **kw: self._set(pid, **kw))
                self._set(pid, state="done", summary=summary, finished_at=time.time())
            except ApiError as exc:
                self._set(pid, state="failed", error=exc.message, code=exc.code, finished_at=time.time())
            except Exception:                                       # noqa: BLE001 - reported, never crashes the server
                log.exception("specification scan failed")
                self._set(pid, state="failed", error="掃描規範時發生未預期的錯誤。細節已寫入記錄檔。", code="INTERNAL_ERROR",
                          finished_at=time.time())

        t = threading.Thread(target=work, name=f"spec-scan-{pid}", daemon=True)
        self._threads[pid] = t
        t.start()
        if wait:
            t.join()
        return self.status(pid)
