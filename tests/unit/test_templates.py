import pytest

from app import templates as T
from core.rules.schema import RuleError


@pytest.mark.parametrize("text,expected", [
    ("水平淨距不得小於 300 mm。", [(300.0, "mm")]),
    ("間距不得小於30公分，且不超過 1.5公尺", [(30.0, "cm"), (1.5, "m")]),
    ("管線長度 1,200 mm 以內", [(1200.0, "mm")]),
    ("偏差不得超過 1°，或 2 度", [(1.0, "deg"), (2.0, "deg")]),
    ("支架間距 5 ft 或 60 in", [(5.0, "ft"), (60.0, "in")]),
    ("第 4.2 條規定 3 處", [(4.2, None), (3.0, None)]),       # numbers without units are still offered
    ("沒有任何數字", []),
    ("inside 300", [(300.0, None)]),                          # "in" inside a word is not a unit
])
def test_suggest_values(text, expected):
    assert [(s["value"], s["unit"]) for s in T.suggest_values(text)] == expected


def test_selector_to_predicate():
    assert T.selector_to_predicate({"system": "A"}, "x") == {"system": "A"}
    assert T.selector_to_predicate({"layer": "L"}, "x") == {"layer_equals": "L"}
    assert T.selector_to_predicate({"layers": ["L"]}, "x") == {"layer_equals": "L"}
    assert T.selector_to_predicate({"layers": ["A", "B"]}, "x") == {"or": [{"layer_equals": "A"}, {"layer_equals": "B"}]}
    for bad in (None, {}, {"system": ""}, {"layers": []}, {"layers": [1]}, {"a": 1, "b": 2}, "A", {"weird": "x"}):
        with pytest.raises(RuleError):
            T.selector_to_predicate(bad, "檢查對象")


def test_build_rule_defaults_ids_and_names():
    base = {"subject": {"system": "SCADA"}, "target": {"system": "POWER"}, "value": 300, "unit": "mm"}
    r1 = T.build_rule("clearance", base, set())
    r2 = T.build_rule("clearance", base, {r1["id"]})
    assert r1["id"] == "CLEARANCE-01" and r2["id"] == "CLEARANCE-02"
    assert r1["measurement"] == "horizontal_clearance" and r1["operator"] == ">=" and r1["severity"] == "FAIL"
    assert "SCADA" in r1["name"] and "300 mm" in r1["name"]
    custom = T.build_rule("clearance", dict(base, name="我的規則", severity="warning", id="MINE", warn_margin=20), set())
    assert (custom["name"], custom["severity"], custom["id"], custom["warn_margin"]) == ("我的規則", "WARNING", "MINE", 20)
    o = T.build_rule("orthogonal", {"subject": {"layer": "X"}}, set())
    assert o["value"] == 1 and o["unit"] == "deg"                       # template default
    assert T.build_rule("no_crossing", {"subject": {"layer": "A"}, "target": {"layer": "B"}}, set())["value"] == 0


@pytest.mark.parametrize("params", [
    {"subject": {"system": "A"}, "target": {"system": "B"}, "value": -1, "unit": "mm"},
    {"subject": {"system": "A"}, "target": {"system": "B"}, "value": "300", "unit": "mm"},
    {"subject": {"system": "A"}, "target": {"system": "B"}, "value": True, "unit": "mm"},
    {"subject": {"system": "A"}, "target": {"system": "B"}, "value": 5, "unit": "km"},
    {"subject": {"system": "A"}, "target": {"system": "B"}, "value": 5, "severity": "INFO"},
    {"subject": {"system": "A"}, "value": 5},
])
def test_build_rule_rejects_bad_params(params):
    with pytest.raises(RuleError):
        T.build_rule("clearance", params, set())


def test_min_count_needs_a_whole_number():
    with pytest.raises(RuleError):
        T.build_rule("min_count", {"subject": {"layer": "T"}, "value": 1.5}, set())
    assert T.build_rule("min_count", {"subject": {"layer": "T"}, "value": 2}, set())["unit"] == "count"


def test_built_rules_remember_how_to_be_edited():
    params = {"subject": {"system": "A"}, "target": {"system": "B"}, "value": 300, "unit": "mm", "warn_margin": 50}
    r = T.build_rule("clearance", dict(params, evidence_id="EV-1", junk="x"), set())
    assert r["builder"] == {"template_id": "clearance", "params": params}      # junk and evidence_id are not kept
    # rebuilding from the stored parameters gives the same rule (this is what "edit" does)
    again = T.build_rule(r["builder"]["template_id"], dict(r["builder"]["params"], id=r["id"], evidence_id="EV-1"), set())
    assert {k: v for k, v in again.items() if k != "builder"} == {k: v for k, v in r.items() if k != "builder"}
