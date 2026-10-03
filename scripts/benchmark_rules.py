"""Rule-engine benchmark without any DXF I/O: isolates the "every target within X" (ALL-WITHIN) path.

    python scripts/benchmark_rules.py                         # 1,000 / 10,000 / 50,000 objects
    python scripts/benchmark_rules.py --sizes 1000 10000 --rules ALL-WITHIN

It drives the production code path (``core.rules.engine.evaluate_rule`` on a real ``RuleContext``) over the
synthetic drawing of ``benchmark_geometry.py`` and reports, per size and per rule, wall-clock seconds on this
machine: index construction, rule total, and (for the spatial queries the rules issue) how many ``query`` calls
were made, how many candidates they returned and how long they took. Numbers are for comparing two checkouts on
the same machine; they are not a promise about yours.

Every run also prints a fingerprint of the complete result list (issue-relevant fields of every result), so the
output of two checkouts can be compared byte for byte:  ``--fingerprint-out FILE`` writes it.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.analysis.issues import assign_ids  # noqa: E402
from core.analysis.pipeline import classify_systems  # noqa: E402
from core.rules.engine import Cancelled, RuleContext, evaluate_rule  # noqa: E402
from core.spatial.index import GridIndex  # noqa: E402


def _load_geometry_benchmark():
    spec = importlib.util.spec_from_file_location("benchmark_geometry", ROOT / "scripts" / "benchmark_geometry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def result_fingerprint_rows(results) -> list[list]:
    """Everything that identifies a result: id, status, rule, entities, numbers, location, violations."""
    rows = []
    for r in results:
        v = r.details.get("violations") or {}
        rows.append([r.issue_id, r.status, r.severity, r.rule_id, list(r.subject_handles), list(r.target_handles),
                     r.measured, r.required_op, r.required_value, r.unit,
                     None if r.location is None else [round(r.location[0], 6), round(r.location[1], 6)],
                     v.get("count"), list(v.get("handles") or []), r.message])
    return rows


def fingerprint(results) -> str:
    blob = json.dumps(result_fingerprint_rows(results), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class CountingIndex:
    """Wraps an index and records how often it is asked and what it costs (the production index is untouched)."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0
        self.candidates = 0
        self.seconds = 0.0

    def query(self, box):
        t0 = time.perf_counter()
        out = self.inner.query(box)
        self.seconds += time.perf_counter() - t0
        self.calls += 1
        self.candidates += len(out)
        return out

    def __getattr__(self, name):
        return getattr(self.inner, name)


def run_size(n: int, rule_ids: list[str] | None, budget: float, geo) -> dict:
    ents = geo.make_entities(n)
    ruleset = geo.RULESET
    rules = [r for r in ruleset["rules"] if not rule_ids or r["id"] in rule_ids]
    case: dict = {"objects": n, "rules": {}}
    t0 = time.perf_counter()
    classify_systems(ents, ruleset["systems"])
    case["classify_s"] = round(time.perf_counter() - t0, 3)
    t0 = time.perf_counter()
    index = GridIndex([e.bbox for e in ents])
    case["index_build_s"] = round(time.perf_counter() - t0, 3)
    deadline = time.monotonic() + budget
    counting = CountingIndex(index)
    ctx = RuleContext(entities=ents, index=counting, cancel_check=lambda: time.monotonic() > deadline)
    all_results = []
    rules_total = 0.0
    for rule in rules:
        counting.calls = counting.candidates = 0
        counting.seconds = 0.0
        t0 = time.perf_counter()
        try:
            res = evaluate_rule(rule, ctx)
        except Cancelled:
            case["rules"][rule["id"]] = {"aborted_after_s": round(time.perf_counter() - t0, 1)}
            case["aborted"] = True
            break
        dt = time.perf_counter() - t0
        rules_total += dt
        all_results.extend(res)
        case["rules"][rule["id"]] = {
            "seconds": round(dt, 4), "results": len(res), "query_calls": counting.calls,
            "query_candidates": counting.candidates, "query_seconds": round(counting.seconds, 4),
        }
    case["rules_total_s"] = round(rules_total, 3)
    if not case.get("aborted"):
        case["results"] = len(all_results)
        assign_ids("bench.dxf", all_results)                       # ids are assigned by the pipeline, not the rules
        case["fingerprint"] = fingerprint(all_results)
    return case


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sizes", type=int, nargs="+", default=[1_000, 10_000, 50_000])
    ap.add_argument("--rules", nargs="*", default=None, help="rule ids to run (default: all rules of the benchmark)")
    ap.add_argument("--budget", type=float, default=900.0, help="seconds allowed per size before it is aborted")
    ap.add_argument("--out", default=str(ROOT / "benchmark_out" / "benchmark_rules.json"))
    ap.add_argument("--fingerprint-out", default=None, help="write the full result rows (for diffing two checkouts)")
    args = ap.parse_args(argv)
    geo = _load_geometry_benchmark()
    import ezdxf
    print(f"Python {platform.python_version()} on {platform.system()} {platform.machine()}; ezdxf {ezdxf.__version__}")
    cases = []
    for n in args.sizes:
        print(f"\n== {n:,} objects ==", flush=True)
        c = run_size(n, args.rules, args.budget, geo)
        cases.append(c)
        print(f"  classify {c['classify_s']}s, index build {c['index_build_s']}s, rules total {c.get('rules_total_s')}s"
              + ("  (ABORTED: over budget)" if c.get("aborted") else f", {c.get('results')} results"))
        for rid, s in c["rules"].items():
            if "aborted_after_s" in s:
                print(f"  {rid:<11} aborted after {s['aborted_after_s']}s")
            else:
                print(f"  {rid:<11} {s['seconds']:>9}s  results={s['results']:<6} queries={s['query_calls']:<6} "
                      f"candidates={s['query_candidates']:<10} query_time={s['query_seconds']}s")
        if "fingerprint" in c:
            print(f"  fingerprint {c['fingerprint'][:16]}")
    report = {"python": platform.python_version(), "platform": f"{platform.system()} {platform.machine()}",
              "when": time.strftime("%Y-%m-%d %H:%M:%S"), "cases": cases}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nreport: {out}")
    if args.fingerprint_out:
        rows = {}
        for n in args.sizes:                                       # regenerate rows for exact diffing
            ents = geo.make_entities(n)
            classify_systems(ents, geo.RULESET["systems"])
            ctx = RuleContext(entities=ents, index=GridIndex([e.bbox for e in ents]))
            res = []
            for rule in geo.RULESET["rules"]:
                if not args.rules or rule["id"] in args.rules:
                    res.extend(evaluate_rule(rule, ctx))
            assign_ids("bench.dxf", res)
            rows[str(n)] = result_fingerprint_rows(res)
        Path(args.fingerprint_out).write_text(json.dumps(rows, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        print(f"fingerprint rows: {args.fingerprint_out}")
    return 0 if not any(c.get("aborted") for c in cases) else 1


if __name__ == "__main__":
    sys.exit(main())
