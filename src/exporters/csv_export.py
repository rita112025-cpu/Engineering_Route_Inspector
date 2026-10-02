"""CSV export (UTF-8 with BOM so Excel on Windows shows Chinese correctly)."""
from __future__ import annotations

import csv
import io

from .report import BASELINE_ZH, Report, confidence_zh, measurement_zh, status_zh

HEADERS = ["問題編號", "狀態", "規則嚴重度", "信心", "規則編號", "規則名稱", "檢查方式", "圖層", "系統",
           "Handle", "X", "Y", "實測值", "規則要求", "單位", "證據編號", "證據出處", "說明", "判定依據",
           "建議處理", "與基準比較", "專案"]
_DANGEROUS = ("=", "+", "-", "@", "\t", "\r", "\n")


def safe_cell(value) -> str:
    """Neutralise spreadsheet formulas (CSV injection) in text coming from drawings or documents."""
    text = "" if value is None else str(value)
    if text.startswith(_DANGEROUS):
        return "'" + text
    return text


def render_csv(report: Report) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(HEADERS)
    for i in report.issues:
        loc = i["location"] or ["", ""]
        w.writerow([
            i["issue_id"], status_zh(i["status"]), i["severity"], confidence_zh(i["confidence"]),
            safe_cell(i["rule_id"]), safe_cell(i["rule_name"]), measurement_zh(i["measurement"]),
            safe_cell(i["layer"]), safe_cell(i["system"]), safe_cell(",".join(i["handles"])),
            "" if loc[0] == "" else f"{loc[0]:.3f}", "" if loc[1] == "" else f"{loc[1]:.3f}",
            "" if i["measured"] is None else f"{i['measured']:g}", report.required_text(i), i["unit"] or "",
            i["evidence_id"] or "", safe_cell(report.evidence_ref_text(i)), safe_cell(i["message"]),
            safe_cell(i["reason"]), safe_cell(i["fix"]), BASELINE_ZH.get(i["baseline_state"], i["baseline_state"]),
            safe_cell(report.project["name"]),
        ])
    return ("﻿" + buf.getvalue()).encode("utf-8")
