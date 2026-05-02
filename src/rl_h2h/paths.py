"""Filesystem layout for the app.

Source code lives at ``<repo>/src/rl_h2h/``; runtime data and logs are written to
``<repo>/data/`` and ``<repo>/logs/`` respectively. Folders are created on
import so every other module can write without a guarded ``mkdir``.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
LOG_DIR = ROOT_DIR / "logs"
ASSETS_DIR = ROOT_DIR / "assets"

for _d in (DATA_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = DATA_DIR / "config.json"
MATCHES_PATH = DATA_DIR / "matches.jsonl"
PLAYERS_PATH = DATA_DIR / "players.json"
MMR_CACHE_PATH = DATA_DIR / "mmr_cache.json"
MMR_HISTORY_PATH = DATA_DIR / "mmr_history.jsonl"

MMR_LOG_PATH = LOG_DIR / "mmr.log"
MY_MMR_LOG_PATH = LOG_DIR / "my_mmr.log"
API_DUMP_PATH = LOG_DIR / "api_dump.log"


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
