from core.analysis.pipeline import run_analysis
from core.rules.engine import build_context, evaluate_rule
from core.rules.schema import RuleError, normalize_rule
from exporters.html_report import render_html
from exporters.markdown import render_markdown
from exporters.report import Report


def _exists(rid, layer="A", **extra):
    return normalize_rule({"id": rid, "name": rid, "subject": {"layer_equals": layer},
                           "measurement": "entity_exists", **extra})


def _connectivity(**extra):
    raw = {"id": "CONN", "name": "route connected", "subject": {"layer_equals": "PATH"},
           "measurement": "connectivity", "snap_tolerance_mm": 1}
    raw.update(extra)
    return normalize_rule(raw)


def test_entity_exists_pass_fail_and_missing_evidence_is_not_confirmed(ef):
    present = evaluate_rule(_exists("PRESENT"), build_context([ef.line("A", (0, 0), (1, 0))]))[0]
    missing = evaluate_rule(_exists("MISSING"), build_context([]))[0]
    assert (present.status, present.measured) == ("PASS", 1.0)
    assert (missing.status, missing.measured) == ("FAIL", 0.0)
    assert present.evidence_id is None and present.confidence == "INFERRED"


def test_rule_source_metadata_is_preserved_and_links_evidence_id():
    source = {"document": "Appendix C", "section": "4.2", "page": 12,
              "clause": "4.2.1", "evidence_id": "EV-12"}
    rule = _exists("SOURCE", source=source)
    assert rule["source"] == source and rule["evidence_id"] == "EV-12"


def test_connectivity_continuous_snap_boundary_disconnected_and_isolated(ef):
    joined = [ef.line("PATH", (0, 0), (10, 0)), ef.line("PATH", (11, 0), (20, 0))]
    result = evaluate_rule(_connectivity(), build_context(joined))[0]
    assert result.status == "PASS" and result.details["component_count"] == 1

    split = [ef.line("PATH", (0, 0), (10, 0)), ef.line("PATH", (12, 0), (20, 0))]
    result = evaluate_rule(_connectivity(), build_context(split))[0]
    assert result.status == "FAIL" and result.details["component_count"] == 2

    isolated = evaluate_rule(_connectivity(), build_context([ef.line("PATH", (0, 0), (10, 0))]))[0]
    assert isolated.status == "UNKNOWN" and "至少需要兩個" in isolated.reason


def test_connectivity_unsupported_geometry_and_missing_tolerance(ef):
    result = evaluate_rule(_connectivity(), build_context([ef.circle("PATH", (0, 0), 5)]))[0]
    assert result.status == "UNKNOWN" and "Unsupported geometry" in result.reason
    try:
        normalize_rule({"id": "BAD", "subject": {"layer_equals": "PATH"},
                        "measurement": "connectivity"})
    except RuleError as exc:
        assert "snap_tolerance_mm" in str(exc)
    else:
        raise AssertionError("connectivity without explicit tolerance must be rejected")


def test_pair_rule_without_target_is_unknown_not_pass(ef):
    rule = normalize_rule({"id": "DIST", "subject": {"layer_equals": "A"},
                           "target": {"layer_equals": "B"}, "measurement": "minimum_distance",
                           "operator": ">=", "value": 300, "unit": "mm"})
    result = evaluate_rule(rule, build_context([ef.line("A", (0, 0), (1, 0))]))[0]
    assert result.status == "UNKNOWN"


def test_coverage_loaded_applicable_evaluated_skipped(ef):
    rules = [_exists(f"E{i}") for i in range(5)]
    rules += [normalize_rule({"id": f"S{i}", "subject": {"layer_equals": "NOPE"},
                              "measurement": "length", "operator": ">=", "value": 1, "unit": "mm"})
              for i in range(3)]
    rules += [_exists(f"D{i}", enabled=False) for i in range(2)]
    out = run_analysis([ef.line("A", (0, 0), (10, 0))], {"systems": [], "rules": rules}, drawing_key="x")
    assert out.coverage == {"loaded": 10, "applicable": 8, "evaluated": 5, "skipped": 3,
                            "passed": 5, "failed": 0, "warning": 0, "undetermined": 0,
                            "percent": 62.5}


def test_reports_do_not_claim_complete_compliance_when_coverage_is_partial():
    summary = {"counts": {"FAIL": 0, "WARNING": 0, "UNKNOWN": 0, "PASS": 1},
               "coverage": {"loaded": 10, "applicable": 8, "evaluated": 5, "skipped": 3,
                            "passed": 1, "failed": 0, "warning": 0, "undetermined": 0, "percent": 62.5},
               "rule_stats": [], "unit_to_mm": 1, "units_assumed": False, "warnings": []}
    report = Report(project={"name": "P"}, drawing={"logical_name": "x.dxf", "sha256": "a" * 64},
                    run={"id": "run", "finished_at": "now", "ruleset_sha256": "b" * 64},
                    summary=summary, issues=[], evidence={}, rules={}, extent=(0, 0, 1, 1))
    md = render_markdown(report).decode("utf-8")
    html = render_html(report, []).decode("utf-8")
    warning = "本次分析僅覆蓋部分工程規則，不代表整體工程設計合格。"
    assert "62.5%" in md and warning in md and "全部合格" not in md
    assert "62.5" in html and warning in html and "全部合格" not in html
