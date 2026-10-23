"""Engine entry point for the desktop app (the Tauri shell starts this as a sidecar).

    engine --port 8765 --parent-pid 1234

Binds to 127.0.0.1 only. Exits when the parent process (the desktop window) is gone, so no engine
is ever left running. Logs to <workspace>/logs/engine.log because a packaged engine has no console.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        SYNCHRONIZE, WAIT_TIMEOUT = 0x00100000, 0x102
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _watch_parent(pid: int) -> None:
    while _alive(pid):
        time.sleep(2)
    os._exit(0)


def _log_to_file() -> Path:
    home = Path(os.environ.get("ENTITY_MAPPER_HOME", Path.home() / "EntityMapper"))
    log = home / "logs" / "engine.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.exists() and log.stat().st_size > 2_000_000:  # keep one previous log
        log.replace(log.with_suffix(".log.1"))
    stream = open(log, "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = stream
    return log


def main() -> None:
    import multiprocessing
    multiprocessing.freeze_support()  # a packaged app must not relaunch itself for worker processes
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("ENTITY_MAPPER_PORT", "8765")))
    ap.add_argument("--parent-pid", type=int, default=0)
    a = ap.parse_args()

    if getattr(sys, "frozen", False) or sys.stdout is None:
        _log_to_file()
    if a.parent_pid:
        threading.Thread(target=_watch_parent, args=(a.parent_pid,), daemon=True).start()

    import uvicorn
    try:
        from api.server import app
    except Exception as e:  # most likely: the workspace is open in another copy of the app
        print(f"Engine could not start: {e}", flush=True)
        raise SystemExit(3)
    print(f"Engine starting on 127.0.0.1:{a.port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="info", log_config=None)


if __name__ == "__main__":
    main()
