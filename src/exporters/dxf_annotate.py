"""Annotated DXF: the source drawing plus issue markers on ERI_* layers.

The source file is only read. The result is written to a different file;
the source's SHA-256 is compared before and after, so a bug can never
silently modify the user's drawing.
"""
from __future__ import annotations

import math
from pathlib import Path

from importers.dxf import DrawingImportError, _open
from persistence.storage import sha256_file
from .report import ExportError, Report

LAYERS = {"FAIL": ("ERI_FAIL", 1), "WARNING": ("ERI_WARNING", 2), "UNKNOWN": ("ERI_UNKNOWN", 5)}  # red/yellow/blue
MAX_MARKERS = 20000


def marker_size(report: Report) -> float:
    ext = report.extent
    span = max(ext[2] - ext[0], ext[3] - ext[1]) if ext else 0.0
    return max(span * 0.006, 1.0 / (report.summary.get("unit_to_mm") or 1.0) * 20)


def label_for(report: Report, i: dict) -> str:
    """ASCII only: older DXF versions cannot store Chinese text reliably."""
    text = f"{i['issue_id'][4:12]} {i['status']}"
    if i["measured"] is not None and i["required_value"] is not None:
        unit = "deg" if i["unit"] == "deg" else (i["unit"] if i["unit"] not in ("", "count") else "")
        text += f" {i['measured']:g}{unit} vs {i['required_op']}{i['required_value']:g}{unit}"
    return text


def annotate(report: Report, source: Path, dest: Path) -> dict:
    """Write ``dest`` = source + markers. Returns {"markers": n, "warnings": [...]}."""
    if not source.is_file():
        raise ExportError("找不到這份圖面的原始檔，無法產生標註 DXF。")
    before = sha256_file(source)
    try:
        doc, warnings = _open(source)
    except DrawingImportError as exc:
        raise ExportError(f"{exc.user_message}：{exc.reason}") from exc
    msp = doc.modelspace()
    for name, color in LAYERS.values():
        if name not in doc.layers:
            doc.layers.add(name, color=color)
    size = marker_size(report)
    n = 0
    for i in report.issues_by_status("FAIL", "WARNING", "UNKNOWN"):
        if i["location"] is None:
            continue
        if n >= MAX_MARKERS:
            warnings.append(f"標註數量超過 {MAX_MARKERS}，其餘問題未標註（請看 CSV）。")
            break
        layer, _ = LAYERS[i["status"]]
        x, y = i["location"]
        attrs = {"layer": layer}
        msp.add_circle((x, y), size, dxfattribs=attrs)
        msp.add_line((x - size, y), (x + size, y), dxfattribs=attrs)
        msp.add_line((x, y - size), (x, y + size), dxfattribs=attrs)
        closest = i["details"].get("closest_points")
        if closest and math.dist(closest[0], closest[1]) > 0:
            msp.add_line(tuple(closest[0]), tuple(closest[1]), dxfattribs=attrs)
        text = msp.add_text(label_for(report, i), height=size * 0.8, dxfattribs=attrs)
        text.set_placement((x + size * 1.3, y + size * 0.3))
        n += 1
    doc.saveas(dest)
    if sha256_file(source) != before:     # pragma: no cover - defensive; would indicate a serious bug
        dest.unlink(missing_ok=True)
        raise ExportError("內部錯誤：原始圖面在匯出過程中被改動，已取消匯出。")
    return {"markers": n, "warnings": warnings}
