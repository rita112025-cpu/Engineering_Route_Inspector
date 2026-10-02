"""Rule templates behind the Rule Builder: the user picks a sentence-shaped template and fills in blanks,
the server turns that into a validated rule. Nobody has to write JSON.

A *selector* names the drawing objects a blank refers to:
  {"system": "SCADA"}                     objects of a system (layers grouped in the project's systems)
  {"layer": "ZONE-A"} / {"layers": [...]} objects on one / several layers
"""
from __future__ import annotations

import re
from typing import Any

from core.evidence.chunks import numbers_in
from core.rules.schema import RuleError, normalize_rule

TEMPLATES: list[dict[str, Any]] = [
    {"id": "clearance", "name": "水平淨距不得小於",
     "sentence": "「{subject}」與「{target}」的水平淨距不得小於 {value} {unit}",
     "needs": ["subject", "target", "value", "unit"], "optional": ["warn_margin"],
     "measurement": "horizontal_clearance", "operator": ">=", "default_unit": "mm"},
    {"id": "vertical_clearance", "name": "垂直淨距不得小於",
     "sentence": "「{subject}」與「{target}」的垂直淨距（高程差）不得小於 {value} {unit}",
     "needs": ["subject", "target", "value", "unit"], "optional": ["warn_margin"],
     "measurement": "vertical_clearance", "operator": ">=", "default_unit": "mm"},
    {"id": "no_crossing", "name": "不得交叉",
     "sentence": "「{subject}」不得與「{target}」在平面上交叉",
     "needs": ["subject", "target"], "optional": [],
     "measurement": "intersection", "operator": "==", "value": 0},
    {"id": "keep_out", "name": "不得進入區域",
     "sentence": "「{subject}」不得進入「{zone}」",
     "needs": ["subject", "zone"], "optional": [],
     "measurement": "outside_zone", "operator": "==", "value": 1},
    {"id": "inside_zone", "name": "必須位於區域內",
     "sentence": "「{subject}」必須完全位於「{zone}」內",
     "needs": ["subject", "zone"], "optional": [],
     "measurement": "inside_zone", "operator": "==", "value": 1},
    {"id": "max_distance", "name": "附近必須有（距離不超過）",
     "sentence": "「{subject}」距離最近的「{target}」不得超過 {value} {unit}",
     "needs": ["subject", "target", "value", "unit"], "optional": [],
     "measurement": "distance", "operator": "<=", "default_unit": "mm"},
    {"id": "max_length", "name": "單一物件長度不得超過",
     "sentence": "「{subject}」的單一物件長度不得超過 {value} {unit}",
     "needs": ["subject", "value", "unit"], "optional": [],
     "measurement": "length", "operator": "<=", "default_unit": "mm"},
    {"id": "orthogonal", "name": "必須水平或垂直（正交）",
     "sentence": "「{subject}」的走向與水平/垂直的偏差不得超過 {value} 度",
     "needs": ["subject", "value"], "optional": [],
     "measurement": "off_axis_angle", "operator": "<=", "default_value": 1, "unit": "deg"},
    {"id": "min_count", "name": "至少要有幾個物件",
     "sentence": "圖面中「{subject}」至少要有 {value} 個",
     "needs": ["subject", "value"], "optional": [],
     "measurement": "entity_count", "operator": ">=", "unit": "count"},
]
BY_ID = {t["id"]: t for t in TEMPLATES}

UNIT_WORDS = {"mm": "mm", "毫米": "mm", "公釐": "mm", "cm": "cm", "公分": "cm", "m": "m", "公尺": "m", "米": "m",
              "ft": "ft", "呎": "ft", "英尺": "ft", "in": "in", "吋": "in", "英吋": "in", "°": "deg", "度": "deg"}
_NUM_UNIT = re.compile(r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(mm|cm|ft|in|m|毫米|公釐|公分|公尺|英尺|英吋|呎|吋|米|°|度)(?![A-Za-z])",
                       re.IGNORECASE)


def selector_to_predicate(sel: Any, label: str) -> dict:
    if not isinstance(sel, dict) or len(sel) != 1:
        raise RuleError([f"請選擇「{label}」"])
    (kind, val), = sel.items()
    if kind == "system" and isinstance(val, str) and val.strip():
        return {"system": val}
    if kind == "layer" and isinstance(val, str) and val.strip():
        return {"layer_equals": val}
    if kind == "layers" and isinstance(val, list) and val and all(isinstance(v, str) and v for v in val):
        return {"layer_equals": val[0]} if len(val) == 1 else {"or": [{"layer_equals": v} for v in val]}
    raise RuleError([f"「{label}」的選擇格式不正確"])


def describe_selector(sel: dict) -> str:
    (kind, val), = sel.items()
    return "、".join(val) if kind == "layers" else str(val)


def suggest_values(text: str) -> list[dict]:
    """Numbers (with unit when one follows) found in a specification paragraph, in reading order."""
    found = [{"value": float(m.group(1).replace(",", "")), "unit": UNIT_WORDS.get(m.group(2).lower()),
              "text": m.group(0)} for m in _NUM_UNIT.finditer(text or "")]
    if not found:
        found = [{"value": n, "unit": None, "text": f"{n:g}"} for n in numbers_in(text)]
    return found


def new_rule_id(base: str, taken: set[str]) -> str:
    n = 1
    while f"{base}-{n:02d}" in taken:
        n += 1
    return f"{base}-{n:02d}"


def build_rule(template_id: str, params: dict, taken_ids: set[str]) -> dict:
    """Turn the Rule Builder's blanks into a normalized rule. Raises RuleError with readable messages."""
    t = BY_ID.get(template_id)
    if t is None:
        raise RuleError(["找不到這個規則範本"])
    if not isinstance(params, dict):
        raise RuleError(["參數格式錯誤"])
    errs = []
    rule: dict[str, Any] = {"measurement": t["measurement"], "operator": t["operator"]}
    for key, label in (("subject", "檢查對象"), ("target", "附近物件"), ("zone", "區域")):
        if key in t["needs"]:
            try:
                rule[key] = selector_to_predicate(params.get(key), label)
            except RuleError as exc:
                errs.extend(exc.errors)
    if "value" in t["needs"]:
        value = params.get("value", t.get("default_value"))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errs.append("請輸入數值")
        elif value < 0 or (t["measurement"] == "entity_count" and value != int(value)):
            errs.append("數值必須是非負數" + ("（整數）" if t["measurement"] == "entity_count" else ""))
        else:
            rule["value"] = value
    elif "value" in t:
        rule["value"] = t["value"]
    if "unit" in t["needs"]:
        rule["unit"] = params.get("unit") or t["default_unit"]
    elif "unit" in t:
        rule["unit"] = t["unit"]
    wm = params.get("warn_margin")
    if wm not in (None, "", 0):
        rule["warn_margin"] = wm
    severity = str(params.get("severity") or "FAIL").upper()
    rule["severity"] = severity
    if errs:
        raise RuleError(errs)
    # names a person can read
    parts = {"subject": describe_selector(params["subject"]) if "subject" in params else "",
             "target": describe_selector(params["target"]) if "target" in params else "",
             "zone": describe_selector(params["zone"]) if "zone" in params else "",
             "value": f"{rule.get('value', ''):g}" if isinstance(rule.get("value"), (int, float)) else "",
             "unit": rule.get("unit", "")}
    rule["name"] = (params.get("name") or "").strip() or t["sentence"].format(**parts)
    rule["description"] = t["sentence"].format(**parts)
    ev_id = params.get("evidence_id")
    if ev_id:
        if not isinstance(ev_id, str):
            raise RuleError(["規範證據編號格式錯誤"])
        rule["evidence_id"] = ev_id
    base = re.sub(r"[^A-Za-z0-9]+", "-", template_id.upper()).strip("-")
    rule["id"] = params.get("id") or new_rule_id(base, taken_ids)
    return normalize_rule(rule)


def public_templates() -> list[dict]:
    return [{k: v for k, v in t.items() if k in ("id", "name", "sentence", "needs", "optional", "default_unit",
                                                    "default_value")} for t in TEMPLATES]
