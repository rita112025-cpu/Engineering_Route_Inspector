"""Findings of the review of 7dd73bb (D-1 .. D-5). Each test failed before its fix."""
from __future__ import annotations

import csv
import io
import os
import threading
import time

import psutil
import pytest

from exporters.report import measured_text, required_text
from jobs import manager as M

from tests.integration.conftest import ruleset


def _issue(measurement, op, value, measured=None, unit="count"):
    return {"measurement": measurement, "required_op": op, "required_value": value, "measured": measured, "unit": unit}


# -- D-1 -----------------------------------------------------------------------------------------------

def test_d1_concurrent_demo_loads_create_exactly_one_project(live):
    """6 simultaneous 'load the demo' calls (a double click, two tabs) must end with one demo project."""
    out, lock = [], threading.Lock()
    start = threading.Barrier(6)

    def go():
        start.wait()
        r = live.http.post("/api/demo")
        with lock:
            out.append((r.status_code, r.json()))
    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    assert len(out) == 6 and all(code == 200 for code, _ in out), out
    assert sum(1 for _, b in out if b["created"]) == 1, [b["created"] for _, b in out]
    assert len({b["project"]["id"] for _, b in out}) == 1
    assert all(b["drawing_id"] for _, b in out), "a caller must never get a half-built demo"
    demos = [p for p in live.http.get("/api/projects").json()["projects"] if p["is_demo"]]
    assert len(demos) == 1


# -- D-2 -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("m,op,value,expected", [
    ("outside_zone", "==", 1, "必須在區域外"),
    ("outside_zone", ">=", 1, "必須在區域外"),
    ("outside_zone", "==", 0, "必須進入區域（不在區域外）"),
    ("outside_zone", "<", 1, "必須進入區域（不在區域外）"),
    ("inside_zone", "==", 1, "必須完全在區域內"),
    ("inside_zone", "==", 0, "不得完全在區域內"),
    ("inside_zone", "!=", 1, "不得完全在區域內"),
])
def test_d2_zone_requirement_text_follows_operator_and_value(m, op, value, expected):
    assert required_text(_issue(m, op, value)) == expected


def test_d2_zone_requirement_that_everything_or_nothing_satisfies_is_said_plainly():
    assert required_text(_issue("outside_zone", ">=", 0)) == "任何結果都符合（沒有限制）"
    assert required_text(_issue("outside_zone", ">", 1)) == "沒有任何結果能符合"


def test_d2_measured_text_unchanged():
    assert measured_text(_issue("outside_zone", "==", 1, measured=0)) == "進入了區域"
    assert measured_text(_issue("outside_zone", "==", 1, measured=1)) == "在區域外"


# -- D-4 -----------------------------------------------------------------------------------------------

def test_d4_csv_says_which_project_it_comes_from(live):
    info = live.http.post("/api/demo").json()
    pid = info["project"]["id"]
    run = live.http.post(f"/api/projects/{pid}/runs", json={"drawing_id": info["drawing_id"]}).json()
    deadline = time.monotonic() + 60
    while live.http.get(f"/api/runs/{run['id']}").json()["status"] not in ("completed", "failed"):
        assert time.monotonic() < deadline
        time.sleep(0.1)
    r = live.http.post(f"/api/runs/{run['id']}/exports", json={"format": "csv"})
    assert r.status_code in (200, 201), r.text
    body = live.http.get(f"/api/exports/{r.json()['id']}/download")
    rows = list(csv.reader(io.StringIO(body.content.decode("utf-8-sig"))))
    head = rows[0]
    assert "專案" in head
    assert all(row[head.index("專案")] == info["project"]["name"] for row in rows[1:])
    assert "示範" in info["project"]["name"]
