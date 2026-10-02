"""Markdown report."""
from __future__ import annotations

from .report import BASELINE_ZH, Report, confidence_zh, measurement_zh, status_zh

MAX_DETAIL_ISSUES = 500   # non-PASS issues written in full; the rest are counted and the cut is stated


def esc(value) -> str:
    """Text safe inside a Markdown table cell or paragraph (no HTML, tables or emphasis injected)."""
    t = "" if value is None else str(value)
    t = t.replace("\\", "\\\\").replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")
    for ch in ("*", "_", "`", "[", "]"):
        t = t.replace(ch, "\\" + ch)
    return t.replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")


def render_markdown(report: Report) -> bytes:
    r, d, p = report.run, report.drawing, report.project
    c = report.counts
    out: list[str] = []
    w = out.append
    w(f"# 工程管線檢查報告：{esc(d['logical_name'])}\n")
    w("| 項目 | 內容 |\n|---|---|")
    w(f"| 專案 | {esc(p['name'])} |")
    w(f"| 圖面 | {esc(d['logical_name'])}（SHA-256 `{d['sha256'][:16]}…`） |")
    w(f"| 分析編號 | `{r['id']}` |")
    w(f"| 分析完成時間 | {esc(r['finished_at'])} |")
    w(f"| 報告產生時間 | {esc(report.generated_at)} |")
    w(f"| 軟體版本 | {esc(report.software_version)} |")
    w(f"| 規則集 SHA-256 | `{(r['ruleset_sha256'] or '')[:16]}…` |")
    units = "圖面未宣告單位，以 mm 推定" if report.summary.get("units_assumed") else f"1 圖面單位 = {report.summary.get('unit_to_mm')} mm"
    w(f"| 單位 | {esc(units)} |\n")

    w("## 結果總覽\n")
    w("| 不合格 | 警告 | 無法判定 | 合格 |\n|---:|---:|---:|---:|")
    w(f"| {c.get('FAIL', 0)} | {c.get('WARNING', 0)} | {c.get('UNKNOWN', 0)} | {c.get('PASS', 0)} |\n")
    cmp = report.summary.get("comparison")
    if cmp and cmp.get("available"):
        k = cmp["counts"]
        w(f"與基準分析 `{cmp['baseline_run_id']}` 比較：新增 {k['NEW']}、已解決 {k['RESOLVED']}、"
          f"有變化 {k['CHANGED']}、未變 {k['UNCHANGED']}。")
        if cmp.get("ruleset_changed"):
            w("規則集與基準不同。")
        if cmp.get("drawing_changed"):
            w("圖面內容與基準不同。")
        w("")
    for warn in report.summary.get("warnings", []):
        w(f"> 注意：{esc(warn)}")
    if report.summary.get("warnings"):
        w("")

    w("## 規則統計\n")
    w("| 規則 | 不合格 | 警告 | 無法判定 | 合格 |\n|---|---:|---:|---:|---:|")
    for s in report.summary.get("rule_stats", []):
        w(f"| {esc(s['rule_id'])} {esc(s['name'])} | {s['FAIL']} | {s['WARNING']} | {s['UNKNOWN']} | {s['PASS']} |")
    w("")

    shown = report.issues_by_status("FAIL", "WARNING", "UNKNOWN")
    w("## 問題清單\n")
    if not shown:
        w("沒有不合格、警告或無法判定的項目。\n")
    written = 0
    for status in ("FAIL", "WARNING", "UNKNOWN"):
        group = [i for i in shown if i["status"] == status]
        if not group:
            continue
        w(f"### {status_zh(status)}（{len(group)}）\n")
        for i in group:
            if written >= MAX_DETAIL_ISSUES:
                break
            written += 1
            w(f"#### {i['issue_id']}　{esc(i['rule_name'])}\n")
            w("| 欄位 | 內容 |\n|---|---|")
            w(f"| 狀態 | {status_zh(i['status'])}（信心：{confidence_zh(i['confidence'])}） |")
            w(f"| Handle | {esc(', '.join(i['handles']))} |")
            w(f"| 圖層 | {esc(i['layer'])} |")
            w(f"| 系統 | {esc(i['system'])} |")
            w(f"| 檢查方式 | {measurement_zh(i['measurement'])} |")
            w(f"| 實測值 | {esc(report.measured_text(i)) or '—'} |")
            w(f"| 規則要求 | {esc(report.required_text(i)) or '—'} |")
            w(f"| 規則 | {esc(i['rule_id'])} {esc(i['rule_name'])} |")
            if i["location"]:
                w(f"| 位置 | ({i['location'][0]:.1f}, {i['location'][1]:.1f}) |")
            if i["baseline_state"]:
                w(f"| 與基準比較 | {BASELINE_ZH.get(i['baseline_state'], i['baseline_state'])} |")
            w(f"| 說明 | {esc(i['message'])} |")
            w(f"| 判定依據 | {esc(i['reason'])} |")
            if i["fix"]:
                w(f"| 建議處理 | {esc(i['fix'])} |")
            reasons = i["details"].get("confidence_reasons") or []
            if reasons:
                w(f"| 信心說明 | {esc('；'.join(reasons))} |")
            ev = report.evidence_of(i)
            if ev:
                w(f"| 規範證據 | {esc(report.evidence_ref_text(i))}（`{ev['id']}`） |")
            w("")
            if ev:
                quoted = "\n".join("> " + esc(line) for line in ev["text"].splitlines())
                w(quoted + "\n")
    hidden = len(shown) - MAX_DETAIL_ISSUES
    if hidden > 0:
        w(f"> 另有 {hidden} 筆未列出（報告最多詳列 {MAX_DETAIL_ISSUES} 筆）；完整清單請看 CSV 匯出。\n")
    w(f"## 合格項目\n\n共 {c.get('PASS', 0)} 筆合格，明細請看 CSV 或 HTML 匯出。\n")
    return "\n".join(out).encode("utf-8")
