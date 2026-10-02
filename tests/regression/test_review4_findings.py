"""Findings N1-N3 and the two suggestions of the review of e14e066, reproduced before the fixes."""
from __future__ import annotations

import time

import pytest

from core.analysis.pipeline import run_analysis
from core.evidence.chunks import quantities_in
from core.models.results import EvidenceChunk
from core.rules.schema import RuleError, normalize_rule, normalize_ruleset

SYSTEMS = [{"name": "SCADA", "layer_regex": "^SCADA"}, {"name": "POWER", "layer_regex": "^POWER"},
           {"name": "SUP", "layer_regex": "^SUP"}]


def _run(entities, rules, **kw):
    return run_analysis(entities, normalize_ruleset({"systems": SYSTEMS, "rules": rules}), drawing_key="t.dxf", **kw)


def _clearance(**kw):
    r = {"id": "CLR", "name": "淨距", "subject": {"system": "SCADA"}, "target": {"system": "POWER"},
         "measurement": "horizontal_clearance", "operator": ">=", "value": 300}
    r.update(kw)
    return r


def _confidence(ef, text, rule):
    ev = EvidenceChunk("EV-1", "d", "s.txt", 1, 1, 1, "", text, "h")
    ents = [ef.line("SCADA", (0, 0), (1000, 0)), ef.line("POWER", (0, 250), (1000, 250))]
    (r,) = _run(ents, [rule], evidence=[ev]).results
    return r


# -- N1: the number in the evidence must carry the matching unit ------------------------------------------------------

@pytest.mark.parametrize("text", [
    "水平淨距不得小於 300 cm。",                      # 300 cm is 3000 mm
    "依第 30 條規定，間距不得小於 500 mm。",           # 30 is a clause number, not 30 cm
    "共 1 項檢查，間距不得小於 500 mm。",              # 1 is a count, not 1 m
    "淨距不得小於 300。",                              # no unit at all: cannot support a length
    "水平淨距不得小於 0.3 mm。",
])
def test_n1_number_without_the_right_unit_does_not_confirm(ef, text):
    r = _confidence(ef, text, _clearance(evidence_ref={"quote": text[:-1]}))
    assert r.confidence == "INFERRED", (text, r.details["confidence_reasons"])
    assert any("規範證據" in x for x in r.details["confidence_reasons"])


@pytest.mark.parametrize("text", [
    "水平淨距不得小於 300 mm。", "水平淨距不得小於 30 cm。", "水平淨距不得小於 0.3 m。", "水平淨距不得小於 300毫米。",
    "水平淨距不得小於 30公分，共 3 項。", "clearance at least 0.3 meters", "第 30 條：不得小於 300 mm。",
])
def test_n1_matching_value_and_unit_confirms(ef, text):
    r = _confidence(ef, text, _clearance(evidence_ref={"quote": text.rstrip("。")}))
    assert r.confidence == "CONFIRMED", (text, r.details["confidence_reasons"])


def test_n1_angle_rules_need_degrees(ef):
    rule = {"id": "ORT", "name": "o", "subject": {"system": "SCADA"}, "measurement": "off_axis_angle",
            "operator": "<=", "value": 1}
    ev_ok = EvidenceChunk("EV-1", "d", "s.txt", 1, 1, 1, "", "偏差不得超過 1°。", "h")
    ev_bad = EvidenceChunk("EV-2", "d", "s.txt", 1, 1, 1, "", "偏差不得超過 1 mm。", "h")
    for ev, want in ((ev_ok, "CONFIRMED"), (ev_bad, "INFERRED")):
        ents = [ef.line("SCADA", (0, 0), (1000, 20))]
        (r,) = _run(ents, [dict(rule, evidence_id=ev.id)], evidence=[ev]).results
        assert r.confidence == want, ev.text


def test_n1_quantities_pair_numbers_with_units():
    q = [(v, u) for v, u, _ in quantities_in("第 30 條：1,200 mm、0.3 m、5 度、7 項、2 ft 6 in，3 meters")]
    assert q == [(1200.0, "mm"), (0.3, "m"), (5.0, "deg"), (2.0, "ft"), (6.0, "in"), (3.0, "m")]
    assert quantities_in("面積 5 m2 與 3 mm2") == []                        # squared units are not lengths


# -- N2: "all" with an upper bound ---------------------------------------------------------------------------------------

def test_n2_all_upper_bound_gives_one_result_per_subject(ef):
    rule = _clearance(measurement="distance", operator="<=", value=100, quantifier="all")
    ents = [ef.line("SCADA", (0, 0), (1000, 0))] + [ef.line("POWER", (0, y), (1000, y)) for y in (400, 700, 1000)]
    out = _run(ents, [rule])
    assert len(out.results) == 1
    r = out.results[0]
    assert r.status == "FAIL" and r.measured > 100                                # one violating target is measured
    assert r.details["violations"]["count"] == 3 and len(r.details["violations"]["handles"]) == 3
    assert "另有 2 個" in r.message


def test_n2_all_upper_bound_scales(ef):
    rule = _clearance(measurement="distance", operator="<=", value=100, quantifier="all")
    n = 240
    ents = [ef.line("SCADA", (0, i * 50), (1000, i * 50)) for i in range(n)]
    ents += [ef.line("POWER", (0, 20000 + i * 50), (1000, 20000 + i * 50)) for i in range(n)]
    t0 = time.perf_counter()
    out = _run(ents, [rule])
    elapsed = time.perf_counter() - t0
    assert len(out.results) == n and all(r.status == "FAIL" for r in out.results)
    assert elapsed < 8, elapsed                       # was 55 s and 57,121 results for the same input


def test_n2_all_upper_bound_still_passes_when_everything_is_close(ef):
    rule = _clearance(measurement="distance", operator="<=", value=500, quantifier="all")
    ents = [ef.line("SCADA", (0, 0), (1000, 0)), ef.line("POWER", (0, 200), (1000, 200)),
            ef.line("POWER", (0, 300), (1000, 300))]
    (r,) = _run(ents, [rule]).results
    assert r.status == "PASS"


# -- N3: catastrophic regular expressions are caught when rules are saved ----------------------------------------------------

def test_n3_probe_finds_the_reported_patterns_and_keeps_legitimate_ones():
    from core.rules.regexcheck import find_slow_patterns
    slow = ["(a|aa)+$", "^(.*a){20}$", "^(a?){25}a{25}$", "^(a+)+$"]
    fine = ["^SCADA", "^(POWER|SCADA)-\\d+$", "^A(?:-\\d+){0,20}$", "[A-Z]+-\\d+", "^.*CABLE.*$", "(ab){2,3}"]
    t0 = time.perf_counter()
    found = find_slow_patterns(slow + fine)
    assert set(found) == set(slow), found
    assert time.perf_counter() - t0 < 30
    t0 = time.perf_counter()
    assert find_slow_patterns(fine) == []
    assert time.perf_counter() - t0 < 2             # a clean rule set costs one quick subprocess


def test_n3_collect_regexes_walks_systems_and_nested_predicates():
    from core.rules.regexcheck import collect_regexes
    rs = {"systems": [{"name": "S", "layer_regex": "^A"}],
          "rules": [{"id": "R1", "subject": {"and": [{"layer_regex": "^B"}, {"not": {"text_regex": "C"}}]},
                     "target": {"or": [{"layer_regex": "^D"}]}, "zone": {"layer_regex": "^E"},
                     "pair_filter": {"different_layer": True}}]}
    assert sorted(p for _, p in collect_regexes(rs)) == ["C", "^A", "^B", "^D", "^E"]


# -- suggestions -------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [["EV-1"], 5, {"a": 1}])
def test_evidence_id_must_be_text(bad):
    raw = {"id": "R", "name": "n", "subject": {"layer_equals": "A"}, "measurement": "length", "operator": "<=",
           "value": 1, "evidence_id": bad}
    with pytest.raises(RuleError) as exc:
        normalize_rule(raw)
    assert any("evidence_id" in e for e in exc.value.errors)


def test_run_timeout_settings_with_bad_values_fall_back(tmp_path, monkeypatch):
    from jobs.manager import DEFAULT_RUN_TIMEOUT, DEFAULT_STALL_TIMEOUT, JobManager
    from persistence.db import open_database
    open_database(tmp_path / "eri.sqlite3").close()
    monkeypatch.setenv("ERI_RUN_TIMEOUT", "soon")
    monkeypatch.setenv("ERI_STALL_TIMEOUT", "-5")
    m = JobManager(tmp_path)
    try:
        assert m.run_timeout == DEFAULT_RUN_TIMEOUT and m.stall_timeout == DEFAULT_STALL_TIMEOUT
        assert any(e["kind"] == "bad_setting" for e in m.events)
    finally:
        m.stop()


def test_n2_indexed_result_equals_brute_force(ef):
    import random
    rng = random.Random(5)
    rule = _clearance(measurement="distance", operator="<=", value=5200, quantifier="all")
    ents = []
    for _ in range(70):
        x, y = rng.uniform(0, 6000), rng.uniform(0, 6000)
        ents.append(ef.line(rng.choice(["SCADA", "POWER"]), (x, y), (x + rng.uniform(-500, 500), y + rng.uniform(-500, 500))))
    a = _run(ents, [rule], index_kind="grid")
    b = _run(ents, [rule], index_kind="brute")
    key = lambda o: sorted((r.issue_id, r.status, r.measured, r.details.get("violations", {}).get("count")) for r in o.results)  # noqa: E731
    assert key(a) == key(b) and len(a.results) > 0
    assert {r.status for r in a.results} >= {"PASS", "FAIL"}


def test_n2_all_upper_bound_grows_roughly_linearly(ef):
    """10x the objects must cost far less than 100x the time (all-pairs would cost 100x)."""
    rule = _clearance(measurement="distance", operator="<=", value=100, quantifier="all")

    def timed(n):
        ents = [ef.line("SCADA", (i * 300, 0), (i * 300 + 200, 0)) for i in range(n)]
        ents += [ef.line("POWER", (i * 300, 50), (i * 300 + 200, 50)) for i in range(n)]
        t0 = time.perf_counter()
        out = _run(ents, [rule])
        return time.perf_counter() - t0, out
    t1, o1 = timed(300)
    t2, o2 = timed(3000)
    assert len(o2.results) == 3000 and all(r.status == "FAIL" for r in o2.results)     # all but the near one violate
    assert t2 < max(t1, 0.05) * 40, (t1, t2)
