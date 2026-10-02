"""Regression tests for the review of units 1-3 (findings S1-S9, T1-T2 and the accepted suggestions)."""
from __future__ import annotations

import io
import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from core.rules.schema import RuleError
from jobs import manager as M
from persistence import db as DB
from persistence.repo import Repo
from persistence.storage import Storage, StorageError, safe_filename
from .conftest import ruleset
from .test_jobs import HANG_RULE, _status, _wait

PID = "p_0123456789abcdef"
SRC = Path(__file__).resolve().parents[2] / "src"


# -- S1 / R3 / R10: stored file names -----------------------------------------------------------

@pytest.mark.parametrize("name", ["a" * 100 + ".dxf", "a" * 107 + ".dxf", "a" * 108 + ".dxf", "a" * 300 + ".dxf",
                                  "圖" * 117 + ".dxf", "圖" * 400 + ".dxf", "x.y.z." * 40 + "dxf"])
def test_s1_any_upload_name_can_be_stored_and_read_back(tmp_path, name):
    st = Storage(tmp_path / "data")
    st.project_dir(PID, create=True)
    f = st.save_stream(PID, "drawings", name, io.BytesIO(b"data"), 1000)
    assert st.file_path(PID, "drawings", f.stored_name) == f.path and f.path.read_bytes() == b"data"
    assert len(f.stored_name) <= 113 and len(f.stored_name.encode("utf-8")) <= 213


def test_r3_safe_filename_is_idempotent_and_drops_invisible_characters():
    rng = random.Random(1)
    alphabet = "ab. \t‮\u0085​日本-_<>"
    for _ in range(20000):
        raw = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        once = safe_filename(raw)
        assert safe_filename(once) == once, repr(raw)
    out = safe_filename("a‮b\u0085c​d.dxf")
    assert all(ch not in out for ch in ("‮", "\u0085", "​")) and out.endswith(".dxf")


# -- S2: concurrent migration ----------------------------------------------------------------------

def test_s2_threads_migrating_the_same_new_database(tmp_path):
    errors = []

    def go(path):
        try:
            c = DB.connect(path)
            DB.migrate(c)
            c.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    for trial in range(5):
        path = tmp_path / f"race{trial}.sqlite3"
        ts = [threading.Thread(target=go, args=(path,)) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        c = DB.connect(path)
        assert [r["version"] for r in c.execute("SELECT version FROM schema_migrations ORDER BY version")] == [1, 2]
        c.close()
    assert errors == []


def test_s2_processes_migrating_the_same_new_database(tmp_path):
    path = tmp_path / "procs.sqlite3"
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from persistence import db; "
            "c = db.connect(sys.argv[2]); db.migrate(c)")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(SRC), str(path)], stderr=subprocess.PIPE)
             for _ in range(4)]
    results = []
    for proc in procs:
        _, err = proc.communicate()
        results.append((proc.returncode, err.decode()))
    assert [r[0] for r in results] == [0, 0, 0, 0], results
    c = DB.connect(path)
    assert DB.schema_version(c) == 2
    c.close()


def test_split_statements_handles_comments_and_quoted_semicolons():
    sql = "-- header\nCREATE TABLE a (x TEXT DEFAULT ';');\n-- middle\n\nINSERT INTO a VALUES ('a;b');\n-- tail\n"
    assert list(DB.split_statements(sql)) == ["-- header\nCREATE TABLE a (x TEXT DEFAULT ';');",
                                              "-- middle\n\nINSERT INTO a VALUES ('a;b');"]


# -- S3 / S4 / S5 / R2 / R4: repository and storage ----------------------------------------------------

def _drawing(env, ef, project, name="plan.dxf"):
    env.storage.project_dir(project["id"], create=True)
    return env.repo.add_drawing(project["id"], name, f"abc_{name}", "a" * 64, 1, [ef.line("SCADA", (0, 0), (1, 0))],
                                unit_to_mm=1.0, units_assumed=False, info={})


def test_s3_run_must_use_a_drawing_and_baseline_of_its_own_project(env, ef):
    p1, p2 = env.repo.create_project("P1"), env.repo.create_project("P2")
    d1, d2 = _drawing(env, ef, p1), _drawing(env, ef, p2)
    with pytest.raises(ValueError, match="不屬於"):
        env.repo.create_run(p1["id"], d2, ruleset(), None)
    other = env.repo.create_run(p2["id"], d2, ruleset(), None)
    with pytest.raises(ValueError, match="基準"):
        env.repo.create_run(p1["id"], d1, ruleset(), other["id"])
    assert env.repo.create_run(p1["id"], d1, ruleset(), None)["status"] == "queued"


def test_s4_importing_the_same_file_twice_keeps_the_first_import(env, ef):
    p = env.repo.create_project("P")
    first = _drawing(env, ef, p)
    again = _drawing(env, ef, p)
    assert again["id"] == first["id"] and again["already_imported"] is True and "already_imported" not in first
    assert env.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 1
    assert env.conn.execute("SELECT COUNT(*) FROM drawings").fetchone()[0] == 1
    d1 = env.repo.add_document(p["id"], "s.md", "h_s.md", "md", "e" * 64, 3, [], [])
    d2 = env.repo.add_document(p["id"], "s.md", "h_s.md", "md", "e" * 64, 3, [], [])
    assert d2["id"] == d1["id"] and d2["already_imported"] is True


def test_s5_database_gives_every_thread_its_own_connection(tmp_path):
    db = DB.Database(tmp_path / "t.sqlite3")
    failures = []

    def work(n):
        try:
            repo = db.repo()
            for i in range(100):
                repo.create_project(f"p{n}-{i}")
        except Exception as exc:  # noqa: BLE001
            failures.append(repr(exc))

    ts = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert failures == []
    assert len(db.repo().list_projects()) == 400
    assert len({id(c) for c in db._all}) == 5          # 4 workers + this thread
    db.close()


def test_r2_save_ruleset_validates_and_normalises(env):
    p = env.repo.create_project("P")
    with pytest.raises(RuleError):
        env.repo.save_ruleset(p["id"], {"systems": [], "rules": [{"id": "X", "measurement": "bogus"}]})
    assert env.repo.get_ruleset(p["id"])["rules"] == []
    env.repo.save_ruleset(p["id"], {"systems": [], "rules": [
        {"id": "L", "subject": {"layer_equals": "A"}, "measurement": "length", "operator": "<=", "value": 5}]})
    stored = env.repo.get_ruleset(p["id"])["rules"][0]
    assert stored["unit"] == "mm" and stored["severity"] == "FAIL" and stored["enabled"] is True


def test_r4_storage_does_not_invent_project_folders(tmp_path):
    st = Storage(tmp_path / "data")
    with pytest.raises(StorageError, match="不存在"):
        st.area(PID, "drawings")
    assert not (tmp_path / "data" / "projects" / PID).exists()


# -- T1: spawn failure ---------------------------------------------------------------------------

def test_t1_unstartable_worker_fails_the_run_instead_of_retrying_forever(env, ef, monkeypatch):
    p, d, rs = env.project_with_drawing([ef.line("SCADA", (0, 0), (1, 0))])
    r = env.repo.create_run(p["id"], d, rs, None)
    monkeypatch.setattr(M.sys, "executable", "/nonexistent/python")
    m = M.JobManager(env.storage.data_dir)
    try:
        m.tick()
        run = _status(env, r["id"])
        assert run["status"] == "failed" and run["error_code"] == "WORKER_START_FAILED"
        assert "FileNotFoundError" in run["error_detail"]
        m.tick()
        assert [e["kind"] for e in m.events].count("spawn_failed") == 1       # not retried
    finally:
        m.stop()


# -- stall detection, recovery and housekeeping --------------------------------------------------------

def test_stalled_worker_is_stopped_long_before_the_run_timeout(env, ef):
    p, d, rs = env.project_with_drawing([ef.text("NOTE", (0, 0), "a" * 40 + "!")], rules=ruleset(HANG_RULE))
    r = env.repo.create_run(p["id"], d, rs, None)
    m = M.JobManager(env.storage.data_dir, run_timeout=3600, stall_timeout=2.0)
    try:
        m.start()
        _wait(lambda: _status(env, r["id"])["status"] == "failed", timeout=30)
        run = _status(env, r["id"])
        assert run["error_code"] == "WORKER_STALLED" and "沒有回應" in run["error_message"]
        assert M.same_process(run["worker_pid"], None) is None
    finally:
        m.stop()


def test_a_busy_but_alive_worker_is_not_called_stalled(env, ef):
    ents = [ef.line("SCADA", (i * 50, 0), (i * 50 + 40, 0)) for i in range(4000)]
    p, d, rs = env.project_with_drawing(ents)
    r = env.repo.create_run(p["id"], d, rs, None)
    m = M.JobManager(env.storage.data_dir, stall_timeout=5.0)
    try:
        m.start()
        assert m.wait_idle(60)
        assert _status(env, r["id"])["status"] == "completed"
    finally:
        m.stop()


def test_live_process_without_creation_time_is_not_adopted(env, ef):
    p, d, rs = env.project_with_drawing([ef.line("SCADA", (0, 0), (1, 0))])
    r = env.repo.create_run(p["id"], d, rs, None)
    orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        env.repo.claim_run(r["id"], orphan.pid, None)
        m = M.JobManager(env.storage.data_dir)
        try:
            report = m.start()
            assert report["adopted"] == [] and report["marked_failed"] == [r["id"]]
            orphan.poll()
            assert orphan.returncode is None                     # an unrelated process is never killed
        finally:
            m.stop()
    finally:
        orphan.kill()
        orphan.wait()


def test_old_logs_of_finished_runs_are_pruned(env, ef):
    logs = env.storage.data_dir / "logs"
    logs.mkdir(exist_ok=True)
    old, fresh = logs / "run_aaaaaaaaaaaaaaaa.log", logs / "run_bbbbbbbbbbbbbbbb.log"
    old.write_text("x")
    fresh.write_text("x")
    long_ago = time.time() - 40 * 86400
    os.utime(old, (long_ago, long_ago))
    m = M.JobManager(env.storage.data_dir)
    try:
        assert m.prune_logs() == 1
        assert not old.exists() and fresh.exists()
    finally:
        m.stop()


def test_exports_keep_evidence_after_the_document_is_deleted(env, ef):
    from core.models.results import EvidenceChunk
    from exporters import service
    from exporters.report import build_report
    ents = [ef.line("SCADA", (0, 0), (5000, 0)), ef.line("POWER", (0, 250), (5000, 250))]
    rule = {"id": "CLR", "name": "淨距", "subject": {"system": "SCADA"}, "target": {"system": "POWER"},
            "measurement": "horizontal_clearance", "operator": ">=", "value": 300,
            "evidence_ref": {"quote": "不得小於 300 mm"}}
    p, d, rs = env.project_with_drawing(ents, rules=ruleset(rule))
    doc = env.repo.add_document(p["id"], "spec.md", "h_spec.md", "md", "e" * 64, 1,
                                [EvidenceChunk("EV-SNAP", "x", "spec.md", 1, 2, 2, "", "淨距不得小於 300 mm。", "h")], [])
    run = env.run_in_process(p, d, rs)
    assert run["summary"]["evidence_snapshot"]["EV-SNAP"]["text"] == "淨距不得小於 300 mm。"
    env.repo.delete_document(doc["id"])
    assert env.repo.get_evidence(p["id"], "EV-SNAP") is None
    rep = build_report(env.repo, run["id"])
    assert rep.evidence["EV-SNAP"]["text"] == "淨距不得小於 300 mm。"
    row = service.export_run(env.repo, env.storage, run["id"], "markdown")
    assert "淨距不得小於 300 mm。" in service.export_file(env.repo, env.storage, row["id"])[0].read_text(encoding="utf-8")


def test_markers_come_from_the_folder_below_tests_only(tmp_path):
    """-m integration must select integration tests only, wherever the repository is checked out."""
    import subprocess as sp
    root = Path(__file__).resolve().parents[2]
    out = sp.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", "regression",
                  "tests/regression"], cwd=root, capture_output=True, text=True)
    assert "test_audit_findings" in out.stdout and "test_jobs" not in out.stdout


def test_hung_worker_is_reported_with_the_rule_that_hung(env, ef):
    p, d, rs = env.project_with_drawing([ef.text("NOTE", (0, 0), "a" * 40 + "!")], rules=ruleset(HANG_RULE))
    r = env.repo.create_run(p["id"], d, rs, None)
    m = M.JobManager(env.storage.data_dir, run_timeout=3600, stall_timeout=2.0)
    try:
        m.start()
        _wait(lambda: _status(env, r["id"])["status"] == "failed", timeout=30)
        run = _status(env, r["id"])
        assert run["error_code"] == "WORKER_STALLED"
        assert "執行規則：HANG" in run["error_message"] and "regex" in run["error_message"]
    finally:
        m.stop()


def test_exclusive_create_run_is_atomic_under_concurrency(env, ef):
    """Eight simultaneous requests for one drawing: exactly one run is created."""
    import threading
    from persistence.repo import Repo, RunInProgress
    p, d, rs = env.project_with_drawing([ef.line("SCADA", (0, 0), (1, 0))])
    results = []

    def go():
        repo = Repo(DB.connect(env.storage.db_path))
        try:
            results.append(repo.create_run(p["id"], d, rs, None, exclusive=True)["id"])
        except RunInProgress:
            results.append("busy")
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(r == "busy" for r in results) == [False] + [True] * 7, results


def test_heartbeat_thread_touches_its_file_and_stops(tmp_path):
    import threading
    from jobs import worker as W
    path = tmp_path / "logs" / "run_0123456789abcdef.hb"
    stop = threading.Event()
    t = W.start_heartbeat(path, stop)
    _wait(lambda: path.exists())
    first = path.stat().st_mtime
    stop.set()
    t.join(5)
    assert not t.is_alive() and first > 0


def test_manager_counts_the_heartbeat_file_as_a_sign_of_life(env, ef):
    """A worker silent in the database but still beating (long write) is not called stalled."""
    from datetime import datetime, timedelta, timezone
    p, d, rs = env.project_with_drawing([ef.line("SCADA", (0, 0), (1, 0))])
    r = env.repo.create_run(p["id"], d, rs, None)
    env.repo.claim_run(r["id"], os.getpid(), None)
    old = (datetime.now(timezone.utc) - timedelta(seconds=500)).isoformat()
    env.conn.execute("UPDATE runs SET heartbeat_at = ? WHERE id = ?", (old, r["id"]))
    m = M.JobManager(env.storage.data_dir, stall_timeout=60)
    try:
        tr = M.Tracked(r["id"], os.getpid(), proc=__import__("psutil").Process(os.getpid()))
        assert m._stalled(r["id"], tr) is True                      # silent everywhere
        (m.logs_dir / f"{r['id']}.hb").touch()
        assert m._stalled(r["id"], tr) is False                     # the side thread is still beating
    finally:
        m.stop()
