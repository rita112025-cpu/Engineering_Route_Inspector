"""Findings of the review of 283c120: B-1, R7-1, R7-2 (+ wording / race / heartbeat items)."""
from __future__ import annotations

import pytest

from core.analysis.pipeline import run_analysis
from core.evidence.chunks import quantities_in
from core.models.results import EvidenceChunk
from core.rules.schema import normalize_ruleset

SYSTEMS = [{"name": "SCADA", "layer_regex": "^SCADA"}, {"name": "POWER", "layer_regex": "^POWER"}]


def _run(entities, rule, **kw):
    return run_analysis(entities, normalize_ruleset({"systems": SYSTEMS, "rules": [rule]}), drawing_key="t.dxf", **kw)


def _rule(**kw):
    r = {"id": "R", "name": "r", "subject": {"system": "SCADA"}, "target": {"system": "POWER"},
         "measurement": "distance", "operator": "<=", "value": 100, "quantifier": "all"}
    r.update(kw)
    return r


# -- B-1: pair_filter must not hide far targets ----------------------------------------------------------------------
@pytest.mark.parametrize("gap,expected", [(50, "PASS"), (150, "FAIL"), (250, "FAIL"), (400, "FAIL"), (2000, "FAIL")])
@pytest.mark.parametrize("index_kind", ["grid", "brute"])
def test_b1_all_upper_bound_with_pair_filter(ef, gap, expected, index_kind):
    rule = _rule(pair_filter={"different_layer": True})
    ents = [ef.line("SCADA-A", (0, 0), (1000, 0)), ef.line("POWER-A", (0, gap), (1000, gap))]
    (r,) = _run(ents, rule, index_kind=index_kind).results
    assert r.status == expected, (gap, r.status, r.message)


def test_b1_pair_filter_only_counts_targets_that_pass_the_filter(ef):
    # subject and targets are all SCADA; the filter keeps only targets on a different layer
    rule = _rule(target={"system": "SCADA"}, pair_filter={"different_layer": True})
    s = ef.line("SCADA-A", (0, 0), (1000, 0))
    near_same_layer = ef.line("SCADA-A", (0, 50), (1000, 50))          # ineligible for s: same layer
    far_other_layer = ef.line("SCADA-B", (0, 900), (1000, 900))        # the only eligible target for s
    out = _run([s, near_same_layer, far_other_layer], rule)
    mine = next(r for r in out.results if r.subject_handles == [s.handle])
    assert mine.status == "FAIL" and mine.measured == pytest.approx(900.0)
    assert mine.details["violations"]["count"] == 1


def test_b1_grid_equals_brute_force_with_filter(ef):
    import random
    rng = random.Random(11)
    rule = _rule(value=4800, pair_filter={"different_layer": True})
    ents = []
    for _ in range(80):
        x, y = rng.uniform(0, 6000), rng.uniform(0, 6000)
        ents.append(ef.line(rng.choice(["SCADA-A", "SCADA-B", "POWER-A", "POWER-B"]), (x, y),
                            (x + rng.uniform(-400, 400), y + rng.uniform(-400, 400))))
    a, b = _run(ents, rule, index_kind="grid"), _run(ents, rule, index_kind="brute")
    key = lambda o: sorted((r.issue_id, r.status, r.details.get("violations", {}).get("count")) for r in o.results)  # noqa: E731
    assert key(a) == key(b) and {r.status for r in a.results} == {"PASS", "FAIL"}


# -- R7-1: only distance-like measurements use the "within window" path ------------------------------------------------
@pytest.mark.parametrize("measurement,op,value", [("overlap", "<=", 100), ("overlap", "<", 100),
                                                  ("intersection", "<=", 0), ("intersection", "<", 1)])
def test_r71_non_distance_measurements_keep_their_meaning(ef, measurement, op, value):
    ents = [ef.line("SCADA", (0, 0), (1000, 0)), ef.line("POWER", (0, 800), (1000, 800))]    # apart, no contact
    results = _run(ents, _rule(measurement=measurement, operator=op, value=value)).results
    assert results and all(r.status == "PASS" for r in results), [(r.status, r.message) for r in results]


def test_r71_crossing_still_fails_intersection_upper_bound(ef):
    ents = [ef.line("SCADA", (0, 0), (1000, 0)), ef.line("POWER", (500, -100), (500, 100))]
    (r,) = _run(ents, _rule(measurement="intersection", operator="<=", value=0)).results
    assert r.status == "FAIL"


# -- R7-2: remaining unit mistakes in evidence -------------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["高度 0,3 m", "坡度不得小於 2 mm/m", "速度 300 m/s", "流量 300 m/h", "300 in the drawing",
                                  "面積 5 m²", "壓力 760 mmHg", "尺寸 3 m×4 m"])
def test_r72_not_a_plain_quantity(text):
    got = [(v, u) for v, u, _ in quantities_in(text)]
    assert (3.0, "m") not in got and (2.0, "mm") not in got and (300.0, "m") not in got and (300.0, "in") not in got, got
    assert (5.0, "m") not in got and (760.0, "mm") not in got


@pytest.mark.parametrize("text,expected", [("間距 0.3 m", [(0.3, "m")]), ("12 in of cover", []), ("12 in. 與 3 in", [(12.0, "in"), (3.0, "in")]),
                                          ("長 1,200 mm", [(1200.0, "mm")]), ("Φ 300 mm。", [(300.0, "mm")])])
def test_r72_ordinary_quantities_still_found(text, expected):
    assert [(v, u) for v, u, _ in quantities_in(text)] == expected


def test_confirmed_reason_does_not_overclaim(ef):
    ev = EvidenceChunk("EV-1", "d", "s.txt", 1, 1, 1, "", "水平淨距不得小於 300 mm。", "h")
    rule = {"id": "C", "name": "c", "subject": {"system": "SCADA"}, "target": {"system": "POWER"},
            "measurement": "horizontal_clearance", "operator": ">=", "value": 300, "evidence_id": "EV-1"}
    ents = [ef.line("SCADA", (0, 0), (1000, 0)), ef.line("POWER", (0, 250), (1000, 250))]
    (r,) = _run(ents, rule, evidence=[ev]).results
    assert r.confidence == "CONFIRMED"
    text = " ".join(r.details["confidence_reasons"])
    assert "相同的數值與單位" in text and "人工確認" in text and "皆可追溯" not in text


def test_all_within_message_says_which_target_is_listed(ef):
    ents = [ef.line("SCADA", (0, 0), (1000, 0))] + [ef.line("POWER", (0, y), (1000, y)) for y in (400, 700)]
    (r,) = _run(ents, _rule()).results
    # the listed target is the first one outside the window (not necessarily the first in drawing order)
    assert "超出範圍者中的第一個" in r.message and "圖面順序" not in r.message and "另有 1 個" in r.message
