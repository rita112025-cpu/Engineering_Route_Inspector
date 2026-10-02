import pytest

from core.analysis.pipeline import run_analysis
from core.models.results import EvidenceChunk
from core.rules.engine import Cancelled, build_context, evaluate_rule
from core.rules.schema import normalize_rule, normalize_ruleset
from core.spatial.index import BruteForceIndex

SYSTEMS = [{"name": "SCADA", "layer_regex": "^SCADA"}, {"name": "POWER", "layer_regex": "^POWER"},
           {"name": "SUPPORT", "layer_regex": "^SUPPORT"}]


def _clearance(**kw):
    r = {"id": "CLR", "name": "SCADA/POWER 淨距", "subject": {"system": "SCADA"},
         "target": {"system": "POWER"}, "measurement": "horizontal_clearance",
         "operator": ">=", "value": 300, "unit": "mm", "warn_margin": 50}
    r.update(kw)
    return r


def _run(entities, rules, **kw):
    rs = normalize_ruleset({"systems": SYSTEMS, "rules": rules})
    return run_analysis(entities, rs, drawing_key="t.dxf", **kw)


def _by_status(out):
    d = {}
    for r in out.results:
        d.setdefault(r.status, []).append(r)
    return d


def test_horizontal_clearance_fail_warning_pass(ef):
    ents = [
        ef.line("SCADA-1", (0, 0), (5000, 0)), ef.line("POWER-1", (0, 250), (5000, 250)),      # 250 -> FAIL
        ef.line("SCADA-2", (0, 2000), (5000, 2000)), ef.line("POWER-2", (0, 2320), (5000, 2320)),  # 320 -> WARNING
        ef.line("SCADA-3", (0, 5000), (5000, 5000)), ef.line("POWER-3", (0, 5500), (5000, 5500)),  # 500 -> PASS
    ]
    out = _run(ents, [_clearance()])
    s = _by_status(out)
    assert [r.measured for r in s["FAIL"]] == [250.0]
    assert [r.measured for r in s["WARNING"]] == [320.0]
    assert [r.measured for r in s["PASS"]] == [500.0]
    fail = s["FAIL"][0]
    assert fail.subject_handles == [ents[0].handle] and fail.target_handles == [ents[1].handle]
    assert "低於規則要求的 300 mm" in fail.message
    assert fail.fix and fail.required_value == 300 and fail.unit == "mm"
    assert fail.location == pytest.approx((0.0, 125.0))
    assert out.counts == {"FAIL": 1, "WARNING": 1, "PASS": 1, "UNKNOWN": 0}


def test_units_drawing_in_metres(ef):
    ents = [ef.line("SCADA", (0, 0), (5, 0)), ef.line("POWER", (0, 0.25), (5, 0.25))]
    out = _run(ents, [_clearance(value=30, unit="cm", warn_margin=0)], unit_to_mm=1000.0)
    (r,) = out.results
    assert r.status == "FAIL" and r.measured == pytest.approx(25.0) and r.unit == "cm"


def test_crossing_without_z_is_unknown_and_with_levels_excluded(ef):
    ents = [ef.line("SCADA", (0, 0), (1000, 0)), ef.line("POWER", (500, -500), (500, 500))]
    (r,) = _run(ents, [_clearance()]).results
    assert r.status == "UNKNOWN" and r.confidence == "UNKNOWN"
    assert "高程" in r.message
    # different elevations -> not a horizontal clearance situation -> nothing reported against it
    ents = [ef.line("SCADA", (0, 0), (1000, 0), z_known=True, z_min=0, z_max=0),
            ef.line("POWER", (500, -500), (500, 500), z_known=True, z_min=3000, z_max=3000)]
    (r,) = _run(ents, [_clearance()]).results
    assert r.status == "PASS" and r.target_handles == []


def test_vertical_clearance(ef):
    rule = _clearance(id="VC", measurement="vertical_clearance", value=200, warn_margin=0)
    ents = [ef.line("SCADA", (0, 0), (1000, 0), z_known=True, z_min=0, z_max=0),
            ef.line("POWER", (500, -500), (500, 500), z_known=True, z_min=150, z_max=150)]
    (r,) = _run(ents, [rule]).results
    assert r.status == "FAIL" and r.measured == pytest.approx(150.0)
    ents = [ef.line("SCADA", (0, 0), (1000, 0)), ef.line("POWER", (500, -500), (500, 500))]
    (r,) = _run(ents, [rule]).results
    assert r.status == "UNKNOWN"


def test_intersection_rule(ef):
    rule = {"id": "X", "name": "不得交叉", "subject": {"system": "SCADA"}, "target": {"layer_equals": "WATER"},
            "measurement": "intersection"}
    ents = [ef.line("SCADA", (0, 0), (1000, 0)), ef.line("WATER", (500, -10), (500, 10)),
            ef.line("SCADA", (0, 5000), (1000, 5000))]
    s = _by_status(_run(ents, [rule]))
    assert len(s["FAIL"]) == 1 and s["FAIL"][0].subject_handles == [ents[0].handle]
    assert len(s["PASS"]) == 1 and s["PASS"][0].subject_handles == [ents[2].handle]


def test_zone_rules(ef):
    no_go = {"id": "NOGO", "name": "禁設區", "subject": {"system": "SCADA"}, "zone": {"layer_equals": "NOGO"},
             "measurement": "outside_zone"}
    must_in = {"id": "IN", "name": "需在管溝內", "subject": {"system": "SCADA"}, "zone": {"layer_equals": "TRENCH"},
               "measurement": "inside_zone"}
    ents = [ef.rect("NOGO", 100, -100, 200, 100), ef.rect("TRENCH", -50, -50, 2000, 50),
            ef.line("SCADA", (0, 0), (1000, 0)),            # crosses NOGO, inside trench
            ef.line("SCADA", (0, 3000), (1000, 3000))]      # clear of NOGO, outside trench
    out = _run(ents, [no_go, must_in])
    res = {(r.rule_id, r.subject_handles[0]): r.status for r in out.results}
    assert res[("NOGO", ents[2].handle)] == "FAIL"
    assert res[("NOGO", ents[3].handle)] == "PASS"
    assert res[("IN", ents[2].handle)] == "PASS"
    assert res[("IN", ents[3].handle)] == "FAIL"
    # no zone geometry at all -> UNKNOWN, never PASS
    out = _run([ef.line("SCADA", (0, 0), (1, 0))], [no_go])
    assert [r.status for r in out.results] == ["UNKNOWN"]


def test_any_quantifier_support_distance(ef):
    rule = {"id": "SUP", "name": "支架間距", "subject": {"system": "SCADA"}, "target": {"system": "SUPPORT"},
            "measurement": "distance", "operator": "<=", "value": 500}
    ents = [ef.line("SCADA", (0, 0), (1000, 0)), ef.circle("SUPPORT", (500, 300), 50),
            ef.line("SCADA", (0, 10000), (1000, 10000))]
    out = _run(ents, [rule])
    res = {r.subject_handles[0]: r for r in out.results}
    assert res[ents[0].handle].status == "PASS" and res[ents[0].handle].measured == pytest.approx(250.0)
    far = res[ents[2].handle]
    assert far.status == "FAIL" and far.measured is None and "搜尋半徑" in far.reason
    # no support anywhere -> cannot decide
    out = _run([ef.line("SCADA", (0, 0), (1000, 0))], [rule])
    assert [r.status for r in out.results] == ["UNKNOWN"]


def test_single_entity_measurements(ef):
    rules = [
        {"id": "LEN", "name": "最大長度", "subject": {"system": "SCADA"}, "measurement": "length",
         "operator": "<=", "value": 5, "unit": "m"},
        {"id": "ORTHO", "name": "正交", "subject": {"system": "SCADA"}, "measurement": "off_axis_angle",
         "operator": "<=", "value": 1},
    ]
    ents = [ef.line("SCADA", (0, 0), (6000, 0)), ef.line("SCADA", (0, 1000), (1000, 1100)),
            ef.circle("SCADA", (0, 0), 10)]
    out = _run(ents, rules)
    res = {(r.rule_id, r.subject_handles[0]): r for r in out.results}
    assert res[("LEN", ents[0].handle)].status == "FAIL"
    assert res[("LEN", ents[0].handle)].measured == pytest.approx(6.0)
    assert res[("LEN", ents[1].handle)].status == "PASS"
    assert res[("ORTHO", ents[0].handle)].status == "PASS"
    assert res[("ORTHO", ents[1].handle)].status == "FAIL"
    assert res[("ORTHO", ents[2].handle)].status == "UNKNOWN"   # circle has no direction


def test_entity_count(ef):
    rule = {"id": "CNT", "name": "至少一個標示", "subject": {"entity_type": "TEXT", "text_regex": "SCADA"},
            "measurement": "entity_count", "operator": ">=", "value": 1}
    out = _run([ef.text("NOTE", (0, 0), "POWER")], [rule])
    assert [r.status for r in out.results] == ["FAIL"]
    out = _run([ef.text("NOTE", (0, 0), "SCADA 線槽")], [rule])
    assert [r.status for r in out.results] == ["PASS"]


def test_confidence_levels(ef):
    ev = EvidenceChunk(id="EV-1", document_id="d", filename="spec.txt", page=1, line_start=3, line_end=3,
                       section="4.2", text="SCADA 與電力電纜水平淨距不得小於 300 mm。", hash="h")
    ents = [ef.line("SCADA", (0, 0), (1000, 0)), ef.line("POWER", (0, 250), (1000, 250))]
    with_ref = _clearance(evidence_ref={"document": "spec.txt", "quote": "不得小於 300 mm"})
    (r,) = _run(ents, [with_ref], evidence=[ev]).results
    assert r.confidence == "CONFIRMED" and r.evidence_id == "EV-1"
    (r,) = _run(ents, [_clearance()], evidence=[ev]).results
    assert r.confidence == "INFERRED"
    assert "規則未連結到已匯入的規範證據" in r.details["confidence_reasons"]
    (r,) = _run(ents, [with_ref], evidence=[ev], units_assumed=True).results
    assert r.confidence == "INFERRED"
    # quote not present verbatim -> no evidence link (no fuzzy guessing)
    (r,) = _run(ents, [_clearance(evidence_ref={"quote": "不得小於 500 mm"})], evidence=[ev]).results
    assert r.evidence_id is None and r.confidence == "INFERRED"


def test_disabled_rule_is_skipped(ef):
    ents = [ef.line("SCADA", (0, 0), (1000, 0)), ef.line("POWER", (0, 250), (1000, 250))]
    out = _run(ents, [_clearance(enabled=False)])
    assert out.results == [] and out.rule_stats == []


def test_cancellation(ef):
    ents = [ef.line("SCADA", (0, i * 1000), (1000, i * 1000)) for i in range(10)]
    with pytest.raises(Cancelled):
        _run(ents, [_clearance()], cancel_check=lambda: True)
    # evaluate_rule directly (no system classification): select by layer
    ctx = build_context(ents, cancel_check=lambda: True)
    rule = _clearance(subject={"layer_regex": "^SCADA"}, target={"layer_regex": "^SCADA"})
    with pytest.raises(Cancelled):
        evaluate_rule(normalize_rule(rule), ctx)


def test_grid_and_brute_force_give_identical_results(ef):
    import random
    rng = random.Random(7)
    ents = []
    for i in range(150):
        x, y = rng.uniform(0, 20000), rng.uniform(0, 20000)
        layer = rng.choice(["SCADA-A", "POWER-B", "NOGO", "SUPPORT"])
        if layer == "NOGO":
            ents.append(ef.rect(layer, x, y, x + 800, y + 600))
        elif layer == "SUPPORT":
            ents.append(ef.circle(layer, (x, y), 40))
        else:
            ents.append(ef.line(layer, (x, y), (x + rng.uniform(-3000, 3000), y + rng.uniform(-3000, 3000))))
    rules = [_clearance(),
             {"id": "NOGO", "name": "n", "subject": {"system": "SCADA"}, "zone": {"layer_equals": "NOGO"},
              "measurement": "outside_zone"},
             {"id": "SUP", "name": "s", "subject": {"system": "SCADA"}, "target": {"system": "SUPPORT"},
              "measurement": "distance", "operator": "<=", "value": 1500}]
    a = _run(ents, rules, index_kind="grid")
    b = _run(ents, rules, index_kind="brute")
    key = lambda o: sorted((r.issue_id, r.status, r.measured) for r in o.results)  # noqa: E731
    assert key(a) == key(b)
    assert len(a.results) > 0 and a.counts["FAIL"] > 0
    assert isinstance(BruteForceIndex([]).query((0, 0, 1, 1)), list)


def test_routes_group_touching_segments(ef):
    ents = [ef.line("SCADA", (0, 0), (100, 0)), ef.line("SCADA", (100, 0), (100, 100)),
            ef.line("SCADA", (5000, 0), (5100, 0)), ef.line("POWER", (100, 100), (200, 100))]
    out = _run(ents, [])
    routes = {r["route_id"]: r for r in out.routes}
    assert routes["R-SCADA-001"]["entity_count"] == 2
    assert routes["R-SCADA-002"]["entity_count"] == 1
    assert routes["R-POWER-001"]["entity_count"] == 1
