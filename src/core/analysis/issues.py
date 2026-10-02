"""Deterministic issue IDs and baseline comparison."""
from __future__ import annotations

import hashlib
from typing import Iterable

from core.models.results import RuleResult

LOCATION_ROUND = 1.0  # drawing units (mm for typical drawings)


def issue_id(drawing_key: str, result: RuleResult) -> str:
    """SHA256(drawing key + rule id + sorted handles + rounded location).

    ``drawing_key`` is the logical drawing identity (its original filename
    within the project) rather than the file content hash, so that editing
    a drawing keeps the IDs of unaffected issues stable and baseline
    comparison can report RESOLVED / UNCHANGED. The content hash is stored
    separately on every run.
    """
    if result.location is None:
        loc = "none"
    else:
        x, y = result.location
        loc = f"{round(x / LOCATION_ROUND) * LOCATION_ROUND:.0f},{round(y / LOCATION_ROUND) * LOCATION_ROUND:.0f}"
    key = "|".join([drawing_key, result.rule_id, ",".join(result.handles), loc])
    return "ISS-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16].upper()


def assign_ids(drawing_key: str, results: Iterable[RuleResult]) -> None:
    seen: dict[str, int] = {}
    for r in results:
        base = issue_id(drawing_key, r)
        n = seen.get(base, 0)
        seen[base] = n + 1
        r.issue_id = base if n == 0 else f"{base}-{n}"


def compare(previous: list[dict], current: list[dict]) -> dict:
    """Compare non-PASS issues of two runs.

    Each item needs: id, status, measured. Returns category -> list of ids.
    NEW: in current, not (or PASS) in previous.
    RESOLVED: non-PASS before, PASS/absent now.
    CHANGED: present both, status or measured differ.
    UNCHANGED: present both, identical.
    """
    prev = {i["id"]: i for i in previous if i["status"] != "PASS"}
    cur = {i["id"]: i for i in current if i["status"] != "PASS"}
    out = {"NEW": [], "RESOLVED": [], "UNCHANGED": [], "CHANGED": []}
    for iid, item in cur.items():
        if iid not in prev:
            out["NEW"].append(iid)
        else:
            p = prev[iid]
            same_measure = (p.get("measured") is None and item.get("measured") is None) or (
                p.get("measured") is not None and item.get("measured") is not None
                and abs(float(p["measured"]) - float(item["measured"])) < 1e-6)
            out["UNCHANGED" if p["status"] == item["status"] and same_measure else "CHANGED"].append(iid)
    for iid in prev:
        if iid not in cur:
            out["RESOLVED"].append(iid)
    for k in out:
        out[k].sort()
    return out
