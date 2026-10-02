"""Regression tests for the review findings against the initial commit 9152ac8.

Each test reproduces one finding; numbering follows the review report.
"""
from __future__ import annotations

import math

import ezdxf
import pytest

from core.analysis.pipeline import run_analysis
from core.evidence.chunks import chunk_lines, find_by_quote
from core.geometry.measure import entity_distance, zone_relation
from core.models.results import EvidenceChunk
from core.rules import predicate as P
from core.rules.schema import RuleError, normalize_rule, normalize_ruleset
from importers.dxf import MAX_ENTITIES, load_dxf

SYSTEMS = [{"name": "SCADA", "layer_regex": "^SCADA"}, {"name": "POWER", "layer_regex": "^POWER"}]


def _run(entities, rules, **kw):
    rs = normalize_ruleset({"systems": SYSTEMS, "rules": rules})
    return run_analysis(entities, rs, drawing_key="t.dxf", **kw)


def _clearance(**kw):
    r = {"id": "CLR", "name": "淨距", "subject": {"system": "SCADA"}, "target": {"system": "POWER"},
         "measurement": "horizontal_clearance", "operator": ">=", "value": 300}
    r.update(kw)
    return r


# 1 -- evidence quote must not match inside a larger number ---------------------

def test_01_quote_does_not_match_inside_larger_number():
    chunks, _ = chunk_lines(["水平淨距不得小於 1300 mm。", "另一段：間距 13.5 mm。"],
                            document_id="d", filename="s.txt", doc_hash="h", page=1)
    assert find_by_quote(chunks, "300 mm") is None
    assert find_by_quote(chunks, "3.5 mm") is None
    assert find_by_quote(chunks, "1300 mm") is not None
    assert find_by_quote(chunks, "13.5 mm") is not None
    ok, _ = chunk_lines(["淨距不得小於 300 mm。"], document_id="d", filename="s.txt", doc_hash="h", page=1)
    assert find_by_quote(ok, "300 mm") is not None
    assert find_by_quote(ok, "300mm") is not None


def test_01_confirmed_requires_quote_number_to_match_rule_value(ef):
    ev = EvidenceChunk("EV-1", "d", "s.txt", 1, 1, 1, "", "水平淨距不得小於 500 mm。", "h")
    ents = [ef.line("SCADA", (0, 0), (1000, 0)), ef.line("POWER", (0, 250), (1000, 250))]
    (r,) = _run(ents, [_clearance(evidence_ref={"quote": "不得小於 500 mm"})], evidence=[ev]).results
    assert r.confidence == "INFERRED"
    assert any("數值" in x for x in r.details["confidence_reasons"])
    ev2 = EvidenceChunk("EV-2", "d", "s.txt", 1, 1, 1, "", "水平淨距不得小於 0.3 m。", "h")
    (r,) = _run(ents, [_clearance(evidence_ref={"quote": "不得小於 0.3 m"})], evidence=[ev2]).results
    assert r.confidence == "CONFIRMED"           # 0.3 m == 300 mm
    ev3 = EvidenceChunk("EV-3", "d", "s.txt", 1, 1, 1, "", "水平淨距不得小於 300 mm。", "h")
    (r,) = _run(ents, [_clearance(evidence_id="EV-3")], evidence=[ev3]).results
    assert r.confidence == "CONFIRMED"
    (r,) = _run(ents, [_clearance(evidence_id="EV-1")], evidence=[ev]).results
    assert r.confidence == "INFERRED"


# 2 -- MINSERT --------------------------------------------------------------------

def test_02_minsert_is_expanded(tmp_path):
    doc = ezdxf.new("R2018")
    blk = doc.blocks.new("B")
    blk.add_line((0, 0), (1, 0), dxfattribs={"layer": "SCADA"})
    doc.modelspace().add_blockref("B", (0, 0), dxfattribs={
        "column_count": 3, "row_count": 2, "column_spacing": 10, "row_spacing": 20})
    p = tmp_path / "m.dxf"
    doc.saveas(p)
    d = load_dxf(p)
    lines = [e for e in d.entities if e.entity_type == "LINE"]
    assert len(lines) == 6
    assert len({e.handle for e in lines}) == 6
    origins = sorted((e.geometry["points"][0][0], e.geometry["points"][0][1]) for e in lines)
    assert origins == [(0, 0), (0, 20), (10, 0), (10, 20), (20, 0), (20, 20)]


def test_02_minsert_over_limit_warns(tmp_path, monkeypatch):
    import importers.dxf as D
    monkeypatch.setattr(D, "MAX_MINSERT", 4)
    doc = ezdxf.new("R2018")
    doc.blocks.new("B").add_line((0, 0), (1, 0))
    doc.modelspace().add_blockref("B", (0, 0), dxfattribs={"column_count": 3, "row_count": 3,
                                                           "column_spacing": 5, "row_spacing": 5})
    p = tmp_path / "m.dxf"
    doc.saveas(p)
    d = load_dxf(p)
    assert [e for e in d.entities if e.entity_type == "LINE"] == []
    assert any("陣列" in w for w in d.warnings)        # never silently dropped


# 3 -- circles ---------------------------------------------------------------------

def _tangent_line(ef, layer, gap, radius=1000.0, deg=3.75):
    """A long line perpendicular to direction `deg`, `gap` mm outside a circle of `radius` at the origin.

    3.75 degrees is half a step of the former 48-gon, i.e. exactly between two polygon vertices
    where an inscribed polygon is furthest from the true circle.
    """
    t = math.radians(deg)
    ux, uy = math.cos(t), math.sin(t)
    px, py = (radius + gap) * ux, (radius + gap) * uy
    return ef.line(layer, (px - 5000 * uy, py + 5000 * ux), (px + 5000 * uy, py - 5000 * ux))


def test_03_circle_distance_is_exact(ef):
    big = ef.circle("SCADA", (0, 0), 1000.0)
    assert entity_distance(big, _tangent_line(ef, "POWER", 299.0)).distance == pytest.approx(299.0, abs=1e-6)
    c2 = ef.circle("POWER", (2299, 0), 1000.0)
    assert entity_distance(big, c2).distance == pytest.approx(299.0, abs=1e-6)
    inner = ef.circle("POWER", (100, 0), 200.0)
    r = entity_distance(big, inner)
    assert r.distance == 0.0 and r.contained
    assert entity_distance(big, ef.line("POWER", (-3000, 0), (3000, 0))).distance == 0.0   # crosses
    r = entity_distance(big, ef.line("POWER", (-100, 0), (100, 0)))                        # fully inside
    assert r.distance == 0.0 and r.contained


def test_03_circle_clearance_rule_not_overestimated(ef):
    ents = [ef.circle("SCADA", (0, 0), 1000.0), _tangent_line(ef, "POWER", 299.0)]
    (r,) = _run(ents, [_clearance()]).results
    assert r.status == "FAIL" and r.measured == pytest.approx(299.0, abs=0.01)


def test_03_arcs_use_fine_tolerance(tmp_path):
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    doc.modelspace().add_arc((0, 0), 1000, 0, 180, dxfattribs={"layer": "SCADA"})
    p = tmp_path / "a.dxf"
    doc.saveas(p)
    (arc,) = load_dxf(p).entities
    worst = max(abs(math.hypot(x, y) - 1000) for x, y in arc.geometry["points"])
    assert worst < 1e-6                       # vertices lie on the arc
    mid = [(a + b) / 2 for a, b in zip(arc.geometry["points"][0], arc.geometry["points"][1])]
    assert 1000 - math.hypot(*mid) <= 0.1 + 1e-9   # chord error at most 0.1 mm


# 4 -- concave zones ---------------------------------------------------------------

COMB = [(0, 0), (100, 0), (100, 100), (80, 100), (80, 20), (60, 20), (60, 100), (40, 100), (40, 20),
        (20, 20), (20, 100), (0, 100)]


def test_04_segment_leaving_a_concave_zone_is_not_inside(ef):
    zone = ef.line("ZONE", *COMB, closed=True)
    through_notch = ef.line("SCADA", (10, 50), (30, 50), (50, 50))     # a vertex lies in the notch
    assert zone_relation(ef.line("SCADA", (10, 50), (30, 50)), zone) == "partial"
    assert zone_relation(through_notch, zone) == "partial"
    straight = ef.line("SCADA", (10, 50), (90, 50))
    assert zone_relation(straight, zone) == "partial"
    assert zone_relation(ef.line("SCADA", (10, 10), (90, 10)), zone) == "inside"
    rule = {"id": "IN", "name": "in", "subject": {"system": "SCADA"}, "zone": {"layer_equals": "ZONE"},
            "measurement": "inside_zone"}
    (r,) = _run([zone, ef.line("SCADA", (10, 50), (90, 50))], [rule]).results
    assert r.status == "FAIL"


def test_04_touching_the_boundary_still_counts_as_inside(ef):
    zone = ef.rect("ZONE", 0, 0, 100, 100)
    assert zone_relation(ef.line("SCADA", (0, 50), (100, 50)), zone) == "inside"
    assert zone_relation(ef.line("SCADA", (0, 0), (100, 0)), zone) == "inside"


# 5 -- quantifier "all" with an upper bound ------------------------------------------

def test_05_all_with_upper_bound_sees_far_targets(ef):
    rule = _clearance(measurement="distance", operator="<=", value=100, quantifier="all")
    ents = [ef.line("SCADA", (0, 0), (1000, 0)), ef.line("POWER", (0, 400), (1000, 400))]
    (r,) = _run(ents, [rule]).results
    assert r.status == "FAIL" and r.measured == pytest.approx(400.0)
    assert "找不到" not in r.message


# 6 -- message rounding --------------------------------------------------------------

def test_06_message_does_not_round_a_failure_into_the_limit(ef):
    ents = [ef.line("SCADA", (0, 0), (1000, 0)), ef.line("POWER", (0, 299.96), (1000, 299.96))]
    (r,) = _run(ents, [_clearance()]).results
    assert r.status == "FAIL"
    assert "299.96" in r.message and "增加至少 0.04 mm" in r.fix
    assert "增加至少 0 mm" not in r.fix


# 7 -- issue ids: documented behaviour, see issues.py (location is part of the id) ---

# 8 -- INSUNITS -------------------------------------------------------------------------

@pytest.mark.parametrize("code,factor", [(13, 0.001), (12, 1e-6), (15, 1e4), (21, 304.8006096)])
def test_08_declared_units_are_not_reported_as_undeclared(tmp_path, code, factor):
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = code
    doc.modelspace().add_line((0, 0), (1, 0))
    p = tmp_path / "u.dxf"
    doc.saveas(p)
    d = load_dxf(p)
    assert d.unit_to_mm == pytest.approx(factor) and not d.units_assumed
    assert not any("未宣告" in w for w in d.warnings)


def test_08_unsupported_unit_code_gets_its_own_warning(tmp_path):
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 20           # parsecs
    doc.modelspace().add_line((0, 0), (1, 0))
    p = tmp_path / "u.dxf"
    doc.saveas(p)
    d = load_dxf(p)
    assert d.units_assumed and any("不支援" in w and "20" in w for w in d.warnings)
    assert not any("未宣告" in w for w in d.warnings)


# 9 / 10 -- schema and predicate validation never raise raw TypeErrors ---------------------

@pytest.mark.parametrize("field,value", [("measurement", []), ("measurement", {}), ("operator", []),
                                         ("unit", []), ("severity", []), ("name", []), ("id", []),
                                         ("quantifier", []), ("warn_margin", [])])
def test_09_bad_field_types_give_readable_errors(field, value):
    raw = {"id": "R", "name": "n", "subject": {"layer_equals": "A"}, "target": {"layer_equals": "B"},
           "measurement": "distance", "operator": ">=", "value": 1}
    raw[field] = value
    with pytest.raises(RuleError):
        normalize_rule(raw)


def test_10_entity_type_list_items_must_be_strings():
    with pytest.raises(P.PredicateError):
        P.validate({"entity_type": [123]})


# 11 / MAX_ENTITIES --------------------------------------------------------------------------

def test_entity_cap_warns_once_and_caps_attribs(tmp_path, monkeypatch):
    import importers.dxf as D
    monkeypatch.setattr(D, "MAX_ENTITIES", 5)
    doc = ezdxf.new("R2018")
    inner = doc.blocks.new("INNER")
    for i in range(4):
        inner.add_line((i, 0), (i + 1, 0))
    outer = doc.blocks.new("OUTER")
    outer.add_blockref("INNER", (0, 0))
    outer.add_blockref("INNER", (0, 10))
    msp = doc.modelspace()
    for i in range(3):
        ref = msp.add_blockref("OUTER", (0, i * 100))
    for i in range(10):
        msp.add_line((0, i), (1, i))
    p = tmp_path / "cap.dxf"
    doc.saveas(p)
    d = D.load_dxf(p)
    assert len(d.entities) <= 5
    assert len([w for w in d.warnings if "上限" in w]) == 1
    assert MAX_ENTITIES == 2_000_000


# ReDoS guard -------------------------------------------------------------------------------

@pytest.mark.parametrize("pattern", ["^(a+)+$", "(a*)*b", "(x+x+)+y", "^(\\w+\\s?)*$", "((a+)b)+"])
def test_nested_unbounded_quantifiers_are_rejected(pattern):
    with pytest.raises(P.PredicateError, match="回溯"):
        P.compile_regex(pattern)


@pytest.mark.parametrize("pattern", ["^SCADA", "^(POWER|SCADA)-\\d+$", "(ab){2,3}", "[A-Z]+-\\d+", "a+b*c?"])
def test_ordinary_patterns_still_accepted(pattern):
    assert P.compile_regex(pattern) is not None


def test_regex_input_is_length_limited(ef):
    e = ef.text("NOTE", (0, 0), "a" * 100000 + "Z")
    assert not P.match_entity({"text_regex": "Z$"}, e)       # text truncated before matching
    assert P.match_entity({"text_regex": "^a"}, e)


# intersection wording ---------------------------------------------------------------------

def test_intersection_message_for_containment_does_not_say_crossing(ef):
    rule = {"id": "X", "name": "x", "subject": {"system": "SCADA"}, "target": {"layer_equals": "ROOM"},
            "measurement": "intersection"}
    ents = [ef.rect("ROOM", 0, 0, 1000, 1000), ef.line("SCADA", (100, 500), (900, 500))]
    (r,) = _run(ents, [rule]).results
    assert r.status == "FAIL"
    assert "交叉" not in r.message and "位於" in r.message
