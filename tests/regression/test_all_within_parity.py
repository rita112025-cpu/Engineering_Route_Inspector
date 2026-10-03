"""The ALL-WITHIN speed-up must not change one answer.

Three independent oracles:

1. ``measure._containment`` (which got an early exit): compared with a verbatim copy of the previous
   implementation on thousands of random shape pairs.
2. A straightforward ALL-WITHIN evaluator that uses no spatial index and no search cutoff: every subject is
   compared with every eligible target through ``entity_distance``.
3. Golden hashes of the complete result list of the benchmark drawing (1,000 and 10,000 objects), produced by
   the code *before* the change (commit 346383d).

Plus the full fingerprint of the grid index against the brute-force index, determinism and cancellation.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import random
from pathlib import Path

import pytest

from core.analysis.pipeline import run_analysis
from core.geometry import measure
from core.geometry.measure import entity_distance
from core.geometry.primitives import point_in_polygon
from core.models.entities import make_circle, make_polyline
from core.rules import predicate as P
from core.rules.engine import Cancelled, RuleContext, RuleEvaluator, evaluate_rule
from core.rules.schema import normalize_ruleset
from core.spatial.index import BruteForceIndex, GridIndex

ROOT = Path(__file__).resolve().parents[2]
SYSTEMS = [{"name": "SCADA", "layer_regex": "^SCADA"}, {"name": "POWER", "layer_regex": "^POWER"},
           {"name": "SUPPORT", "layer_regex": "^SUPPORT"}]


# -- helpers -----------------------------------------------------------------------------------------------

def _rule(**kw):
    r = {"id": "R", "name": "r", "subject": {"system": "SCADA"}, "target": {"system": "POWER"},
         "measurement": "distance", "operator": "<=", "value": 100, "quantifier": "all"}
    r.update(kw)
    return r


def _run(entities, rule, **kw):
    return run_analysis(list(entities), normalize_ruleset({"systems": SYSTEMS, "rules": [rule]}),
                        drawing_key="t.dxf", **kw)


def fingerprint(out) -> list:
    """Everything that identifies a result, not just how many there are."""
    rows = []
    for r in out.results:
        v = r.details.get("violations") or {}
        rows.append((r.issue_id, r.status, r.severity, r.rule_id, tuple(r.subject_handles), tuple(r.target_handles),
                     r.measured, r.required_op, r.required_value, r.unit,
                     None if r.location is None else (round(r.location[0], 6), round(r.location[1], 6)),
                     v.get("count"), tuple(v.get("handles") or ()), r.message))
    return sorted(rows, key=lambda row: (row[0], row[3], row[4], row[5]))


# -- 1. _containment against the previous implementation --------------------------------------------------------

def _old_containment(a, b):
    """Verbatim copy of measure._containment before the early exit."""
    for outer, inner in ((a, b), (b, a)):
        if not outer.closed:
            continue
        poly = outer.vertices()
        ob = outer.bbox
        for v in inner.vertices():
            if ob[0] <= v[0] <= ob[2] and ob[1] <= v[1] <= ob[3] and point_in_polygon(v, poly):
                return v
    return None


def _random_shape(rng, ef, near):
    kind = rng.choice(["circle", "rect", "line", "poly"])
    x, y = near[0] + rng.uniform(-300, 300), near[1] + rng.uniform(-300, 300)
    if kind == "circle":
        return ef.circle("L", (x, y), rng.choice([20, 90, 180, 400]))
    if kind == "rect":
        w, h = rng.uniform(50, 600), rng.uniform(50, 600)
        return ef.rect("L", x, y, x + w, y + h)
    if kind == "line":
        return ef.line("L", (x, y), (x + rng.uniform(-500, 500), y + rng.uniform(-500, 500)))
    return ef.line("L", (x, y), (x + rng.uniform(-300, 300), y + rng.uniform(-300, 300)),
                   (x + rng.uniform(-300, 300), y + rng.uniform(-300, 300)), closed=rng.random() < 0.5)


def test_containment_early_exit_equals_the_previous_implementation(ef):
    rng = random.Random(2026)
    contained = disjoint = 0
    for _ in range(4000):
        c = (rng.uniform(0, 3000), rng.uniform(0, 3000))
        a = _random_shape(rng, ef, c)
        b = _random_shape(rng, ef, (c[0] + rng.choice([0, 0, 150, 900, 5000]), c[1] + rng.choice([0, 0, 150, 900, 5000])))
        assert measure._containment(a, b) == _old_containment(a, b)
        assert measure._containment(b, a) == _old_containment(b, a)
        d_new = entity_distance(a, b)
        if d_new.contained:
            contained += 1
        elif d_new.distance > 1000:
            disjoint += 1
    assert contained > 50 and disjoint > 50          # the random scenes really cover nesting and far-apart shapes


def test_entity_distance_is_unchanged_end_to_end(ef, monkeypatch):
    rng = random.Random(5)
    pairs = []
    for _ in range(1500):
        c = (rng.uniform(0, 2000), rng.uniform(0, 2000))
        pairs.append((_random_shape(rng, ef, c), _random_shape(rng, ef, (c[0] + rng.choice([0, 200, 4000]), c[1]))))
    new = [(entity_distance(a, b), entity_distance(a, b, 500.0)) for a, b in pairs]
    monkeypatch.setattr(measure, "_containment", _old_containment)
    old = [(entity_distance(a, b), entity_distance(a, b, 500.0)) for a, b in pairs]
    assert new == old


# -- 2. a straightforward ALL-WITHIN evaluator (no spatial index, no search cutoff) -------------------------------

def reference_all_within(entities, rule):
    rs = normalize_ruleset({"systems": SYSTEMS, "rules": [rule]})
    from core.analysis.pipeline import classify_systems
    ents = list(entities)
    classify_systems(ents, rs["systems"])
    rule = rs["rules"][0]
    ctx = RuleContext(entities=ents, index=BruteForceIndex([e.bbox for e in ents]))
    ev = RuleEvaluator(rule, ctx)
    reach = max(ev.to_drawing_units(ev.value), 1e-6)
    subjects = [e for e in ents if e.is_geometric and P.match_entity(rule["subject"], e)]
    targets = [e for e in ents if e.is_geometric and P.match_entity(rule["target"], e)]
    pf = rule.get("pair_filter")
    expected = {}
    for s in subjects:
        far, found = [], []
        for t in targets:
            if t is s or (pf and not P.match_pair(pf, s, t)):
                continue
            pv = ev.pair_value(s, t, math.inf)
            if pv is None:
                continue                                    # different elevations: not compared
            kind, v, dr, _ = pv
            if dr.distance > reach + 1e-6:
                far.append(t)
            else:
                found.append(("UNKNOWN" if kind == "unknown" else ev.classify_value(v), t, v))
        expected[s.handle] = (far, found)
    return expected, ev


def assert_matches_reference(entities, rule):
    out = _run(entities, rule)
    expected, ev = reference_all_within(entities, rule)
    by_subject = {r.subject_handles[0]: r for r in out.results}
    assert set(by_subject) == set(expected)
    rank = {"FAIL": 0, "WARNING": 1, "UNKNOWN": 2}
    for handle, (far, found) in expected.items():
        r = by_subject[handle]
        if far:
            assert r.status == ev.rule["severity"] or r.status in ("FAIL", "WARNING")
            assert r.details["violations"]["count"] == len(far)
            assert r.target_handles[0] in {t.handle for t in far}
            assert r.measured > ev.value
            continue
        bad = [f for f in found if f[0] != "PASS"]
        if bad:
            top = min(rank[f[0]] for f in bad)
            group = [f for f in bad if rank[f[0]] == top]
            assert r.status == group[0][0]
            assert r.details["violations"]["count"] == len(group)
        else:
            assert r.status == "PASS" and "violations" not in r.details
            if found:
                assert r.measured == pytest.approx(max(f[2] for f in found), abs=1e-3)
            else:
                assert r.measured is None and r.target_handles == []
    return out


# -- scenario matrix --------------------------------------------------------------------------------------------

def _scene_few_targets_many_others(ef):
    ents = [ef.line("SCADA-A", (i * 400, 0), (i * 400 + 300, 0)) for i in range(40)]
    ents += [ef.line("SUPPORT-X", (i * 50, 3000), (i * 50 + 10, 3000)) for i in range(200)]       # unrelated clutter
    ents += [ef.line("POWER-A", (0, 80), (300, 80)), ef.line("POWER-A", (4000, 50), (4300, 50))]
    return ents


def _scene_many_targets(ef):
    rng = random.Random(3)
    ents = [ef.line("SCADA-A", (i * 700, 0), (i * 700 + 600, 0)) for i in range(25)]
    ents += [ef.line("POWER-A", (rng.uniform(0, 17000), rng.uniform(-150, 150)),
                     (rng.uniform(0, 17000), rng.uniform(-150, 150))) for _ in range(150)]
    return ents


def _scene_subject_is_also_target(ef):
    rng = random.Random(8)
    return [ef.line("SCADA-A", (x := rng.uniform(0, 3000), y := rng.uniform(0, 3000)),
                    (x + rng.uniform(-200, 200), y + rng.uniform(-200, 200))) for _ in range(60)]


def _scene_pair_filter(ef):
    rng = random.Random(11)
    ents = []
    for _ in range(80):
        x, y = rng.uniform(0, 6000), rng.uniform(0, 6000)
        ents.append(ef.line(rng.choice(["SCADA-A", "SCADA-B", "POWER-A", "POWER-B"]), (x, y),
                            (x + rng.uniform(-400, 400), y + rng.uniform(-400, 400))))
    return ents


def _pair_scene(gap):
    """One subject and one target ``gap`` mm apart (ALL-WITHIN looks at every target in the drawing, so each
    threshold case needs a drawing of its own)."""
    return lambda ef: [ef.line("SCADA-A", (0, 0), (1000, 0)), ef.line("POWER-A", (0, gap), (1000, gap))]


GAPS = [40, 99.9999, 100, 100.001, 100.5, 101, 250, 5000]


def _hc_scene(kind):
    """One subject and one POWER target per drawing (ALL-WITHIN considers every target in the drawing)."""
    z0 = dict(z_known=True, z_min=0.0, z_max=0.0)
    z9 = dict(z_known=True, z_min=900.0, z_max=900.0)

    def build(ef):
        if kind == "apart_no_z":
            return [ef.line("SCADA-A", (0, 0), (1000, 0)), ef.line("POWER-A", (0, 60), (1000, 60))]
        if kind == "cross_same_level":
            return [ef.line("SCADA-A", (0, 0), (1000, 0), **z0), ef.line("POWER-A", (500, -1000), (500, 1000), **z0)]
        if kind == "cross_other_level":
            return [ef.line("SCADA-A", (0, 0), (1000, 0), **z0), ef.line("POWER-A", (500, -1000), (500, 1000), **z9)]
        if kind == "cross_no_z":
            return [ef.line("SCADA-A", (0, 0), (1000, 0)), ef.line("POWER-A", (500, -1000), (500, 1000))]
        if kind == "overlap_no_z":
            return [ef.line("SCADA-A", (0, 0), (1000, 0)), ef.line("POWER-A", (0, 0), (1000, 0))]
        raise KeyError(kind)
    return build


HC_KINDS = ["apart_no_z", "cross_same_level", "cross_other_level", "cross_no_z", "overlap_no_z"]


SCENES = {
    "few_targets_many_others": (_scene_few_targets_many_others, {}),
    "many_targets": (_scene_many_targets, {"value": 700}),
    "subject_is_also_target": (_scene_subject_is_also_target, {"target": {"system": "SCADA"}, "value": 150}),
    "pair_filter_layer": (_scene_pair_filter, {"value": 4800, "pair_filter": {"different_layer": True}}),
    "pair_filter_system": (_scene_pair_filter, {"value": 1500, "target": {"layer_regex": "^(SCADA|POWER)"},
                                                "pair_filter": {"different_system": True}}),
    "pair_filter_not_same_layer": (_scene_pair_filter, {"value": 900, "target": {"layer_regex": "^(SCADA|POWER)"},
                                                        "pair_filter": {"not": {"same_layer": True}}}),
    **{f"gap_{g}_le": (_pair_scene(g), {"value": 100}) for g in GAPS},
    **{f"gap_{g}_lt": (_pair_scene(g), {"value": 100, "operator": "<"}) for g in GAPS},
    **{f"gap_{g}_minimum_distance": (_pair_scene(g), {"value": 100, "measurement": "minimum_distance"}) for g in (40, 100, 250)},
    **{f"gap_{g}_margin": (_pair_scene(g), {"value": 200, "warn_margin": 150}) for g in (40, 100, 150, 200, 250)},
    **{f"gap_{g}_margin_severity_warning": (_pair_scene(g), {"value": 100, "severity": "WARNING", "warn_margin": 30})
       for g in (40, 80, 100, 250)},
    **{f"hc_{k}": (_hc_scene(k), {"measurement": "horizontal_clearance", "value": 300}) for k in HC_KINDS},
    **{f"gap_{g}_metres": (_pair_scene(g), {"value": 0.1, "unit": "m"}) for g in (40, 100, 250)},
    "no_target_in_drawing": (lambda ef: [ef.line("SCADA-A", (0, 0), (10, 0)), ef.line("SCADA-A", (0, 50), (10, 50))], {}),
    "no_subject_in_drawing": (lambda ef: [ef.line("POWER-A", (0, 0), (10, 0))], {}),
    "circles_and_rectangles": (lambda ef: [
        ef.circle("SCADA-A", (0, 0), 50), ef.rect("SCADA-A", 1000, 0, 1200, 100),
        ef.circle("POWER-A", (60, 0), 20), ef.rect("POWER-A", 1050, 20, 1100, 60), ef.circle("POWER-A", (4000, 0), 90),
    ], {"value": 500}),
}


@pytest.mark.parametrize("name", sorted(SCENES))
def test_scene_grid_equals_brute_force_and_the_reference(ef, name):
    build, overrides = SCENES[name]
    ents = build(ef)
    rule = _rule(**overrides)
    grid, brute = _run(ents, rule, index_kind="grid"), _run(ents, rule, index_kind="brute")
    assert fingerprint(grid) == fingerprint(brute)
    again = _run(ents, rule, index_kind="grid")
    assert fingerprint(again) == fingerprint(grid)                                  # deterministic
    if name not in ("no_subject_in_drawing",):
        assert_matches_reference(ents, rule)
    else:
        assert grid.results == []


@pytest.mark.parametrize("kind,expected", [("apart_no_z", "PASS"), ("cross_same_level", "PASS"), ("cross_other_level", "PASS"),
                                            ("cross_no_z", "UNKNOWN"), ("overlap_no_z", "UNKNOWN")])
def test_horizontal_clearance_unknown_only_without_elevation(kind, expected):
    from tests.conftest import EntityFactory
    (r,) = _run(_hc_scene(kind)(EntityFactory()), _rule(measurement="horizontal_clearance", value=300)).results
    assert r.status == expected, (kind, r.status, r.message)
    if expected == "UNKNOWN":
        assert "Insufficient evidence" in r.reason and r.confidence == "UNKNOWN"


def test_scenes_really_produce_every_status():
    seen = set()
    for name in ("gap_40_le", "gap_250_le", "gap_100_margin", "hc_cross_no_z"):
        build, overrides = SCENES[name]
        from tests.conftest import EntityFactory
        seen |= {r.status for r in _run(build(EntityFactory()), _rule(**overrides)).results}
    assert seen == {"PASS", "WARNING", "FAIL", "UNKNOWN"}, seen


# expected statuses come from the rule's meaning (<= 100 mm, tolerance 1e-6), not from running the engine
@pytest.mark.parametrize("gap,op,expected", [
    (40, "<=", "PASS"), (99.9999, "<=", "PASS"), (100, "<=", "PASS"),
    (100.001, "<=", "FAIL"), (100.5, "<=", "FAIL"), (101, "<=", "FAIL"), (250, "<=", "FAIL"), (5000, "<=", "FAIL"),
    (40, "<", "PASS"), (99.9999, "<", "PASS"), (100, "<", "FAIL"), (100.001, "<", "FAIL"), (5000, "<", "FAIL"),
])
def test_threshold_edges_are_decided_as_before(ef, gap, op, expected):
    (r,) = _run(_pair_scene(gap)(ef), _rule(value=100, operator=op)).results
    assert r.status == expected, (gap, op, r.status, r.measured)
    assert r.measured == pytest.approx(gap, abs=1e-3)


@pytest.mark.parametrize("gap,expected", [(40, "PASS"), (50, "PASS"), (100, "WARNING"), (150, "WARNING"), (200, "WARNING"), (250, "FAIL")])
def test_warn_margin_band(ef, gap, expected):
    (r,) = _run(_pair_scene(gap)(ef), _rule(value=200, warn_margin=150)).results
    assert r.status == expected


def test_violation_count_handles_and_first_offender(ef):
    s = ef.line("SCADA-A", (0, 0), (100, 0))
    near = ef.line("POWER-A", (0, 50), (100, 50))
    far1 = ef.line("POWER-A", (0, 900), (100, 900))
    far2 = ef.line("POWER-A", (0, 5000), (100, 5000))
    far3 = ef.line("POWER-B", (0, 800), (100, 800))
    out = _run([s, near, far1, far2, far3], _rule(value=100))
    (r,) = out.results
    assert r.status == "FAIL" and r.details["violations"]["count"] == 3
    assert set(r.details["violations"]["handles"]) == {far1.handle, far2.handle, far3.handle}
    assert r.target_handles == [far1.handle]                     # the first one in (layer, system) group order
    assert fingerprint(_run([s, near, far1, far2, far3], _rule(value=100), index_kind="brute")) == fingerprint(out)


# -- 3. golden hashes produced by the code before the change (346383d) -------------------------------------------

# Hashes of the complete result rows of the pre-change code (346383d), with exactly one difference applied by hand:
# the sentence "...圖面順序中的第一個" of a multi-violation message now reads "...超出範圍者中的第一個" (the listed target is
# the first one outside the window, which is what it always was). Every other character of every result - ids, statuses,
# measured values, locations, violation counts and handles - is the pre-change value.
GOLDEN = {
    1000: "4cdde9122a4d38a1339fec6450d485c71995c53d79dc7f66f5a777f0c2eadbcb",
    10000: "7cdf612044e95ed9efca15304b329f4ac4881a44780703381a4e29a120c59a1e",
}


@pytest.fixture(scope="module")
def bench_rules():
    spec = importlib.util.spec_from_file_location("benchmark_rules", ROOT / "scripts" / "benchmark_rules.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("n", sorted(GOLDEN))
def test_benchmark_drawing_results_equal_the_pre_change_golden_hash(bench_rules, n):
    geo = bench_rules._load_geometry_benchmark()
    ents = geo.make_entities(n)
    bench_rules.classify_systems(ents, geo.RULESET["systems"])
    ctx = RuleContext(entities=ents, index=GridIndex([e.bbox for e in ents]))
    results = [r for rule in geo.RULESET["rules"] for r in evaluate_rule(rule, ctx)]
    bench_rules.assign_ids("bench.dxf", results)
    assert bench_rules.fingerprint(results) == GOLDEN[n]


# -- cancellation stays responsive -----------------------------------------------------------------------------

def test_all_within_still_checks_for_cancellation_between_subjects(ef):
    rs = normalize_ruleset({"systems": SYSTEMS, "rules": [_rule(value=5000)]})
    ents = [ef.line("SCADA-A", (i * 300, 0), (i * 300 + 200, 0)) for i in range(400)]
    ents += [ef.line("POWER-A", (i * 300, 30), (i * 300 + 200, 30)) for i in range(400)]
    from core.analysis.pipeline import classify_systems
    classify_systems(ents, rs["systems"])
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 5

    ctx = RuleContext(entities=ents, index=GridIndex([e.bbox for e in ents]), cancel_check=cancel)
    with pytest.raises(Cancelled):
        evaluate_rule(rs["rules"][0], ctx)
    assert calls["n"] <= 7                       # stops within a couple of subjects, not after the whole rule


# -- a PASS must never carry a violation count (found by the review of 7236ff8) ------------------------------------

BAND_GAPS = [99.9999995, 100, 100.0000005, 100.0000009, 100.0000011, 100.000002, 100.001, 250]


def _violators(entities_gap_pairs, op):
    """Independent count: targets whose own distance fails the rule (<= 100 mm or < 100 mm, tolerance 1e-6)."""
    from core.rules.engine import compare
    return [g for g in entities_gap_pairs if not compare(g, op, 100)]


@pytest.mark.parametrize("op", ["<=", "<"])
@pytest.mark.parametrize("gap", BAND_GAPS)
def test_a_target_within_the_tolerance_is_a_clean_pass_not_a_pass_with_a_violation(ef, gap, op):
    from core.rules.engine import compare
    (r,) = _run(_pair_scene(gap)(ef), _rule(value=100, operator=op)).results
    expected = "PASS" if compare(gap, op, 100) else "FAIL"
    assert r.status == expected, (gap, op, r.status)
    if r.status == "PASS":
        assert "violations" not in r.details, (gap, op, r.details.get("violations"))
    else:
        assert r.details["violations"]["count"] == 1 and r.details["violations"]["handles"] == r.target_handles


@pytest.mark.parametrize("unit,value,scale", [("mm", 100.0, 1.0), ("cm", 10.0, 1.0), ("m", 0.1, 1.0)])
@pytest.mark.parametrize("extra", [-2e-6, -5e-7, 0.0, 5e-7, 9e-7, 1.5e-6, 1e-3])
def test_the_tolerance_band_is_consistent_in_every_rule_unit(ef, unit, value, scale, extra):
    from core.rules.engine import compare
    unit_mm = {"mm": 1.0, "cm": 10.0, "m": 1000.0}[unit]
    gap_mm = value * unit_mm + extra * unit_mm                         # the same relative offset in each unit
    (r,) = _run(_pair_scene(gap_mm)(ef), _rule(value=value, unit=unit)).results
    assert (r.status == "PASS") == compare(gap_mm / unit_mm, "<=", value)
    assert (r.status == "PASS") == ("violations" not in r.details)


def test_status_and_violation_count_never_contradict_each_other_in_random_scenes(ef):
    """Random subjects with several targets at distances scattered around the limit (inside, on the tolerance edge,
    just outside, far): PASS <=> no violations; the count is the number of targets that fail on their own."""
    from core.rules.engine import compare
    rng = random.Random(77)
    offsets = [-1e-3, -2e-6, -9e-7, 0.0, 5e-7, 9e-7, 1.1e-6, 2e-6, 1e-3, 50.0, 400.0]
    seen_pass = seen_fail = 0
    for op in ("<=", "<"):
        for trial in range(60):
            from tests.conftest import EntityFactory
            f = EntityFactory()
            ents = [f.line("SCADA-A", (0, 0), (1000, 0))]
            gaps = [100 + rng.choice(offsets) for _ in range(rng.randint(1, 4))]
            for k, g in enumerate(gaps):                              # targets above and below, all parallel to the subject
                ents.append(f.line("POWER-A", (0, g if k % 2 == 0 else -g), (1000, g if k % 2 == 0 else -g)))
            rule = _rule(value=100, operator=op)
            out = _run(ents, rule)
            (r,) = out.results
            failing = _violators(gaps, op)
            assert (r.status == "PASS") == (not failing), (op, gaps, r.status)
            if failing:
                assert r.details["violations"]["count"] == len(failing), (op, gaps, r.details["violations"])
                seen_fail += 1
            else:
                assert "violations" not in r.details, (op, gaps, r.details.get("violations"))
                seen_pass += 1
            assert fingerprint(_run(ents, rule, index_kind="brute")) == fingerprint(out)
    assert seen_pass > 20 and seen_fail > 20          # both outcomes are really exercised


def test_the_multi_violation_message_describes_the_listed_target_truthfully(ef):
    """The target shown is the first one outside the window, and the count also includes targets inside the window that
    fail on their own (just under X for a strict "< X")."""
    s = ef.line("SCADA-A", (0, 0), (1000, 0))
    inside_but_failing = ef.line("POWER-A", (0, 99.9999991), (1000, 99.9999991))        # < 100 needs < 100 - 1e-6
    far1 = ef.line("POWER-A", (0, 400), (1000, 400))
    far2 = ef.line("POWER-A", (0, -900), (1000, -900))
    (r,) = _run([s, inside_but_failing, far1, far2], _rule(value=100, operator="<")).results
    assert r.details["violations"]["count"] == 3 and r.target_handles == [far1.handle]
    assert "另有 2 個目標同樣不符" in r.message and "超出範圍者中的第一個" in r.message
    assert "圖面順序" not in r.message
    assert r.measured > 100                                                              # what is listed really is out of range
