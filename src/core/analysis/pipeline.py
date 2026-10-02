"""Analysis pipeline: classify -> index -> routes -> rules -> evidence -> results.

Pure engine code. Parsing is injected (a list of GeometryEntity), so this
module is independent of ezdxf / file formats.
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable, Sequence

from core.evidence.chunks import find_by_quote
from core.geometry.measure import entity_distance
from core.geometry.primitives import expand_bbox
from core.models.entities import GeometryEntity
from core.models.results import EvidenceChunk, RuleResult
from core.rules import predicate as P
from core.rules.engine import Cancelled, RuleContext, evaluate_rule
from core.spatial.index import build_index
from .issues import assign_ids

STAGES = [
    ("parse", "解析圖面"),
    ("index", "建立空間索引"),
    ("routes", "辨識路徑"),
    ("rules", "執行規則"),
    ("evidence", "建立證據鏈"),
    ("results", "產生結果"),
]

ProgressFn = Callable[[str, float, str], None]  # stage, fraction(0..1 overall), note


@dataclass
class AnalysisOutput:
    results: list[RuleResult]
    routes: list[dict]
    timings: dict[str, float]
    counts: dict[str, int]
    rule_stats: list[dict]
    evidence_used: dict[str, EvidenceChunk] = field(default_factory=dict)


def classify_systems(entities: Sequence[GeometryEntity], systems: list[dict]) -> None:
    compiled = [(s["name"], s["layer_regex"], P.compile_regex(s["layer_regex"])) for s in systems]
    for e in entities:
        for name, pattern, rx in compiled:
            if rx.search(e.layer):
                e.metadata["system"] = name
                e.metadata["classification_source"] = f"Layer name rule：圖層 {e.layer} 符合 /{pattern}/ → {name}"
                break
        else:
            e.metadata.pop("system", None)
            e.metadata["classification_source"] = "未分類（無符合的系統圖層規則）"


def identify_routes(entities: Sequence[GeometryEntity], index, tol: float,
                    cancel_check: Callable[[], bool] = lambda: False) -> list[dict]:
    """Group touching geometric entities of the same system into routes (union-find)."""
    pos = list(range(len(entities)))
    parent = pos[:]

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    members = [i for i, e in enumerate(entities) if e.is_geometric and e.metadata.get("system")]
    member_set = set(members)
    for n, i in enumerate(members):
        if n % 500 == 0 and cancel_check():
            raise Cancelled()
        e = entities[i]
        for j in index.query(expand_bbox(e.bbox, tol)):
            if j <= i or j not in member_set:
                continue
            f = entities[j]
            if f.metadata.get("system") != e.metadata.get("system"):
                continue
            if entity_distance(e, f, tol).distance <= tol:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in members:
        groups[find(i)].append(i)
    routes = []
    counters: Counter = Counter()
    for root in sorted(groups):
        idxs = groups[root]
        sys_name = entities[idxs[0]].metadata["system"]
        counters[sys_name] += 1
        rid = f"R-{sys_name}-{counters[sys_name]:03d}"
        for i in idxs:
            entities[i].metadata["route_id"] = rid
        routes.append({
            "route_id": rid, "system": sys_name,
            "entity_count": len(idxs),
            "length": round(sum(entities[i].length() for i in idxs), 3),
            "handles": [entities[i].handle for i in idxs[:50]],
        })
    return routes


def nearby_objects(entity_ids: list[str], entities: Sequence[GeometryEntity], by_id: dict, index,
                   radius: float, limit: int = 8) -> list[dict]:
    out = []
    seen = set(entity_ids)
    for eid in entity_ids:
        e = by_id.get(eid)
        if e is None:
            continue
        for j in index.query(expand_bbox(e.bbox, radius)):
            f = entities[j]
            if f.id in seen:
                continue
            seen.add(f.id)
            d = entity_distance(e, f, radius).distance
            if d <= radius:
                out.append({"handle": f.handle, "layer": f.layer, "entity_type": f.entity_type,
                            "system": f.metadata.get("system", ""), "distance": round(d, 1)})
    out.sort(key=lambda o: (o["distance"], o["handle"]))
    return out[:limit]


def run_analysis(entities: list[GeometryEntity], ruleset: dict, *, drawing_key: str,
                 unit_to_mm: float = 1.0, units_assumed: bool = False,
                 evidence: Sequence[EvidenceChunk] = (), progress: ProgressFn | None = None,
                 cancel_check: Callable[[], bool] = lambda: False,
                 index_kind: str = "grid", parse_seconds: float = 0.0) -> AnalysisOutput:
    progress = progress or (lambda *a: None)
    timings = {"parse": parse_seconds}

    def stage(name, frac, note=""):
        if cancel_check():
            raise Cancelled()
        progress(name, frac, note)

    stage("index", 0.15, f"{len(entities)} 個物件")
    t0 = time.perf_counter()
    classify_systems(entities, ruleset.get("systems", []))
    index = build_index([e.bbox for e in entities], index_kind)
    timings["index"] = time.perf_counter() - t0

    stage("routes", 0.25)
    t0 = time.perf_counter()
    tol = 1.0 / unit_to_mm if unit_to_mm else 1.0  # 1 mm
    routes = identify_routes(entities, index, tol, cancel_check)
    timings["routes"] = time.perf_counter() - t0

    stage("rules", 0.35)
    t0 = time.perf_counter()
    ev_list = list(evidence)
    ctx = RuleContext(
        entities=entities, index=index, unit_to_mm=unit_to_mm, units_assumed=units_assumed,
        evidence_by_id={c.id: c for c in ev_list},
        evidence_resolver=lambda ref: find_by_quote(ev_list, ref.get("quote", ""), ref.get("document")),
        cancel_check=cancel_check,
    )
    rules = [r for r in ruleset.get("rules", []) if r.get("enabled", True)]
    results: list[RuleResult] = []
    rule_stats = []
    for n, rule in enumerate(rules):
        rt = time.perf_counter()
        rr = evaluate_rule(rule, ctx)
        results.extend(rr)
        c = Counter(r.status for r in rr)
        rule_stats.append({"rule_id": rule["id"], "name": rule["name"], "severity": rule["severity"],
                           "measurement": rule["measurement"],
                           "FAIL": c["FAIL"], "WARNING": c["WARNING"], "PASS": c["PASS"], "UNKNOWN": c["UNKNOWN"],
                           "seconds": round(time.perf_counter() - rt, 4),
                           "evidence_id": rr[0].evidence_id if rr else None})
        progress("rules", 0.35 + 0.45 * (n + 1) / max(1, len(rules)), f"{rule['id']}")
    timings["rules"] = time.perf_counter() - t0

    stage("evidence", 0.82)
    t0 = time.perf_counter()
    by_id = {e.id: e for e in entities}
    used = {r.evidence_id: ctx.evidence_by_id.get(r.evidence_id) for r in results if r.evidence_id}
    for c in ev_list:
        if c.id in used and used[c.id] is None:
            used[c.id] = c
    radius = 1000.0 / unit_to_mm if unit_to_mm else 1000.0
    for i, r in enumerate(results):
        if r.status != "PASS":
            r.details["nearby"] = nearby_objects(r.subject_ids + r.target_ids, entities, by_id, index, radius)
            r.details["route_ids"] = sorted({by_id[x].metadata.get("route_id") for x in r.subject_ids + r.target_ids
                                             if x in by_id and by_id[x].metadata.get("route_id")})
        if i % 2000 == 0 and cancel_check():
            raise Cancelled()
    timings["evidence"] = time.perf_counter() - t0

    stage("results", 0.92)
    assign_ids(drawing_key, results)
    counts = Counter(r.status for r in results)
    return AnalysisOutput(
        results=results, routes=routes, timings=timings,
        counts={k: counts.get(k, 0) for k in ("FAIL", "WARNING", "PASS", "UNKNOWN")},
        rule_stats=rule_stats, evidence_used={k: v for k, v in used.items() if v is not None},
    )
