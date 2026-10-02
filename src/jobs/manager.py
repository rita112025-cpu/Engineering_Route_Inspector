"""Background job manager: schedules worker processes for queued runs,
enforces cancellation and recovers runs whose worker died.

Crash recovery covers three cases:
  * the server restarts while a run is 'running': if its worker process
    (same PID *and* creation time) is still alive it is adopted and
    watched, otherwise the run is marked failed (WORKER_LOST);
  * a worker exits without recording a final status (killed, crashed,
    out of memory): the run is marked failed (WORKER_CRASHED) with the
    tail of its log;
  * runs still 'queued' are simply started again.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from core.analysis.pipeline import STAGES
from persistence.db import connect
from persistence.repo import Repo

SRC_DIR = Path(__file__).resolve().parents[1]
CANCEL_GRACE_SECONDS = 5.0
POLL_SECONDS = 0.25
DEFAULT_RUN_TIMEOUT = 1800.0   # seconds; last line of defence against a run that never ends
DEFAULT_STALL_TIMEOUT = 60.0   # seconds without a heartbeat from a running worker (it writes one every ~0.3 s)
LOG_RETENTION_DAYS = 30


@dataclass
class Tracked:
    run_id: str
    pid: int
    popen: subprocess.Popen | None = None      # started by this manager
    proc: object | None = None                 # psutil.Process for adopted workers
    log_path: Path | None = None
    started: float = field(default_factory=time.monotonic)
    cancel_seen: float | None = None

    def exit_code(self) -> int | None:
        """None while running, otherwise the exit code (-1 if unknown)."""
        if self.popen is not None:
            return self.popen.poll()
        try:
            if self.proc is not None and self.proc.is_running() and self.proc.status() != "zombie":
                return None
        except Exception:  # noqa: BLE001 - psutil.NoSuchProcess etc.
            pass
        return -1

    def kill(self) -> None:
        try:
            if self.popen is not None:
                self.popen.kill()
                self.popen.wait(timeout=10)
            elif self.proc is not None:
                self.proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _log_tail(path: Path | None, limit: int = 3000) -> str:
    if not path or not path.exists():
        return ""
    data = path.read_bytes()[-limit:]
    return data.decode("utf-8", errors="replace")


def same_process(pid: int | None, create_time: float | None):
    """psutil.Process for pid if it is alive and was created at create_time."""
    if not pid:
        return None
    try:
        import psutil
        p = psutil.Process(pid)
        if create_time is not None and abs(p.create_time() - create_time) > 1.0:
            return None          # PID reused by another program
        if p.status() == psutil.STATUS_ZOMBIE:
            return None
        return p
    except Exception:  # noqa: BLE001
        return None


class JobManager:
    def __init__(self, data_dir: str | Path, max_workers: int = 1, run_timeout: float | None = None,
                 stall_timeout: float | None = None):
        self.events: list[dict] = []        # recent manager events (diagnostics)
        self.run_timeout = self._setting(run_timeout, "ERI_RUN_TIMEOUT", DEFAULT_RUN_TIMEOUT)
        self.stall_timeout = self._setting(stall_timeout, "ERI_STALL_TIMEOUT", DEFAULT_STALL_TIMEOUT)
        self.data_dir = Path(data_dir).resolve()
        self.db_path = self.data_dir / "eri.sqlite3"
        self.logs_dir = self.data_dir / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.max_workers = max(1, int(max_workers))
        self.tracked: dict[str, Tracked] = {}
        self.lock = threading.Lock()
        self.wake_event = threading.Event()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.conn = connect(self.db_path)
        self.repo = Repo(self.conn)

    # -- lifecycle -----------------------------------------------------------
    def prune_logs(self, days: int = LOG_RETENTION_DAYS) -> int:
        """Delete worker logs of finished runs older than ``days``."""
        cutoff = time.time() - days * 86400
        active = {r["id"] for r in self.repo.active_runs()}
        n = 0
        for f in self.logs_dir.glob("run_*.log"):
            try:
                if f.stem not in active and f.stat().st_mtime < cutoff:
                    f.unlink()
                    n += 1
            except OSError:
                pass
        return n

    def start(self) -> dict:
        self.prune_logs()
        report = self.recover()
        self.thread = threading.Thread(target=self._loop, name="eri-job-manager", daemon=True)
        self.thread.start()
        return report

    def stop(self, timeout: float = 10.0) -> None:
        """Stop scheduling; running workers are stopped and their runs marked interrupted."""
        self.stop_event.set()
        self.wake_event.set()
        if self.thread is not None:
            self.thread.join(timeout)
        with self.lock:
            for t in list(self.tracked.values()):
                t.kill()
                self.repo.finish_run(t.run_id, "failed", error_code="INTERRUPTED",
                                     error_message="程式關閉時分析被中斷，請重新執行分析。")
            self.tracked.clear()
        self.conn.close()

    def wake(self) -> None:
        self.wake_event.set()

    def _setting(self, explicit, env: str, default: float) -> float:
        raw = explicit if explicit is not None else os.environ.get(env)
        if raw is None:
            return default
        try:
            value = float(raw)
            if value > 0 and value != float("inf"):
                return value
        except (TypeError, ValueError):
            pass
        self._event("bad_setting", "", f"{env}={raw!r} ignored, using {default:g}")
        return default

    def _event(self, kind: str, run_id: str, detail: str = "") -> None:
        self.events.append({"ts": time.time(), "kind": kind, "run_id": run_id, "detail": detail})
        del self.events[:-100]

    # -- recovery --------------------------------------------------------------
    def recover(self) -> dict:
        lost, adopted = [], []
        for run in self.repo.active_runs():
            if run["status"] != "running":
                continue
            # without a recorded creation time a PID cannot be told apart from a reused one: do not adopt
            ct = run["worker_create_time"]
            proc = same_process(run["worker_pid"], ct) if ct is not None else None
            if proc is not None:
                with self.lock:
                    self.tracked[run["id"]] = Tracked(run["id"], run["worker_pid"], proc=proc,
                                                      log_path=self.logs_dir / f"{run['id']}.log")
                adopted.append(run["id"])
                self._event("adopted", run["id"])
            else:
                if self.repo.finish_run(run["id"], "failed", error_code="WORKER_LOST",
                                        error_message="上次執行時分析程序意外中止（程式或電腦可能曾關閉），請重新執行分析。"):
                    lost.append(run["id"])
                    self._event("recovered_failed", run["id"])
        queued = [r["id"] for r in self.repo.active_runs() if r["status"] == "queued"]
        return {"marked_failed": lost, "adopted": adopted, "requeued": queued}

    # -- main loop -----------------------------------------------------------
    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - keep the scheduler alive
                self._event("error", "", repr(exc))
            self.wake_event.wait(POLL_SECONDS)
            self.wake_event.clear()

    def tick(self) -> None:
        with self.lock:
            self._reap()
            self._enforce_cancel()
            self._schedule()

    def _reap(self) -> None:
        for rid, t in list(self.tracked.items()):
            code = t.exit_code()
            if code is None:
                continue
            del self.tracked[rid]
            run = self.repo.get_run(rid)
            if run is None or run["status"] not in ("queued", "running"):
                self._event("exited", rid, f"code={code}")
                continue
            if t.cancel_seen is not None or run["cancel_requested"]:
                self.repo.finish_run(rid, "cancelled", error_code="CANCELLED", error_message="使用者取消")
                self._event("cancelled", rid, f"code={code}")
                continue
            code_name = "WORKER_START_FAILED" if run["status"] == "queued" else "WORKER_CRASHED"
            self.repo.finish_run(
                rid, "failed", error_code=code_name,
                error_message=f"分析程序意外結束（結束碼 {code}），沒有產生結果。請重新執行；若持續發生請查看診斷頁面。",
                error_detail=_log_tail(t.log_path))
            self._event("crashed", rid, f"code={code}")

    def _enforce_cancel(self) -> None:
        now = time.monotonic()
        for rid, t in list(self.tracked.items()):
            if t.cancel_seen is None:
                if self.repo.cancel_requested(rid):
                    t.cancel_seen = now
                elif self._stalled(rid, t):
                    t.kill()
                    self.repo.finish_run(
                        rid, "failed", error_code="WORKER_STALLED",
                        error_message=self._stall_message(rid),
                        error_detail=_log_tail(t.log_path))
                    self._event("stalled", rid)
                elif now - t.started > self.run_timeout and t.exit_code() is None:
                    t.kill()
                    self.repo.finish_run(
                        rid, "failed", error_code="RUN_TIMEOUT",
                        error_message=f"分析超過時間上限（{self.run_timeout:.0f} 秒）而被停止。"
                                      "可能是規則或圖面過於複雜，請簡化規則後重試。",
                        error_detail=_log_tail(t.log_path))
                    self._event("timeout", rid)
                continue
            if now - t.cancel_seen > CANCEL_GRACE_SECONDS and t.exit_code() is None:
                t.kill()
                self.repo.finish_run(rid, "cancelled", error_code="CANCELLED",
                                     error_message="使用者取消（分析程序未及時回應，已強制停止）")
                self._event("killed", rid)

    def _stall_message(self, rid: str) -> str:
        run = self.repo.get_run(rid) or {}
        label = dict(STAGES).get(run.get("stage"), run.get("stage") or "")
        note = f"：{run['stage_note']}" if run.get("stage_note") else ""
        hint = ("這通常是這條規則的比對規則（regex）太複雜，請簡化它。" if run.get("stage") == "rules"
                else "請重新執行；若持續發生，請在診斷頁面匯出診斷資料。")
        return f"分析在「{label}{note}」超過 {self.stall_timeout:.0f} 秒沒有回應，已停止。{hint}"

    def _stalled(self, rid: str, t: Tracked) -> bool:
        """A running worker that stopped writing heartbeats (hung inside one step) and is still alive."""
        if t.exit_code() is not None:
            return False
        run = self.repo.get_run(rid)
        if not run or run["status"] != "running" or not run.get("heartbeat_at"):
            return False
        try:
            beat = datetime.fromisoformat(run["heartbeat_at"])
        except ValueError:
            return False
        return (datetime.now(timezone.utc) - beat).total_seconds() > self.stall_timeout

    def _schedule(self) -> None:
        if self.stop_event.is_set():
            return
        free = self.max_workers - len(self.tracked)
        if free <= 0:
            return
        for run in self.repo.active_runs():
            if free <= 0:
                break
            if run["status"] != "queued" or run["id"] in self.tracked:
                continue
            self._spawn(run["id"])
            free -= 1

    def _spawn(self, run_id: str) -> None:
        log_path = self.logs_dir / f"{run_id}.log"
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC_DIR) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["PYTHONIOENCODING"] = "utf-8"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            with open(log_path, "ab") as log:
                popen = subprocess.Popen(
                    [sys.executable, "-m", "jobs.worker", "--data-dir", str(self.data_dir), "--run-id", run_id],
                    stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, cwd=str(SRC_DIR),
                    creationflags=flags)
        except (OSError, ValueError) as exc:       # missing interpreter, no handles/memory left, ...
            self.repo.finish_run(run_id, "failed", error_code="WORKER_START_FAILED",
                                 error_message="無法啟動分析程序，請查看診斷頁面或重新啟動程式。",
                                 error_detail=f"{type(exc).__name__}: {exc}")
            self._event("spawn_failed", run_id, repr(exc))
            return
        self.tracked[run_id] = Tracked(run_id, popen.pid, popen=popen, log_path=log_path)
        self._event("spawned", run_id, f"pid={popen.pid}")

    # -- status ----------------------------------------------------------------
    def status(self) -> dict:
        with self.lock:
            running = [{"run_id": t.run_id, "pid": t.pid, "seconds": round(time.monotonic() - t.started, 1),
                        "adopted": t.popen is None} for t in self.tracked.values()]
        return {"alive": bool(self.thread and self.thread.is_alive()), "max_workers": self.max_workers,
                "running": running, "recent_events": list(self.events[-20:])}

    def wait_idle(self, timeout: float = 60.0) -> bool:
        """Block until no run is queued or running (tests and CLI)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.wake()
            with self.lock:   # the manager connection is shared with the scheduler thread
                busy = bool(self.tracked) or bool(self.repo.active_runs())
            if not busy:
                return True
            time.sleep(0.05)
        return False
