"""File-cap-aware logging used by the MMR client and the API dump."""
from __future__ import annotations

import json
import sys
import threading
from datetime import datetime
from pathlib import Path

from .paths import API_DUMP_PATH, MMR_LOG_PATH, now_iso


# Cap log size at ~256 KB so it doesn't grow forever in long-running sessions.
# We rotate by truncating once we cross the cap — last write wins, no archive.
MMR_LOG_CAP = 256 * 1024
API_DUMP_CAP_BYTES = 2 * 1024 * 1024  # 2 MB; truncate-rotate when exceeded

_mmr_log_lock = threading.Lock()
_api_dump_lock = threading.Lock()


def _append_capped(path: Path, line: str, cap_bytes: int, lock: threading.Lock) -> None:
    """Append `line + \\n` under `lock`, truncate-rotating once `path` exceeds
    `cap_bytes`. Swallows OSError — a logging failure must never crash the
    caller. Reused by `mmr_log` and `api_dump`."""
    try:
        with lock:
            try:
                if path.stat().st_size > cap_bytes:
                    path.write_text("", encoding="utf-8")
            except FileNotFoundError:
                pass
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass


def mmr_log(msg: str) -> None:
    """Append a timestamped line to mmr.log AND mirror to stderr.

    Designed to work even under start.bat's `pythonw` launch (which discards
    stdout/stderr) — the log file is the sole source of truth for debugging."""
    line = f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}"
    print(f"[mmr] {msg}", file=sys.stderr)
    _append_capped(MMR_LOG_PATH, line, MMR_LOG_CAP, _mmr_log_lock)


def api_dump(event: str, data: dict) -> None:
    """Append one envelope as a JSON line to api_dump.log."""
    try:
        line = json.dumps({"ts": now_iso(), "Event": event, "Data": data})
    except (TypeError, ValueError):
        return
    _append_capped(API_DUMP_PATH, line, API_DUMP_CAP_BYTES, _api_dump_lock)
