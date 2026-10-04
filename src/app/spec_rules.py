"""Rule candidates as the application serves and applies them.

Statuses are *derived* every time from three things: what the parser read (stored per document), what the drawing
and the project's systems / glossary offer now, and what a person decided. Nothing here runs an analysis: ``apply``
turns the candidates that are CONFIRMED (and not switched off) into ordinary rules of the existing rule engine,
next to the rules a person wrote by hand, which it never touches.
"""
from __future__ import annotations

import math
from typing import Any

from core.rule_extraction import validator as V
from core.rule_extraction.candidate import (CONFIRMED, CONFLICTING, NEEDS_REVIEW, NOT_ENFORCEABLE, CandidateView,
                                            norm_text)
from core.rules.schema import UNITS_TO_MM, RuleError, normalize_ruleset
from exporters.report import evidence_ref_text
from persistence.repo import Repo
from . import templates
from .errors import bad_request, not_found

ORIGIN = "spec-extraction"
BUCKETS = ("usable", "review", "conflict", "not_enforceable", "off")
SLOT_LABEL = {"subject": "檢查對象", "target": "附近物件", "zone": "區域"}
PATCH_KEYS = {"disabled", "accept", "selectors", "template_id", "unit", "value", "operator", "choose", "reset"}


def is_spec_rule(rule: dict) -> bool:
    return (rule.get("origin") or {}).get("type") == ORIGIN


# ---- evaluation ------------------------------------------------------------------------------------------------------
def load_context(repo: Repo, pid: str) -> V.MappingContext:
    layers: list[str] = []
    for d in repo.list_drawings(pid):
        layers += [l["name"] for l in (d.get("info") or {}).get("layers", [])]
    settings = repo.spec.settings(pid)
    return V.MappingContext(layers=layers, systems=repo.get_ruleset(pid).get("systems", []),
                            glossary=V.effective_glossary(settings["glossary"]))


def _decisions(repo: Repo, pid: str, records: list[dict]) -> tuple[dict[str, dict], dict[str, str]]:
    reviews = repo.spec.reviews(pid)
    by_key = {r["key"]: r["decision"] for r in reviews}
    by_sem = {r["semantic_key"]: r["decision"] for r in reviews if r["key"].startswith("cand_") and r["semantic_key"]}
    decisions = {rec["id"]: by_key.get(rec["id"]) or by_sem.get(rec["raw"].semantic_key) or {} for rec in records}
    choices = {r["key"]: r["decision"].get("chosen") for r in reviews if r["key"].startswith("grp_")}
    return decisions, choices


def evaluate_project(repo: Repo, pid: str) -> tuple[list[CandidateView], V.MappingContext]:
    records = repo.spec.candidate_records(pid)
    ctx = load_context(repo, pid)
    decisions, choices = _decisions(repo, pid, records)
    return V.evaluate(records, ctx, decisions, choices, repo.spec.evidence_texts(pid)), ctx


def summarize(views: list[CandidateView]) -> dict[str, Any]:
    out = {b: sum(1 for v in views if v.bucket == b) for b in BUCKETS}
    out["total"] = len(views)
    out["by_status"] = {s: sum(1 for v in views if v.status == s) for s in (CONFIRMED, NEEDS_REVIEW, CONFLICTING, NOT_ENFORCEABLE)}
    return out


def gate(repo: Repo, pid: str, views: list[CandidateView], ctx: V.MappingContext | None = None) -> dict[str, Any]:
    """What 'start analysis' would run. One enabled CONFIRMED rule (or one enabled hand-made rule) is enough;
    unresolved candidates do not block, they simply do not run."""
    manual, built, _ = plan(repo, pid, views, ctx)
    enabled_manual = sum(1 for r in manual if r.get("enabled", True))
    s = summarize(views)
    run = len(built) + enabled_manual
    return {"will_run": run, "from_spec": len(built), "manual": enabled_manual, "review": s["review"],
            "conflict": s["conflict"], "not_enforceable": s["not_enforceable"], "runnable": run >= 1}


def refresh_statuses(repo: Repo, pid: str) -> dict[str, Any]:
    views, _ = evaluate_project(repo, pid)
    repo.spec.update_statuses(pid, {v.id: v.status for v in views})
    return summarize(views)


# ---- JSON for the page -----------------------------------------------------------------------------------------------
def _evidence_view(raw, text: str | None = None) -> dict:
    ev = {"filename": raw.document, "page": raw.page, "line_start": raw.line_start, "line_end": raw.line_end}
    return {"evidence_id": raw.evidence_id, "document_id": raw.document_id, "document": raw.document, "page": raw.page,
            "section": raw.section, "ref": evidence_ref_text(ev), "text": raw.sentence}


def candidate_json(v: CandidateView) -> dict:
    raw = v.raw
    parties = []
    for slot, text, sel in (("subject", raw.subject_text, v.subject_selector), ("target", raw.target_text, v.target_selector),
                            ("zone", raw.zone_text, v.zone_selector)):
        if slot not in V.SLOTS.get(v.template_id or "", ()):
            continue
        parties.append({"slot": slot, "label": SLOT_LABEL[slot], "text": text, "selector": sel,
                        "selector_label": V.selector_label(sel), "mapped": sel is not None})
    names = [(p["selector_label"] or p["text"] or "？") for p in parties]
    mark = "→" if v.template_id in ("keep_out", "inside_zone") else "↔"
    title = (f"「{names[0]}」{mark}「{names[1]}」" if len(names) == 2 else f"「{names[0]}」" if names else
             "（沒有可擷取的規則）")
    return {"id": v.id, "status": v.status, "bucket": v.bucket, "enabled": v.enabled, "confirmed_by": v.confirmed_by,
            "severity": v.severity, "title": title, "parties": parties, "requirement": v.requirement,
            "template_id": v.template_id, "measurement": v.measurement, "operator": v.operator, "value": v.value,
            "unit": v.unit, "value_text": raw.value_text, "modality": raw.modality, "missing": v.missing, "fix": v.fix,
            "reason": v.reason, "warnings": v.warnings, "evidence": _evidence_view(raw),
            "duplicates": v.duplicates, "decision": {k: val for k, val in v.decision.items() if k != "chosen"},
            "conflict": ({"group": v.conflict_group, "options": v.conflict_options,
                          "chosen": bool(v.decision.get("chosen"))} if v.conflict_group else None)}


_BUCKET_ORDER = {"usable": 0, "conflict": 1, "review": 2, "not_enforceable": 3, "off": 4}


def _order(v: CandidateView) -> tuple:
    """What to look at first: ready rules, then conflicts, then the doubtful ones that already touch this drawing
    (objects found in it, few things missing), then the rest in reading order (sorted() is stable)."""
    if v.bucket != "review":
        return (_BUCKET_ORDER.get(v.bucket, 5), 0, 0)
    mapped = sum(1 for sel in (v.subject_selector, v.target_selector, v.zone_selector) if sel is not None)
    return (_BUCKET_ORDER["review"], -mapped, len(v.problems))


def _matches(v: CandidateView, q: str) -> bool:
    hay = norm_text(" ".join([v.raw.sentence, v.raw.subject_text, v.raw.target_text, v.raw.zone_text, v.raw.document,
                              v.raw.section, v.requirement]))
    return all(norm_text(t) in hay for t in q.split() if norm_text(t))


def list_candidates(repo: Repo, pid: str, bucket: str | None, q: str, offset: int, limit: int) -> dict:
    views, ctx = evaluate_project(repo, pid)
    if bucket and bucket != "all":
        if bucket not in BUCKETS:
            raise bad_request("未知的篩選條件。")
        shown = [v for v in views if v.bucket == bucket]
    else:
        shown = views
    shown = sorted(shown, key=_order)
    if q:
        shown = [v for v in shown if _matches(v, q)]
    return {"summary": summarize(views), "gate": gate(repo, pid, views, ctx), "total": len(shown),
            "items": [candidate_json(v) for v in shown[offset:offset + limit]]}


def get_candidate(repo: Repo, pid: str, cid: str) -> dict:
    views, _ = evaluate_project(repo, pid)
    v = next((x for x in views if x.id == cid), None)
    if v is None:
        raise not_found("規則候選")
    return candidate_json(v)


# ---- decisions --------------------------------------------------------------------------------------------------------
def _num(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise bad_request("數值必須是非負數字。")
    return float(value)


def patch_candidate(repo: Repo, pid: str, cid: str, body: Any) -> dict:
    if not isinstance(body, dict) or not body:
        raise bad_request("沒有要修改的內容。")
    unknown = set(body) - PATCH_KEYS
    if unknown:
        raise bad_request(f"不支援的欄位：{'、'.join(sorted(unknown))}")
    views, ctx = evaluate_project(repo, pid)
    view = next((x for x in views if x.id == cid), None)
    if view is None:
        raise not_found("規則候選")
    sem = view.raw.semantic_key
    stored = next((r["decision"] for r in repo.spec.reviews(pid) if r["key"] == cid), {})
    new = {k: val for k, val in stored.items() if k != "chosen"}
    if body.get("reset"):
        new = {}
    for key in ("disabled", "accept"):
        if key in body:
            if not isinstance(body[key], bool):
                raise bad_request(f"{key} 必須是 true 或 false。")
            if body[key]:
                new[key] = True
            else:
                new.pop(key, None)
    if "selectors" in body:
        sels = body["selectors"]
        if not isinstance(sels, dict):
            raise bad_request("selectors 格式不正確。")
        cur = dict(new.get("selectors") or {})
        for slot, sel in sels.items():
            if slot not in SLOT_LABEL:
                raise bad_request("selectors 只能是 subject、target、zone。")
            if sel is None:
                cur.pop(slot, None)
            elif V.valid_selector(sel, ctx):
                cur[slot] = sel
            else:
                raise bad_request(f"「{SLOT_LABEL[slot]}」選的系統或圖層不存在於目前的專案。")
        if cur:
            new["selectors"] = cur
        else:
            new.pop("selectors", None)
    if "template_id" in body:
        if body["template_id"] is None:
            new.pop("template_id", None)
        elif body["template_id"] in V.AXIS_TEMPLATES:
            new["template_id"] = body["template_id"]
        else:
            raise bad_request("方向只能選水平淨距或垂直淨距。")
    if "operator" in body:
        if body["operator"] is None:
            new.pop("operator", None)
        elif body["operator"] in (">=", "<="):
            new["operator"] = body["operator"]
        else:
            raise bad_request("比較方向只能選「不得小於（≥）」或「不得大於（≤）」。")
    if "unit" in body:
        if body["unit"] is None:
            new.pop("unit", None)
        elif body["unit"] in UNITS_TO_MM:
            new["unit"] = body["unit"]
        else:
            raise bad_request("單位只能是 mm、cm、m、ft、in。")
    if "value" in body:
        if body["value"] is None:
            new.pop("value", None)
        else:
            new["value"] = _num(body["value"])
    if "choose" in body:
        if not isinstance(body["choose"], bool):
            raise bad_request("choose 必須是 true 或 false。")
        if not view.conflict_group:
            raise bad_request("這條規則沒有與其他規範衝突，不需要選擇。")
        repo.spec.set_review(pid, view.conflict_group, None, {"chosen": sem} if body["choose"] else None)
        repo.audit("spec.choose", pid, {"candidate": cid, "group": view.conflict_group, "chosen": body["choose"]})
    repo.spec.set_review(pid, cid, sem, new or None)
    repo.audit("spec.review", pid, {"candidate": cid, "keys": sorted(body)})
    views, _ = evaluate_project(repo, pid)
    repo.spec.update_statuses(pid, {x.id: x.status for x in views})
    now = next((x for x in views if x.id == cid), None)
    merged = now is None                                   # the decision made it identical to another rule: one rule, two sources
    if merged:
        now = next(x for x in views if any(d.get("id") == cid for d in x.duplicates))
    return {"candidate": candidate_json(now), "merged": merged, "summary": summarize(views), "gate": gate(repo, pid, views)}


# ---- applying -----------------------------------------------------------------------------------------------------------
def build_rule(v: CandidateView, taken: set[str]) -> dict:
    t = templates.BY_ID[v.template_id]
    base = v.template_id.upper().replace("_", "-")
    params: dict[str, Any] = {"id": f"SPEC-{base}-{v.id[5:13].upper()}", "severity": v.severity, "evidence_id": v.raw.evidence_id}
    for slot, sel in (("subject", v.subject_selector), ("target", v.target_selector), ("zone", v.zone_selector)):
        if slot in t["needs"]:
            params[slot] = sel
    if "value" in t["needs"]:
        params["value"] = int(v.value) if v.measurement == "entity_count" else v.value
    if "unit" in t["needs"]:
        params["unit"] = v.unit
    rule = templates.build_rule(v.template_id, params, taken)
    rule["evidence_ref"] = {"document": v.raw.document, "quote": v.raw.sentence}
    rule["origin"] = {"type": ORIGIN, "candidate_id": v.id}
    return rule


def has_spec_material(repo: Repo, pid: str) -> bool:
    return repo.spec.candidate_count(pid) > 0 or any(is_spec_rule(r) for r in repo.get_ruleset(pid)["rules"])


def _layers_of(pred: Any, ctx: V.MappingContext) -> frozenset[str] | None:
    """Layers a rule's selector covers in this drawing (None when the selector is too complex to tell)."""
    if not isinstance(pred, dict) or len(pred) != 1:
        return None
    (key, val), = pred.items()
    low = {l.casefold(): l for l in ctx.layers}
    if key == "system":
        names = [val] if isinstance(val, str) else val
        out: frozenset[str] = frozenset()
        for n in names:
            match = [x for x in ctx.systems if x["name"].casefold() == str(n).casefold()]
            out |= frozenset().union(*(ctx.system_layers(m["name"]) for m in match)) if match else frozenset()
        return out
    if key == "layer_equals":
        return frozenset({low[val.casefold()]}) if str(val).casefold() in low else frozenset()
    if key == "or":
        parts = [_layers_of(p, ctx) for p in val]
        return None if any(p is None for p in parts) else frozenset().union(*parts)
    return None


def _same_value(manual: dict, built: dict) -> bool:
    def mm(r):
        return r["value"] * UNITS_TO_MM.get(r.get("unit"), 1.0) if r.get("unit") in UNITS_TO_MM else r["value"]
    return (manual["measurement"] == built["measurement"] and manual["operator"] == built["operator"]
            and abs(mm(manual) - mm(built)) < 1e-9)


def covered_by(manual_rules: list[dict], built: dict, ctx: V.MappingContext) -> dict | None:
    """An enabled hand-made rule that already checks exactly this (same kind, comparison, number and objects)."""
    for m in manual_rules:
        if not m.get("enabled", True) or not _same_value(m, built):
            continue
        same = True
        for slot in ("subject", "target", "zone"):
            if slot in built or slot in m:
                a, b = _layers_of(m.get(slot), ctx), _layers_of(built.get(slot), ctx)
                if a is None or b is None or not a or a != b:
                    same = False
                    break
        if same:
            return m
    return None


def plan(repo: Repo, pid: str, views: list[CandidateView], ctx: V.MappingContext | None = None
         ) -> tuple[list[dict], list[dict], list[dict]]:
    """(hand-made rules, rules built from CONFIRMED candidates, candidates left out and why)."""
    ctx = ctx or load_context(repo, pid)
    rs = repo.get_ruleset(pid)
    manual = [r for r in rs["rules"] if not is_spec_rule(r)]
    taken = {r["id"] for r in manual}
    built: list[dict] = []
    skipped: list[dict] = []
    for v in views:
        if not v.enabled:
            continue
        try:
            rule = build_rule(v, taken)
        except RuleError as exc:
            skipped.append({"candidate": v.id, "reason": "；".join(exc.errors)})
            continue
        if rule["id"] in taken:                       # a rule of that ID was edited by hand: the hand-made one wins
            skipped.append({"candidate": v.id, "reason": "同編號的規則已被手動修改"})
            continue
        twin = covered_by(manual, rule, ctx)
        if twin is not None:                          # the same check exists by hand: run it once, not twice
            skipped.append({"candidate": v.id, "reason": f"手動規則「{twin['id']}」已經涵蓋"})
            continue
        taken.add(rule["id"])
        built.append(rule)
    return manual, built, skipped


def apply_candidates(repo: Repo, pid: str) -> dict:
    """Replace the project's spec-derived rules by the candidates that are CONFIRMED now. Hand-made rules stay."""
    views, ctx = evaluate_project(repo, pid)
    rs = repo.get_ruleset(pid)
    manual, built, skipped = plan(repo, pid, views, ctx)
    new = normalize_ruleset({"systems": rs["systems"], "rules": manual + built})
    changed = new["rules"] != rs["rules"]
    if changed:
        repo.save_ruleset(pid, new, operation="rules.apply_candidates")
    return {"applied": len(built), "manual": len(manual), "skipped": skipped, "changed": changed}
