import pytest

from core.rules import predicate as P
from core.rules.schema import (
    RuleError, describe_requirement, normalize_rule, normalize_ruleset,
)


def _rule(**kw):
    r = {"id": "R-1", "name": "淨距", "subject": {"layer_regex": "^SCADA"},
         "target": {"layer_regex": "^POWER"}, "measurement": "horizontal_clearance",
         "operator": ">=", "value": 300}
    r.update(kw)
    return r


# -- predicates ----------------------------------------------------------------

def test_match_entity_leaf_keys(ef):
    e = ef.line("SCADA-Cable", (0, 0), (1, 0), system="SCADA")
    assert P.match_entity({"layer_regex": "^scada"}, e)          # case-insensitive
    assert P.match_entity({"layer_equals": "scada-cable"}, e)
    assert P.match_entity({"layer_contains": "CABLE"}, e)
    assert P.match_entity({"entity_type": ["line", "LWPOLYLINE"]}, e)
    assert P.match_entity({"system": "scada"}, e)
    assert not P.match_entity({"system": "POWER"}, e)
    assert not P.match_entity({"text_regex": "x"}, e)            # not a text entity
    t = ef.text("NOTE", (0, 0), "SCADA 300mm")
    assert P.match_entity({"text_regex": r"\d+mm"}, t)


def test_combinators(ef):
    e = ef.line("SCADA-A", (0, 0), (1, 0))
    assert P.match_entity({"and": [{"layer_contains": "SCADA"}, {"layer_contains": "-A"}]}, e)
    assert P.match_entity({"or": [{"layer_equals": "X"}, {"layer_contains": "SCADA"}]}, e)
    assert not P.match_entity({"not": {"layer_contains": "SCADA"}}, e)
    assert P.match_entity({"layer_contains": "SCADA", "not": {"layer_equals": "B"}}, e)


def test_pair_predicates(ef):
    a = ef.line("L1", (0, 0), (1, 0), system="SCADA")
    b = ef.line("L2", (0, 0), (1, 0), system="POWER")
    c = ef.line("L3", (0, 0), (1, 0))
    assert P.match_pair({"different_layer": True}, a, b)
    assert not P.match_pair({"same_layer": True}, a, b)
    assert P.match_pair({"different_system": True}, a, b)
    assert not P.match_pair({"different_system": True}, a, c)   # unclassified -> cannot assert
    assert not P.match_pair({"same_system": True}, a, c)


@pytest.mark.parametrize("node,kind", [
    ({}, "entity"),
    ({"unknown": 1}, "entity"),
    ({"layer_regex": "("}, "entity"),
    ({"layer_regex": "a" * 201}, "entity"),
    ({"layer_regex": 5}, "entity"),
    ({"entity_type": []}, "entity"),
    ({"system": ["A", 3]}, "entity"),
    ({"and": []}, "entity"),
    ({"different_layer": "yes"}, "pair"),
    ({"layer_equals": "A"}, "pair"),
    ([], "entity"),
])
def test_predicate_validation_rejects(node, kind):
    with pytest.raises(P.PredicateError):
        P.validate(node, kind)


def test_predicate_depth_limit():
    node = {"layer_equals": "A"}
    for _ in range(P.MAX_DEPTH + 2):
        node = {"not": node}
    with pytest.raises(P.PredicateError):
        P.validate(node)


def test_describe_and_text_opt_in():
    d = P.describe({"layer_regex": "^SCADA", "not": {"entity_type": "TEXT"}})
    assert "圖層符合 /^SCADA/" in d and "非 類型為 TEXT" in d
    assert P.explicitly_mentions_text({"or": [{"entity_type": "MTEXT"}]})
    assert not P.explicitly_mentions_text({"not": {"entity_type": "TEXT"}})


# -- schema --------------------------------------------------------------------

def test_normalize_rule_defaults():
    r = normalize_rule(_rule())
    assert r["severity"] == "FAIL" and r["unit"] == "mm" and r["enabled"] is True
    assert r["quantifier"] == "all"
    near = normalize_rule(_rule(operator="<=", value=500))
    assert near["quantifier"] == "any"
    zone = normalize_rule({"id": "Z", "subject": {"layer_equals": "A"}, "zone": {"layer_equals": "Z"},
                           "measurement": "outside_zone"})
    assert zone["operator"] == "==" and zone["value"] == 1 and zone["unit"] == "count"
    x = normalize_rule({"id": "X", "subject": {"layer_equals": "A"}, "target": {"layer_equals": "B"},
                        "measurement": "intersection"})
    assert (x["operator"], x["value"]) == ("==", 0)


@pytest.mark.parametrize("bad,msg", [
    (_rule(id="bad id!"), "規則 ID"),
    (_rule(severity="INFO"), "嚴重度"),
    (_rule(measurement="nope"), "不支援的檢查方式"),
    (_rule(operator="=>"), "運算子"),
    (_rule(value=True), "數值必須是數字"),
    (_rule(value="300"), "數值必須是數字"),
    (_rule(unit="km"), "長度單位"),
    (_rule(warn_margin=-1), "warn_margin"),
    (_rule(target=None) | {"target": {"bogus": 1}}, "不支援的條件欄位"),
    ({k: v for k, v in _rule().items() if k != "target"}, "需要 target"),
    ({k: v for k, v in _rule().items() if k != "subject"}, "缺少 subject"),
    (_rule(measurement="angle", unit="mm"), "角度單位"),
    (_rule(quantifier="most"), "quantifier"),
])
def test_normalize_rule_errors(bad, msg):
    with pytest.raises(RuleError) as exc:
        normalize_rule(bad)
    assert any(msg in e for e in exc.value.errors), exc.value.errors


def test_normalize_ruleset_duplicates_and_systems():
    with pytest.raises(RuleError) as exc:
        normalize_ruleset({"systems": [{"name": "A", "layer_regex": "("}], "rules": [_rule(), _rule()]})
    errs = " ".join(exc.value.errors)
    assert "規則 ID 重複" in errs and "正規表示式格式錯誤" in errs
    rs = normalize_ruleset({"systems": [{"name": "SCADA", "layer_regex": "^SCADA"}], "rules": [_rule()]})
    assert rs["format"] == "eri-rules" and len(rs["rules"]) == 1
    with pytest.raises(RuleError):
        normalize_ruleset([])


def test_describe_requirement():
    assert describe_requirement(normalize_rule(_rule())) == ">= 300 mm"
    z = normalize_rule({"id": "Z", "subject": {"layer_equals": "A"}, "zone": {"layer_equals": "Z"},
                        "measurement": "inside_zone"})
    assert describe_requirement(z) == "必須完全位於指定區域內"


def test_pair_predicates_only_look_at_layer_and_system():
    """engine._all_within groups targets by (layer, system) because a pair filter can see nothing else.
    Adding a pair condition on another attribute must come with a change there: this test fails first."""
    assert P.PAIR_KEYS == {"different_layer", "same_layer", "different_system", "same_system"}
