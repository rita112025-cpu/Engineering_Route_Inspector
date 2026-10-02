"""Standalone HTML report: one file, no external resources, works offline.

Dynamic text is never concatenated into markup: the issues travel as JSON
in a data block and the page renders them with ``textContent``. Only the
drawing geometry (numbers) and layer colours are written as SVG markup.
"""
from __future__ import annotations

import hashlib
import html
import json

from .report import BASELINE_ZH, Report, confidence_zh, measurement_zh, status_zh

MAX_SVG_ENTITIES = 20000
MAX_PASS_ROWS = 300
STATUS_COLOR = {"FAIL": "#c62828", "WARNING": "#b26a00", "UNKNOWN": "#1565c0", "PASS": "#2e7d32"}
PALETTE = ["#546e7a", "#6d4c41", "#00796b", "#5e35b1", "#7b1fa2", "#455a64", "#3949ab", "#00838f", "#827717",
           "#4e342e"]


def _layer_color(layer: str) -> str:
    return PALETTE[int(hashlib.md5(layer.encode("utf-8")).hexdigest(), 16) % len(PALETTE)]


def _json_for_html(obj) -> str:
    """JSON that cannot close the surrounding <script> element or start an HTML comment."""
    return (json.dumps(obj, ensure_ascii=True, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def _f(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".") or "0"


def build_svg(report: Report, entities: list[dict]) -> tuple[str, str, int]:
    """(svg inner markup, viewBox, number of entities left out)."""
    ext = report.extent or (0.0, 0.0, 1.0, 1.0)
    pad = max(ext[2] - ext[0], ext[3] - ext[1], 1.0) * 0.03
    minx, miny, maxx, maxy = ext[0] - pad, ext[1] - pad, ext[2] + pad, ext[3] + pad
    involved = {h for i in report.issues_by_status("FAIL", "WARNING", "UNKNOWN") for h in i["handles"]}
    chosen = [e for e in entities if e["handle"] in involved]
    rest = [e for e in entities if e["handle"] not in involved and e["g"]["kind"] != "text"]
    room = max(0, MAX_SVG_ENTITIES - len(chosen))
    chosen += rest[:room]
    left_out = len(rest) - room if len(rest) > room else 0
    parts: list[str] = []
    for e in chosen:
        g = e["g"]
        cls = ' class="inv"' if e["handle"] in involved else ""
        col = _layer_color(e["layer"])
        if g["kind"] == "polyline":
            pts = " ".join(f"{_f(x)},{_f(y)}" for x, y in g["points"])
            tag = "polygon" if g.get("closed") else "polyline"
            parts.append(f'<{tag} points="{pts}" stroke="{col}"{cls}/>')
        elif g["kind"] == "circle":
            cx, cy = g["center"]
            parts.append(f'<circle cx="{_f(cx)}" cy="{_f(cy)}" r="{_f(g["radius"])}" stroke="{col}"{cls}/>')
    viewbox = f"{_f(minx)} {_f(-maxy)} {_f(maxx - minx)} {_f(maxy - miny)}"
    return "".join(parts), viewbox, left_out


def _issue_payload(report: Report, i: dict) -> dict:
    ev = report.evidence_of(i)
    return {
        "id": i["issue_id"], "status": i["status"], "statusZh": status_zh(i["status"]),
        "rule": i["rule_id"], "ruleName": i["rule_name"], "measurement": measurement_zh(i["measurement"]),
        "layer": i["layer"] or "", "system": i["system"] or "", "handles": i["handles"],
        "x": i["location"][0] if i["location"] else None, "y": i["location"][1] if i["location"] else None,
        "measured": report.measured_text(i), "required": report.required_text(i),
        "message": i["message"], "reason": i["reason"] or "", "fix": i["fix"] or "",
        "confidence": confidence_zh(i["confidence"]),
        "confidenceReasons": i["details"].get("confidence_reasons", []),
        "baseline": BASELINE_ZH.get(i["baseline_state"], i["baseline_state"] or ""),
        "evidence": ({"ref": report.evidence_ref_text(i), "id": ev["id"], "text": ev["text"]} if ev else None),
        "closest": i["details"].get("closest_points"),
    }


def render_html(report: Report, entities: list[dict]) -> bytes:
    shown = report.issues_by_status("FAIL", "WARNING", "UNKNOWN")
    passes = report.issues_by_status("PASS")
    payload_issues = [_issue_payload(report, i) for i in shown + passes[:MAX_PASS_ROWS]]
    svg, viewbox, left_out = build_svg(report, entities)
    c = report.counts
    units = ("圖面未宣告單位，以 mm 推定" if report.summary.get("units_assumed")
             else f"1 圖面單位 = {report.summary.get('unit_to_mm')} mm")
    notes = list(report.summary.get("warnings", []))
    if left_out:
        notes.append(f"圖面物件過多，示意圖省略 {left_out} 個與問題無關的物件。")
    if len(passes) > MAX_PASS_ROWS:
        notes.append(f"合格項目共 {len(passes)} 筆，此頁僅列前 {MAX_PASS_ROWS} 筆；完整清單請看 CSV。")
    data = {
        "colors": STATUS_COLOR, "viewBox": viewbox, "issues": payload_issues,
        "counts": {k: c.get(k, 0) for k in ("FAIL", "WARNING", "UNKNOWN", "PASS")},
    }
    meta_rows = [
        ("專案", report.project["name"]), ("圖面", report.drawing["logical_name"]),
        ("圖面 SHA-256", report.drawing["sha256"]), ("分析編號", report.run["id"]),
        ("分析完成時間", report.run["finished_at"] or ""), ("報告產生時間", report.generated_at),
        ("軟體版本", report.software_version), ("單位", units),
    ]
    meta_html = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>" for k, v in meta_rows)
    notes_html = "".join(f"<li>{html.escape(str(n))}</li>" for n in notes)
    page = (_TEMPLATE
            .replace("__TITLE__", html.escape(f"工程管線檢查報告 – {report.drawing['logical_name']}"))
            .replace("__META__", meta_html)
            .replace("__NOTES__", f"<ul class='notes'>{notes_html}</ul>" if notes else "")
            .replace("__SVG__", svg)
            .replace("__VIEWBOX__", html.escape(viewbox))
            .replace("__DATA__", _json_for_html(data)))
    return page.encode("utf-8")


_TEMPLATE = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--fg:#1d2733;--bg:#fff;--muted:#5b6876;--line:#d6dce3;--panel:#f4f6f8;--fail:#c62828;--warn:#b26a00;--unk:#1565c0;--pass:#2e7d32}
@media (prefers-color-scheme: dark){:root{--fg:#e6ebf0;--bg:#14181d;--muted:#9aa7b4;--line:#303942;--panel:#1c222a;--fail:#ef7b7b;--warn:#e0a24a;--unk:#6aa7f0;--pass:#6cc070}}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 "Segoe UI","Microsoft JhengHei",system-ui,sans-serif;color:var(--fg);background:var(--bg)}
header{padding:16px 20px;border-bottom:1px solid var(--line)}h1{margin:0 0 8px;font-size:20px}
table.meta{border-collapse:collapse;font-size:13px}table.meta th{text-align:left;color:var(--muted);padding:1px 14px 1px 0;font-weight:500;white-space:nowrap}
table.meta td{word-break:break-all}
.notes{margin:8px 0 0;padding-left:20px;color:var(--warn)}
.cards{display:flex;gap:10px;flex-wrap:wrap;padding:12px 20px}
.card{border:1px solid var(--line);border-radius:6px;padding:8px 14px;min-width:96px;background:var(--panel)}
.card b{display:block;font-size:22px}.card.FAIL b{color:var(--fail)}.card.WARNING b{color:var(--warn)}.card.UNKNOWN b{color:var(--unk)}.card.PASS b{color:var(--pass)}
main{display:grid;grid-template-columns:minmax(320px,1fr) minmax(320px,1fr);gap:14px;padding:0 20px 24px}
@media (max-width:900px){main{grid-template-columns:1fr}}
.toolbar{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
button,input{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:4px 10px}
button{cursor:pointer}button[aria-pressed=true]{background:var(--fg);color:var(--bg)}input{flex:1;min-width:140px}
#list{border:1px solid var(--line);border-radius:6px;max-height:70vh;overflow:auto}
.row{display:grid;grid-template-columns:64px 1fr;gap:8px;padding:7px 10px;border-bottom:1px solid var(--line);cursor:pointer}
.row:hover,.row.sel{background:var(--panel)}.row.sel{outline:2px solid var(--fg);outline-offset:-2px}
.tag{font-weight:600;font-size:12px}.tag.FAIL{color:var(--fail)}.tag.WARNING{color:var(--warn)}.tag.UNKNOWN{color:var(--unk)}.tag.PASS{color:var(--pass)}
.sub{color:var(--muted);font-size:12px}
#detail{margin-top:10px;border:1px solid var(--line);border-radius:6px;padding:10px 12px;background:var(--panel)}
#detail h3{margin:0 0 6px;font-size:15px}#detail dl{margin:0;display:grid;grid-template-columns:96px 1fr;gap:3px 10px}
#detail dt{color:var(--muted)}#detail dd{margin:0;word-break:break-word}blockquote{margin:6px 0 0;padding:6px 10px;border-left:3px solid var(--line);white-space:pre-wrap}
.view{border:1px solid var(--line);border-radius:6px;background:var(--panel);position:sticky;top:8px}
svg{width:100%;height:68vh;display:block}svg polyline,svg polygon,svg circle{fill:none;stroke-width:1;vector-effect:non-scaling-stroke;opacity:.55}
svg .inv{stroke-width:2;opacity:1}svg .mk{stroke-width:2;vector-effect:non-scaling-stroke}svg .mk.sel{stroke-width:4}
.hint{color:var(--muted);font-size:12px;padding:6px 10px}
@media print{.toolbar,.view button{display:none}#list{max-height:none}}
</style>
</head>
<body>
<header><h1>__TITLE__</h1><table class="meta">__META__</table>__NOTES__</header>
<div class="cards" id="cards"></div>
<main>
<section>
<div class="toolbar" id="filters"></div>
<div id="list" role="listbox" aria-label="問題清單"></div>
<div id="detail" hidden></div>
</section>
<section class="view">
<svg id="plan" viewBox="__VIEWBOX__" role="img" aria-label="圖面示意圖"><g transform="scale(1,-1)" id="geo">__SVG__</g><g transform="scale(1,-1)" id="marks"></g></svg>
<div class="hint">點選左側問題，圖面會定位到問題位置。<button id="reset" type="button">顯示全圖</button></div>
</section>
</main>
<script type="application/json" id="data">__DATA__</script>
<script>
(function(){
"use strict";
var D=JSON.parse(document.getElementById("data").textContent);
var NS="http://www.w3.org/2000/svg",plan=document.getElementById("plan"),marks=document.getElementById("marks");
var ZH={FAIL:"不合格",WARNING:"警告",UNKNOWN:"無法判定",PASS:"合格"};
var active={FAIL:true,WARNING:true,UNKNOWN:true,PASS:false},query="",selected=null,vb0=D.viewBox.split(" ").map(Number);
function el(tag,attrs,text){var e=document.createElement(tag);if(attrs)for(var k in attrs)e.setAttribute(k,attrs[k]);if(text!=null)e.textContent=text;return e;}
var cards=document.getElementById("cards");
["FAIL","WARNING","UNKNOWN","PASS"].forEach(function(s){var c=el("div",{"class":"card "+s});c.appendChild(el("b",null,String(D.counts[s])));c.appendChild(document.createTextNode(ZH[s]));cards.appendChild(c);});
var filters=document.getElementById("filters");
["FAIL","WARNING","UNKNOWN","PASS"].forEach(function(s){var b=el("button",{type:"button","aria-pressed":String(active[s])},ZH[s]);b.onclick=function(){active[s]=!active[s];b.setAttribute("aria-pressed",String(active[s]));render();};filters.appendChild(b);});
var q=el("input",{type:"search",placeholder:"搜尋 編號 / Handle / 圖層 / 規則 / 說明","aria-label":"搜尋"});q.oninput=function(){query=q.value.trim().toLowerCase();render();};filters.appendChild(q);
function hay(i){return [i.id,i.handles.join(" "),i.layer,i.rule,i.ruleName,i.message].join(" ").toLowerCase();}
function render(){
  var list=document.getElementById("list");list.textContent="";
  D.issues.forEach(function(i,idx){
    if(!active[i.status]||(query&&hay(i).indexOf(query)<0))return;
    var r=el("div",{"class":"row"+(idx===selected?" sel":""),role:"option",tabindex:"0"});
    r.appendChild(el("span",{"class":"tag "+i.status},i.statusZh));
    var body=el("div");body.appendChild(el("div",null,i.ruleName+"　"+i.id));
    body.appendChild(el("div",{"class":"sub"},"Handle "+i.handles.join(", ")+" · 圖層 "+i.layer+(i.measured?" · 實測 "+i.measured:"")+(i.required?" · 要求 "+i.required:"")));
    r.appendChild(body);r.onclick=function(){select(idx);};r.onkeydown=function(e){if(e.key==="Enter"||e.key===" "){e.preventDefault();select(idx);}};list.appendChild(r);
  });
  drawMarks();
}
function mark(i,idx,size){
  if(i.x==null)return null;var g=document.createElementNS(NS,"g"),col=D.colors[i.status],sel=idx===selected,e;
  if(i.status==="WARNING"){e=document.createElementNS(NS,"polygon");e.setAttribute("points",[[i.x,i.y+size],[i.x-size,i.y-size],[i.x+size,i.y-size]].map(function(p){return p.join(",");}).join(" "));}
  else if(i.status==="UNKNOWN"){e=document.createElementNS(NS,"rect");e.setAttribute("x",i.x-size);e.setAttribute("y",i.y-size);e.setAttribute("width",2*size);e.setAttribute("height",2*size);}
  else{e=document.createElementNS(NS,"circle");e.setAttribute("cx",i.x);e.setAttribute("cy",i.y);e.setAttribute("r",size);}
  e.setAttribute("class","mk"+(sel?" sel":""));e.setAttribute("stroke",col);e.setAttribute("fill",sel?col:"none");e.setAttribute("fill-opacity","0.25");g.appendChild(e);
  if(i.closest&&sel){var l=document.createElementNS(NS,"line");l.setAttribute("x1",i.closest[0][0]);l.setAttribute("y1",i.closest[0][1]);l.setAttribute("x2",i.closest[1][0]);l.setAttribute("y2",i.closest[1][1]);l.setAttribute("stroke",col);l.setAttribute("class","mk");g.appendChild(l);}
  g.style.cursor="pointer";g.onclick=function(){select(idx);};return g;
}
function drawMarks(){
  marks.textContent="";var size=Math.max(vb0[2],vb0[3])*0.012;
  D.issues.forEach(function(i,idx){if(!active[i.status]||(query&&hay(i).indexOf(query)<0)||idx===selected)return;var m=mark(i,idx,size);if(m)marks.appendChild(m);});
  if(selected!=null){var m2=mark(D.issues[selected],selected,size*1.4);if(m2)marks.appendChild(m2);}
}
function select(idx){
  selected=idx;var i=D.issues[idx],d=document.getElementById("detail");d.hidden=false;d.textContent="";
  d.appendChild(el("h3",null,i.statusZh+"　"+i.id));var dl=el("dl");
  function add(k,v){if(v===""||v==null)return;dl.appendChild(el("dt",null,k));dl.appendChild(el("dd",null,v));}
  add("規則",i.rule+" "+i.ruleName);add("檢查方式",i.measurement);add("Handle",i.handles.join(", "));add("圖層",i.layer);add("系統",i.system);
  add("實測值",i.measured);add("規則要求",i.required);add("說明",i.message);add("判定依據",i.reason);add("建議處理",i.fix);
  add("信心",i.confidence+(i.confidenceReasons.length?"（"+i.confidenceReasons.join("；")+"）":""));add("與基準比較",i.baseline);
  if(i.x!=null)add("位置",i.x.toFixed(1)+", "+i.y.toFixed(1));
  d.appendChild(dl);
  if(i.evidence){d.appendChild(el("div",{"class":"sub"},"規範證據："+i.evidence.ref+"（"+i.evidence.id+"）"));d.appendChild(el("blockquote",null,i.evidence.text));}
  if(i.x!=null){var span=Math.max(vb0[2],vb0[3])*0.12;plan.setAttribute("viewBox",[i.x-span,-i.y-span,2*span,2*span].join(" "));}
  render();d.scrollIntoView({block:"nearest"});
}
document.getElementById("reset").onclick=function(){plan.setAttribute("viewBox",D.viewBox);};
render();
var first=D.issues.findIndex(function(i){return i.status==="FAIL";});if(first>=0)select(first);
})();
</script>
</body>
</html>
"""
