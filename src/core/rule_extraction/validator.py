"""Candidate -> status. Decides what may run on its own (CONFIRMED) and what must wait for a person.

Pure functions over plain data: no database, no files. Inputs are the stored parser output, what the drawing offers
(layers, the project's systems), the project's glossary and the review decisions a person already made.

A candidate is CONFIRMED only when *all* of these hold; otherwise it is NEEDS_REVIEW with the reasons listed:
  - the sentence is a mandatory requirement (or a person accepted a softer wording as a WARNING)
  - the check type, comparison direction, number and unit were read without a guess
  - every drawing object it refers to maps to exactly one system / layer group
  - its source sentence is found in the stored evidence and really contains the number
and it is not in disagreement with another requirement about the same objects (that is CONFLICTING).
"""
from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable

from core.evidence.chunks import normalize as normalize_evidence
from core.rules import predicate as P
from core.rules.schema import MEASUREMENT_ZH, UNITS_TO_MM
from .candidate import (ADVISORY, CONFIRMED, CONFLICTING, NEEDS_REVIEW, NOT_ENFORCEABLE, PROBLEM_LABELS, CandidateView,
                        RawCandidate, norm_text, fmt_num)
from .patterns import to_mm

# template -> the drawing objects it needs named
SLOTS = {"clearance": ("subject", "target"), "vertical_clearance": ("subject", "target"),
         "no_crossing": ("subject", "target"), "max_distance": ("subject", "target"),
         "keep_out": ("subject", "zone"), "inside_zone": ("subject", "zone"),
         "max_length": ("subject",), "orthogonal": ("subject",), "min_count": ("subject",)}
SLOT_MISSING = {"subject": "SUBJECT_MISSING", "target": "TARGET_MISSING", "zone": "ZONE_MISSING"}
SLOT_UNMAPPED = {"subject": "SUBJECT_UNMAPPED", "target": "TARGET_UNMAPPED", "zone": "ZONE_UNMAPPED"}
SOFT_PROBLEMS = {"ADVISORY", "NO_MODAL", "INHERITED_PARTIES"}      # a person may accept these as they are
AXIS_TEMPLATES = {"clearance": "horizontal_clearance", "vertical_clearance": "vertical_clearance"}
OP_SYMBOL = {">=": "≥", "<=": "≤", "==": "＝", ">": ">", "<": "<", "!=": "≠"}

# Concept words that appear in specifications -> layer-name patterns that usually carry them in a drawing.
# A hit is only a *proposal*: it counts only when a layer of the imported drawing really matches, and it is
# overridden by the project's own glossary. More than one different answer is never resolved automatically.
DEFAULT_GLOSSARY: dict[str, list[str]] = {
    "SCADA": ["SCADA", "SCADA-*"],
    "通信": ["COMM", "COMM-*", "COM-*", "TEL", "TEL-*"],
    "通訊": ["COMM", "COMM-*", "COM-*", "TEL", "TEL-*"],
    "電力": ["POWER", "POWER-*", "PWR", "PWR-*", "ELEC", "ELEC-*"],
    "號誌": ["SIGNAL", "SIGNAL-*", "SIG", "SIG-*"],
}


@dataclass
class MappingContext:
    layers: list[str] = field(default_factory=list)            # layers of the project's drawings
    systems: list[dict] = field(default_factory=list)          # [{"name", "layer_regex"}]
    glossary: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self):
        self.layers = sorted({l for l in self.layers if l}, key=str.casefold)
        self._sys_layers: dict[str, frozenset[str]] = {}
        for s in self.systems:
            try:
                rx = P.compile_regex(s["layer_regex"])
            except (P.PredicateError, KeyError, TypeError):
                continue
            self._sys_layers[s["name"]] = frozenset(l for l in self.layers if rx.search(l))

    def system_layers(self, name: str) -> frozenset[str]:
        return self._sys_layers.get(name, frozenset())

    def layers_matching(self, patterns: Iterable[str]) -> frozenset[str]:
        pats = [p.casefold() for p in patterns if isinstance(p, str) and p.strip()]
        return frozenset(l for l in self.layers if any(fnmatch.fnmatchcase(l.casefold(), p) for p in pats))


def effective_glossary(project: dict[str, list[str]] | None) -> dict[str, list[str]]:
    """Built-in concepts, replaced / extended / (with an empty list) switched off by the project's own."""
    out = {k: list(v) for k, v in DEFAULT_GLOSSARY.items()}
    for k, v in (project or {}).items():
        if isinstance(k, str) and k.strip() and isinstance(v, list):
            out[k.strip()] = [x for x in v if isinstance(x, str) and x.strip()]
    return {k: v for k, v in out.items() if v}


def parse_glossary_text(text: str) -> dict[str, list[str]]:
    """'通信 = COMM, COM-*' per line -> {'通信': ['COMM', 'COM-*']} (a line with nothing after '=' switches a built-in off)."""
    out: dict[str, list[str]] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, rest = line.partition("=")
        key = key.strip()
        pats = [p.strip() for p in rest.replace("，", ",").replace("、", ",").split(",") if p.strip()]
        if key:
            out[key] = pats
    return out


def format_glossary_text(glossary: dict[str, list[str]]) -> str:
    return "\n".join(f"{k} = {', '.join(v)}" for k, v in glossary.items())


# ---- selectors -------------------------------------------------------------------------------------------------------
def selector_layers(sel: dict | None, ctx: MappingContext) -> frozenset[str]:
    if not sel:
        return frozenset()
    (kind, val), = sel.items()
    if kind == "system":
        return ctx.system_layers(val)
    if kind == "layer":
        return frozenset(l for l in ctx.layers if l.casefold() == str(val).casefold())
    if kind == "layers":
        low = {str(v).casefold() for v in val}
        return frozenset(l for l in ctx.layers if l.casefold() in low)
    return frozenset()


def selector_label(sel: dict | None) -> str:
    if not sel:
        return ""
    (kind, val), = sel.items()
    return "、".join(val) if kind == "layers" else str(val)


def valid_selector(sel: Any, ctx: MappingContext) -> bool:
    """A selector a person picked must name something the drawing / project really has."""
    if not isinstance(sel, dict) or len(sel) != 1:
        return False
    (kind, val), = sel.items()
    if kind == "system":
        return isinstance(val, str) and val in {s["name"] for s in ctx.systems}
    if kind == "layer":
        return isinstance(val, str) and bool(selector_layers(sel, ctx))
    if kind == "layers":
        return isinstance(val, list) and bool(val) and all(isinstance(v, str) for v in val) and bool(selector_layers(sel, ctx))
    return False


def _tokens(text: str) -> list[str]:
    return [t.strip() for t in text.replace("/", "、").replace("／", "、").split("、") if t.strip()]


def _candidates_for(token: str, ctx: MappingContext) -> tuple[int, dict[frozenset, dict]]:
    """Mappings for one phrase, best tier only: tier 0 = the phrase *is* a layer / system / glossary word,
    tier 1 = it merely contains (or is contained in) one."""
    tn = norm_text(token)
    tiers: dict[int, dict[frozenset, dict]] = {0: {}, 1: {}}

    def add(tier: int, sel: dict, layers: frozenset, prefer: bool = False):
        if not layers:
            return
        cur = tiers[tier].get(layers)
        if cur is None or (prefer and "system" not in cur):
            tiers[tier][layers] = sel

    def match_tier(name: str) -> int | None:
        nn = norm_text(name)
        if not nn:
            return None
        if nn == tn:
            return 0
        if len(nn) >= 2 and len(tn) >= 2 and (nn in tn or tn in nn):
            return 1
        return None

    for layer in ctx.layers:
        if norm_text(layer) == tn:
            add(0, {"layer": layer}, frozenset({layer}))
    for s in ctx.systems:
        t = match_tier(s["name"])
        if t is not None:
            add(t, {"system": s["name"]}, ctx.system_layers(s["name"]), prefer=True)
    for concept, patterns in ctx.glossary.items():
        t = match_tier(concept)
        if t is not None:
            layers = ctx.layers_matching(patterns)
            if len(layers) == 1:
                add(t, {"layer": next(iter(layers))}, layers)
            else:
                add(t, {"layers": sorted(layers, key=str.casefold)}, layers)
    for t in (0, 1):
        if tiers[t]:
            return t, tiers[t]
    return 1, {}


def resolve_term(text: str, ctx: MappingContext) -> tuple[dict | None, str]:
    """('selector', 'ok') | (None, 'none') | (None, 'multiple'). Several phrases (A、B) are joined as a union,
    each of which must map to exactly one thing."""
    toks = _tokens(text)
    if not toks:
        return None, "none"
    chosen: list[tuple[dict, frozenset]] = []
    for tok in toks:
        _, cands = _candidates_for(tok, ctx)
        if not cands:
            return None, "none"
        if len(cands) > 1:
            return None, "multiple"
        (layers, sel), = cands.items()
        chosen.append((sel, layers))
    if len(chosen) == 1:
        return chosen[0][0], "ok"
    union = frozenset().union(*(l for _, l in chosen))
    sels = {tuple(sorted(s.items(), key=str)) for s, _ in chosen}
    if len(sels) == 1:
        return chosen[0][0], "ok"
    return {"layers": sorted(union, key=str.casefold)}, "ok"


# ---- assessing one candidate -----------------------------------------------------------------------------------------
def requirement_text(template_id: str | None, measurement: str | None, op: str | None, value: float | None,
                     unit: str | None) -> str:
    if measurement == "intersection":
        return "不得交叉"
    if measurement == "outside_zone":
        return "不得進入區域"
    if measurement == "inside_zone":
        return "必須位於區域內"
    if not measurement:
        return ""
    name = MEASUREMENT_ZH.get(measurement, measurement)
    if template_id == "max_distance":
        name = "與最近物件的距離"
    if value is None or not op:
        return f"{name}（{'數值' if value is None else '比較方向'}未確定）"
    u = {"deg": "°", "count": " 個"}.get(unit or "", f" {unit}" if unit else "")
    return f"{name} {OP_SYMBOL.get(op, op)} {fmt_num(value)}{u}"


def fix_options(problems: list[str], raw: RawCandidate, template_id: str | None) -> dict:
    """What a person can still supply for the remaining problems."""
    slots: list[str] = []
    for code, slot in (("SUBJECT_MISSING", "subject"), ("SUBJECT_UNMAPPED", "subject"), ("TARGET_MISSING", "target"),
                       ("TARGET_UNMAPPED", "target"), ("ZONE_MISSING", "zone"), ("ZONE_UNMAPPED", "zone")):
        if code in problems and slot not in slots:
            slots.append(slot)
    if "AMBIGUOUS_PARTIES" in problems:
        slots = list(SLOTS.get(template_id or "", ())) or ["subject", "target"]
    return {"slots": slots, "axis": "UNSPECIFIED_AXIS" in problems,
            "direction": bool({"NO_DIRECTION", "BOTH_DIRECTIONS"} & set(problems)), "direction_ops": _ops_for(template_id), "unit": "NO_UNIT" in problems,
            "tolerance": "MISSING_TOLERANCE" in problems,
            "accept": bool(SOFT_PROBLEMS & set(problems))}


def _ops_for(template_id: str | None) -> list[str]:
    return [{"clearance": ">=", "vertical_clearance": ">=", "max_length": "<=", "max_distance": "<=", "orthogonal": "<=",
             "min_count": ">="}[template_id]] if template_id in ("clearance", "vertical_clearance", "max_length",
                                                                 "max_distance", "orthogonal", "min_count") else []


class _Evidence:
    def __init__(self, texts: dict[str, str]):
        self.texts = texts
        self._norm: dict[str, str] = {}

    def normalized(self, eid: str) -> str | None:
        if eid not in self.texts:
            return None
        if eid not in self._norm:
            self._norm[eid] = normalize_evidence(self.texts[eid])
        return self._norm[eid]


def _assess(cid: str, raw: RawCandidate, ctx: MappingContext, decision: dict, ev: _Evidence) -> CandidateView:
    v = CandidateView(id=cid, status=NEEDS_REVIEW, bucket="review", raw=raw, decision=dict(decision))
    if not raw.enforceable:
        v.status, v.bucket = NOT_ENFORCEABLE, "not_enforceable"
        v.reason = raw.reason or "目前無法由 2D DXF 自動量化。"
        return v

    problems = list(raw.problems)
    template_id, measurement = raw.template_id, raw.measurement
    op, value, unit = raw.operator, raw.value, raw.unit
    by_user = False

    # --- what a person supplied
    t_over = decision.get("template_id")
    if "UNSPECIFIED_AXIS" in problems and t_over in AXIS_TEMPLATES:
        template_id, measurement = t_over, AXIS_TEMPLATES[t_over]
        problems.remove("UNSPECIFIED_AXIS")
        by_user = True
    o_over = decision.get("operator")
    if o_over in (">=", "<=") and ("NO_DIRECTION" in problems or "BOTH_DIRECTIONS" in problems):
        want = {"clearance": ">=", "vertical_clearance": ">=", "max_length": "<=", "max_distance": "<=",
                "orthogonal": "<=", "min_count": ">="}.get(template_id or "")
        if want == o_over:                                  # a person may pick the direction, but only one the rule type can check
            op = o_over
            problems = [p for p in problems if p not in ("NO_DIRECTION", "BOTH_DIRECTIONS")]
            by_user = True
    u_over = decision.get("unit")
    if "NO_UNIT" in problems and u_over in UNITS_TO_MM and value is not None:
        value, unit = to_mm(value, u_over), "mm"
        problems.remove("NO_UNIT")
        by_user = True
    if "MISSING_TOLERANCE" in problems and isinstance(decision.get("value"), (int, float)) \
            and not isinstance(decision.get("value"), bool) and decision["value"] >= 0:
        value, unit = float(decision["value"]), "deg"
        problems.remove("MISSING_TOLERANCE")
        by_user = True
    overrides = {k: s for k, s in (decision.get("selectors") or {}).items() if valid_selector(s, ctx)}

    slots = SLOTS.get(template_id or "", ())
    for slot in slots:
        if slot in overrides:
            problems = [p for p in problems if p != SLOT_MISSING[slot]]
            by_user = True
    if "AMBIGUOUS_PARTIES" in problems and all(s in overrides for s in slots):
        problems.remove("AMBIGUOUS_PARTIES")
    if "INHERITED_PARTIES" in problems and decision.get("accept"):
        problems.remove("INHERITED_PARTIES")
        by_user = True

    # --- which drawing objects
    selectors: dict[str, dict | None] = {}
    for slot in slots:
        text = {"subject": raw.subject_text, "target": raw.target_text, "zone": raw.zone_text}[slot]
        if slot in overrides:
            selectors[slot] = overrides[slot]
        elif not text or "AMBIGUOUS_PARTIES" in problems or SLOT_MISSING[slot] in problems:
            selectors[slot] = None
        elif not ctx.layers:
            selectors[slot] = None
            if "NO_DRAWING" not in problems:
                problems.append("NO_DRAWING")
        else:
            sel, how = resolve_term(text, ctx)
            selectors[slot] = sel
            if sel is None:
                problems.append(SLOT_UNMAPPED[slot])
    v.subject_selector, v.target_selector, v.zone_selector = (selectors.get(s) for s in ("subject", "target", "zone"))

    # --- the sentence must be in the evidence and contain the number
    text = ev.normalized(raw.evidence_id)
    if text is None:
        problems.append("EVIDENCE_MISSING")
    elif normalize_evidence(raw.sentence) not in text or (
            raw.value_text and normalize_evidence(raw.value_text) not in normalize_evidence(raw.sentence)):
        problems.append("EVIDENCE_MISMATCH")

    # --- wording
    v.severity = "FAIL"
    if SOFT_PROBLEMS & {"ADVISORY", "NO_MODAL"} & set(problems) and decision.get("accept"):
        problems = [p for p in problems if p not in ("ADVISORY", "NO_MODAL")]
        v.severity = "WARNING"
        by_user = True
        v.warnings.append("原文不是強制用語，依你的確認以「警告」處理。")

    v.problems = list(dict.fromkeys(problems))
    v.missing = [{"code": p, "label": PROBLEM_LABELS.get(p, p)} for p in v.problems]
    v.fix = fix_options(v.problems, raw, template_id)
    v.template_id, v.measurement, v.operator, v.value, v.unit = template_id, measurement, op, value, unit
    v.requirement = requirement_text(template_id, measurement, op, value, unit)
    if "UNSPECIFIED_AXIS" in v.problems:                     # the placeholder measurement must not read as a decision
        v.requirement = v.requirement.replace("水平淨距", "淨距（水平或垂直未定）", 1)
    if not v.problems:
        v.status, v.bucket = CONFIRMED, "usable"
        v.confirmed_by = "user" if by_user or v.severity == "WARNING" else "auto"
        v.reason = "原文的檢查類型、比較方向、數值與單位都已讀出，對象也對應到圖面的系統／圖層。"
    else:
        v.reason = "需要人工確認：" + "；".join(m["label"] for m in v.missing)
    return v


# ---- a set of candidates -----------------------------------------------------------------------------------------------
def _canon_sel(sel: dict | None, text: str, ctx: MappingContext) -> str:
    layers = selector_layers(sel, ctx)
    return "L:" + ",".join(sorted(layers)) if sel and layers else "T:" + norm_text(text)


def _family(v: CandidateView, ctx: MappingContext) -> str:
    third = v.target_selector if v.template_id in ("clearance", "vertical_clearance", "no_crossing", "max_distance") \
        else v.zone_selector
    third_text = v.raw.target_text if third is v.target_selector else v.raw.zone_text
    return "|".join([v.measurement or "?", _canon_sel(v.subject_selector, v.raw.subject_text, ctx),
                     _canon_sel(third, third_text, ctx)])


def group_id(family: str) -> str:
    return "grp_" + hashlib.sha256(family.encode("utf-8")).hexdigest()[:16]


def evaluate(records: list[dict], ctx: MappingContext, decisions: dict[str, dict], group_choices: dict[str, str],
             evidence_texts: dict[str, str]) -> list[CandidateView]:
    """records: [{"id", "raw": RawCandidate}] in reading order. decisions: candidate id -> decision (already looked up
    by id or semantic key). group_choices: conflict group id -> semantic key of the requirement a person chose."""
    ev = _Evidence(evidence_texts)
    primary: dict[str, CandidateView] = {}
    order: list[str] = []
    for rec in records:
        raw: RawCandidate = rec["raw"]
        if raw.semantic_key in primary:                                 # the same meaning in another document
            primary[raw.semantic_key].duplicates.append(_source(raw, rec["id"]))
            continue
        view = _assess(rec["id"], raw, ctx, decisions.get(rec["id"], {}), ev)
        primary[raw.semantic_key] = view
        order.append(raw.semantic_key)
    views = [primary[k] for k in order]

    # identical rules written with different words (same objects, same comparison): one rule, several sources
    seen: dict[tuple, CandidateView] = {}
    merged: list[CandidateView] = []
    for v in views:
        if v.status in (CONFIRMED, NEEDS_REVIEW) and v.value is not None and v.operator and v.template_id:
            sig = (_family(v, ctx), v.operator, fmt_num(v.value), v.unit, v.severity)
            if sig in seen:
                seen[sig].duplicates.append(_source(v.raw, v.id))
                seen[sig].duplicates.extend(v.duplicates)
                continue
            seen[sig] = v
        merged.append(v)
    views = merged

    # disagreement between requirements about the same objects
    groups: dict[str, list[CandidateView]] = {}
    for v in views:
        if _comparable(v):
            groups.setdefault(_family(v, ctx), []).append(v)
    for fam, members in groups.items():
        if len({(m.operator, fmt_num(m.value), m.unit) for m in members}) < 2:
            continue
        gid = group_id(fam)
        chosen_key = group_choices.get(gid)
        has_chosen = any(m.raw.semantic_key == chosen_key for m in members)
        options = [{"id": m.id, "operator": m.operator, "value": m.value, "unit": m.unit, "text": m.requirement,
                    "semantic_key": m.raw.semantic_key, "source": _source(m.raw, m.id)} for m in members]
        for m in members:
            m.conflict_group = gid
            m.conflict_options = options
            m.conflict_with = [o["id"] for o in options if o["id"] != m.id]
            if has_chosen and m.raw.semantic_key == chosen_key:
                m.decision["chosen"] = True              # keeps its own status (CONFIRMED when everything else is known)
                if m.status == CONFIRMED:
                    m.confirmed_by = "user"
            elif has_chosen:
                m.status, m.bucket = CONFLICTING, "off"
                m.reason = "與其他規範的要求不同，你已選擇另一個數值。"
            else:
                m.status, m.bucket = CONFLICTING, "conflict"
                m.reason = "同一組對象在規範中有不同的要求，請選擇採用哪一個。"

    for v in views:
        off = bool(v.decision.get("disabled"))
        if off and v.status != NOT_ENFORCEABLE:
            v.bucket = "off"
        v.enabled = v.status == CONFIRMED and not off
    return views


def _comparable(v: CandidateView) -> bool:
    """Takes part in conflict detection: a complete, mandatory (or accepted) requirement with a known direction."""
    if v.status == NOT_ENFORCEABLE or v.value is None or not v.operator or not v.template_id or not v.unit:
        return False
    blocking = {"ADVISORY", "NO_MODAL", "NO_DIRECTION", "BOTH_DIRECTIONS", "UNSUPPORTED_DIRECTION", "NO_UNIT",
                "UNIT_MISMATCH", "UNSPECIFIED_AXIS", "NO_VALUE", "MISSING_TOLERANCE", "EVIDENCE_MISSING",
                "EVIDENCE_MISMATCH", "SUBJECT_MISSING", "TARGET_MISSING", "ZONE_MISSING", "AMBIGUOUS_PARTIES",
                "INHERITED_PARTIES"}
    return not (blocking & set(v.problems)) and not v.decision.get("disabled")


def _source(raw: RawCandidate, cid: str | None = None) -> dict:
    return {"id": cid, "document_id": raw.document_id, "document": raw.document, "page": raw.page, "line_start": raw.line_start,
            "line_end": raw.line_end, "section": raw.section, "evidence_id": raw.evidence_id, "text": raw.sentence}
