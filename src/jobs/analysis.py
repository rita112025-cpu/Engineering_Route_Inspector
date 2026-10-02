"""Execute one analysis run: load inputs from the database, evaluate the
rule snapshot stored on the run, compare with the baseline run and store
the results atomically.

Used by the worker process (``jobs.worker``); tests call it in-process.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from typing import Callable

from core.analysis.issues import compare
from core.analysis.pipeline import Cancelled, run_analysis
from persistence.db import utcnow
from persistence.repo import Repo

PROGRESS_INTERVAL = 0.3   # seconds between progress writes
CANCEL_INTERVAL = 0.25    # seconds between cancel-flag reads
MAX_ROUTES_IN_SUMMARY = 200


class RunGone(Exception):
    """The run or its drawing no longer exists (deleted meanwhile)."""


class Throttle:
    def __init__(self, interval: float):
        self.interval = interval
        self.last = 0.0

    def ready(self) -> bool:
        now = time.monotonic()
        if now - self.last >= self.interval:
            self.last = now
            return True
        return False


def make_hooks(repo: Repo, run_id: str) -> tuple[Callable[[str, float, str], None], Callable[[], bool]]:
    """Progress writer and cancel checker backed by the runs table."""
    prog_t, cancel_t = Throttle(PROGRESS_INTERVAL), Throttle(CANCEL_INTERVAL)
    state = {"stage": None, "note": None, "cancelled": False}

    def progress(stage: str, frac: float, note: str = "") -> None:
        if stage != state["stage"] or note != state["note"] or prog_t.ready():
            state["stage"], state["note"] = stage, note
            repo.update_progress(run_id, stage, frac, note)

    def cancel_check() -> bool:
        if not state["cancelled"] and cancel_t.ready():
            state["cancelled"] = repo.cancel_requested(run_id)
            if not state["cancelled"]:
                repo.conn.execute("UPDATE runs SET heartbeat_at = ? WHERE id = ? AND status = 'running'",
                                  (utcnow(), run_id))
        return state["cancelled"]

    return progress, cancel_check


def baseline_comparison(repo: Repo, run: dict, results) -> tuple[dict[str, str], dict | None]:
    """Per-issue states (NEW / CHANGED / UNCHANGED) and the summary block."""
    bid = run.get("baseline_run_id")
    if not bid:
        return {}, None
    base = repo.get_run(bid)
    if not base or base["status"] != "completed":
        return {}, {"baseline_run_id": bid, "available": False,
                    "note": "基準分析不存在或未完成，無法比較"}
    current = [{"id": r.issue_id, "status": r.status, "measured": r.measured} for r in results]
    cats = compare(repo.issue_brief(bid), current)
    states = {}
    for cat in ("NEW", "CHANGED", "UNCHANGED"):
        for iid in cats[cat]:
            states[iid] = cat
    return states, {
        "baseline_run_id": bid, "available": True,
        "baseline_finished_at": base["finished_at"],
        "ruleset_changed": base["ruleset_sha256"] != run["ruleset_sha256"],
        "drawing_changed": base["drawing_sha256"] != run["drawing_sha256"],
        "counts": {k: len(v) for k, v in cats.items()},
        "resolved": cats["RESOLVED"],
    }


def execute_run(repo: Repo, run_id: str, *, progress=None, cancel_check=None) -> dict:
    """Run the analysis for a run already in status 'running'. Returns the summary.

    Raises Cancelled when cancellation was requested, RunGone when the
    run/drawing disappeared. Any other exception is a real failure.
    """
    if progress is None or cancel_check is None:
        p, c = make_hooks(repo, run_id)
        progress, cancel_check = progress or p, cancel_check or c
    run = repo.get_run(run_id)
    if run is None:
        raise RunGone(run_id)
    drawing = repo.get_drawing(run["drawing_id"])
    if drawing is None:
        raise RunGone(run_id)
    timings: dict[str, float] = {}

    progress("parse", 0.02, "載入圖面物件")
    t0 = time.perf_counter()
    entities = repo.load_entities(drawing["id"])
    evidence = repo.load_evidence(run["project_id"])
    ruleset = repo.run_ruleset(run_id)
    timings["load"] = round(time.perf_counter() - t0, 4)
    if cancel_check():
        raise Cancelled()

    out = run_analysis(
        entities, ruleset, drawing_key=drawing["logical_name"], unit_to_mm=drawing["unit_to_mm"] or 1.0,
        units_assumed=drawing["units_assumed"], evidence=evidence, progress=progress, cancel_check=cancel_check,
        index_kind=run.get("index_kind") or "grid")
    timings.update({k: round(v, 4) for k, v in out.timings.items() if k != "parse"})

    progress("results", 0.95, "比對基準並寫入結果")
    states, comparison = baseline_comparison(repo, run, out.results)
    by_system = Counter(r["system"] for r in out.routes)
    warnings = list(drawing["info"].get("warnings", []))
    summary = {
        "counts": out.counts,
        "total": len(out.results),
        "rule_stats": out.rule_stats,
        "entity_count": len(entities),
        "routes_total": len(out.routes),
        "routes_by_system": dict(sorted(by_system.items())),
        "routes": [dict(r, handles=r["handles"][:20]) for r in out.routes[:MAX_ROUTES_IN_SUMMARY]],
        "evidence_used": sorted(out.evidence_used),
        # the evidence texts as they were during this run: exports stay reproducible after a document is deleted
        "evidence_snapshot": {k: v.to_dict() for k, v in sorted(out.evidence_used.items())},
        "units_assumed": drawing["units_assumed"],
        "unit_to_mm": drawing["unit_to_mm"],
        "drawing": {"id": drawing["id"], "logical_name": drawing["logical_name"], "sha256": drawing["sha256"]},
        "warnings": warnings[:50],
        "comparison": comparison,
        "index_kind": run.get("index_kind") or "grid",
        "timings": timings,
    }
    t0 = time.perf_counter()
    if cancel_check():
        raise Cancelled()
    stored = repo.complete_run(run_id, out.results, summary, states)
    timings["store"] = round(time.perf_counter() - t0, 4)
    if not stored:
        # cancelled (or recovered as failed) while we were computing: discard
        raise Cancelled()
    repo.conn.execute("UPDATE runs SET summary_json = json_set(summary_json, '$.timings', json(?)) WHERE id = ?",
                      (json.dumps(timings), run_id))
    return summary
