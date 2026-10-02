"""File storage under the data directory.

Every file the application reads or writes lives below
``<data_dir>/projects/<project_id>/``. Uploaded files are copied there
under a generated name; the user's original file is never opened for
writing. All paths are built from validated IDs and re-checked with
``resolve_inside`` so a crafted name cannot escape the project folder.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable

ID_RE = re.compile(r"^[a-z]{1,4}_[0-9a-f]{16}$")
_BAD_CHARS = re.compile(r'[<>:"/\\|?*]')
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
# Unicode categories removed from names: controls (incl. C1), format characters such as the bidi
# overrides U+202E, surrogates, private use, unassigned, line/paragraph separators
_DROP_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
MAX_NAME_LEN = 100          # characters of a stored file name
MAX_NAME_BYTES = 200        # UTF-8 bytes (file systems limit bytes: 255 on ext4/APFS)
STORED_PREFIX_LEN = 13      # "<12 hex>_" in front of an uploaded file's name
CHUNK = 1024 * 1024


class StorageError(ValueError):
    pass


class UploadTooLarge(StorageError):
    pass


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def check_id(value: str) -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise StorageError("ID 格式錯誤")
    return value


def _clip(text: str, max_chars: int, max_bytes: int) -> str:
    text = text[:max_chars]
    while len(text.encode("utf-8")) > max_bytes:
        text = text[:-1]
    return text


def _safe_once(name: str, default: str, max_chars: int, max_bytes: int) -> str:
    name = unicodedata.normalize("NFC", str(name or ""))
    name = re.split(r"[\\/]", name)[-1]                  # drop any directory part (both separators)
    name = "".join("_" if unicodedata.category(ch) in _DROP_CATEGORIES else ch for ch in name)
    name = _BAD_CHARS.sub("_", name).strip().strip(".").strip()
    if not name:
        name = default
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    ext = ext[:16].strip()
    if stem.split(".")[0].strip().upper() in _WIN_RESERVED:
        stem = "_" + stem
    room_chars = max_chars - (len(ext) + 1 if ext else 0)
    room_bytes = max_bytes - (len(ext.encode("utf-8")) + 1 if ext else 0)
    stem = _clip(stem, max(room_chars, 1), max(room_bytes, 1)).strip().rstrip(".")
    if not stem:
        stem = default
    return f"{stem}.{ext}" if ext else stem


def safe_filename(name: str, default: str = "file", max_chars: int = MAX_NAME_LEN,
                  max_bytes: int = MAX_NAME_BYTES) -> str:
    """Reduce an uploaded file name to a plain, portable base name.

    The result is idempotent (``safe_filename(safe_filename(x)) == safe_filename(x)``), which is what
    ``Storage.file_path`` relies on to accept only names this function could have produced.
    """
    out = _safe_once(name, default, max_chars, max_bytes)
    for _ in range(4):                                       # truncation can expose a new edge: settle
        again = _safe_once(out, default, max_chars, max_bytes)
        if again == out:
            break
        out = again
    return out


def resolve_inside(base: Path, *parts: str) -> Path:
    """Join parts onto base and refuse anything that resolves outside base."""
    base_r = base.resolve()
    target = base_r.joinpath(*parts).resolve()
    if target != base_r and base_r not in target.parents:
        raise StorageError("路徑超出允許的資料夾")
    return target


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class StoredFile:
    stored_name: str
    path: Path
    sha256: str
    size: int


class Storage:
    SUBDIRS = ("drawings", "documents", "exports")

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).resolve()
        self.projects_dir = self.data_dir / "projects"
        self.projects_dir.mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "eri.sqlite3"

    def project_dir(self, project_id: str, create: bool = False) -> Path:
        p = resolve_inside(self.projects_dir, check_id(project_id))
        if create:
            for sub in self.SUBDIRS:
                (p / sub).mkdir(parents=True, exist_ok=True)
        return p

    def area(self, project_id: str, area: str) -> Path:
        """Folder of one storage area. The project folder must already exist (``project_dir(create=True)``)."""
        if area not in self.SUBDIRS:
            raise StorageError("未知的儲存區")
        project = self.project_dir(project_id)
        if not project.is_dir():
            raise StorageError("專案資料夾不存在")
        d = resolve_inside(project, area)
        d.mkdir(exist_ok=True)
        return d

    def file_path(self, project_id: str, area: str, stored_name: str) -> Path:
        if stored_name != safe_filename(stored_name, max_chars=MAX_NAME_LEN + STORED_PREFIX_LEN,
                                        max_bytes=MAX_NAME_BYTES + STORED_PREFIX_LEN):
            raise StorageError("檔名格式錯誤")
        return resolve_inside(self.area(project_id, area), stored_name)

    def save_stream(self, project_id: str, area: str, original_name: str, stream: BinaryIO,
                    max_bytes: int) -> StoredFile:
        """Copy an upload into the project, hashing it on the way.

        Written to a temp file in the same folder first and renamed only
        when complete, so an interrupted upload never leaves a partial file
        under its final name.
        """
        return self.save_chunks(project_id, area, original_name, iter(lambda: stream.read(CHUNK), b""), max_bytes)

    def save_chunks(self, project_id: str, area: str, original_name: str, chunks: Iterable[bytes],
                    max_bytes: int) -> StoredFile:
        folder = self.area(project_id, area)
        h = hashlib.sha256()
        size = 0
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".upload-", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as out:
                for block in chunks:
                    size += len(block)
                    if size > max_bytes:
                        raise UploadTooLarge(f"檔案超過上限 {max_bytes // (1024 * 1024)} MB")
                    h.update(block)
                    out.write(block)
            digest = h.hexdigest()
            stored = f"{digest[:12]}_{safe_filename(original_name)}"
            if len(stored) > MAX_NAME_LEN + STORED_PREFIX_LEN:     # cannot happen; guards file_path's check
                raise StorageError("檔名過長")
            final = resolve_inside(folder, stored)
            if final.exists() and sha256_file(final) == digest:
                os.unlink(tmp)
            else:
                os.replace(tmp, final)
            return StoredFile(stored, final, digest, size)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def copy_in(self, project_id: str, area: str, source: Path, max_bytes: int) -> StoredFile:
        """Copy a local file (demo assets) into the project; the source is only read."""
        with open(source, "rb") as f:
            return self.save_stream(project_id, area, source.name, f, max_bytes)

    def delete_project_files(self, project_id: str) -> None:
        p = self.project_dir(project_id)
        if p.exists():
            shutil.rmtree(p)

    def cleanup_partial_uploads(self, min_age_seconds: float = 3600.0) -> int:
        """Remove ``.upload-*.part`` leftovers of interrupted uploads.

        Only files older than ``min_age_seconds`` are removed, so a call made while the server is
        running cannot delete an upload that is still in progress.
        """
        n = 0
        now = time.time()
        for p in self.projects_dir.glob("*/*/.upload-*.part"):
            try:
                if now - p.stat().st_mtime >= min_age_seconds:
                    p.unlink()
                    n += 1
            except OSError:
                pass
        return n
