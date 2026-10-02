"""Rule set JSON format and validation.

Rule file::

  {
    "format": "eri-rules", "version": 1,
    "systems": [{"name": "SCADA", "layer_regex": "^SCADA"}],
    "rules": [ {rule}, ... ]
  }

Rule::

  {
    "id": "SCADA-CLEARANCE-H-300", "name": "SCADA 水平淨距",
    "severity": "FAIL" | "WARNING",
    "subject": {predicate}, "target": {predicate}, "zone": {predicate},
    "pair_filter": {pair predicate},
    "measurement": "horizontal_clearance", "operator": ">=", "value": 300, "unit": "mm",
    "warn_margin": 50,
    "evidence_id": "EV-...", "evidence_ref": {"document": "spec.txt", "quote": "300 mm"},
    "fix_hint": "...", "description": "...", "enabled": true
  }
"""
from __future__ import annotations

import copy
import re
from typing import Any

from . import predicate as P

PAIR_MEASUREMENTS = {"distance", "minimum_distance", "horizontal_clearance", "vertical_clearance",
                     "intersection", "overlap"}
ZONE_MEASUREMENTS = {"inside_zone", "outside_zone"}
SINGLE_MEASUREMENTS = {"length", "angle", "off_axis_angle"}
GLOBAL_MEASUREMENTS = {"entity_count"}
MEASUREMENTS = PAIR_MEASUREMENTS | ZONE_MEASUREMENTS | SINGLE_MEASUREMENTS | GLOBAL_MEASUREMENTS
OPERATORS = {">=", ">", "<=", "<", "==", "!="}
SEVERITIES = {"FAIL", "WARNING"}
UNITS_TO_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "ft": 304.8}
ANGLE_UNITS = {"deg"}
COUNT_UNITS = {"count", ""}
RULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,79}$")

MEASUREMENT_ZH = {
    "distance": "距離", "minimum_distance": "最短距離", "horizontal_clearance": "水平淨距",
    "vertical_clearance": "垂直淨距", "intersection": "交叉", "overlap": "重疊長度",
    "inside_zone": "位於區域內", "outside_zone": "位於區域外", "length": "長度",
    "angle": "角度", "off_axis_angle": "偏離正交角度", "entity_count": "物件數量",
}

DISTANCE_MEASUREMENTS = {"distance", "minimum_distance", "horizontal_clearance", "vertical_clearance"}


class RuleError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def default_unit(measurement: str) -> str:
    if measurement in ("angle", "off_axis_angle"):
        return "deg"
    if measurement in ("intersection", "entity_count", "inside_zone", "outside_zone"):
        return "count"
    return "mm"


def default_quantifier(r: dict) -> str:
    """'all': every nearby target must satisfy (clearances, no-crossing).
    'any': at least one target must satisfy (e.g. "must be within 500 mm of a support")."""
    m, op, val = r.get("measurement"), r.get("operator"), r.get("value")
    if m in DISTANCE_MEASUREMENTS and op in ("<=", "<"):
        return "any"
    if m == "intersection" and op in (">=", ">", "==") and isinstance(val, (int, float)) and val >= 1:
        return "any"
    return "all"


def normalize_rule(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and fill defaults. Raises RuleError with readable messages."""
    errs: list[str] = []
    if not isinstance(raw, dict):
        raise RuleError(["規則必須是物件"])
    r = copy.deepcopy(raw)
    rid = r.get("id")
    label = rid or "(未命名)"
    if not isinstance(rid, str) or not RULE_ID_RE.match(rid):
        errs.append(f"{label}: 規則 ID 只能包含英數字、-、_、.，且不可為空")
    name = r.get("name")
    if name is not None and not isinstance(name, str):
        errs.append(f"{label}: 名稱必須是文字")
    if not isinstance(name, str) or not name.strip():
        r["name"] = rid if isinstance(rid, str) else ""
    sev_raw = r.get("severity", "FAIL")
    sev = sev_raw.upper() if isinstance(sev_raw, str) else None
    if sev not in SEVERITIES:
        errs.append(f"{label}: 嚴重度只能是 FAIL 或 WARNING")
    r["severity"] = sev
    m = r.get("measurement")
    if not isinstance(m, str) or m not in MEASUREMENTS:
        errs.append(f"{label}: 不支援的檢查方式 {m!r}")
        m = None                      # later checks only look at a valid measurement
    if m in ("inside_zone", "outside_zone"):
        r.setdefault("operator", "==")
        r.setdefault("value", 1)
    if m == "intersection":
        r.setdefault("operator", "==")
        r.setdefault("value", 0)
    op = r.get("operator")
    if not isinstance(op, str) or op not in OPERATORS:
        errs.append(f"{label}: 不支援的比較運算子 {op!r}")
    val = r.get("value")
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        errs.append(f"{label}: 數值必須是數字")
    unit = r.get("unit")
    if unit is None or unit == "":
        unit = default_unit(m or "")
    elif not isinstance(unit, str):
        errs.append(f"{label}: 單位必須是文字")
        unit = default_unit(m or "")
    r["unit"] = unit
    if m in (PAIR_MEASUREMENTS - {"intersection"}) | {"length"}:
        if unit not in UNITS_TO_MM:
            errs.append(f"{label}: 長度單位必須是 {sorted(UNITS_TO_MM)}")
    elif m in ("angle", "off_axis_angle"):
        if unit not in ANGLE_UNITS:
            errs.append(f"{label}: 角度單位必須是 deg")
    wm = r.get("warn_margin")
    if wm is not None and (isinstance(wm, bool) or not isinstance(wm, (int, float)) or wm < 0):
        errs.append(f"{label}: warn_margin 必須是非負數字")
    try:
        if "subject" not in r:
            errs.append(f"{label}: 缺少 subject（檢查對象）")
        else:
            P.validate(r["subject"], "entity")
        if m in PAIR_MEASUREMENTS:
            if "target" not in r:
                errs.append(f"{label}: 此檢查方式需要 target（附近物件條件）")
            else:
                P.validate(r["target"], "entity")
        if m in ZONE_MEASUREMENTS:
            if "zone" not in r:
                errs.append(f"{label}: 此檢查方式需要 zone（區域條件）")
            else:
                P.validate(r["zone"], "entity")
        if "pair_filter" in r and r["pair_filter"]:
            P.validate(r["pair_filter"], "pair")
    except P.PredicateError as exc:
        errs.append(f"{label}: {exc}")
    ref = r.get("evidence_ref")
    if ref is not None:
        if not isinstance(ref, dict) or not isinstance(ref.get("quote", ""), str):
            errs.append(f"{label}: evidence_ref 格式錯誤")
    q = r.get("quantifier")
    if q is None:
        q = default_quantifier(r) if not errs else "all"
    if not isinstance(q, str) or q not in ("all", "any"):
        errs.append(f"{label}: quantifier 只能是 all 或 any")
    r["quantifier"] = q
    r["enabled"] = bool(r.get("enabled", True))
    if errs:
        raise RuleError(errs)
    return r


def normalize_ruleset(raw: dict[str, Any]) -> dict[str, Any]:
    errs: list[str] = []
    if not isinstance(raw, dict):
        raise RuleError(["規則檔必須是 JSON 物件"])
    systems = raw.get("systems", [])
    if not isinstance(systems, list):
        errs.append("systems 必須是清單")
        systems = []
    for s in systems:
        if not isinstance(s, dict) or not isinstance(s.get("name"), str) or not isinstance(s.get("layer_regex"), str):
            errs.append("每個 system 需要 name 與 layer_regex")
            continue
        try:
            P.compile_regex(s["layer_regex"])
        except P.PredicateError as exc:
            errs.append(str(exc))
    rules = []
    seen = set()
    for raw_rule in raw.get("rules", []):
        try:
            rule = normalize_rule(raw_rule)
        except RuleError as exc:
            errs.extend(exc.errors)
            continue
        if rule["id"] in seen:
            errs.append(f"規則 ID 重複：{rule['id']}")
        seen.add(rule["id"])
        rules.append(rule)
    if errs:
        raise RuleError(errs)
    return {"format": "eri-rules", "version": 1, "systems": systems, "rules": rules}


def describe_requirement(rule: dict) -> str:
    m = rule["measurement"]
    if m == "inside_zone":
        return "必須完全位於指定區域內"
    if m == "outside_zone":
        return "不得進入指定區域"
    if m == "intersection":
        return f"交叉數 {rule['operator']} {rule['value']:g}"
    return f"{rule['operator']} {rule['value']:g} {rule['unit']}"
