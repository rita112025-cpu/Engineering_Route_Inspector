"""Create export files for a completed run inside the project's export folder."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from persistence.repo import Repo
from persistence.storage import Storage, resolve_inside, safe_filename, sha256_file
from . import csv_export, dxf_annotate, html_report, markdown
from .report import ExportError, build_report

FORMATS = {
    "csv": ("csv", "issues.csv"),
    "markdown": ("md", "report.md"),
    "html": ("html", "report.html"),
    "dxf": ("dxf", "annotated.dxf"),
}
CONTENT_TYPES = {"csv": "text/csv; charset=utf-8", "md": "text/markdown; charset=utf-8",
                 "html": "text/html; charset=utf-8", "dxf": "application/dxf"}


def export_name(drawing_name: str, run_id: str, fmt: str) -> str:
    ext, tag = FORMATS[fmt]
    stem = safe_filename(Path(drawing_name).stem, "drawing")[:60]
    tag_stem = tag.rsplit(".", 1)[0]
    return safe_filename(f"{stem}_{run_id[-8:]}_{tag_stem}.{ext}")


def export_run(repo: Repo, storage: Storage, run_id: str, fmt: str) -> dict:
    """Write one export and register it. Returns the ``exports`` row plus a ``warnings`` list."""
    if fmt not in FORMATS:
        raise ExportError("不支援的匯出格式。")
    report = build_report(repo, run_id)
    pid = report.project["id"]
    folder = storage.area(pid, "exports")
    name = export_name(report.drawing["logical_name"], run_id, fmt)
    final = resolve_inside(folder, name)
    fd, tmp_name = tempfile.mkstemp(dir=folder, prefix=".export-", suffix=".part")
    os.close(fd)
    tmp = Path(tmp_name)
    warnings: list[str] = []
    try:
        if fmt == "dxf":
            source = storage.file_path(pid, "drawings", report.drawing["stored_name"])
            info = dxf_annotate.annotate(report, source, tmp)
            warnings = info["warnings"]
        else:
            if fmt == "csv":
                data = csv_export.render_csv(report)
            elif fmt == "markdown":
                data = markdown.render_markdown(report)
            else:
                data = html_report.render_html(report, repo.entity_rows(report.drawing["id"]))
            tmp.write_bytes(data)
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    row = repo.add_export(run_id, fmt, f"projects/{pid}/exports/{name}", sha256_file(final))
    row["warnings"] = warnings
    row["name"] = name
    return row


def export_file(repo: Repo, storage: Storage, export_id: str) -> tuple[Path, str, str]:
    """Resolve a registered export to (path, download name, content type); raises ExportError if gone."""
    row = repo.get_export(export_id)
    if row is None:
        raise ExportError("找不到這個匯出檔。")
    rel = Path(row["path"])
    if len(rel.parts) != 4 or rel.parts[0] != "projects" or rel.parts[2] != "exports":
        raise ExportError("匯出檔位置不正確。")
    path = resolve_inside(storage.area(row["project_id"], "exports"), rel.name)
    if not path.is_file():
        raise ExportError("匯出檔已不存在，請重新匯出。")
    return path, rel.name, CONTENT_TYPES[FORMATS[row["format"]][0]]
