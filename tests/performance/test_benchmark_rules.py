"""scripts/benchmark_rules.py (rule engine without DXF I/O) is tested at small sizes; the large runs are manual:
``python scripts/benchmark_rules.py --sizes 1000 10000 50000``."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_rules.py"


@pytest.fixture(scope="module")
def br():
    spec = importlib.util.spec_from_file_location("benchmark_rules", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_runs_the_production_code_path_and_reports_every_rule(br, tmp_path, capsys):
    out = tmp_path / "b.json"
    assert br.main(["--sizes", "300", "1000", "--out", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert [c["objects"] for c in report["cases"]] == [300, 1000]
    for case in report["cases"]:
        assert not case.get("aborted") and case["results"] > case["objects"] // 2
        assert {"CLEARANCE", "KEEP-OUT", "SUPPORT", "NO-CROSS", "ALL-WITHIN", "ORTHO", "LENGTH"} == set(case["rules"])
        assert case["rules"]["ALL-WITHIN"]["query_calls"] > 0 and case["rules"]["ALL-WITHIN"]["query_candidates"] > 0
        assert case["rules_total_s"] >= case["rules"]["ALL-WITHIN"]["seconds"] >= 0
        assert case["index_build_s"] >= 0 and len(case["fingerprint"]) == 64
    assert "ALL-WITHIN" in capsys.readouterr().out


def test_fingerprint_is_deterministic_and_sensitive(br):
    geo = br._load_geometry_benchmark()
    a, b = br.run_size(400, ["ALL-WITHIN"], 300, geo), br.run_size(400, ["ALL-WITHIN"], 300, geo)
    assert a["fingerprint"] == b["fingerprint"] and a["results"] == b["results"]
    assert br.run_size(401, ["ALL-WITHIN"], 300, geo)["fingerprint"] != a["fingerprint"]


def test_rule_selection_and_budget(br):
    geo = br._load_geometry_benchmark()
    only = br.run_size(300, ["LENGTH"], 300, geo)
    assert set(only["rules"]) == {"LENGTH"}
    aborted = br.run_size(2000, ["ALL-WITHIN"], 0.0, geo)               # no time at all: must abort, not hang or pass
    assert aborted.get("aborted") is True and "fingerprint" not in aborted
