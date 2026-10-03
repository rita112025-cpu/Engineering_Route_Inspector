"""Rule evaluation over normalized entities using a spatial index."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Sequence

from core.geometry.measure import (
    entity_distance, max_off_axis, overlap_length, principal_angle, zone_relation,
)
from core.evidence.chunks import quantities_in
from core.geometry.primitives import expand_bbox
from core.models.entities import GeometryEntity
from core.models.results import EvidenceChunk, RuleResult
from core.spatial.index import GridIndex
from . import predicate as P
from .schema import MEASUREMENT_ZH, UNITS_TO_MM, describe_requirement

EPS = 1e-6
WITHIN_MEASUREMENTS = {"distance", "minimum_distance", "horizontal_clearance"}


class Cancelled(Exception):
    pass


@dataclass
class RuleContext:
    entities: Sequence[GeometryEntity]
    index: object                      # GridIndex / BruteForceIndex (query(bbox) -> [idx])
    unit_to_mm: float = 1.0            # drawing unit -> millimetres
    units_assumed: bool = False        # True when $INSUNITS was missing and mm was assumed
    evidence_by_id: dict[str, EvidenceChunk] = field(default_factory=dict)
    evidence_resolver: Callable[[dict], EvidenceChunk | None] | None = None
    cancel_check: Callable[[], bool] = lambda: False

    def __post_init__(self):
        self.pos = {id(e): i for i, e in enumerate(self.entities)}
        # largest drawing dimension: search radius when a rule must consider every target
        if self.entities:
            minx = min(e.bbox[0] for e in self.entities); maxx = max(e.bbox[2] for e in self.entities)
            miny = min(e.bbox[1] for e in self.entities); maxy = max(e.bbox[3] for e in self.entities)
            self.extent = max(maxx - minx, maxy - miny)
        else:
            self.extent = 0.0


def compare(v: float, op: str, ref: float) -> bool:
    if op == ">=":
        return v >= ref - EPS
    if op == ">":
        return v > ref + EPS
    if op == "<=":
        return v <= ref + EPS
    if op == "<":
        return v < ref - EPS
    if op == "==":
        return abs(v - ref) <= EPS
    if op == "!=":
        return abs(v - ref) > EPS
    raise ValueError(op)


def fmt(v: float | None) -> str:
    """Up to 2 decimals, trailing zeros dropped, so 299.96 is never shown as 300."""
    if v is None:
        return "—"
    if abs(v - round(v)) < 0.005:
        return f"{round(v):d}"
    return f"{v:.2f}".rstrip("0").rstrip(".")


def fmt_gap(gap: float) -> str:
    """A positive shortfall, never rounded down to 0."""
    return "<0.01" if 0 < gap < 0.005 else fmt(gap)


def label(e: GeometryEntity) -> str:
    sys_name = e.metadata.get("system")
    head = f"{sys_name} " if sys_name else ""
    return f"{head}{e.entity_type}（圖層 {e.layer}，Handle {e.handle}）"


def _selects(rule_pred: dict, ctx: RuleContext) -> list[GeometryEntity]:
    allow_text = P.explicitly_mentions_text(rule_pred)
    return [e for e in ctx.entities
            if (e.is_geometric or allow_text) and P.match_entity(rule_pred, e)]


def _z_info(e: GeometryEntity):
    md = e.metadata
    if not md.get("z_known"):
        return None
    return (float(md.get("z_min", 0.0)), float(md.get("z_max", 0.0)) + float(md.get("height", 0.0)))


def _classification(e: GeometryEntity) -> dict:
    return {
        "handle": e.handle, "layer": e.layer, "entity_type": e.entity_type,
        "system": e.metadata.get("system") or "",
        "classification_source": e.metadata.get("classification_source", "未分類（無符合的系統圖層規則）"),
    }


class RuleEvaluator:
    def __init__(self, rule: dict, ctx: RuleContext):
        self.rule = rule
        self.ctx = ctx
        self.m = rule["measurement"]
        self.unit = rule["unit"]
        self.op = rule["operator"]
        self.value = float(rule["value"])
        self.margin = float(rule.get("warn_margin") or 0.0)
        self._eligible: dict[tuple, tuple] = {}
        self.unit_mm = UNITS_TO_MM.get(self.unit, 1.0)
        self.mzh = MEASUREMENT_ZH.get(self.m, self.m)

    # -- unit helpers -------------------------------------------------
    def to_rule_units(self, drawing_len: float) -> float:
        return drawing_len * self.ctx.unit_to_mm / self.unit_mm

    def to_drawing_units(self, rule_len: float) -> float:
        return rule_len * self.unit_mm / self.ctx.unit_to_mm

    def _check(self):
        if self.ctx.cancel_check():
            raise Cancelled()

    # -- result factory ----------------------------------------------
    def result(self, status: str, subject: GeometryEntity | None, targets=(), measured=None,
               location=None, message="", reason="", details=None) -> RuleResult:
        r = self.rule
        subjects = [subject] if subject is not None else []
        d = {"subject": [_classification(s) for s in subjects],
             "targets": [_classification(t) for t in targets],
             "measurement_zh": self.mzh,
             "subject_rule": P.describe(r["subject"]),
             "target_rule": P.describe(r["target"]) if r.get("target") else "",
             "quantifier": r.get("quantifier", "all")}
        if details:
            d.update(details)
        return RuleResult(
            rule_id=r["id"], rule_name=r["name"], status=status, severity=r["severity"],
            measurement=self.m,
            subject_ids=[s.id for s in subjects], subject_handles=[s.handle for s in subjects],
            target_ids=[t.id for t in targets], target_handles=[t.handle for t in targets],
            layer=subject.layer if subject else "",
            system=(subject.metadata.get("system") or "") if subject else "",
            location=location, measured=None if measured is None else round(measured, 3),
            required_op=self.op, required_value=self.value, unit=self.unit,
            message=message, reason=reason, details=d,
        )

    def classify_value(self, v: float) -> str:
        if not compare(v, self.op, self.value):
            return self.rule["severity"]
        if self.margin > 0:
            if self.op in (">=", ">") and v < self.value + self.margin - EPS:
                return "WARNING"
            if self.op in ("<=", "<") and v > self.value - self.margin + EPS:
                return "WARNING"
        return "PASS"

    def fix_text(self, v: float | None, subj: str, tgt: str = "") -> str:
        if self.rule.get("fix_hint"):
            return self.rule["fix_hint"]
        if self.m in ("distance", "minimum_distance", "horizontal_clearance") and self.op in (">=", ">") and v is not None:
            return f"將 {subj} 與 {tgt} 的間距增加至少 {fmt_gap(self.value - v)} {self.unit}（移動或改道其中一條路徑）。"
        if self.m == "vertical_clearance":
            return "補充或確認兩者的安裝高程（Z 值），再判斷垂直淨距；必要時調整其中一者的高程。"
        if self.m == "intersection":
            return f"調整 {subj} 路徑避免與 {tgt} 交叉，或以剖面/高程資料證明兩者位於不同高度。"
        if self.m == "outside_zone":
            return f"將 {subj} 改道避開禁設區域。"
        if self.m == "inside_zone":
            return f"將 {subj} 移入指定區域內。"
        if self.m == "length":
            return f"調整 {subj} 長度使其 {self.op} {fmt(self.value)} {self.unit}。"
        if self.m in ("angle", "off_axis_angle"):
            return f"調整 {subj} 走向，使角度 {self.op} {fmt(self.value)}°。"
        return f"調整設計使其符合「{describe_requirement(self.rule)}」。"

    # -- evaluation -----------------------------------------------------
    def evaluate(self) -> list[RuleResult]:
        m = self.m
        if m == "entity_count":
            return self.eval_count()
        if m in ("length", "angle", "off_axis_angle"):
            return self.eval_single()
        if m in ("inside_zone", "outside_zone"):
            return self.eval_zone()
        return self.eval_pairs()

    def eval_count(self) -> list[RuleResult]:
        subjects = _selects(self.rule["subject"], self.ctx)
        n = float(len(subjects))
        status = self.classify_value(n)
        loc = None
        if subjects:
            xs = [s.center()[0] for s in subjects]; ys = [s.center()[1] for s in subjects]
            loc = (sum(xs) / len(xs), sum(ys) / len(ys))
        ok = status == "PASS"
        msg = (f"符合「{P.describe(self.rule['subject'])}」的物件共 {len(subjects)} 個，"
               + ("符合" if ok else "不符合") + f"規則要求（{self.op} {fmt(self.value)}）。")
        res = self.result(status, None, (), n, loc, msg, "物件數量統計",
                          {"matched_handles": [s.handle for s in subjects[:200]]})
        res.fix = "" if ok else self.fix_text(n, "相關物件")
        return [res]

    def eval_single(self) -> list[RuleResult]:
        out = []
        for s in _selects(self.rule["subject"], self.ctx):
            self._check()
            if self.m == "length":
                v = self.to_rule_units(s.length())
            elif self.m == "angle":
                v = principal_angle(s)
            else:
                v = max_off_axis(s)
            if v is None:
                out.append(self.result("UNKNOWN", s, (), None, s.center(),
                                       f"{label(s)} 沒有可量測的方向（例如圓形或單點），無法判定{self.mzh}。",
                                       "Insufficient evidence：物件不具備此量測所需的幾何資訊"))
                continue
            status = self.classify_value(v)
            unit = "°" if self.unit == "deg" else f" {self.unit}"
            if status == "PASS":
                msg = f"{label(s)} 的{self.mzh}為 {fmt(v)}{unit}，符合規則要求（{self.op} {fmt(self.value)}{unit}）。"
            else:
                msg = f"{label(s)} 的{self.mzh}為 {fmt(v)}{unit}，不符合規則要求（{self.op} {fmt(self.value)}{unit}）。"
            r = self.result(status, s, (), v, s.center(), msg, f"量測物件{self.mzh}")
            if status != "PASS":
                r.fix = self.fix_text(v, label(s))
            out.append(r)
        return out

    def eval_zone(self) -> list[RuleResult]:
        ctx = self.ctx
        zones = [z for z in _selects(self.rule["zone"], ctx) if z.closed]
        zone_ids = {id(z) for z in zones}
        out = []
        for s in _selects(self.rule["subject"], ctx):
            self._check()
            if not zones:
                out.append(self.result("UNKNOWN", s, (), None, s.center(),
                                       f"圖面中找不到符合「{P.describe(self.rule['zone'])}」的封閉區域，無法判定 {label(s)} 是否在區域內。",
                                       "Insufficient evidence：找不到區域圖元"))
                continue
            touched, inside_any = [], False
            for idx in ctx.index.query(s.bbox):
                z = ctx.entities[idx]
                if id(z) not in zone_ids or z is s:
                    continue
                rel = zone_relation(s, z)
                if rel != "outside":
                    touched.append(z)
                if rel == "inside":
                    inside_any = True
            v = 1.0 if (inside_any if self.m == "inside_zone" else not touched) else 0.0
            status = self.classify_value(v)
            loc = s.center()
            if self.m == "outside_zone":
                msg = (f"{label(s)} 未進入禁設區域。" if v == 1.0 else
                       f"{label(s)} 進入了區域 " + "、".join(f"{z.layer}（Handle {z.handle}）" for z in touched) + "。")
            else:
                msg = (f"{label(s)} 完全位於指定區域內。" if v == 1.0 else f"{label(s)} 不在（或未完全在）指定區域內。")
            r = self.result(status, s, touched, v, loc, msg, "區域包含關係（2D 平面判斷）")
            if status != "PASS":
                r.fix = self.fix_text(v, label(s))
            out.append(r)
        return out

    def _search_radius(self) -> float:
        """Search distance in drawing units for candidate targets."""
        if self.m in ("intersection", "overlap", "vertical_clearance"):
            return self.to_drawing_units(EPS) if self.ctx.unit_to_mm else EPS
        base = self.value + self.margin
        if self.op in (">=", ">", "==", "!="):
            return max(self.to_drawing_units(base) * 2.0, EPS)
        return max(self.to_drawing_units(self.value) * 3.0, EPS)

    def pair_value(self, s: GeometryEntity, t: GeometryEntity, cutoff: float):
        """Returns (kind, value_in_rule_units, DistanceResult, reason)."""
        dr = entity_distance(s, t, cutoff)
        if dr.distance > cutoff + EPS:
            return None
        m = self.m
        if m in ("distance", "minimum_distance"):
            return ("value", self.to_rule_units(dr.distance), dr, "2D 平面最短距離")
        if m == "horizontal_clearance":
            zs, zt = _z_info(s), _z_info(t)
            if dr.distance <= EPS:
                if zs is None or zt is None:
                    return ("unknown", None, dr,
                            "Insufficient evidence：兩者在平面上交疊/交叉，但圖面沒有高程(Z)資料，無法判斷是否位於同一高度")
                if zs[1] < zt[0] or zt[1] < zs[0]:
                    return None  # different levels: not a horizontal clearance situation
            return ("value", self.to_rule_units(dr.distance), dr, "2D 平面水平淨距")
        if m == "vertical_clearance":
            if dr.distance > EPS:
                return None  # not stacked in plan
            zs, zt = _z_info(s), _z_info(t)
            if zs is None or zt is None:
                return ("unknown", None, dr,
                        "Insufficient evidence：2D 圖面沒有高程(Z)資料，無法量測垂直淨距")
            gap = max(0.0, zt[0] - zs[1], zs[0] - zt[1])
            return ("value", self.to_rule_units(gap), dr, "依 Z 高程計算垂直淨距")
        if m == "intersection":
            return ("value", 1.0 if dr.distance <= EPS else 0.0, dr, "2D 平面交叉判斷")
        if m == "overlap":
            ol = overlap_length(s, t, tol=max(EPS, self.to_drawing_units(0.01)))
            if ol <= 0 and not dr.contained:
                return None
            return ("value", self.to_rule_units(ol), dr, "2D 邊界重疊長度")
        return None

    def pair_message(self, status, s, t, v, unknown_reason="", contained=False) -> str:
        S, T = label(s), (label(t) if t is not None else "")
        u = self.unit
        if status == "UNKNOWN":
            return f"{S} 與 {T} 的{self.mzh}無法判定。{unknown_reason}"
        if self.m == "intersection":
            if v and v >= 1:
                if contained:
                    return f"{S} 完全位於 {T} 的範圍內（平面上重疊，沒有穿越邊界）。"
                return f"{S} 與 {T} 在平面上交叉。"
            return f"{S} 與附近目標物件沒有交叉。"
        if v is None:
            return f"{S} 在搜尋範圍內找不到符合「{P.describe(self.rule['target'])}」的物件。"
        req = f"{self.op} {fmt(self.value)} {u}"
        if status == "PASS":
            if t is None:
                return f"{S} 附近沒有違反規則的物件（要求 {req}）。"
            return f"{S} 與 {T} 的{self.mzh}為 {fmt(v)} {u}，符合規則要求的 {req}。"
        if self.op in (">=", ">") and not compare(v, self.op, self.value):
            return f"{S} 與 {T} 的{self.mzh}為 {fmt(v)} {u}，低於規則要求的 {fmt(self.value)} {u}。"
        if self.op in ("<=", "<") and not compare(v, self.op, self.value):
            return f"{S} 與 {T} 的{self.mzh}為 {fmt(v)} {u}，超過規則上限 {fmt(self.value)} {u}。"
        if status == "WARNING":
            return f"{S} 與 {T} 的{self.mzh}為 {fmt(v)} {u}，雖符合 {req}，但在警示範圍（{fmt(self.margin)} {u}）內。"
        return f"{S} 與 {T} 的{self.mzh}為 {fmt(v)} {u}，不符合規則要求的 {req}。"

    def eval_pairs(self) -> list[RuleResult]:
        ctx, rule = self.ctx, self.rule
        subjects = _selects(rule["subject"], ctx)
        target_pred = rule["target"]
        allow_text = P.explicitly_mentions_text(target_pred)
        target_flag = [((e.is_geometric or allow_text) and P.match_entity(target_pred, e)) for e in ctx.entities]
        any_target = any(target_flag)
        pair_filter = rule.get("pair_filter") or None
        radius = self._search_radius()
        quant = rule.get("quantifier", "all")
        emitted: set[frozenset] = set()
        out: list[RuleResult] = []
        # "every target within X" for distances is answered with the spatial index (see _all_within);
        # targets are grouped by (layer, system) because that is all a pair filter can look at
        within_mode = quant == "all" and self.op in ("<=", "<") and self.m in WITHIN_MEASUREMENTS
        groups: dict[tuple, list[int]] = {}
        if within_mode:
            for i, flag in enumerate(target_flag):
                if flag:
                    e = ctx.entities[i]
                    groups.setdefault((e.layer.casefold(), (e.metadata.get("system") or "").casefold() or None), []).append(i)
        for s in subjects:
            self._check()
            if not any_target:
                if quant == "any":
                    out.append(self.result("UNKNOWN", s, (), None, s.center(),
                                           f"圖面中沒有符合「{P.describe(target_pred)}」的物件，無法判定 {label(s)}。",
                                           "Insufficient evidence：找不到目標物件"))
                else:
                    out.append(self.result("PASS", s, (), None, s.center(),
                                           f"圖面中沒有符合「{P.describe(target_pred)}」的物件，{label(s)} 無衝突對象。",
                                           "無目標物件"))
                continue
            sid = ctx.pos[id(s)]
            if within_mode:
                out.append(self._all_within(s, sid, target_flag, groups, pair_filter))
                continue
            evals = []
            for idx in ctx.index.query(expand_bbox(s.bbox, radius)):
                if idx == sid or not target_flag[idx]:
                    continue
                t = ctx.entities[idx]
                if pair_filter and not P.match_pair(pair_filter, s, t):
                    continue
                pv = self.pair_value(s, t, radius)
                if pv is None:
                    continue
                kind, v, dr, why = pv
                status = "UNKNOWN" if kind == "unknown" else self.classify_value(v)
                evals.append((status, t, v, dr, why))
            if quant == "any":
                out.extend(self._resolve_any(s, evals))
            else:
                out.extend(self._resolve_all(s, evals, emitted))
        return out

    def _pair_result(self, status, s, t, v, dr, why) -> RuleResult:
        msg = self.pair_message(status, s, t, v, why if status == "UNKNOWN" else "",
                                contained=bool(dr is not None and dr.contained))
        r = self.result(status, s, [t] if t is not None else [], v,
                        dr.midpoint if dr is not None else s.center(), msg,
                        why, {"closest_points": [list(dr.point_a), list(dr.point_b)] if dr else None})
        if status != "PASS":
            r.fix = self.fix_text(v, label(s), label(t) if t is not None else "")
        return r

    def _all_within(self, s, sid, target_flag, groups, pair_filter) -> RuleResult:
        """"Every target must be within X" without comparing each subject with each target.

        The spatial index returns the targets inside X; every eligible target outside that window violates
        the rule, so the violation count is ``eligible - found`` and only one violator is measured (the first
        in drawing order). A pair filter only looks at layer and system, so eligibility is decided once per
        (layer, system) group. One result per subject.
        """
        ctx = self.ctx
        ents = ctx.entities
        # a pair filter only looks at layer and system (predicate.PAIR_KEYS; a test pins that), so the set of
        # eligible targets is the same for every subject of one (layer, system): compute it once per key
        key = (s.layer.casefold(), (s.metadata.get("system") or "").casefold() or None)
        cached = self._eligible.get(key)
        if cached is None:
            eligible = [idxs for idxs in groups.values()
                        if not pair_filter or P.match_pair(pair_filter, s, ents[idxs[0]])]
            cached = self._eligible[key] = (eligible, {i for idxs in eligible for i in idxs})
        eligible, eligible_set = cached
        others = len(eligible_set) - (1 if sid in eligible_set else 0)
        # The window is X plus the tolerance compare() allows: a target up to EPS beyond X still satisfies
        # "<= X", so it has to be evaluated like any other target in the window. If it were left outside it would
        # be counted as "far" (a violation) while its own value classifies as PASS: a PASS with violations.count 1.
        reach = max(self.to_drawing_units(self.value), EPS) + self.to_drawing_units(EPS)
        found: dict[int, tuple] = {}
        skipped: set[int] = set()                          # inside the window but not comparable (other level)
        for idx in ctx.index.query(expand_bbox(s.bbox, reach)):
            if idx == sid or idx not in eligible_set:
                continue
            t = ents[idx]
            pv = self.pair_value(s, t, reach)
            if pv is None:
                if entity_distance(s, t, reach).distance <= reach + EPS:
                    skipped.add(idx)                       # e.g. different elevations: not part of the comparison
                continue                                   # otherwise farther than X (window corner)
            kind, v, dr, why = pv
            found[idx] = ("UNKNOWN" if kind == "unknown" else self.classify_value(v), t, v, dr, why)
        far = others - len(found) - len(skipped)
        if far > 0:
            handles, first = [], None
            for idxs in eligible:
                for i in idxs:
                    if i == sid or i in found or i in skipped:
                        continue
                    first = i if first is None else first
                    handles.append(ents[i].handle)
                    if len(handles) >= 20:                 # enough to show: never walk every target per subject
                        break
                if len(handles) >= 20:
                    break
            t = ents[first]
            dr = entity_distance(s, t)
            v = self.to_rule_units(dr.distance)
            res = self._pair_result(self.classify_value(v), s, t, v, dr, "2D 平面最短距離")
            # targets inside the window whose own value fails the rule (e.g. just under X for a strict "< X") are
            # violations too: the count is the number of targets that fail, not only the ones beyond the window
            failing_found = [ev for ev in found.values() if ev[2] is not None and not compare(ev[2], self.op, self.value)]
            total = far + len(failing_found)
            handles.extend(ev[1].handle for ev in failing_found[:max(0, 20 - len(handles))])
            res.details["violations"] = {"count": total, "handles": handles[:20]}
            if total > 1:
                res.message += f"（另有 {total - 1} 個目標同樣不符；這裡列出的是圖面順序中的第一個）"
            return res
        evals = list(found.values())
        collapsed = self._collapse_upper_bound(s, evals)
        if collapsed is not None:
            return collapsed
        best = min(evals, key=lambda ev: -ev[2]) if evals else None
        if best is None:
            return self.result("PASS", s, (), None, s.center(), self.pair_message("PASS", s, None, None), "搜尋範圍內無目標物件")
        return self._pair_result("PASS", s, best[1], best[2], best[3], best[4])

    def _collapse_upper_bound(self, s, evals) -> RuleResult | None:
        """"every target must be within X": one result per subject, for the worst offender.

        Reporting each too-far target separately would produce subjects x targets results.
        """
        bad = [ev for ev in evals if ev[0] != "PASS"]
        if not bad:
            return None
        rank = {"FAIL": 0, "WARNING": 1, "UNKNOWN": 2}
        top = min(rank[ev[0]] for ev in bad)
        group = [ev for ev in bad if rank[ev[0]] == top]
        status, t, v, dr, why = max(group, key=lambda ev: ev[2] if ev[2] is not None else -math.inf)
        res = self._pair_result(status, s, t, v, dr, why)
        res.details["violations"] = {"count": len(group), "handles": [ev[1].handle for ev in group[:20]]}
        if len(group) > 1:
            res.message += f"（另有 {len(group) - 1} 個目標同樣不符）"
        return res

    def _resolve_all(self, s, evals, emitted) -> list[RuleResult]:
        out = []
        if self.op in ("<=", "<"):
            collapsed = self._collapse_upper_bound(s, evals)
            if collapsed is not None:
                return [collapsed]
            evals = [ev for ev in evals if ev[0] == "PASS"]
        for status, t, v, dr, why in evals:
            if status == "PASS":
                continue
            key = frozenset((s.id, t.id))
            if key in emitted:
                continue
            emitted.add(key)
            out.append(self._pair_result(status, s, t, v, dr, why))
        if not out and not any(ev[0] != "PASS" for ev in evals):
            passes = [ev for ev in evals if ev[0] == "PASS"]
            if passes:
                best = min(passes, key=lambda ev: ev[2] if self.op in (">=", ">") else -ev[2])
                out.append(self._pair_result("PASS", s, best[1], best[2], best[3], best[4]))
            else:
                out.append(self.result("PASS", s, (), None, s.center(),
                                       self.pair_message("PASS", s, None, None) if self.m != "intersection"
                                       else f"{label(s)} 與附近目標物件沒有交叉。",
                                       "搜尋範圍內無目標物件"))
        return out

    def _resolve_any(self, s, evals) -> list[RuleResult]:
        passes = [ev for ev in evals if ev[0] == "PASS"]
        if passes:
            best = min(passes, key=lambda ev: abs(ev[2] - self.value))
            return [self._pair_result("PASS", s, best[1], best[2], best[3], best[4])]
        warns = [ev for ev in evals if ev[0] == "WARNING" and ev[0] != self.rule["severity"]]
        if warns:
            w = warns[0]
            return [self._pair_result("WARNING", s, w[1], w[2], w[3], w[4])]
        unknowns = [ev for ev in evals if ev[0] == "UNKNOWN"]
        if unknowns:
            u = unknowns[0]
            return [self._pair_result("UNKNOWN", s, u[1], u[2], u[3], u[4])]
        valued = [ev for ev in evals if ev[2] is not None]
        if valued:
            best = min(valued, key=lambda ev: abs(ev[2] - self.value))
            return [self._pair_result(best[0], s, best[1], best[2], best[3], best[4])]
        r = self.result(self.rule["severity"], s, (), None, s.center(),
                        self.pair_message(self.rule["severity"], s, None, None),
                        f"搜尋半徑 {fmt(self.to_rule_units(self._search_radius()))} {self.unit} 內沒有目標物件")
        r.fix = self.fix_text(None, label(s))
        return [r]


NUMERIC_VALUE_MEASUREMENTS = {"distance", "minimum_distance", "horizontal_clearance", "vertical_clearance",
                              "overlap", "length", "angle", "off_axis_angle"}
ANGLE_MEASUREMENTS = {"angle", "off_axis_angle"}


def evidence_number_problem(rule: dict, evidence: EvidenceChunk) -> str:
    """Why the evidence text does not support the rule's value ('' when it does or is not checkable).

    Quantities are compared as value *and* unit, normalised to millimetres (or degrees): 300 mm = 30 cm =
    0.3 m, but 300 cm is 3000 mm. A bare number never supports a length or an angle.
    """
    m = rule["measurement"]
    if m not in NUMERIC_VALUE_MEASUREMENTS:
        return ""
    ref = rule.get("evidence_ref") or {}
    text = ref.get("quote") if ref.get("quote") else evidence.text
    value, unit = float(rule["value"]), rule.get("unit", "")
    angle = m in ANGLE_MEASUREMENTS
    want = value if angle else value * UNITS_TO_MM.get(unit, 1.0)
    same_kind = [(v, u, t) for v, u, t in quantities_in(text) if (u == "deg") == angle]
    if not same_kind:
        return f"規範證據文字沒有帶單位的數值，無法佐證規則數值 {fmt(value)} {unit}"
    for v, u, _ in same_kind:
        got = v if angle else v * UNITS_TO_MM[u]
        if math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-9):
            return ""
    shown = "、".join(t for _, _, t in same_kind[:4])
    return f"規範證據中的數值（{shown}）與規則數值 {fmt(value)} {unit} 不一致"


def apply_confidence(results: list[RuleResult], rule: dict, evidence: EvidenceChunk | None,
                     ctx: RuleContext) -> None:
    mismatch = evidence_number_problem(rule, evidence) if evidence is not None else ""
    for r in results:
        reasons = []
        if evidence is not None:
            r.evidence_id = evidence.id
        if r.status == "UNKNOWN":
            r.confidence = "UNKNOWN"
            reasons.append("資料不足，無法判定")
        else:
            if evidence is None:
                reasons.append("規則未連結到已匯入的規範證據")
            elif mismatch:
                reasons.append(mismatch)
            if ctx.units_assumed and r.measurement not in ("entity_count", "angle", "off_axis_angle",
                                                          "intersection", "inside_zone", "outside_zone"):
                reasons.append("圖面未宣告單位（$INSUNITS），以 mm 推定")
            r.confidence = "CONFIRMED" if not reasons else "INFERRED"
            if not reasons:
                if r.measurement in NUMERIC_VALUE_MEASUREMENTS:
                    reasons.append("規範原文含相同的數值與單位（比較方向與適用對象仍需人工確認）")
                else:
                    reasons.append("已連結規範原文（此類規則沒有數值可與原文比對；適用對象與方向仍需人工確認）")
        r.details["confidence_reasons"] = reasons


def resolve_evidence(rule: dict, ctx: RuleContext) -> EvidenceChunk | None:
    eid = rule.get("evidence_id")
    if eid and eid in ctx.evidence_by_id:
        return ctx.evidence_by_id[eid]
    if rule.get("evidence_ref") and ctx.evidence_resolver:
        return ctx.evidence_resolver(rule["evidence_ref"])
    return None


def evaluate_rule(rule: dict, ctx: RuleContext) -> list[RuleResult]:
    results = RuleEvaluator(rule, ctx).evaluate()
    apply_confidence(results, rule, resolve_evidence(rule, ctx), ctx)
    return results


def build_context(entities: Sequence[GeometryEntity], **kw) -> RuleContext:
    return RuleContext(entities=entities, index=GridIndex([e.bbox for e in entities]), **kw)
