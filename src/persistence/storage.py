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
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable

ID_RE = re.compile(r"^[a-z]{1,4}_[0-9a-f]{16}$")
_BAD_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MAX_NAME_LEN = 120
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


def safe_filename(name: str, default: str = "file") -> str:
    """Reduce an uploaded file name to a plain, portable base name."""
    name = unicodedata.normalize("NFC", str(name or ""))
    name = re.split(r"[\\/]", name)[-1]          # drop any directory part (both separators)
    name = _BAD_CHARS.sub("_", name).strip().strip(".").strip()
    if not name:
        name = default
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    if stem.split(".")[0].upper() in _WIN_RESERVED:
        stem = "_" + stem
    ext = ext[:16]
    stem = stem[: MAX_NAME_LEN - len(ext) - 1] if ext else stem[:MAX_NAME_LEN]
    return f"{stem}.{ext}" if ext else stem


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
        if area not in self.SUBDIRS:
            raise StorageError("未知的儲存區")
        d = resolve_inside(self.project_dir(project_id), area)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def file_path(self, project_id: str, area: str, stored_name: str) -> Path:
        if stored_name != safe_filename(stored_name):
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

    def cleanup_partial_uploads(self) -> int:
        """Remove ``.upload-*.part`` leftovers from an interrupted process."""
        n = 0
        for p in self.projects_dir.glob("*/*/.upload-*.part"):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
        return n
