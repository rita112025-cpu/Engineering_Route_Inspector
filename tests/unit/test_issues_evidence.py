import random

from core.analysis.issues import assign_ids, compare, issue_id
from core.analysis.pipeline import run_analysis
from core.evidence.chunks import chunk_lines, evidence_id, find_by_quote, is_heading, search
from core.models.results import RuleResult
from core.rules.schema import normalize_ruleset

RULESET = normalize_ruleset({
    "systems": [{"name": "SCADA", "layer_regex": "^SCADA"}, {"name": "POWER", "layer_regex": "^POWER"}],
    "rules": [{"id": "CLR", "name": "淨距", "subject": {"system": "SCADA"}, "target": {"system": "POWER"},
               "measurement": "horizontal_clearance", "operator": ">=", "value": 300}],
})


def _result(**kw):
    base = dict(rule_id="R", rule_name="R", status="FAIL", severity="FAIL", measurement="distance",
                subject_ids=["a"], subject_handles=["1A"], target_handles=["2B"], location=(10.4, 20.6))
    base.update(kw)
    return RuleResult(**base)


def test_issue_id_is_deterministic_and_order_independent():
    a = issue_id("plan.dxf", _result())
    assert a == issue_id("plan.dxf", _result(subject_handles=["2B"], target_handles=["1A"]))
    assert a.startswith("ISS-") and len(a) == 20
    assert a != issue_id("other.dxf", _result())
    assert a != issue_id("plan.dxf", _result(rule_id="R2"))
    assert a != issue_id("plan.dxf", _result(location=(50, 20.6)))
    assert a == issue_id("plan.dxf", _result(location=(10.3, 20.7)))   # rounding to 1 unit
    assert issue_id("p", _result(location=None)) == issue_id("p", _result(location=None))


def test_assign_ids_disambiguates_duplicates():
    rs = [_result(), _result(), _result(rule_id="Z")]
    assign_ids("plan.dxf", rs)
    assert rs[1].issue_id == rs[0].issue_id + "-1"
    assert len({r.issue_id for r in rs}) == 3


def test_compare_categories():
    prev = [{"id": "A", "status": "FAIL", "measured": 250}, {"id": "B", "status": "FAIL", "measured": 100},
            {"id": "C", "status": "WARNING", "measured": 320}, {"id": "D", "status": "PASS", "measured": 900}]
    cur = [{"id": "A", "status": "FAIL", "measured": 250}, {"id": "C", "status": "FAIL", "measured": 280},
           {"id": "D", "status": "FAIL", "measured": 200}, {"id": "E", "status": "UNKNOWN", "measured": None}]
    assert compare(prev, cur) == {"NEW": ["D", "E"], "RESOLVED": ["B"], "UNCHANGED": ["A"], "CHANGED": ["C"]}


def test_issue_ids_stable_across_runs_and_entity_order(ef):
    ents = [ef.line("SCADA", (0, i * 2000), (5000, i * 2000)) for i in range(5)]
    ents += [ef.line("POWER", (0, i * 2000 + 200), (5000, i * 2000 + 200)) for i in range(5)]
    ids1 = sorted(r.issue_id for r in run_analysis(list(ents), RULESET, drawing_key="plan.dxf").results)
    shuffled = list(ents)
    random.Random(3).shuffle(shuffled)
    ids2 = sorted(r.issue_id for r in run_analysis(shuffled, RULESET, drawing_key="plan.dxf").results)
    assert ids1 == ids2 and len(ids1) == 5


def test_editing_one_route_keeps_other_issue_ids(ef):
    ents = [ef.line("SCADA", (0, 0), (5000, 0)), ef.line("POWER", (0, 200), (5000, 200)),
            ef.line("SCADA", (0, 9000), (5000, 9000)), ef.line("POWER", (0, 9100), (5000, 9100))]
    before = run_analysis(ents, RULESET, drawing_key="plan.dxf").results
    moved = ef.line("POWER", (0, 9600), (5000, 9600))
    moved.handle = ents[3].handle
    after = run_analysis(ents[:3] + [moved], RULESET, drawing_key="plan.dxf").results
    cmp = compare([{"id": r.issue_id, "status": r.status, "measured": r.measured} for r in before],
                  [{"id": r.issue_id, "status": r.status, "measured": r.measured} for r in after])
    assert len(cmp["UNCHANGED"]) == 1 and len(cmp["RESOLVED"]) == 1 and cmp["NEW"] == []


# -- evidence ------------------------------------------------------------------

SPEC = """# 弱電管線規範

4.2 SCADA 線路間距
SCADA 電纜與電力電纜之水平淨距
不得小於 300 mm。

第五章 禁設區域
電纜不得穿越機房禁設區。
""".splitlines()


def test_chunk_lines_sections_and_line_numbers():
    chunks, last = chunk_lines(SPEC, document_id="d1", filename="spec.md", doc_hash="abc", page=1)
    texts = [c.text for c in chunks]
    assert texts[0] == "# 弱電管線規範"
    body = next(c for c in chunks if "不得小於 300 mm" in c.text)
    assert body.section == "4.2 SCADA 線路間距"
    assert (body.line_start, body.line_end) == (4, 5)
    assert body.text == "SCADA 電纜與電力電纜之水平淨距\n不得小於 300 mm。"
    assert last == "第五章 禁設區域"
    again, _ = chunk_lines(SPEC, document_id="d1", filename="spec.md", doc_hash="abc", page=1)
    assert [c.id for c in again] == [c.id for c in chunks]
    assert evidence_id("abc", 1, 4, body.text) == body.id


def test_find_by_quote_exact_only():
    chunks, _ = chunk_lines(SPEC, document_id="d1", filename="spec.md", doc_hash="abc", page=1)
    hit = find_by_quote(chunks, "水平淨距 不得小於３００ mm")   # whitespace + full-width digits
    assert hit is not None and "300 mm" in hit.text
    assert find_by_quote(chunks, "不得小於 500 mm") is None
    assert find_by_quote(chunks, "") is None
    assert find_by_quote(chunks, "不得小於 300 mm", document="other.md") is None
    assert find_by_quote(chunks, "不得小於 300 mm", document="SPEC.MD") is not None


def test_search_and_headings():
    chunks, _ = chunk_lines(SPEC, document_id="d1", filename="spec.md", doc_hash="abc", page=1)
    assert [c.line_start for c in search(chunks, "禁設區")][:1] == [7]
    assert search(chunks, "") == []
    assert is_heading("3.1.2 管線")
    assert is_heading("第十二條 一般規定")
    assert not is_heading("SCADA 電纜與電力電纜之水平淨距")
