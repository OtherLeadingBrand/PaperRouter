#!/usr/bin/env python3
"""
Process harness for the LOC Newspaper Downloader.

Wraps downloader.py to:
  - Monitor memory and CPU usage
  - Kill the process tree if memory exceeds a threshold
  - Kill the process tree if it runs too long (timeout)
  - Write a PID file so the process can be killed externally at any time

Usage:
  python harness.py [downloader args...]

Kill a running harness from another terminal:
  python harness.py --kill

Limits (override via env vars):
  HARNESS_MEM_MB   - max RSS memory in MB (default: 8000)
  HARNESS_TIMEOUT  - max runtime in minutes (default: 120)
"""

import os
import sys
import time
import signal
import subprocess
import logging
import psutil
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PIDFILE = Path(__file__).parent / ".harness.pid"
LOGFILE = Path(__file__).parent / "harness.log"
TIMEOUT_MIN   = int(os.environ.get("HARNESS_TIMEOUT", 120))
POLL_INTERVAL = 10  # seconds between checks

def _default_mem_limit_mb() -> int:
    """
    Use 75% of currently-available RAM as the default memory ceiling.
    This scales automatically: ~39 GB on a 64 GB machine, ~3 GB on a 4 GB machine.
    Override with the HARNESS_MEM_MB environment variable.
    """
    if "HARNESS_MEM_MB" in os.environ:
        return int(os.environ["HARNESS_MEM_MB"])
    available_mb = psutil.virtual_memory().available / 1024 / 1024
    limit = int(available_mb * 0.75)
    return limit

MEM_LIMIT_MB = _default_mem_limit_mb()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [HARNESS] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOGFILE),
    ],
)
log = logging.getLogger("harness")


# ---------------------------------------------------------------------------
# Process tree helpers
# ---------------------------------------------------------------------------
def kill_tree(pid: int, sig=signal.SIGTERM):
    """Kill a process and all its descendants."""
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        procs = children + [parent]
        for p in procs:
            try:
                p.kill()          # SIGKILL on Windows; SIGTERM ignored by torch workers
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(procs, timeout=5)
        log.info(f"Killed process tree rooted at PID {pid} ({len(procs)} processes).")
    except psutil.NoSuchProcess:
        log.warning(f"PID {pid} already gone.")


def write_pid(pid: int):
    PIDFILE.write_text(str(pid))


def clear_pid():
    try:
        PIDFILE.unlink()
    except FileNotFoundError:
        pass


def read_pid() -> int | None:
    try:
        return int(PIDFILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


# ---------------------------------------------------------------------------
# --kill mode: kill a running harness from another terminal
# ---------------------------------------------------------------------------
def do_kill():
    pid = read_pid()
    if pid is None:
        print("No harness PID file found — nothing to kill.")
        return
    print(f"Killing process tree for PID {pid}...")
    kill_tree(pid)
    clear_pid()
    print("Done.")


# ---------------------------------------------------------------------------
# Monitor loop
# ---------------------------------------------------------------------------
def monitor(proc: subprocess.Popen) -> bool:
    """
    Watch proc until it exits, memory limit, or timeout.
    Returns True if proc exited cleanly (returncode == 0).
    """
    start = time.monotonic()
    try:
        ps = psutil.Process(proc.pid)
    except psutil.NoSuchProcess:
        return False

    while proc.poll() is None:
        elapsed_min = (time.monotonic() - start) / 60

        # Collect stats across the whole process tree
        try:
            procs = [ps] + ps.children(recursive=True)
            mem_mb  = sum(p.memory_info().rss for p in procs if p.is_running()) / 1024 / 1024
            cpu_pct = sum(p.cpu_percent(interval=0) for p in procs if p.is_running())
        except psutil.NoSuchProcess:
            break

        log.info(
            f"Memory={mem_mb:.0f}MB  CPU={cpu_pct:.0f}%  "
            f"Elapsed={elapsed_min:.1f}min  "
            f"Limit={MEM_LIMIT_MB}MB/{TIMEOUT_MIN}min"
        )

        if mem_mb > MEM_LIMIT_MB:
            log.warning(f"Memory limit exceeded ({mem_mb:.0f}MB > {MEM_LIMIT_MB}MB). Killing.")
            kill_tree(proc.pid)
            return False

        if elapsed_min > TIMEOUT_MIN:
            log.warning(f"Timeout exceeded ({elapsed_min:.1f}min > {TIMEOUT_MIN}min). Killing.")
            kill_tree(proc.pid)
            return False

        time.sleep(POLL_INTERVAL)

    return proc.returncode == 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if "--kill" in sys.argv:
        do_kill()
        return

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = [sys.executable, "downloader.py"] + sys.argv[1:]
    log.info(f"Starting: {' '.join(cmd)}")
    log.info(f"Limits: memory={MEM_LIMIT_MB}MB  timeout={TIMEOUT_MIN}min")

    # CREATE_NEW_PROCESS_GROUP keeps the child in its own group so Ctrl-C
    # in the parent terminal doesn't immediately kill it (we want the harness
    # to do that cleanly).
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(cmd, creationflags=flags)
    write_pid(proc.pid)
    log.info(f"Child PID: {proc.pid}  (kill anytime: python harness.py --kill)")

    try:
        success = monitor(proc)
    except KeyboardInterrupt:
        log.info("Interrupted — killing child process tree.")
        kill_tree(proc.pid)
        success = False
    finally:
        clear_pid()

    if success:
        log.info("Process completed successfully.")
    else:
        log.error("Process failed or was terminated.")
        sys.exit(1)


if __name__ == "__main__":
    main()
