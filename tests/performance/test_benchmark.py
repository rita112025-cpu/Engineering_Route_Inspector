"""The benchmark script itself is tested at small sizes; the full 50,000-object run is `python scripts/benchmark_geometry.py`."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_geometry.py"


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("benchmark_geometry", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("n", [1, 7, 100, 1234])
def test_generator_makes_exactly_n_objects_deterministically(bench, n):
    a, b = bench.make_entities(n), bench.make_entities(n)
    assert len(a) == n and [e.to_dict() for e in a] == [e.to_dict() for e in b]
    assert len({e.id for e in a}) == n


def test_small_sizes_run_and_the_checks_really_ran(bench, tmp_path, capsys):
    code = bench.main(["--sizes", "100", "1000", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    report = json.loads((tmp_path / "benchmark.json").read_text(encoding="utf-8"))
    assert code == 0 and report["checks_failed"] == []
    for case in report["cases"]:
        names = {c["name"]: c for c in case["checks"]}
        full = names["grid analysis == brute-force analysis (all issues)"]
        assert full["ok"] and not full.get("skipped") and full["detail"].split()[0] == str(case["issues"])
        assert names["grid queries == brute-force queries"]["ok"] and not names["grid queries == brute-force queries"].get("skipped")
        assert case["dxf_entities_read"] == case["objects"] and case["issues"] > case["objects"] // 2
    assert "[SKIP]" not in out and "checks ran and passed" in out


def test_a_large_case_is_labelled_skipped_not_passed(bench, tmp_path, capsys):
    case = bench.run_case(10_001, dxf=False, budget=300)
    brute = [c for c in case["checks"] if c["name"] == "grid analysis == brute-force analysis"]
    assert brute and brute[0].get("skipped")
    assert case["brute_analysis_s"] is None


def test_parity_check_really_detects_a_wrong_index(bench, monkeypatch):
    ents = bench.make_entities(300)
    assert bench.index_parity(ents, 100)[0] is True
    real = bench.GridIndex.query
    monkeypatch.setattr(bench.GridIndex, "query", lambda self, box: real(self, box)[:-1])       # drops one hit
    assert bench.index_parity(ents, 200)[0] is False


def test_analysis_parity_check_detects_a_difference(bench):
    ents = bench.make_entities(100)
    a = bench.run_analysis(ents, bench.RULESET, drawing_key="b")
    b = bench.run_analysis(list(ents), bench.RULESET, drawing_key="b", index_kind="brute")
    assert bench.fingerprint(a) == bench.fingerprint(b)
    b.results[0].measured = (b.results[0].measured or 0) + 1.0
    assert bench.fingerprint(a) != bench.fingerprint(b)
