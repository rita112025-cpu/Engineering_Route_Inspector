"""Analysis worker process: ``python -m jobs.worker --data-dir D --run-id R``.

Each run executes in its own process so that cancelling or a crash in
the analysis never takes the UI server down, and so a hung analysis can
be stopped by terminating the process.

Exit codes: 0 completed / cancelled / nothing to do, 1 analysis failed,
2 bad arguments or database unavailable.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import traceback
from pathlib import Path

if __package__ in (None, ""):  # allow running the file directly
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.rules.engine import Cancelled  # noqa: E402
from persistence.db import connect  # noqa: E402
from persistence.repo import Repo  # noqa: E402
from persistence.storage import check_id  # noqa: E402
from jobs.analysis import RunGone, execute_run  # noqa: E402


def process_create_time(pid: int) -> float | None:
    try:
        import psutil
        return psutil.Process(pid).create_time()
    except Exception:  # noqa: BLE001 - psutil missing or process gone
        return None


HEARTBEAT_SECONDS = 2.0


def heartbeat_path(data_dir: Path, run_id: str) -> Path:
    return data_dir / "logs" / f"{run_id}.hb"


def start_heartbeat(path: Path, stop: threading.Event) -> threading.Thread:
    """Touch ``path`` every couple of seconds from a side thread.

    A file (not the database) so it keeps beating while one long database write holds the lock, yet it
    stops when the main thread is stuck inside a C call that never releases the GIL (a runaway regex):
    the job manager treats a silent heartbeat as a stall.
    """
    def beat():
        while not stop.is_set():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            except OSError:
                pass
            stop.wait(HEARTBEAT_SECONDS)
    t = threading.Thread(target=beat, name="eri-heartbeat", daemon=True)
    t.start()
    return t


def run_worker(db_path: Path, run_id: str) -> int:
    conn = connect(db_path)
    repo = Repo(conn)
    stop = threading.Event()
    try:
        if not repo.claim_run(run_id, os.getpid(), process_create_time(os.getpid())):
            print(f"run {run_id}: not claimable (already started, cancelled or deleted)", flush=True)
            return 0
        print(f"run {run_id}: started pid={os.getpid()}", flush=True)
        start_heartbeat(heartbeat_path(db_path.parent, run_id), stop)
        try:
            summary = execute_run(repo, run_id)
        except Cancelled:
            repo.finish_run(run_id, "cancelled", error_code="CANCELLED", error_message="使用者取消")
            print(f"run {run_id}: cancelled", flush=True)
            return 0
        except RunGone:
            print(f"run {run_id}: run or drawing deleted", flush=True)
            return 0
        except MemoryError:
            repo.finish_run(run_id, "failed", error_code="OUT_OF_MEMORY",
                            error_message="記憶體不足，無法完成分析。請關閉其他程式或縮小圖面範圍後重試。",
                            error_detail=traceback.format_exc()[-4000:])
            return 1
        except Exception as exc:  # noqa: BLE001 - recorded for the user and diagnostics
            repo.finish_run(run_id, "failed", error_code="ANALYSIS_ERROR",
                            error_message=f"分析過程發生錯誤：{type(exc).__name__}: {exc}"[:500],
                            error_detail=traceback.format_exc()[-4000:])
            traceback.print_exc()
            return 1
        print(f"run {run_id}: completed {summary['counts']}", flush=True)
        return 0
    finally:
        stop.set()
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Engineering Route Inspector analysis worker")
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--run-id", required=True)
    args = ap.parse_args(argv)
    try:
        check_id(args.run_id)
    except ValueError:
        print("invalid run id", file=sys.stderr)
        return 2
    db_path = Path(args.data_dir) / "eri.sqlite3"
    if not db_path.is_file():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 2
    return run_worker(db_path, args.run_id)


if __name__ == "__main__":
    sys.exit(main())
