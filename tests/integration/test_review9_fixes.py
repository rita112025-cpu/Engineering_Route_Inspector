"""Review of 7dd73bb, findings D-3 (heartbeat files left behind) and D-5 (no-progress message). Each failed before its fix."""
from __future__ import annotations

import os
import time

import pytest

from jobs import manager as M


class FakeWorker(M.Tracked):
    """A worker that is 'alive' until killed. Never signals a real process (the test process itself must survive)."""
    killed = False

    def exit_code(self):
        return None

    def kill(self):
        self.killed = True


# -- D-3 / D-5 -----------------------------------------------------------------------------------------

@pytest.fixture
def running(env, ef):
    p, d, rs = env.project_with_drawing([ef.line("SCADA", (0, 0), (1, 0))])
    r = env.repo.create_run(p["id"], d, rs, None)
    env.repo.claim_run(r["id"], os.getpid(), None)
    return r


def test_d3_stopping_the_manager_removes_heartbeat_files(env, running):
    m = M.JobManager(env.storage.data_dir)
    hb = m.logs_dir / f"{running['id']}.hb"
    hb.touch()
    worker = FakeWorker(running["id"], 1, log_path=m.logs_dir / f"{running['id']}.log")
    m.tracked[running["id"]] = worker
    m.stop()
    assert worker.killed and not hb.exists()


def test_d3_killing_a_run_removes_its_heartbeat_file(env, running):
    from datetime import datetime, timedelta, timezone
    env.conn.execute("UPDATE runs SET heartbeat_at = ? WHERE id = ?",
                     ((datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat(), running["id"]))
    m = M.JobManager(env.storage.data_dir, stall_timeout=600, progress_timeout=30)
    hb = m.logs_dir / f"{running['id']}.hb"
    hb.touch()
    tr = FakeWorker(running["id"], 1, log_path=m.logs_dir / f"{running['id']}.log")
    m.tracked[running["id"]] = tr
    try:
        m._enforce_cancel()
        assert tr.killed
        run = env.repo.get_run(running["id"])
        assert run["error_code"] == "WORKER_NO_PROGRESS"
        assert not hb.exists()
    finally:
        m.tracked.clear()
        m.stop()


def test_d3_prune_logs_removes_old_heartbeat_files_of_finished_runs_only(env, running):
    m = M.JobManager(env.storage.data_dir)
    try:
        old_done = m.logs_dir / "run_00000000000000aa.hb"
        old_active = m.logs_dir / f"{running['id']}.hb"
        recent = m.logs_dir / "run_00000000000000bb.hb"
        for f in (old_done, old_active, recent):
            f.touch()
        long_ago = time.time() - 40 * 86400
        for f in (old_done, old_active):
            os.utime(f, (long_ago, long_ago))
        m.prune_logs()
        assert not old_done.exists() and old_active.exists() and recent.exists()
    finally:
        m.stop()


def test_d5_no_progress_message_names_the_stage_and_position(env, running):
    from datetime import datetime, timedelta, timezone
    env.conn.execute("UPDATE runs SET heartbeat_at = ?, stage = 'rules', stage_note = ? WHERE id = ?",
                     ((datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat(), "CLEARANCE（第 1/3 條）", running["id"]))
    m = M.JobManager(env.storage.data_dir, stall_timeout=600, progress_timeout=30)
    m.tracked[running["id"]] = FakeWorker(running["id"], 1, log_path=m.logs_dir / f"{running['id']}.log")
    try:
        m._enforce_cancel()
        msg = env.repo.get_run(running["id"])["error_message"]
        assert "第 1/3 條" in msg and "沒有任何進度" in msg and "30 秒" in msg, msg   # position, not the rule id (U8-1 c)
    finally:
        m.tracked.clear()
        m.stop()
