"""Background jobs with real worker processes: completion, cancellation,
hard stop of a hung worker, crash detection and startup recovery."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from core.rules.engine import Cancelled
from jobs import manager as M
from jobs.analysis import execute_run
from persistence.db import connect
from persistence.repo import Repo
from .conftest import CLEARANCE, ruleset

# A pathological (catastrophic backtracking) regex that the nested-quantifier guard cannot
# recognise: matching it never yields to cancel_check, which is exactly the "worker does not
# respond" case that the force-kill and the run timeout exist for.
HANG_RULE = {"id": "HANG", "name": "hang", "subject": {"entity_type": "TEXT", "text_regex": "^(a|aa)+$"},
             "measurement": "entity_count", "operator": ">=", "value": 1}


def _wait(pred, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")


def _status(env, rid):
    return Repo(connect(env.storage.db_path)).get_run(rid)


@pytest.fixture
def manager(env):
    m = M.JobManager(env.storage.data_dir)
    yield m
    m.stop()


def _clearance_drawing(ef, n=3):
    ents = []
    for i in range(n):
        ents += [ef.line("SCADA", (0, i * 3000), (5000, i * 3000)),
                 ef.line("POWER", (0, i * 3000 + 200 + 60 * i), (5000, i * 3000 + 200 + 60 * i))]
    return ents


def test_execute_run_in_process_and_baseline(env, ef):
    p, d, rs = env.project_with_drawing(_clearance_drawing(ef))
    r1 = env.repo.create_run(p["id"], d, rs, None)
    env.repo.claim_run(r1["id"], os.getpid(), None)
    s1 = execute_run(env.repo, r1["id"])
    assert s1["counts"] == {"FAIL": 2, "WARNING": 1, "PASS": 0, "UNKNOWN": 0}
    assert s1["comparison"] is None and s1["entity_count"] == 6 and s1["routes_total"] == 6
    run = env.repo.get_run(r1["id"])
    assert run["status"] == "completed" and run["summary"]["timings"]["store"] >= 0
    # revised drawing: first conflict fixed, a new one introduced
    ents = _clearance_drawing(ef)
    ents[1] = ef.line("POWER", (0, 900), (5000, 900))
    ents.append(ef.line("POWER", (0, 9100), (5000, 9100)))
    ents.append(ef.line("SCADA", (0, 9000), (5000, 9000)))
    for e, old in zip(ents, env.repo.load_entities(d["id"])):
        e.handle, e.id = old.handle, old.id
    _, d2, _ = env.project_with_drawing(ents, rules=rs, project=p)
    base = env.repo.latest_completed_run(p["id"], "plan.dxf")
    r2 = env.repo.create_run(p["id"], d2, rs, base["id"])
    env.repo.claim_run(r2["id"], os.getpid(), None)
    s2 = execute_run(env.repo, r2["id"])
    cmp = s2["comparison"]
    assert cmp["available"] and not cmp["ruleset_changed"]
    assert cmp["counts"] == {"NEW": 1, "RESOLVED": 1, "UNCHANGED": 2, "CHANGED": 0}
    states = {i["baseline_state"] for i in env.repo.all_issues(r2["id"]) if i["status"] != "PASS"}
    assert states == {"NEW", "UNCHANGED"}


def test_execute_run_cancel_discards_results(env, ef):
    p, d, rs = env.project_with_drawing(_clearance_drawing(ef))
    r = env.repo.create_run(p["id"], d, rs, None)
    env.repo.claim_run(r["id"], os.getpid(), None)
    with pytest.raises(Cancelled):
        execute_run(env.repo, r["id"], progress=lambda *a: None, cancel_check=lambda: True)
    assert env.repo.list_issues(r["id"])[1] == 0


def test_worker_process_completes_run(env, ef, manager):
    p, d, rs = env.project_with_drawing(_clearance_drawing(ef))
    r = env.repo.create_run(p["id"], d, rs, None)
    manager.start()
    assert manager.wait_idle(60)
    run = _status(env, r["id"])
    assert run["status"] == "completed", run
    assert run["worker_pid"] and run["worker_pid"] != os.getpid()
    assert run["summary"]["counts"]["FAIL"] == 2
    log = (env.storage.data_dir / "logs" / f"{r['id']}.log").read_text(encoding="utf-8")
    assert "completed" in log


def test_cooperative_cancel_of_running_worker(env, ef, manager):
    ents = []
    for i in range(6000):
        ents += [ef.line("SCADA", (i * 50, 0), (i * 50 + 40, 0)), ef.line("SCADA", (i * 50, 200), (i * 50 + 40, 200))]
    rule = dict(CLEARANCE, target={"system": "SCADA"}, value=5000)
    p, d, rs = env.project_with_drawing(ents, rules=ruleset(rule))
    r = env.repo.create_run(p["id"], d, rs, None)
    manager.start()
    _wait(lambda: _status(env, r["id"])["stage"] == "rules")
    env.repo.request_cancel(r["id"])
    t0 = time.monotonic()
    _wait(lambda: _status(env, r["id"])["status"] in ("cancelled", "completed", "failed"))
    run = _status(env, r["id"])
    assert run["status"] == "cancelled" and run["error_message"] == "使用者取消"   # worker stopped itself
    assert time.monotonic() - t0 < M.CANCEL_GRACE_SECONDS
    assert env.repo.list_issues(r["id"])[1] == 0


def test_hung_worker_is_force_stopped_on_cancel(env, ef, manager, monkeypatch):
    monkeypatch.setattr(M, "CANCEL_GRACE_SECONDS", 1.0)
    p, d, rs = env.project_with_drawing([ef.text("NOTE", (0, 0), "a" * 40 + "!")], rules=ruleset(HANG_RULE))
    r = env.repo.create_run(p["id"], d, rs, None)
    manager.start()
    _wait(lambda: _status(env, r["id"])["stage"] == "rules")
    pid = _status(env, r["id"])["worker_pid"]
    env.repo.request_cancel(r["id"])
    _wait(lambda: _status(env, r["id"])["status"] == "cancelled", timeout=20)
    assert "強制停止" in _status(env, r["id"])["error_message"]
    assert M.same_process(pid, None) is None


def test_killed_worker_is_marked_failed(env, ef, manager):
    p, d, rs = env.project_with_drawing([ef.text("NOTE", (0, 0), "a" * 40 + "!")], rules=ruleset(HANG_RULE))
    r = env.repo.create_run(p["id"], d, rs, None)
    manager.start()
    _wait(lambda: _status(env, r["id"])["stage"] == "rules")
    pid = _status(env, r["id"])["worker_pid"]
    os.kill(pid, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
    _wait(lambda: _status(env, r["id"])["status"] == "failed", timeout=20)
    run = _status(env, r["id"])
    assert run["error_code"] == "WORKER_CRASHED" and "started pid" in run["error_detail"]


def test_startup_recovery_marks_lost_runs_and_requeues(env, ef):
    p, d, rs = env.project_with_drawing(_clearance_drawing(ef))
    lost = env.repo.create_run(p["id"], d, rs, None)
    # a worker that died with the previous server: PID of an exited process
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    env.repo.claim_run(lost["id"], dead.pid, 12345.0)
    queued = env.repo.create_run(p["id"], d, rs, None)
    m = M.JobManager(env.storage.data_dir)
    try:
        report = m.start()
        assert report["marked_failed"] == [lost["id"]] and report["requeued"] == [queued["id"]]
        assert _status(env, lost["id"])["error_code"] == "WORKER_LOST"
        assert m.wait_idle(60)
        assert _status(env, queued["id"])["status"] == "completed"
    finally:
        m.stop()


def test_startup_adopts_live_worker_then_detects_its_death(env, ef):
    p, d, rs = env.project_with_drawing(_clearance_drawing(ef))
    r = env.repo.create_run(p["id"], d, rs, None)
    orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        import psutil
        env.repo.claim_run(r["id"], orphan.pid, psutil.Process(orphan.pid).create_time())
        m = M.JobManager(env.storage.data_dir)
        try:
            report = m.start()
            assert report["adopted"] == [r["id"]] and m.status()["running"][0]["adopted"]
            orphan.kill()
            orphan.wait()
            _wait(lambda: _status(env, r["id"])["status"] == "failed", timeout=20)
            assert _status(env, r["id"])["error_code"] == "WORKER_CRASHED"
        finally:
            m.stop()
    finally:
        if orphan.poll() is None:
            orphan.kill()


def test_pid_reuse_is_not_mistaken_for_worker(env):
    me = os.getpid()
    assert M.same_process(me, None) is not None
    assert M.same_process(me, 1.0) is None          # same PID, different creation time
    assert M.same_process(None, None) is None


def test_worker_that_fails_before_claim_is_reported(env, ef, manager):
    p, d, rs = env.project_with_drawing(_clearance_drawing(ef))
    r = env.repo.create_run(p["id"], d, rs, None)
    bad = subprocess.Popen([sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"],
                           stdout=open(env.storage.data_dir / "logs" / f"{r['id']}.log", "wb"),
                           stderr=subprocess.STDOUT)
    manager.tracked[r["id"]] = M.Tracked(r["id"], bad.pid, popen=bad,
                                         log_path=env.storage.data_dir / "logs" / f"{r['id']}.log")
    bad.wait()
    manager.tick()
    run = _status(env, r["id"])
    assert run["status"] == "failed" and run["error_code"] == "WORKER_START_FAILED"
    assert "boom" in run["error_detail"]


def test_stop_interrupts_running_worker(env, ef):
    p, d, rs = env.project_with_drawing([ef.text("NOTE", (0, 0), "a" * 40 + "!")], rules=ruleset(HANG_RULE))
    r = env.repo.create_run(p["id"], d, rs, None)
    m = M.JobManager(env.storage.data_dir)
    m.start()
    _wait(lambda: _status(env, r["id"])["stage"] == "rules")
    pid = _status(env, r["id"])["worker_pid"]
    m.stop()
    run = _status(env, r["id"])
    assert run["status"] == "failed" and run["error_code"] == "INTERRUPTED"
    assert M.same_process(pid, None) is None


def test_worker_cli_rejects_bad_arguments(tmp_path):
    from jobs.worker import main
    assert main(["--data-dir", str(tmp_path), "--run-id", "../../x"]) == 2
    assert main(["--data-dir", str(tmp_path), "--run-id", "run_0123456789abcdef"]) == 2


def test_run_timeout_stops_a_run_that_never_ends(env, ef):
    p, d, rs = env.project_with_drawing([ef.text("NOTE", (0, 0), "a" * 40 + "!")], rules=ruleset(HANG_RULE))
    r = env.repo.create_run(p["id"], d, rs, None)
    m = M.JobManager(env.storage.data_dir, run_timeout=1.5)
    try:
        m.start()
        _wait(lambda: _status(env, r["id"])["status"] == "failed", timeout=20)
        run = _status(env, r["id"])
        assert run["error_code"] == "RUN_TIMEOUT" and "時間上限" in run["error_message"]
        assert M.same_process(run["worker_pid"], None) is None
    finally:
        m.stop()
