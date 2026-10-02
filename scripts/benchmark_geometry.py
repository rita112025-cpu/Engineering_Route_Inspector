"""Benchmark of the geometry / rule pipeline at 100, 1,000, 10,000 and 50,000 objects.

    python scripts/benchmark_geometry.py                 # all four sizes
    python scripts/benchmark_geometry.py --sizes 100 1000 --no-dxf

What it measures (wall-clock seconds, this machine, this run; nothing here is a promise about yours):
  * DXF write and DXF read (ezdxf) of the synthetic drawing
  * the analysis pipeline stage by stage: spatial index, route grouping, rules (per rule), evidence, total

What it verifies (exit code 1 when any check fails):
  * 100 and 1,000 objects: the whole analysis through the grid index equals the analysis through the
    brute-force index (every issue id, status, measured value and violation count)
  * 100, 1,000 and 10,000 objects: grid and brute-force candidate lists agree on random queries
  * 50,000 objects: only the production (grid) pipeline runs; brute force would take far too long

The synthetic drawing keeps local density constant (cables in corridors, supports, water pipes, a few
no-go zones), so a pipeline that scales well shows roughly linear growth with the object count.
"""
from __future__ import annotations

import argparse
import json
import math
import platform
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import ezdxf  # noqa: E402

from core.analysis.pipeline import run_analysis  # noqa: E402
from core.models.entities import GeometryEntity, make_circle, make_polyline  # noqa: E402
from core.rules.engine import Cancelled  # noqa: E402
from core.rules.schema import normalize_ruleset  # noqa: E402
from core.spatial.index import BruteForceIndex, GridIndex  # noqa: E402
from importers.dxf import load_dxf  # noqa: E402

SIZES = (100, 1_000, 10_000, 50_000)
PITCH = 6000.0                    # millimetres between cells
PARITY_ANALYSIS_MAX = 1_000       # full grid-vs-brute analysis comparison up to this many objects
PARITY_QUERY_MAX = 10_000         # sampled index query comparison up to this many objects

RULESET = normalize_ruleset({
    "systems": [{"name": "SCADA", "layer_regex": "^SCADA"}, {"name": "POWER", "layer_regex": "^POWER"}],
    "rules": [
        {"id": "CLEARANCE", "name": "水平淨距", "subject": {"system": "SCADA"}, "target": {"system": "POWER"},
         "measurement": "horizontal_clearance", "operator": ">=", "value": 300, "warn_margin": 50},
        {"id": "KEEP-OUT", "name": "禁設區", "subject": {"system": "SCADA"}, "zone": {"layer_equals": "ZONE"},
         "measurement": "outside_zone"},
        {"id": "SUPPORT", "name": "支架間距", "subject": {"system": "SCADA"}, "target": {"layer_equals": "SUPPORT"},
         "measurement": "distance", "operator": "<=", "value": 1500, "quantifier": "any"},
        {"id": "NO-CROSS", "name": "不得與給水管交叉", "subject": {"system": "SCADA"}, "target": {"layer_equals": "WATER"},
         "measurement": "intersection"},
        {"id": "ALL-WITHIN", "name": "全部支架都在 20 m 內", "subject": {"system": "SCADA"},
         "target": {"layer_equals": "SUPPORT"}, "measurement": "distance", "operator": "<=", "value": 20000,
         "quantifier": "all"},
        {"id": "ORTHO", "name": "正交", "subject": {"system": "SCADA"}, "measurement": "off_axis_angle",
         "operator": "<=", "value": 1},
        {"id": "LENGTH", "name": "單段長度", "subject": {"system": "POWER"}, "measurement": "length",
         "operator": "<=", "value": 6, "unit": "m"},
    ],
})


def make_entities(n: int, seed: int = 7) -> list[GeometryEntity]:
    """Exactly n objects: per cell a SCADA run, a POWER run beside it, supports, water pipes, now and then a zone."""
    rng = random.Random(seed)
    out: list[GeometryEntity] = []
    side = max(1, math.ceil(math.sqrt(n / 7)))
    k = 0

    def eid():
        nonlocal k
        k += 1
        return f"bench#{k:X}", f"{k:X}"

    for cell in range(10 ** 9):
        if len(out) >= n:
            break
        cx, cy = (cell % side) * PITCH, (cell // side) * PITCH
        gap = rng.choice([180, 250, 320, 340, 450, 700])             # FAIL / WARNING / PASS mix
        tilt = rng.choice([0, 0, 0, 0, 8])                            # a few slightly off-axis cables
        x1 = cx + rng.uniform(2500, 4800)
        for layer, y in (("SCADA-CABLE", cy + 1000), ("POWER-CABLE", cy + 1000 + gap)):
            i, h = eid()
            out.append(make_polyline(i, h, "bench.dxf", layer, "LWPOLYLINE", [(cx + 200, y), (x1, y + (tilt if layer.startswith("S") else 0))]))
        for _ in range(2):
            i, h = eid()
            out.append(make_circle(i, h, "bench.dxf", "SUPPORT", (cx + rng.uniform(300, 5500), cy + rng.uniform(300, 2500)), 90.0))
        if cell % 3 == 0:
            i, h = eid()
            x = cx + rng.uniform(1500, 4000)
            out.append(make_polyline(i, h, "bench.dxf", "WATER", "LINE", [(x, cy + 300), (x, cy + 2600)]))
        if cell % 40 == 0:
            i, h = eid()
            out.append(make_polyline(i, h, "bench.dxf", "ZONE", "LWPOLYLINE",
                                     [(cx + 3000, cy + 3200), (cx + 5200, cy + 3200), (cx + 5200, cy + 5200), (cx + 3000, cy + 5200)], True))
    return out[:n]


def write_dxf(entities: list[GeometryEntity], path: Path) -> None:
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    for e in entities:
        g = e.geometry
        if g["kind"] == "circle":
            msp.add_circle(tuple(g["center"]), g["radius"], dxfattribs={"layer": e.layer})
        else:
            msp.add_lwpolyline([tuple(p) for p in g["points"]], close=bool(g.get("closed")), dxfattribs={"layer": e.layer})
    doc.saveas(path)


def index_parity(entities: list[GeometryEntity], queries: int, seed: int = 3) -> tuple[bool, int]:
    boxes = [e.bbox for e in entities]
    grid, brute = GridIndex(boxes), BruteForceIndex(boxes)
    rng = random.Random(seed)
    xs = [b[0] for b in boxes] + [b[2] for b in boxes]
    ys = [b[1] for b in boxes] + [b[3] for b in boxes]
    lo_x, hi_x, lo_y, hi_y = min(xs), max(xs), min(ys), max(ys)
    for _ in range(queries):
        w = rng.choice([50.0, 500.0, 3000.0, (hi_x - lo_x) * 0.3])
        x, y = rng.uniform(lo_x, hi_x), rng.uniform(lo_y, hi_y)
        q = (x, y, x + w, y + w * rng.uniform(0.2, 2))
        if grid.query(q) != brute.query(q):
            return False, queries
    return True, queries


def fingerprint(out) -> list:
    return sorted((r.issue_id, r.status, r.measured, r.details.get("violations", {}).get("count")) for r in out.results)


def rss_mb() -> float | None:
    try:
        import psutil
        return round(psutil.Process().memory_info().rss / 1024 / 1024, 1)
    except Exception:  # noqa: BLE001
        return None


def run_case(n: int, *, dxf: bool, budget: float) -> dict:
    case: dict = {"objects": n, "checks": []}
    ents = make_entities(n)
    if dxf:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / f"bench_{n}.dxf"
            t0 = time.perf_counter(); write_dxf(ents, p); case["dxf_write_s"] = round(time.perf_counter() - t0, 3)
            case["dxf_mb"] = round(p.stat().st_size / 1024 / 1024, 2)
            t0 = time.perf_counter(); imp = load_dxf(p); case["dxf_read_s"] = round(time.perf_counter() - t0, 3)
            case["dxf_entities_read"] = len(imp.entities)
            ents = imp.entities                                     # analyse what the importer produced
    deadline = time.monotonic() + budget

    def cancel() -> bool:
        return time.monotonic() > deadline
    t0 = time.perf_counter()
    try:
        out = run_analysis(ents, RULESET, drawing_key="bench.dxf", cancel_check=cancel)
    except Cancelled:
        case["aborted_after_s"] = round(time.perf_counter() - t0, 1)
        case["checks"].append({"name": "finished within budget", "ok": False})
        return case
    case["analysis_s"] = round(time.perf_counter() - t0, 3)
    case["stages_s"] = {k: round(v, 3) for k, v in out.timings.items()}
    case["rules_s"] = {s["rule_id"]: s["seconds"] for s in out.rule_stats}
    case["counts"] = out.counts
    case["issues"] = len(out.results)
    case["rss_mb"] = rss_mb()
    if n <= PARITY_ANALYSIS_MAX:
        t0 = time.perf_counter()
        brute = run_analysis(list(ents), RULESET, drawing_key="bench.dxf", index_kind="brute")
        same = fingerprint(brute) == fingerprint(out)
        case["brute_analysis_s"] = round(time.perf_counter() - t0, 3)
        case["checks"].append({"name": "grid analysis == brute-force analysis (all issues)", "ok": same,
                               "detail": f"{len(out.results)} issues compared"})
    else:
        case["brute_analysis_s"] = None
        case["checks"].append({"name": "grid analysis == brute-force analysis", "ok": True, "skipped": "too large for brute force"})
    if n <= PARITY_QUERY_MAX:
        ok, q = index_parity(ents, 300 if n > PARITY_ANALYSIS_MAX else 600)
        case["checks"].append({"name": "grid queries == brute-force queries", "ok": ok, "detail": f"{q} random queries"})
    else:
        case["checks"].append({"name": "grid queries == brute-force queries", "ok": True, "skipped": "too large for brute force"})
    return case


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sizes", type=int, nargs="+", default=list(SIZES))
    ap.add_argument("--no-dxf", action="store_true", help="skip the DXF write/read measurement")
    ap.add_argument("--budget", type=float, default=900.0, help="seconds allowed per size before it is aborted")
    ap.add_argument("--out", default=str(ROOT / "benchmark_out"), help="folder for benchmark.json")
    args = ap.parse_args(argv)

    cases = []
    print(f"Python {platform.python_version()} on {platform.system()} {platform.machine()}; ezdxf {ezdxf.__version__}")
    for n in args.sizes:
        print(f"\n== {n:,} objects ==", flush=True)
        c = run_case(n, dxf=not args.no_dxf, budget=args.budget)
        cases.append(c)
        if "analysis_s" in c:
            print(f"  analysis {c['analysis_s']}s  (index {c['stages_s'].get('index')}s, routes {c['stages_s'].get('routes')}s, "
                  f"rules {c['stages_s'].get('rules')}s, evidence {c['stages_s'].get('evidence')}s)  issues={c['issues']}  {c['counts']}")
            print("  rules: " + ", ".join(f"{k} {v}s" for k, v in c["rules_s"].items()))
        if "dxf_read_s" in c:
            print(f"  DXF write {c['dxf_write_s']}s, read {c['dxf_read_s']}s ({c['dxf_mb']} MB, {c['dxf_entities_read']} objects)")
        if c.get("brute_analysis_s") is not None:
            print(f"  brute-force analysis (reference) {c['brute_analysis_s']}s")
        for chk in c["checks"]:
            mark = "SKIP" if chk.get("skipped") else ("PASS" if chk["ok"] else "FAIL")   # a skipped check is not a pass
            note = f" ({chk['skipped']})" if chk.get("skipped") else (f" ({chk['detail']})" if chk.get("detail") else "")
            print(f"  [{mark}] {chk['name']}{note}")
    failed = [(c["objects"], k["name"]) for c in cases for k in c["checks"] if not k["ok"]]
    report = {"python": platform.python_version(), "platform": f"{platform.system()} {platform.machine()}",
              "ezdxf": ezdxf.__version__, "when": time.strftime("%Y-%m-%d %H:%M:%S"), "cases": cases,
              "checks_failed": failed}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "benchmark.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nreport: {out / 'benchmark.json'}")
    skipped = sum(1 for c in cases for k in c["checks"] if k.get("skipped"))
    ran = sum(1 for c in cases for k in c["checks"] if not k.get("skipped"))
    print("RESULT:", f"{ran} checks ran and passed, {skipped} skipped by design (too large for brute force)" if not failed
          else f"FAILED {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
