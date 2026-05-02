#!/usr/bin/env python3
"""Rocket League head-to-head overlay.

Connects to the Rocket League Stats API local WebSocket, tracks per-player
W/L history across matches, and shows a transparent always-on-top overlay
while the configured hotkey is held.
"""
from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import os
import queue
import sys
import threading
import time
from collections import deque
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QFont, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QMessageBox, QSystemTrayIcon, QVBoxLayout, QWidget
from pynput import keyboard


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
MATCHES_PATH = APP_DIR / "matches.jsonl"
PLAYERS_PATH = APP_DIR / "players.json"
MMR_CACHE_PATH = APP_DIR / "mmr_cache.json"
MMR_LOG_PATH = APP_DIR / "mmr.log"
MY_MMR_LOG_PATH = APP_DIR / "my_mmr.log"
MMR_HISTORY_PATH = APP_DIR / "mmr_history.jsonl"
API_DUMP_PATH = APP_DIR / "api_dump.log"
API_DUMP_CAP_BYTES = 2 * 1024 * 1024  # 2 MB; truncate-rotate when exceeded


EVT_MATCH_CREATED = "MatchCreated"
EVT_MATCH_INITIALIZED = "MatchInitialized"
EVT_ROUND_STARTED = "RoundStarted"
EVT_UPDATE_STATE = "UpdateState"
EVT_MATCH_ENDED = "MatchEnded"
EVT_MATCH_DESTROYED = "MatchDestroyed"
EVT_REPLAY_CREATED = "ReplayCreated"
EVT_GOAL_SCORED = "GoalScored"
EVT_BALL_HIT = "BallHit"
EVT_CROSSBAR_HIT = "CrossbarHit"
EVT_STATFEED = "StatfeedEvent"

# StatfeedEvent.EventName values we explicitly track.
SF_SAVE = "Save"
SF_SHOT = "Shot"
SF_DEMOLISH = "Demolish"

BUCKET_VS = "vs"
BUCKET_WITH = "with"

VALID_POSITIONS = ("top-left", "top-center", "top-right", "bottom-left", "bottom-right")
SPECTATOR_FIELDS = ("Boost", "bBoosting", "bOnGround", "bOnWall", "bSupersonic")

DEFAULT_TEAM_COLORS = {0: "#3B9EFF", 1: "#FF7A29"}


# Map RL arena asset names to human-friendly labels. Variants (Night/Day/Stormy/...) are
# composed at runtime: e.g. "TrainStation_Night_P" -> "Urban Central (Night)".
ARENA_BASE = {
    "stadium":              "DFH Stadium",
    "park":                 "Mannfield",
    "trainstation":         "Urban Central",
    "haunted_trainstation": "Urban Central (Haunted)",
    "underwater":           "AquaDome",
    "wasteland":            "Wasteland",
    "neotokyo":             "Neo Tokyo",
    "neotokyo_standard":    "Neo Tokyo",
    "eurostadium":          "Champions Field",
    "beach":                "Salty Shores",
    "beachvolley":          "Salty Shores",
    "chinastadium":         "Forbidden Temple",
    "cosmic":               "Starbase ARC",
    "arc_standard":         "Starbase ARC",
    "throwback_stadium":    "Throwback Stadium",
    "hoops_dunkhouse":      "DunkHouse",
    "music":                "Estadio Vida",
    "estadio_vida":         "Estadio Vida",
    "farm":                 "Farmstead",
    "outlaw_oasis":         "Deadeye Canyon",
    "shattershot":          "Champions Field (Snow Day)",
    "labs_octagon":         "Octagon",
    "labs_pillars":         "Pillars",
    "labs_cosmic":          "Cosmic",
    "labs_double_goal":     "Double Goal",
    "labs_underpass":       "Underpass",
    "labs_utopia":          "Utopia Retro",
    "neoasphalt":           "Neon Fields",
}
ARENA_VARIANT = {
    "night":   "Night",
    "day":     "Day",
    "rainy":   "Stormy",
    "stormy":  "Stormy",
    "race_day": "Stormy",
    "snowy":   "Snowy",
    "snowfall": "Snowy",
    "dawn":    "Dawn",
    "spring":  "Spring",
    "spooky":  "Spooky",
    "circuit": "Circuit",
    "p":       "",  # bare _P leftover
}


def pretty_arena(asset: str) -> str:
    if not asset:
        return ""
    base = asset.lower()
    if base.endswith("_p"):
        base = base[:-2]
    if base in ARENA_BASE:
        return ARENA_BASE[base]
    parts = base.split("_")
    for i in range(len(parts), 0, -1):
        candidate = "_".join(parts[:i])
        if candidate in ARENA_BASE:
            variant_key = "_".join(parts[i:])
            if not variant_key:
                return ARENA_BASE[candidate]
            label = ARENA_VARIANT.get(variant_key, variant_key.replace("_", " ").title())
            return f"{ARENA_BASE[candidate]} ({label})" if label else ARENA_BASE[candidate]
    return base.replace("_", " ").title()


# Gamepad button name → (inputs.event_type, inputs.event_code, target_value).
# 'thresh' for analog triggers means "treat ≥ THRESHOLD as pressed".
GAMEPAD_BUTTONS = {
    "a":           ("Key", "BTN_SOUTH",  1),    # Xbox A / PS Cross
    "b":           ("Key", "BTN_EAST",   1),    # Xbox B / PS Circle
    "x":           ("Key", "BTN_WEST",   1),    # Xbox X / PS Square
    "y":           ("Key", "BTN_NORTH",  1),    # Xbox Y / PS Triangle
    "lb":          ("Key", "BTN_TL",     1),    # Xbox LB / PS L1
    "rb":          ("Key", "BTN_TR",     1),    # Xbox RB / PS R1
    "back":        ("Key", "BTN_SELECT", 1),    # Xbox Back/View / PS Share
    "start":       ("Key", "BTN_START",  1),    # Xbox Start/Menu / PS Options
    "lstick":      ("Key", "BTN_THUMBL", 1),    # left stick click
    "rstick":      ("Key", "BTN_THUMBR", 1),    # right stick click
    "dpad_up":     ("Absolute", "ABS_HAT0Y", -1),
    "dpad_down":   ("Absolute", "ABS_HAT0Y",  1),
    "dpad_left":   ("Absolute", "ABS_HAT0X", -1),
    "dpad_right":  ("Absolute", "ABS_HAT0X",  1),
    "lt":          ("Absolute", "ABS_Z",   "thresh"),  # Xbox LT / PS L2
    "rt":          ("Absolute", "ABS_RZ",  "thresh"),  # Xbox RT / PS R2
}
GAMEPAD_TRIGGER_THRESHOLD = 80  # 0..255


DEFAULT_CONFIG = {
    "_comments": [
        "hotkeys:         held to show the head-to-head overlay (per-opponent W/L).",
        "session_hotkeys: held to show the current-session stats overlay.",
        "Keyboard names:  'tab', 'f1'..'f12', 'caps_lock', 'shift', 'esc', 'space',",
        "                 or a single character like 'h'.",
        "Gamepad (Xbox / PlayStation), prefix with 'pad_':",
        "  Buttons:  pad_a (Xbox A / PS X), pad_b (B / Circle), pad_x (X / Square), pad_y (Y / Triangle)",
        "  Bumpers:  pad_lb (LB / L1 — default RL scoreboard button on console), pad_rb (RB / R1)",
        "  Triggers: pad_lt (LT / L2), pad_rt (RT / R2)",
        "  D-pad:    pad_dpad_up, pad_dpad_down, pad_dpad_left, pad_dpad_right",
        "  Other:    pad_back (Xbox View / PS Share),",
        "            pad_start (Menu / Options), pad_lstick, pad_rstick",
        "Gamepad bindings require: pip install inputs",
        "Note: stock RL maps D-pad directions to quickchat. To avoid sending a quickchat",
        "      every time you check the overlay, prefer 'pad_lb' (default scoreboard).",
        "require_rl_focus:      only show the overlay when Rocket League has focus (Windows).",
        "show_match_summary:    flash a result + per-match stats card when a match ends.",
        "match_summary_seconds: max time the popup stays visible (auto-hides earlier when",
        "                       the next match starts or you leave to the menu).",
        "self_player_id:        auto-filled after your first 1v1 — used to hide your own W/L row.",
        "                       Set manually if you only play 2v2/3v3.",
        "recent_size:           number of recent W/L pips shown in the session card (default 5).",
        "name_max_length:       player name truncation length in the H2H card (default 16).",
        "expand_hotkeys:        press to toggle the H2H overlay between compact and expanded.",
        "                       Expanded mode appends the session stats below the H2H card.",
        "cycle_hotkeys:         press to cycle. Context-sensitive:",
        "                         - in H2H view: cycle MMR category",
        "                           (best -> 1v1 -> 2v2 -> 3v3 -> best)",
        "                         - in graph view (F12 held + graph open):",
        "                           cycle plotted playlist (1v1 -> 2v2 -> 3v3)",
        "                       Both choices persist independently.",
        "h2h_default_expanded:  initial expanded state at script launch. Re-saved on every",
        "                       toggle so your last choice persists across restarts.",
        "mmr_enabled:           when true, fetches each opponent's rank/MMR from",
        "                       rocketleague.tracker.network (one HTTP request per new",
        "                       opponent, cached for 10 minutes). Off by default — flipping it",
        "                       on sends opponents' Platform|Uid (Epic) or display names",
        "                       (other platforms) to tracker.network. Toggle via the tray menu.",
        "mmr_category:          which playlist's MMR to show: 'best' | '1v1' | '2v2' | '3v3'.",
        "                       Cycle live with the cycle_hotkeys key.",
        "session_view:          which sub-view F12 shows: 'session' (stats card) | 'graph'",
        "                       (your MMR over time). Toggle with the expand_hotkeys key",
        "                       while F12 is held.",
        "graph_playlist:        which playlist the graph plots: '1v1' | '2v2' | '3v3'.",
        "graph_match_window:    how many recent matches to plot in the graph view.",
        "graph_match_grace_seconds: tolerance when joining matches to MMR snapshots —",
        "                       Psyonix->TRN propagation can lag behind a match's end",
        "                       by a minute or two; this grace catches matches that",
        "                       ended just before a snapshot rolled.",
        "auto_update:           when true, start.bat checks GitHub for a newer version and",
        "                       updates silently before launching the app. Off by default;",
        "                       enable via the tray menu (right-click the H icon).",
        "api_debug_dump:        when true, append every received Stats API envelope to",
        "                       api_dump.log (capped at 2 MB, truncate-rotates). Diagnostic",
        "                       only — leave off in normal use. Edit this file and restart",
        "                       to toggle (no tray UI; intentionally not hot-pluggable).",
        "position: top-left | top-center | top-right | bottom-left | bottom-right",
        "colors: override any overlay color (hex strings). All keys are optional.",
        "  win:   positive accent (wins, +diffs, your YOU tag, recent W pips)",
        "  loss:  negative accent (losses, recent L pips)",
        "  dim:   secondary text (sub-rows, last-played hint)",
        "  muted: tertiary text (separators, score em-dashes)",
        "  faint: dividers and the NEW pill border",
        "  team_blue_fallback / team_orange_fallback: used only when the wire reports gray",
        "         (private/training matches don't supply real ColorPrimary values)."
    ],
    "host": "127.0.0.1",
    "port": 49123,
    "hotkeys": ["tab", "pad_lb"],
    "session_hotkeys": ["f12"],
    "expand_hotkeys": ["f11"],
    "cycle_hotkeys": ["f10"],
    "h2h_default_expanded": False,
    "mmr_enabled": False,
    "mmr_category": "best",
    "session_view": "session",
    "graph_playlist": "2v2",
    "graph_match_window": 30,
    "graph_match_grace_seconds": 120,
    "auto_update": False,
    "api_debug_dump": False,
    "require_rl_focus": True,
    "show_match_summary": True,
    "match_summary_seconds": 30,
    "self_player_id": None,
    "recent_size": 5,
    "name_max_length": 16,
    "position": "top-right",
    "margin": 24,
    "width": 380,
    "background_rgba": [16, 20, 21, 200],
    "border_radius_px": 4,
    "border_rgba": [255, 255, 255, 28],
    "text_color": "#E0E3E5",
    "font_family": "Segoe UI",
    "font_size": 11,
    "colors": {
        "win":                  "#CCFF00",
        "loss":                 "#FF6467",
        "dim":                  "#C4C9AC",
        "muted":                "#8E9379",
        "faint":                "#444933",
        "team_blue_fallback":   "#3B9EFF",
        "team_orange_fallback": "#FF7A29",
    },
}


# Cached Win32 bindings for is_rl_focused — hoisted to avoid re-loading WinDLL each poll.
if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_wchar_p, ctypes.POINTER(wintypes.DWORD)
    ]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
else:
    _user32 = _kernel32 = None  # type: ignore[assignment]


def is_rl_focused() -> bool:
    """True when Rocket League is the foreground window. Always True on non-Windows."""
    if _user32 is None or _kernel32 is None:
        return True
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h_proc = _kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h_proc:
            return False
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            if _kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                return buf.value.lower().endswith("rocketleague.exe")
        finally:
            _kernel32.CloseHandle(h_proc)
    except Exception:
        return False
    return False


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_config(cfg: dict) -> None:
    out = {k: v for k, v in cfg.items() if k != "hotkey"}
    try:
        atomic_write_text(CONFIG_PATH, json.dumps(out, indent=2))
    except OSError as e:
        print(f"[config] could not save: {e}", file=sys.stderr)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    loaded: Optional[dict] = None
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[config] failed to parse {CONFIG_PATH}: {e}", file=sys.stderr)
    # Backward compat: 'hotkey' (single str) → 'hotkeys' (list).
    if not cfg.get("hotkeys"):
        legacy = cfg.get("hotkey")
        cfg["hotkeys"] = [legacy] if isinstance(legacy, str) and legacy else ["tab"]
    elif isinstance(cfg["hotkeys"], str):
        cfg["hotkeys"] = [cfg["hotkeys"]]
    # Rewrite the file if it's missing any new default keys (e.g. _comments, hotkeys),
    # so users see the latest hints next time they open it.
    needs_rewrite = (loaded is None) or any(k not in loaded for k in DEFAULT_CONFIG)
    if needs_rewrite:
        out = {k: cfg[k] for k in cfg if k != "hotkey"}
        try:
            atomic_write_text(CONFIG_PATH, json.dumps(out, indent=2))
        except Exception as e:
            print(f"[config] could not rewrite {CONFIG_PATH}: {e}", file=sys.stderr)
    return cfg


def player_key(primary_id: str) -> str:
    """The splitscreen suffix is unstable across sessions; collapse to Platform|Uid."""
    parts = primary_id.split("|")
    return f"{parts[0]}|{parts[1]}" if len(parts) >= 2 else primary_id


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_players() -> dict:
    if not PLAYERS_PATH.exists():
        return {}
    try:
        return json.loads(PLAYERS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        ts = int(datetime.now(timezone.utc).timestamp())
        backup = PLAYERS_PATH.with_name(f"players.corrupt-{ts}.json")
        try:
            PLAYERS_PATH.rename(backup)
            print(f"[players] corrupt file backed up to {backup.name}: {e}", file=sys.stderr)
        except OSError:
            print(f"[players] corrupt and could not back up: {e}", file=sys.stderr)
        return {}


def save_players(players: dict) -> None:
    atomic_write_text(PLAYERS_PATH, json.dumps(players, indent=2, sort_keys=True))


def append_match(record: dict) -> None:
    with MATCHES_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


_PLAYLIST_BY_PLAYER_COUNT = {2: "1v1", 4: "2v2", 6: "3v3"}


def _playlist_from_player_count(n: int) -> str:
    return _PLAYLIST_BY_PLAYER_COUNT.get(n, "other")


def _match_playlist(record: dict) -> str:
    """Field-or-derive: prefer the explicit playlist key, fall back to roster
    size for matches saved before that field existed. Every consumer should
    go through this — the raw record key is unreliable for legacy entries."""
    pl = record.get("playlist")
    if isinstance(pl, str) and pl:
        return pl
    players = record.get("players") or []
    return _playlist_from_player_count(len(players))


def append_mmr_history(entry: dict) -> None:
    """One JSON line per snapshot of self MMR. Append-only; readers parse the
    whole file (it grows ~80 bytes per line, well under 10 MB even for heavy
    multi-year users). Caller is responsible for the dedupe rule (we write
    only when TRN's lastUpdated has actually advanced)."""
    try:
        with MMR_HISTORY_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        print(f"[mmr-history] write failed: {e}", file=sys.stderr)


def load_mmr_history() -> list[dict]:
    """Parse mmr_history.jsonl into a list, in file order. Skips malformed
    lines silently — one bad line shouldn't blank the whole graph."""
    if not MMR_HISTORY_PATH.exists():
        return []
    out = []
    try:
        with MMR_HISTORY_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        print(f"[mmr-history] read failed: {e}", file=sys.stderr)
    return out


def load_matches() -> list[dict]:
    """Parse matches.jsonl. Same forgiveness as load_mmr_history."""
    if not MATCHES_PATH.exists():
        return []
    out = []
    try:
        with MATCHES_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        print(f"[matches] read failed: {e}", file=sys.stderr)
    return out


def update_players_cache(players: dict, match: dict) -> None:
    my_team = match["myTeam"]
    i_won = match["winner"] == my_team
    when = match["endedAt"]
    score = match.get("score")  # [blue, orange]
    if isinstance(score, list) and len(score) == 2:
        my_pov = [score[my_team], score[1 - my_team]]
    else:
        my_pov = None
    result_letter = "W" if i_won else "L"
    for p in match["players"]:
        rec = players.setdefault(p["key"], {
            "name": p["name"],
            "aliases": [p["name"]],
            BUCKET_VS:   {"wins": 0, "losses": 0, "lastSeenAt": None,
                          "lastResult": None, "lastScore": None},
            BUCKET_WITH: {"wins": 0, "losses": 0, "lastSeenAt": None,
                          "lastResult": None, "lastScore": None},
        })
        rec["name"] = p["name"]
        if p["name"] not in rec["aliases"]:
            rec["aliases"].append(p["name"])
        bucket = rec[BUCKET_WITH if p["team"] == my_team else BUCKET_VS]
        if i_won:
            bucket["wins"] += 1
        else:
            bucket["losses"] += 1
        bucket["lastSeenAt"] = when
        bucket["lastResult"] = result_letter
        bucket["lastScore"] = my_pov


def _last_touch_player(data: dict) -> tuple[Optional[str], Optional[int]]:
    """(Name, TeamNum) of BallLastTouch.Player, or (None, None) if absent/malformed."""
    last_touch = data.get("BallLastTouch")
    if not isinstance(last_touch, dict):
        return (None, None)
    player = last_touch.get("Player")
    if not isinstance(player, dict):
        return (None, None)
    name = player.get("Name")
    team = player.get("TeamNum")
    return (
        name if isinstance(name, str) else None,
        team if isinstance(team, int) and team in (0, 1) else None,
    )


# ---- MMR / TRN integration ---------------------------------------------------
# The official Stats API doesn't expose MMR or rank. We optionally pull it from
# tracker.network's public JSON endpoint, which serves real-time data behind a
# 4-minute server-side cache. curl_cffi impersonates Chrome's TLS fingerprint
# so Cloudflare lets us through without a browser.
#
# Lookup quirk: TRN's profile endpoint indexes by display name across every
# platform we care about. Numeric platform IDs (raw 32-hex Epic, PSN/XBL ints)
# return 400/404. Cache key stays the stable Platform|Uid (so renames don't
# pollute our cache); the lookup string is the live wire's Name field.

MMR_PLATFORM_TO_TRN = {
    "Epic": "epic",
    "Steam": "steam",
    "PS4": "psn",
    "XboxOne": "xbl",
    "Switch": "switch",
}

# TRN playlist IDs we care about. Casual / extra-modes are intentionally
# excluded from "best" — they don't reflect competitive skill.
MMR_PLAYLIST_IDS = {
    10: "1v1",
    11: "2v2",
    13: "3v3",
}
MMR_CATEGORIES = ("best", "1v1", "2v2", "3v3")

# Standard RL rank colors. Tier strings come from TRN as "Bronze I", "Diamond III",
# "Grand Champion II", etc. — we match on the prefix word.
MMR_TIER_COLORS = {
    "Unranked":         "#8E9379",
    "Bronze":           "#B87333",
    "Silver":           "#C0C5CD",
    "Gold":             "#F0C674",
    "Platinum":         "#6FC8D6",
    "Diamond":          "#7FA9F2",
    "Champion":         "#B59CEE",
    "Grand Champion":   "#EC4F50",
    "Supersonic Legend": "#DB2C70",
}

MMR_TTL_SECONDS = 600   # local cache freshness — TRN's own TTL is 4 min
MMR_FETCH_INTERVAL = 2.0   # min seconds between outbound TRN requests

# Cap log size at ~256 KB so it doesn't grow forever in long-running sessions.
# We rotate by truncating once we cross the cap — last write wins, no archive.
_MMR_LOG_CAP = 256 * 1024
_mmr_log_lock = threading.Lock()


def mmr_log(msg: str) -> None:
    """Append a timestamped line to mmr.log AND mirror to stderr.

    Designed to work even under start.bat's `pythonw` launch (which discards
    stdout/stderr) — the log file is the sole source of truth for debugging.
    Failures writing the log are swallowed: a debug log breaking the app is
    a worse outcome than missing one log line."""
    line = f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}"
    print(f"[mmr] {msg}", file=sys.stderr)
    try:
        with _mmr_log_lock:
            if MMR_LOG_PATH.exists() and MMR_LOG_PATH.stat().st_size > _MMR_LOG_CAP:
                MMR_LOG_PATH.write_text("", encoding="utf-8")
            with MMR_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass


_api_dump_lock = threading.Lock()


def api_dump(event: str, data: dict) -> None:
    """Append one envelope as a JSON line to api_dump.log. Truncate-rotates
    when the file exceeds API_DUMP_CAP_BYTES. Diagnostic-only — failures are
    swallowed because a debug dump must never crash the parser."""
    try:
        line = json.dumps({"ts": now_iso(), "Event": event, "Data": data})
    except (TypeError, ValueError):
        return
    try:
        with _api_dump_lock:
            if API_DUMP_PATH.exists() and API_DUMP_PATH.stat().st_size > API_DUMP_CAP_BYTES:
                API_DUMP_PATH.write_text("", encoding="utf-8")
            with API_DUMP_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass


def mmr_lookup_handle(primary_id: str, name: str) -> Optional[tuple[str, str]]:
    """(trn_platform_slug, display_name) for a wire identity, or None if
    unsupported. TRN's lookup endpoint requires display name on every platform
    we care about — numeric platform IDs return 400/404."""
    if not name:
        return None
    parts = primary_id.split("|")
    if not parts:
        return None
    plat = MMR_PLATFORM_TO_TRN.get(parts[0])
    if not plat:
        return None
    return (plat, name)


def _tier_color(tier: Optional[str]) -> str:
    if not tier:
        return MMR_TIER_COLORS["Unranked"]
    for prefix, color in MMR_TIER_COLORS.items():
        if tier.startswith(prefix):
            return color
    return C_TEXT if "C_TEXT" in globals() else "#E0E3E5"


def _parse_trn_payload(data: dict) -> dict:
    """Distill a TRN profile response into our compact cache shape.

    Output:
      {
        "fetched_at":  "2026-05-01T22:21:00+00:00",
        "lastUpdated": "2026-05-01T22:20:22+00:00",  # from TRN
        "handle":      "PantuflaRl",
        "playlists":   {"1v1": {"mmr": 400, "tier": "Silver III", "division": "Division II"}, ...},
        "best":        {"mmr": 473, "tier": "Gold I", "division": "Division I", "playlist": "2v2"},
      }
    """
    info = (data or {}).get("platformInfo") or {}
    meta = (data or {}).get("metadata") or {}
    last_updated = (meta.get("lastUpdated") or {}).get("value")

    playlists: dict[str, dict] = {}
    for seg in (data or {}).get("segments") or []:
        if seg.get("type") != "playlist":
            continue
        attrs = seg.get("attributes") or {}
        pid = attrs.get("playlistId")
        label = MMR_PLAYLIST_IDS.get(pid)
        if not label:
            continue
        stats = seg.get("stats") or {}
        rating = (stats.get("rating") or {}).get("value")
        tier = ((stats.get("tier") or {}).get("metadata") or {}).get("name")
        div = ((stats.get("division") or {}).get("metadata") or {}).get("name")
        if rating is None:
            continue
        playlists[label] = {
            "mmr": int(rating),
            "tier": tier or "Unranked",
            "division": div or "",
        }

    best = None
    for label, p in playlists.items():
        if best is None or p["mmr"] > best["mmr"]:
            best = {**p, "playlist": label}

    return {
        "fetched_at": now_iso(),
        "lastUpdated": last_updated,
        "handle": info.get("platformUserHandle"),
        "playlists": playlists,
        "best": best,
    }


def load_mmr_cache() -> dict:
    if not MMR_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(MMR_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[mmr] failed to read cache, starting fresh: {e}", file=sys.stderr)
        return {}


def save_mmr_cache(cache: dict) -> None:
    try:
        atomic_write_text(MMR_CACHE_PATH, json.dumps(cache, indent=2, sort_keys=True))
    except OSError as e:
        print(f"[mmr] failed to write cache: {e}", file=sys.stderr)


class MMRClient(QObject):
    """Background fetcher for player MMR/rank data from tracker.network.

    Fully off-thread: a single worker thread drains a queue, hits the TRN
    public API, and emits `updated(player_key)` when fresh data lands. The Qt
    main loop only ever touches the cache via `get(key)`. Soft-fails on every
    error path — the overlay never breaks because TRN is down.

    Disabled (`enabled=False`) means: never enqueue, never network. Toggleable
    at runtime via `set_enabled()` so the tray menu can flip it without a
    restart.
    """

    updated = Signal(str)  # cache key (Platform|Uid)

    _BASE_URL = "https://api.tracker.gg/api/v2/rocket-league/standard/profile/{plat}/{ident}"
    _HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://rocketleague.tracker.network",
        "Referer": "https://rocketleague.tracker.network/",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def __init__(self, enabled: bool = False, ttl_seconds: int = MMR_TTL_SECONDS):
        super().__init__()
        self._enabled = bool(enabled)
        self._ttl = max(60, int(ttl_seconds))
        self._cache: dict = load_mmr_cache()
        self._cache_lock = threading.Lock()
        self._queue: "queue.Queue[tuple[str, str, str]]" = queue.Queue()
        self._inflight: set[str] = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._worker, daemon=True, name="MMRFetcher",
        )
        # Defer curl_cffi import: the module is optional. If it's missing, we
        # disable network access but still serve cached data.
        try:
            from curl_cffi import requests as _curl_requests  # noqa
            self._requests = _curl_requests
            mmr_log(f"init enabled={self._enabled} ttl={self._ttl}s "
                    f"cache_entries={len(self._cache)} curl_cffi=ok")
        except ImportError as e:
            self._requests = None
            mmr_log(f"init curl_cffi MISSING ({e}). Run: "
                    f"python -m pip install -r requirements.txt")

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()
            mmr_log("worker thread started")

    def stop(self) -> None:
        self._stop.set()

    def set_enabled(self, on: bool) -> None:
        prev = self._enabled
        self._enabled = bool(on)
        if prev != self._enabled:
            mmr_log(f"set_enabled {prev} -> {self._enabled} "
                    f"(curl_cffi_loaded={self._requests is not None})")

    def is_enabled(self) -> bool:
        return self._enabled and self._requests is not None

    def get(self, key: str) -> Optional[dict]:
        with self._cache_lock:
            entry = self._cache.get(key)
            return dict(entry) if entry else None

    def _is_stale(self, entry: Optional[dict]) -> bool:
        if not entry:
            return True
        ts = entry.get("fetched_at")
        if not isinstance(ts, str):
            return True
        try:
            t = datetime.fromisoformat(ts)
        except ValueError:
            return True
        age = (datetime.now(timezone.utc) - t).total_seconds()
        return age >= self._ttl

    def enqueue(self, primary_id: str, name: str, force: bool = False) -> None:
        """Queue a refresh for this opponent. Skips if disabled or already
        in-flight; also skips on cache-fresh unless `force=True`. Use force
        for after-match self refresh, where we want the absolute latest TRN
        has even if our local cache is technically still warm."""
        key = player_key(primary_id)
        if not self._enabled:
            mmr_log(f"enqueue skip {key!r} ({name!r}): disabled")
            return
        if self._requests is None:
            mmr_log(f"enqueue skip {key!r} ({name!r}): curl_cffi missing")
            return
        if key in self._inflight:
            mmr_log(f"enqueue skip {key!r} ({name!r}): already in-flight")
            return
        if not force:
            with self._cache_lock:
                entry = self._cache.get(key)
            if not self._is_stale(entry):
                mmr_log(f"enqueue skip {key!r} ({name!r}): cache fresh "
                        f"(fetched_at={entry.get('fetched_at')!r})")
                return
        handle = mmr_lookup_handle(primary_id, name)
        if handle is None:
            mmr_log(f"enqueue skip {key!r} ({name!r}): unsupported platform "
                    f"or missing name")
            return
        plat, ident = handle
        self._inflight.add(key)
        self._queue.put((key, plat, ident))
        mmr_log(f"enqueue{' [forced]' if force else ''} {key!r} -> "
                f"{plat}/{ident!r} (queue size={self._queue.qsize()})")

    def enqueue_roster(self, roster: list[dict]) -> None:
        """Convenience: queue every player in a match roster."""
        mmr_log(f"enqueue_roster: {len(roster)} player(s) "
                f"(enabled={self._enabled}, curl_cffi={self._requests is not None})")
        for p in roster:
            pid = p.get("primaryId") or p.get("key")
            if not pid:
                mmr_log(f"  skip: no primaryId/key for {p.get('name')!r}")
                continue
            self.enqueue(pid, p.get("name") or "")

    def _worker(self) -> None:
        last_request = 0.0
        while not self._stop.is_set():
            try:
                key, plat, ident = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            # Throttle global outbound rate.
            since = time.monotonic() - last_request
            if since < MMR_FETCH_INTERVAL:
                wait = MMR_FETCH_INTERVAL - since
                mmr_log(f"throttle wait {wait:.2f}s before {key!r}")
                self._stop.wait(wait)
                if self._stop.is_set():
                    break
            try:
                self._fetch_one(key, plat, ident)
            except Exception as e:
                mmr_log(f"{key!r} fetch FAILED: {type(e).__name__}: {e}")
            finally:
                self._inflight.discard(key)
            last_request = time.monotonic()

    def _fetch_one(self, key: str, plat: str, ident: str) -> None:
        if self._requests is None:
            mmr_log(f"{key!r} fetch aborted: curl_cffi missing")
            return
        url = self._BASE_URL.format(plat=plat, ident=ident)
        mmr_log(f"GET {url}")
        t0 = time.monotonic()
        r = self._requests.get(url, headers=self._HEADERS,
                               impersonate="chrome120", timeout=15)
        dt = (time.monotonic() - t0) * 1000
        mmr_log(f"  -> HTTP {r.status_code} in {dt:.0f}ms ({len(r.content)} bytes)")
        if r.status_code == 404:
            # Player isn't on TRN under this handle. Cache a negative entry so
            # we don't keep hammering the same dead lookup every time the
            # overlay opens.
            with self._cache_lock:
                self._cache[key] = {
                    "fetched_at": now_iso(),
                    "not_found": True,
                    "handle": ident,
                }
                save_mmr_cache(self._cache)
            mmr_log(f"  {key!r} NOT FOUND (cached negative)")
            self.updated.emit(key)
            return
        if r.status_code != 200:
            mmr_log(f"  {key!r} HTTP {r.status_code}: {r.text[:200]!r}")
            return
        try:
            payload = r.json()
        except ValueError as e:
            mmr_log(f"  {key!r} bad JSON: {e}")
            return
        data = (payload or {}).get("data")
        if not isinstance(data, dict):
            mmr_log(f"  {key!r} no .data in response")
            return
        entry = _parse_trn_payload(data)
        with self._cache_lock:
            self._cache[key] = entry
            save_mmr_cache(self._cache)
        best = entry.get("best") or {}
        # Compact one-liner with every playlist's MMR — saves cross-referencing
        # mmr.log and my_mmr.log when debugging "did the number actually move?"
        pl = entry.get("playlists") or {}
        pl_str = " ".join(
            f"{lbl}={(pl.get(lbl) or {}).get('mmr', '—')}"
            for lbl in ("1v1", "2v2", "3v3")
        )
        mmr_log(f"  {key!r} OK handle={entry.get('handle')!r} {pl_str} "
                f"best={best.get('mmr')}@{best.get('playlist')} "
                f"trn_lastUpdated={entry.get('lastUpdated')}")
        self.updated.emit(key)


class StatsClient(QObject):
    """Reads the Rocket League Stats API local TCP socket and emits match-lifecycle signals."""

    match_initialized = Signal(dict)
    match_ended = Signal(dict)
    match_destroyed = Signal()
    connection_status = Signal(bool)
    event_seen = Signal(str, dict)  # raw event name + decoded data, for downstream consumers

    def __init__(self, host: str, port: int, api_dump_enabled: bool = False):
        super().__init__()
        self.host = host
        self.port = port
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="StatsClient")
        self._api_dump_enabled = bool(api_dump_enabled)
        self._update_state_count_this_match = 0
        self._reset()

    def _reset(self):
        self._roster: dict[str, dict] = {}
        self._my_team: Optional[int] = None
        self._arena: str = ""
        self._match_guid: Optional[str] = None
        self._initialized_emitted = False
        self._last_emitted_roster_size = 0
        self._round_started = False  # locks the roster after kickoff
        self._spectator_warned = False
        self._score: list[int] = [0, 0]
        self._team_colors: dict[int, str] = {}
        self._in_replay = False
        self._update_state_count_this_match = 0

    def start(self):
        self._thread.start()

    def stop(self):
        loop, task = self._loop, self._task
        if loop is not None and task is not None and not task.done():
            loop.call_soon_threadsafe(task.cancel)
        self._thread.join(timeout=3)

    def _run(self):
        try:
            asyncio.run(self._main())
        except Exception as e:
            print(f"[stats] event loop crashed: {e}", file=sys.stderr)

    async def _main(self):
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        try:
            await self._connect_loop()
        except asyncio.CancelledError:
            pass
        finally:
            self.connection_status.emit(False)

    async def _connect_loop(self):
        # Local Stats API speaks raw TCP NDJSON despite the doc calling it a "web socket".
        backoff = 1.0
        while True:
            try:
                print(f"[stats] connecting tcp://{self.host}:{self.port}", file=sys.stderr)
                await self._run_tcp()
                print("[stats] disconnected; reconnecting", file=sys.stderr)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except OSError as e:
                self.connection_status.emit(False)
                print(f"[stats] connect failed ({type(e).__name__}: {e}); "
                      f"retry in {backoff:.0f}s", file=sys.stderr)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _run_tcp(self):
        reader, writer = await asyncio.open_connection(self.host, self.port)
        self.connection_status.emit(True)
        try:
            decoder = json.JSONDecoder()
            buf = ""
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    return
                buf += chunk.decode("utf-8", errors="replace")
                while True:
                    stripped = buf.lstrip()
                    if not stripped:
                        buf = ""
                        break
                    try:
                        obj, idx = decoder.raw_decode(stripped)
                    except json.JSONDecodeError:
                        buf = stripped
                        break
                    buf = stripped[idx:]
                    self._safe_handle(obj)
        finally:
            writer.close()
            # wait_closed can hang on Windows when the peer socket is half-open;
            # cap it so stop()'s 3s thread.join() always wins the race.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)

    def _safe_handle(self, msg) -> None:
        if not isinstance(msg, dict):
            return
        event = msg.get("Event", "?")
        if self._api_dump_enabled:
            # Decode the double-encoded Data payload up-front so the dump shows
            # the unwrapped envelope. Mirrors the decoding done in _handle.
            raw_data = msg.get("Data")
            if isinstance(raw_data, str):
                try:
                    decoded = json.loads(raw_data) if raw_data else {}
                except json.JSONDecodeError:
                    decoded = raw_data
            else:
                decoded = raw_data if raw_data is not None else {}
            if event == EVT_UPDATE_STATE:
                if self._update_state_count_this_match < 3:
                    api_dump(event, decoded)
                    self._update_state_count_this_match += 1
            else:
                api_dump(event, decoded)
        try:
            self._handle(msg)
        except Exception as e:
            import traceback
            print(f"[stats] handler error on {event}: {type(e).__name__}: {e}",
                  file=sys.stderr)
            traceback.print_exc(file=sys.stderr)

    def _handle(self, msg: dict):
        event = msg.get("Event")
        # Wire format double-encodes: Data arrives as a JSON-encoded string.
        data = msg.get("Data")
        if isinstance(data, str):
            try:
                data = json.loads(data) if data else {}
            except json.JSONDecodeError:
                data = {}
        if not isinstance(data, dict):
            data = {}
        if event == EVT_GOAL_SCORED:
            if self._classify_goal_scored(data):
                self.event_seen.emit(event, data)
        elif event:
            self.event_seen.emit(event, data)
        if event == EVT_UPDATE_STATE:
            self._on_update_state(data)
        elif event == EVT_MATCH_CREATED:
            self._reset()
            self._match_guid = data.get("MatchGuid")
        elif event == EVT_MATCH_INITIALIZED:
            self._maybe_emit_initialized()
        elif event == EVT_ROUND_STARTED:
            # Lock the roster at kickoff. One last emit if anyone joined since the
            # previous emit; from this point we stop accumulating players.
            self._maybe_emit_initialized()
            if self._initialized_emitted and len(self._roster) > self._last_emitted_roster_size:
                self._emit_match_initialized()
            self._round_started = True
        elif event == EVT_MATCH_ENDED:
            self._on_match_ended(data)
        elif event == EVT_MATCH_DESTROYED:
            self.match_destroyed.emit()
            self._reset()
        elif event == EVT_REPLAY_CREATED:
            self._in_replay = True

    def _on_update_state(self, data: dict):
        if not isinstance(data, dict) or self._in_replay:
            return
        game = data.get("Game")
        if isinstance(game, dict):
            arena = game.get("Arena")
            if isinstance(arena, str) and arena and arena != self._arena:
                self._arena = arena
            teams = game.get("Teams")
            if isinstance(teams, list):
                for t in teams:
                    if not isinstance(t, dict):
                        continue
                    tn = t.get("TeamNum")
                    if tn not in (0, 1):
                        continue
                    sc = t.get("Score")
                    if isinstance(sc, int):
                        self._score[int(tn)] = sc
                    # Skip color capture once both teams' colors are known.
                    if len(self._team_colors) < 2:
                        cp = t.get("ColorPrimary")
                        if (int(tn) not in self._team_colors and isinstance(cp, str)
                                and len(cp) == 6):
                            self._team_colors[int(tn)] = "#" + cp.upper()
        # Once the round has actually started (kickoff fired), the roster is locked.
        # Until then we keep accumulating — players load asynchronously in 2v2/3v3.
        if self._round_started:
            return
        players = data.get("Players")
        if not isinstance(players, list):
            return
        spectator_team_hits: set[int] = set()
        for p in players:
            if not isinstance(p, dict):
                continue
            pid = p.get("PrimaryId")
            team = p.get("TeamNum")
            if not isinstance(pid, str) or team not in (0, 1):
                continue
            key = player_key(pid)
            name_raw = p.get("Name")
            name = name_raw if isinstance(name_raw, str) else "?"
            self._roster[key] = {
                "key": key,
                "primaryId": pid,
                "name": name,
                "team": int(team),
            }
            if any(k in p for k in SPECTATOR_FIELDS):
                spectator_team_hits.add(int(team))
        # Spectator-only fields appear iff the local client is on this player's team.
        # If both teams report them, the user is spectating — leave my_team unset.
        if self._my_team is None and len(spectator_team_hits) == 1:
            (self._my_team,) = spectator_team_hits
        elif self._my_team is None and len(spectator_team_hits) > 1 and not self._spectator_warned:
            self._spectator_warned = True
            print(f"[state] spectator mode? both teams report spectator fields: "
                  f"{spectator_team_hits}", file=sys.stderr)
        # First emit when both teams have a player; re-emit on every roster growth so
        # late joiners (typical in 3v3) appear in the H2H card before kickoff.
        if not self._initialized_emitted:
            self._maybe_emit_initialized()
        elif len(self._roster) > self._last_emitted_roster_size:
            self._emit_match_initialized()

    def _on_match_ended(self, data: dict):
        if self._in_replay:
            return
        winner = data.get("WinnerTeamNum")
        if winner is None or self._my_team is None or not self._roster:
            return
        self.match_ended.emit({
            "winner": int(winner),
            "myTeam": self._my_team,
            "arena": self._arena,
            "matchGuid": self._match_guid,
            "players": list(self._roster.values()),
            "score": list(self._score),
            "teamColors": dict(self._team_colors),
        })

    def _classify_goal_scored(self, data: dict) -> bool:
        """Set `bOwnGoal` on data; return False for placeholder events.

        See `rl_api_text.md` for the placeholder-event quirk and own-goal
        derivation rules.
        """
        scorer = data.get("Scorer")
        if not isinstance(scorer, dict):
            return False
        scorer_name = scorer.get("Name")
        if not scorer_name:
            return False
        scorer_team = scorer.get("TeamNum")
        _, last_team = _last_touch_player(data)
        data["bOwnGoal"] = (
            scorer_team in (0, 1)
            and last_team in (0, 1)
            and scorer_team != last_team
        )
        return True

    def _maybe_emit_initialized(self):
        if (self._initialized_emitted or self._in_replay
                or self._my_team is None or not self._roster):
            return
        teams = {p["team"] for p in self._roster.values()}
        if len(teams) < 2:
            return
        self._initialized_emitted = True
        self._emit_match_initialized()

    def _emit_match_initialized(self):
        self._last_emitted_roster_size = len(self._roster)
        self.match_initialized.emit({
            "teamColors": dict(self._team_colors),
            "arena": self._arena,
            "myTeam": self._my_team,
            "players": list(self._roster.values()),
        })


class HotkeyManager(QObject):
    """Multi-trigger keyboard + gamepad listener.

    Emits `pressed` when going from no-triggers-held to at-least-one-held, and `released`
    when the last held trigger is let go. Holding multiple bindings simultaneously is
    supported (overlay stays up until *all* are released).
    """

    pressed = Signal()
    released = Signal()

    def __init__(self, hotkey_names: list[str]):
        super().__init__()
        self._kb_targets: list[tuple] = []
        self._pad_targets: list[str] = []
        for raw in hotkey_names:
            name = raw.strip().lower()
            if not name:
                continue
            if name.startswith("pad_"):
                pad_name = name[4:]
                if pad_name in GAMEPAD_BUTTONS:
                    self._pad_targets.append(pad_name)
                else:
                    print(f"[hotkey] unknown gamepad key {raw!r}; "
                          f"valid: {sorted(GAMEPAD_BUTTONS)}", file=sys.stderr)
            else:
                self._kb_targets.append(self._parse_kb(name))
        self._down: set[tuple] = set()
        self._kb_listener = keyboard.Listener(
            on_press=self._on_kb_press, on_release=self._on_kb_release,
        )
        self._pad_thread: Optional[threading.Thread] = None
        self._pad_stop = threading.Event()
        if self._pad_targets:
            self._pad_thread = threading.Thread(
                target=self._pad_loop, daemon=True, name="GamepadListener",
            )

    @staticmethod
    def _parse_kb(name: str):
        if hasattr(keyboard.Key, name):
            return ("special", getattr(keyboard.Key, name))
        if len(name) == 1:
            return ("char", name)
        raise ValueError(
            f"Unknown keyboard key {name!r}. Use 'tab', 'f1', 'shift' (etc.), "
            "a single char like 'h', or prefix with 'pad_' for a gamepad button."
        )

    def _kb_match(self, key, target) -> bool:
        kind, value = target
        if kind == "special":
            return key == value
        return getattr(key, "char", None) == value

    def _on_kb_press(self, key):
        for t in self._kb_targets:
            if self._kb_match(key, t):
                self._add_down(("kb",) + t)
                return

    def _on_kb_release(self, key):
        for t in self._kb_targets:
            if self._kb_match(key, t):
                self._remove_down(("kb",) + t)
                return

    def _add_down(self, key_id: tuple):
        was_empty = not self._down
        self._down.add(key_id)
        if was_empty:
            self.pressed.emit()

    def _remove_down(self, key_id: tuple):
        self._down.discard(key_id)
        if not self._down:
            self.released.emit()

    def _pad_loop(self):
        try:
            import inputs as _inputs
        except ImportError:
            print("[hotkey] gamepad bindings configured but 'inputs' is not installed. "
                  "Run: pip install inputs", file=sys.stderr)
            return
        wanted: dict[tuple, list[tuple]] = {}
        for pad_name in self._pad_targets:
            etype, ecode, target_val = GAMEPAD_BUTTONS[pad_name]
            wanted.setdefault((etype, ecode), []).append((pad_name, target_val))
        active: dict[str, bool] = {}
        warned_no_pad = False
        print(f"[hotkey] gamepad listener watching: {self._pad_targets}", file=sys.stderr)
        while not self._pad_stop.is_set():
            try:
                events = _inputs.get_gamepad()
            except _inputs.UnpluggedError:
                if not warned_no_pad:
                    print("[hotkey] no gamepad detected; will keep watching", file=sys.stderr)
                    warned_no_pad = True
                self._pad_stop.wait(2.0)
                continue
            except Exception as e:
                print(f"[hotkey] gamepad read error: {type(e).__name__}: {e}", file=sys.stderr)
                self._pad_stop.wait(2.0)
                continue
            warned_no_pad = False
            for ev in events:
                key = (ev.ev_type, ev.code)
                if key not in wanted:
                    continue
                for pad_name, target_val in wanted[key]:
                    if target_val == "thresh":
                        is_pressed = ev.state >= GAMEPAD_TRIGGER_THRESHOLD
                    elif ev.ev_type == "Absolute":
                        is_pressed = ev.state == target_val
                    else:
                        is_pressed = ev.state == 1
                    was = active.get(pad_name, False)
                    if is_pressed and not was:
                        active[pad_name] = True
                        self._add_down(("pad", pad_name))
                    elif not is_pressed and was:
                        active[pad_name] = False
                        self._remove_down(("pad", pad_name))

    def start(self):
        self._kb_listener.start()
        if self._pad_thread is not None:
            self._pad_thread.start()

    def stop(self):
        self._kb_listener.stop()
        self._pad_stop.set()


class Overlay(QWidget):
    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
            | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        self._label = QLabel(self)
        self._label.setTextFormat(Qt.RichText)
        self._label.setWordWrap(True)
        self._label.setFont(QFont(cfg["font_family"], cfg["font_size"]))
        self._label.setFixedWidth(cfg["width"])
        bg_rgba = ",".join(str(v) for v in cfg.get("background_rgba") or [16, 20, 21, 200])
        border_rgba = ",".join(str(v) for v in cfg.get("border_rgba") or [255, 255, 255, 28])
        radius = int(cfg.get("border_radius_px", 4))
        self._label.setStyleSheet(
            "QLabel {"
            f"  color: {cfg['text_color']};"
            f"  background-color: rgba({bg_rgba});"
            f"  border: 1px solid rgba({border_rgba});"
            f"  border-radius: {radius}px;"
            "  padding: 14px 16px 16px 16px;"
            "}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._label)
        self.resize(cfg["width"], 100)

        self.set_html(idle_html("Waiting for Rocket League…"))
        self.hide()

    def set_html(self, html: str):
        self._label.setText(html)
        self._label.adjustSize()
        self.adjustSize()
        self._reposition()

    def set_pixmap(self, pix) -> None:
        """Switches the QLabel from HTML mode to image mode. QLabel handles
        the mode flip internally; the next call to set_html() switches back
        cleanly. Used by the graph view, which can't be expressed in Qt's
        RichText engine (no SVG, no data-URL <img>)."""
        self._label.setPixmap(pix)
        self._label.adjustSize()
        self.adjustSize()
        self._reposition()

    def _reposition(self):
        screen_obj = QGuiApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        screen = screen_obj.availableGeometry()
        m, w, h = self.cfg["margin"], self.width(), self.height()
        pos = self.cfg["position"]
        if pos not in VALID_POSITIONS:
            print(f"[overlay] unknown position '{pos}', using top-right", file=sys.stderr)
            pos = "top-right"
        coords = {
            "top-left":     (screen.left() + m,                          screen.top() + m),
            "top-center":   (screen.left() + (screen.width() - w) // 2,  screen.top() + m),
            "top-right":    (screen.right() - w - m,                     screen.top() + m),
            "bottom-left":  (screen.left() + m,                          screen.bottom() - h - m),
            "bottom-right": (screen.right() - w - m,                     screen.bottom() - h - m),
        }
        x, y = coords[pos]
        self.move(x, y)


# ---- Visual layer ----

# Palette baseline. Each constant can be overridden via cfg["colors"]; see
# apply_color_overrides() in main(). Layout helpers also reference these via
# EM_DASH / _PAIR_SEP, which are recomputed when the palette changes.
C_TEXT     = "#E0E3E5"  # on-surface
C_DIM      = "#C4C9AC"  # on-surface-variant
C_MUTED    = "#8E9379"  # outline
C_FAINT    = "#444933"  # outline-variant (dividers, NEW pill border)
C_BLUE     = "#3B9EFF"  # fallback only — wire ColorPrimary preferred
C_ORANGE   = "#FF7A29"  # fallback only
C_WIN      = "#CCFF00"  # lime — wins, streaks, +diffs, recent W pips
C_LOSS     = "#FF6467"  # red — losses, recent L pips, negative diffs
# Dark contrasts for high-saturation fills (graph delta pill text on lime/red).
C_PILL_TEXT_WIN  = "#0c1004"
C_PILL_TEXT_LOSS = "#1a0606"

# Player name truncation in the H2H card (overridable via cfg["name_max_length"]).
NAME_MAX_LEN = 16


def idle_html(message: str) -> str:
    is_disconnected = "disconnect" in message.lower() or "stats api" in message.lower()
    dot_color = "#E5484D" if is_disconnected else C_BLUE
    label = "OFFLINE" if is_disconnected else "STANDBY"
    return (
        "<table width='100%' cellspacing='0' cellpadding='0' style='border-collapse:collapse;'>"
        "<tr>"
        f"<td align='left' style='color:{C_TEXT};font-size:10pt;font-weight:700;"
        "letter-spacing:0.18em;'>HEAD&middot;TO&middot;HEAD</td>"
        "<td align='right' style='font-size:8pt;font-weight:700;letter-spacing:0.16em;'>"
        f"<span style='color:{dot_color};'>&#9679;</span>"
        f"&nbsp;<span style='color:#7A8290;'>{label}</span>"
        "</td>"
        "</tr>"
        "</table>"
        "<div style='height:10px;font-size:1px;line-height:1px;'>&nbsp;</div>"
        f"<div style='color:{C_DIM};font-size:10pt;letter-spacing:0.01em;line-height:140%;'>"
        f"{message}</div>"
    )


def humanize_when(iso_ts: Optional[str]) -> str:
    if not iso_ts:
        return ""
    try:
        t = datetime.fromisoformat(iso_ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        secs = int((datetime.now(timezone.utc) - t).total_seconds())
    except (ValueError, TypeError):
        return ""
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _recent_pips(recent) -> str:
    """Render a sequence of W/L letters as colored pips. Accepts any iterable
    (list, deque, tuple) — session.recent is a deque, so we materialise to a
    list first instead of slicing."""
    if not recent:
        return ""
    items = list(recent)[-5:]
    parts = []
    for r in items:
        color = C_WIN if r == "W" else C_LOSS
        parts.append(f"<span style='color:{color};'>{r}</span>")
    return (
        "<span style='font-family:Consolas,\"SF Mono\",monospace;"
        "font-size:8pt;font-weight:700;letter-spacing:0.16em;'>"
        + "".join(parts)
        + "</span>"
    )


def _mmr_pick(entry: Optional[dict], category: str) -> Optional[dict]:
    """Pull the right playlist's MMR record out of a cache entry, given the
    user's selected category. Returns None when nothing useful is available
    (entry missing, only contains a not_found marker, or the requested
    playlist hasn't been seen yet)."""
    if not entry or entry.get("not_found"):
        return None
    if category == "best":
        b = entry.get("best")
        return b if b and b.get("mmr") is not None else None
    pl = (entry.get("playlists") or {}).get(category)
    if not pl or pl.get("mmr") is None:
        return None
    return {**pl, "playlist": category}


def _render_mmr_chip(entry: Optional[dict], category: str, mmr_enabled: bool) -> str:
    """Tier + MMR for the player's selected category, colored by rank.

    States:
      - MMR disabled                          -> empty string
      - enabled, no entry yet (loading)       -> dim "…"
      - enabled, cache says not_found         -> dim "—"
      - enabled, picked playlist not played   -> dim "—"
      - enabled, has data                     -> "Gold II · 531"  (+ "2v2" hint in best mode)
    """
    if not mmr_enabled:
        return ""
    if entry is None:
        return (
            f"<span style='color:{C_MUTED};font-size:8pt;letter-spacing:0.04em;'>"
            "…</span>"
        )
    if entry.get("not_found"):
        return (
            f"<span style='color:{C_MUTED};font-size:8pt;letter-spacing:0.04em;'>"
            "—</span>"
        )
    pick = _mmr_pick(entry, category)
    if not pick:
        return (
            f"<span style='color:{C_MUTED};font-size:8pt;letter-spacing:0.04em;'>"
            "—</span>"
        )
    tier = pick.get("tier") or "Unranked"
    mmr = pick.get("mmr")
    color = _tier_color(tier)
    parts = [
        f"<span style='color:{color};font-size:8pt;font-weight:700;"
        f"letter-spacing:0.02em;'>{tier}</span>",
        f"<span style='color:{C_DIM};font-family:Consolas,\"SF Mono\",monospace;"
        f"font-size:8pt;font-weight:600;'>{mmr}</span>",
    ]
    if category == "best":
        playlist = pick.get("playlist")
        if playlist:
            parts.append(
                f"<span style='color:{C_MUTED};font-size:7pt;font-weight:600;"
                f"letter-spacing:0.06em;text-transform:uppercase;'>{playlist}</span>"
            )
    sep = (
        f"<span style='color:{C_FAINT};font-size:8pt;'>&nbsp;·&nbsp;</span>"
    )
    return sep.join(parts)


def _player_row(p: dict, my_team: int, players_db: dict, self_id: Optional[str] = None,
                mmr_db: Optional[dict] = None, mmr_category: str = "best",
                mmr_enabled: bool = False) -> str:
    rec = players_db.get(p["key"])
    bucket = BUCKET_WITH if p["team"] == my_team else BUCKET_VS
    name = p["name"]
    if len(name) > NAME_MAX_LEN:
        name = name[:NAME_MAX_LEN - 1] + "…"
    is_self = self_id is not None and p["key"] == self_id

    if is_self:
        stat_cell = (
            f"<span style='color:{C_WIN};font-size:8pt;font-weight:700;"
            "letter-spacing:0.16em;'>YOU</span>"
        )
        # Show your own MMR in the sub-row when MMR is enabled — useful for
        # tracking how the number moves between matches without leaving the
        # overlay. Same chip shape as opponents.
        self_chip = ""
        if mmr_enabled:
            self_entry = (mmr_db or {}).get(p["key"])
            self_chip = _render_mmr_chip(self_entry, mmr_category, mmr_enabled)
        sub_row = (
            f"<tr><td colspan='2' style='padding:0 0 2px 0;'>{self_chip}</td></tr>"
            if self_chip else ""
        )
        return (
            "<table width='100%' cellspacing='0' cellpadding='0' "
            "style='border-collapse:collapse;margin:0;'>"
            "<tr>"
            f"<td align='left' style='color:{C_TEXT};font-size:10pt;font-weight:600;"
            f"padding:3px 0 0 0;'>{name}</td>"
            "<td align='right' style='padding:3px 0 0 0;white-space:nowrap;'>"
            f"{stat_cell}</td>"
            "</tr>"
            f"{sub_row}"
            "</table>"
        )

    if not rec or (rec[bucket]["wins"] == 0 and rec[bucket]["losses"] == 0):
        stat_cell = (
            f"<span style='color:{C_DIM};font-size:8pt;font-weight:700;"
            f"letter-spacing:0.14em;border:1px solid {C_FAINT};"
            "padding:1px 6px;'>NEW</span>"
        )
        sub = ""
    else:
        b = rec[bucket]
        stat_cell = (
            f"<span style='font-family:Consolas,\"SF Mono\",monospace;"
            f"font-size:10pt;font-weight:600;color:{C_TEXT};'>"
            f"{b['wins']}<span style='color:{C_MUTED};'>&ndash;</span>{b['losses']}"
            "</span>"
        )
        when = humanize_when(b["lastSeenAt"])
        res = b["lastResult"]
        last_score = b.get("lastScore")
        if when and res:
            res_color = C_WIN if res == "W" else C_LOSS
            score_part = ""
            if isinstance(last_score, list) and len(last_score) == 2:
                score_part = (
                    f"&nbsp;<span style='color:{C_MUTED};"
                    "font-family:Consolas,\"SF Mono\",monospace;'>"
                    f"({last_score[0]}&ndash;{last_score[1]})</span>"
                )
            sub = (
                f"<span style='color:{C_MUTED};font-size:8pt;'>"
                f"{when}&nbsp;<span style='color:{res_color};font-weight:700;'>{res}</span>"
                f"{score_part}"
                "</span>"
            )
        else:
            sub = ""

    # MMR chip lives in the sub-row, before any history hint. We separate the
    # two with a faint dot — Qt RichText collapses adjacent inline spans cleanly.
    mmr_chip = ""
    if mmr_enabled:
        mmr_entry = (mmr_db or {}).get(p["key"])
        mmr_chip = _render_mmr_chip(mmr_entry, mmr_category, mmr_enabled)
    if mmr_chip and sub:
        combined_sub = (
            mmr_chip
            + f"<span style='color:{C_FAINT};font-size:8pt;'>&nbsp;·&nbsp;</span>"
            + sub
        )
    else:
        combined_sub = mmr_chip or sub

    sub_row = (
        f"<tr><td colspan='2' style='padding:0 0 2px 0;'>{combined_sub}</td></tr>"
        if combined_sub else ""
    )
    return (
        "<table width='100%' cellspacing='0' cellpadding='0' "
        "style='border-collapse:collapse;margin:0;'>"
        "<tr>"
        f"<td align='left' style='color:{C_TEXT};font-size:10pt;font-weight:500;"
        f"padding:3px 0 0 0;'>{name}</td>"
        "<td align='right' style='padding:3px 0 0 0;white-space:nowrap;'>"
        f"{stat_cell}</td>"
        "</tr>"
        f"{sub_row}"
        "</table>"
    )


def _team_section(label: str, color: str, players: list, is_you: bool,
                  my_team: int, players_db: dict, self_id: Optional[str] = None,
                  mmr_db: Optional[dict] = None, mmr_category: str = "best",
                  mmr_enabled: bool = False) -> str:
    # Both teams get their actual ColorPrimary from the wire. The YOU tag lives on
    # the player row (in _player_row), not duplicated on the section header.
    bar_color = color
    label_color = C_TEXT if is_you else C_DIM

    header = (
        "<table width='100%' cellspacing='0' cellpadding='0' "
        "style='border-collapse:collapse;margin:0;'>"
        "<tr>"
        f"<td width='3' bgcolor='{bar_color}' style='font-size:1px;line-height:1px;'>&nbsp;</td>"
        "<td width='8' style='font-size:1px;'>&nbsp;</td>"
        f"<td align='left' style='color:{label_color};font-size:9pt;font-weight:700;"
        f"letter-spacing:0.18em;'>{label}</td>"
        "</tr>"
        "</table>"
    )

    if players:
        body = "".join(
            _player_row(p, my_team, players_db, self_id,
                        mmr_db=mmr_db, mmr_category=mmr_category,
                        mmr_enabled=mmr_enabled)
            for p in players
        )
        body_block = (
            "<table width='100%' cellspacing='0' cellpadding='0' "
            "style='border-collapse:collapse;'>"
            "<tr>"
            "<td width='11' style='font-size:1px;'>&nbsp;</td>"
            f"<td>{body}</td>"
            "</tr>"
            "</table>"
        )
    else:
        body_block = (
            f"<div style='color:{C_MUTED};font-size:9pt;"
            "padding:2px 0 0 11px;'>&mdash;</div>"
        )

    return header + "<div style='height:4px;font-size:1px;line-height:1px;'>&nbsp;</div>" + body_block


def render_html(roster: list[dict], my_team: int, arena: str,
                players_db: dict, team_colors: dict,
                self_id: Optional[str] = None,
                mmr_db: Optional[dict] = None, mmr_category: str = "best",
                mmr_enabled: bool = False) -> str:
    blue = sorted([p for p in roster if p["team"] == 0], key=lambda x: x["name"].lower())
    orange = sorted([p for p in roster if p["team"] == 1], key=lambda x: x["name"].lower())

    blue_color = team_colors.get(0) if isinstance(team_colors, dict) else None
    orange_color = team_colors.get(1) if isinstance(team_colors, dict) else None
    if not isinstance(blue_color, str) or not blue_color.startswith("#"):
        blue_color = C_BLUE
    if not isinstance(orange_color, str) or not orange_color.startswith("#"):
        orange_color = C_ORANGE

    arena_label = pretty_arena(arena)
    # When MMR is enabled, the right cell shows the active category pill
    # ("MMR · BEST", "MMR · 2V2") so the user always sees what F10 is set to.
    # We show arena on a second header row so neither piece of info is lost.
    if mmr_enabled:
        cat = (mmr_category or "best").upper()
        right_cell = (
            f"<td align='right' style='color:{C_DIM};font-size:8pt;font-weight:700;"
            f"letter-spacing:0.18em;white-space:nowrap;'>"
            f"<span style='color:{C_MUTED};font-weight:500;'>MMR&nbsp;·&nbsp;</span>"
            f"{cat}"
            "</td>"
        )
    elif arena_label:
        a = arena_label if len(arena_label) <= 22 else arena_label[:21] + "…"
        right_cell = (
            f"<td align='right' style='color:{C_MUTED};font-size:8pt;font-weight:500;"
            f"letter-spacing:0.10em;'>{a.upper()}</td>"
        )
    else:
        right_cell = "<td></td>"

    arena_subline = ""
    if mmr_enabled and arena_label:
        a = arena_label if len(arena_label) <= 22 else arena_label[:21] + "…"
        arena_subline = (
            f"<div style='color:{C_MUTED};font-size:8pt;font-weight:500;"
            f"letter-spacing:0.10em;text-align:right;padding-top:1px;'>"
            f"{a.upper()}</div>"
        )

    header = (
        "<table width='100%' cellspacing='0' cellpadding='0' "
        "style='border-collapse:collapse;'>"
        "<tr>"
        f"<td align='left' style='color:{C_TEXT};font-size:10pt;font-weight:700;"
        "letter-spacing:0.18em;'>HEAD&middot;TO&middot;HEAD</td>"
        f"{right_cell}"
        "</tr>"
        "</table>"
        f"{arena_subline}"
        f"<div style='height:1px;background-color:{C_FAINT};font-size:1px;line-height:1px;"
        "margin-top:8px;'>&nbsp;</div>"
        "<div style='height:10px;font-size:1px;line-height:1px;'>&nbsp;</div>"
    )
    spacer = "<div style='height:10px;font-size:1px;line-height:1px;'>&nbsp;</div>"
    return (
        header
        + _team_section("BLUE",   blue_color,   blue,   my_team == 0, my_team, players_db, self_id,
                        mmr_db=mmr_db, mmr_category=mmr_category, mmr_enabled=mmr_enabled)
        + spacer
        + _team_section("ORANGE", orange_color, orange, my_team == 1, my_team, players_db, self_id,
                        mmr_db=mmr_db, mmr_category=mmr_category, mmr_enabled=mmr_enabled)
    )


class SessionStats:
    """Aggregates stats from script start until kill. Updated from StatsClient signals."""

    def __init__(self, recent_size: int = 5):
        self._recent_size = max(1, int(recent_size))
        self.self_name: Optional[str] = None
        self.reset()

    def reset(self) -> None:
        # Identity (self_name) is preserved across resets; counters start fresh.
        self.started_at = datetime.now(timezone.utc)
        self.matches = 0
        self.wins = 0
        self.losses = 0
        self.win_streak = 0
        self.loss_streak = 0
        self.best_win_streak = 0
        self.recent: deque = deque(maxlen=self._recent_size)  # session-only W/L
        self.goals_for = 0
        self.goals_against = 0
        self.crossbars = 0
        # Each "max" is a pair: session-wide (anyone) and self-only.
        self.max_goal_speed = 0.0
        self.max_goal_speed_self = 0.0
        self.max_ball_speed = 0.0
        self.max_ball_speed_self = 0.0
        self.max_impact_force = 0.0
        self.max_impact_force_self = 0.0
        self.fastest_goal_time: Optional[float] = None
        self.fastest_goal_time_self: Optional[float] = None
        self.own_goals = 0
        self.own_goals_self = 0
        self.statfeed_counts: dict[str, int] = {}
        self.statfeed_counts_self: dict[str, int] = {}

    def _is_self(self, name: Optional[str]) -> bool:
        return bool(name) and name == self.self_name

    def on_event(self, event: str, data: dict) -> None:
        if event == EVT_GOAL_SCORED:
            scorer = data.get("Scorer")
            scorer_name = scorer.get("Name") if isinstance(scorer, dict) else None
            if data.get("bOwnGoal"):
                self.own_goals += 1
                last_name, _ = _last_touch_player(data)
                if self._is_self(last_name):
                    self.own_goals_self += 1
            sp = data.get("GoalSpeed")
            if isinstance(sp, (int, float)):
                if sp > self.max_goal_speed:
                    self.max_goal_speed = float(sp)
                if self._is_self(scorer_name) and sp > self.max_goal_speed_self:
                    self.max_goal_speed_self = float(sp)
            gt = data.get("GoalTime")
            # Filter out 0 — it's an API artifact (e.g. first goal of a session, or
            # instant goals) that would otherwise pin "fastest goal" to 0 forever.
            if isinstance(gt, (int, float)) and gt > 0:
                if self.fastest_goal_time is None or gt < self.fastest_goal_time:
                    self.fastest_goal_time = float(gt)
                if self._is_self(scorer_name):
                    if self.fastest_goal_time_self is None or gt < self.fastest_goal_time_self:
                        self.fastest_goal_time_self = float(gt)
        elif event == EVT_CROSSBAR_HIT:
            self.crossbars += 1
            last_touch = data.get("BallLastTouch")
            toucher_name = None
            if isinstance(last_touch, dict):
                p = last_touch.get("Player")
                if isinstance(p, dict):
                    toucher_name = p.get("Name")
            ifo = data.get("ImpactForce")
            if isinstance(ifo, (int, float)):
                if ifo > self.max_impact_force:
                    self.max_impact_force = float(ifo)
                if self._is_self(toucher_name) and ifo > self.max_impact_force_self:
                    self.max_impact_force_self = float(ifo)
        elif event == EVT_BALL_HIT:
            ball = data.get("Ball")
            sp = ball.get("PostHitSpeed") if isinstance(ball, dict) else None
            if isinstance(sp, (int, float)):
                if sp > self.max_ball_speed:
                    self.max_ball_speed = float(sp)
                # BallHit can have multiple players in the same frame — count if any is self.
                hitters = data.get("Players")
                if isinstance(hitters, list):
                    if any(self._is_self(h.get("Name")) for h in hitters if isinstance(h, dict)):
                        if sp > self.max_ball_speed_self:
                            self.max_ball_speed_self = float(sp)
        elif event == EVT_STATFEED:
            ev_name = data.get("EventName")
            if isinstance(ev_name, str) and ev_name:
                self.statfeed_counts[ev_name] = self.statfeed_counts.get(ev_name, 0) + 1
                main = data.get("MainTarget")
                if isinstance(main, dict) and self._is_self(main.get("Name")):
                    self.statfeed_counts_self[ev_name] = self.statfeed_counts_self.get(ev_name, 0) + 1

    def on_match_ended(self, payload: dict) -> None:
        self.matches += 1
        i_won = payload["winner"] == payload["myTeam"]
        if i_won:
            self.wins += 1
            self.win_streak += 1
            self.loss_streak = 0
            self.best_win_streak = max(self.best_win_streak, self.win_streak)
        else:
            self.losses += 1
            self.loss_streak += 1
            self.win_streak = 0
        self.recent.append("W" if i_won else "L")
        score = payload.get("score")
        if isinstance(score, list) and len(score) == 2:
            mt = payload["myTeam"]
            self.goals_for += score[mt]
            self.goals_against += score[1 - mt]


def _stat_row(label: str, value: str, accent: Optional[str] = None) -> str:
    val_color = accent or C_TEXT
    return (
        "<table width='100%' cellspacing='0' cellpadding='0' "
        "style='border-collapse:collapse;margin:0;'>"
        "<tr>"
        f"<td align='left' style='color:{C_DIM};font-size:9pt;padding:2px 0 2px 0;'>{label}</td>"
        f"<td align='right' style='color:{val_color};font-size:10pt;font-weight:600;"
        "font-family:Consolas,\"SF Mono\",monospace;"
        f"padding:2px 0 2px 0;white-space:nowrap;'>{value}</td>"
        "</tr>"
        "</table>"
    )


def _stat_section(title: str, rows: list[str]) -> str:
    header = (
        f"<div style='color:{C_DIM};font-size:9pt;font-weight:700;letter-spacing:0.16em;"
        "margin-top:6px;'>" + title + "</div>"
        f"<div style='height:1px;background-color:{C_FAINT};font-size:1px;line-height:1px;"
        "margin-top:4px;margin-bottom:4px;'>&nbsp;</div>"
    )
    return header + "".join(rows)


EM_DASH = f"<span style='color:{C_MUTED};'>—</span>"
_PAIR_SEP = f"<span style='color:{C_MUTED};font-weight:500;'>&nbsp;|&nbsp;</span>"


def _opt_int(v) -> str:
    return str(int(v)) if v else EM_DASH


def _pair_max(scope_v: float, self_v: float, always_pair: bool = False) -> str:
    """Render a max-style or counter stat as 'scope | yours'. Scope >= self_v always.

    If you own the scope max the pair collapses to a single number to keep the
    session card compact. Pass `always_pair=True` (used by the post-match
    summary) to always show both values — clearer when the user explicitly
    wants 'match | mine' for every row."""
    if scope_v <= 0:
        return EM_DASH
    if not always_pair and self_v >= scope_v:
        return str(int(scope_v))
    right = str(int(self_v)) if self_v > 0 else EM_DASH
    return f"{int(scope_v)}{_PAIR_SEP}{right}"


# Counters (saves/shots/demos/crossbars) and maxes (speeds/forces) render the
# same way — same int formatting, same collapse-when-you-own-it rule. Alias.
_pair_count = _pair_max


def _pair_fastest(scope_v: Optional[float], self_v: Optional[float], always_pair: bool = False) -> str:
    """Render a min-style stat (fastest goal). Smaller is better.
    See `_pair_max` for the `always_pair` flag."""
    if not scope_v:
        return EM_DASH
    if not always_pair and self_v is not None and self_v <= scope_v:
        return f"{scope_v:.1f}s"
    right = f"{self_v:.1f}s" if self_v else EM_DASH
    return f"{scope_v:.1f}s{_PAIR_SEP}{right}"


def apply_overrides(cfg: dict) -> None:
    """Patch palette + truncation globals from cfg. Called once at startup.

    Color constants are referenced directly inside render functions, so the cleanest
    way to honor user overrides is to mutate the module globals before any rendering
    happens. EM_DASH / _PAIR_SEP also need to be recomputed because they bake C_MUTED
    in at import time.
    """
    global C_TEXT, C_DIM, C_MUTED, C_FAINT, C_BLUE, C_ORANGE, C_WIN, C_LOSS
    global EM_DASH, _PAIR_SEP, NAME_MAX_LEN
    palette = cfg.get("colors") or {}
    if isinstance(cfg.get("text_color"), str):
        C_TEXT = cfg["text_color"]
    if isinstance(palette.get("dim"), str):
        C_DIM = palette["dim"]
    if isinstance(palette.get("muted"), str):
        C_MUTED = palette["muted"]
    if isinstance(palette.get("faint"), str):
        C_FAINT = palette["faint"]
    if isinstance(palette.get("win"), str):
        C_WIN = palette["win"]
    if isinstance(palette.get("loss"), str):
        C_LOSS = palette["loss"]
    if isinstance(palette.get("team_blue_fallback"), str):
        C_BLUE = palette["team_blue_fallback"]
    if isinstance(palette.get("team_orange_fallback"), str):
        C_ORANGE = palette["team_orange_fallback"]
    EM_DASH = f"<span style='color:{C_MUTED};'>—</span>"
    _PAIR_SEP = f"<span style='color:{C_MUTED};font-weight:500;'>&nbsp;|&nbsp;</span>"
    nml = cfg.get("name_max_length")
    if isinstance(nml, int) and nml > 0:
        NAME_MAX_LEN = nml


def render_session_html(s: SessionStats, with_legend: bool = True) -> str:
    elapsed = int((datetime.now(timezone.utc) - s.started_at).total_seconds())
    h, rem = divmod(elapsed, 3600)
    m = rem // 60
    duration = f"{h}h {m:02d}m" if h else f"{m} min"

    win_pct = (s.wins / s.matches * 100.0) if s.matches else 0.0
    matches_val = (
        f"<b>{s.wins}</b><span style='color:{C_MUTED};'>&ndash;</span>"
        f"<b>{s.losses}</b>"
        f" <span style='color:{C_MUTED};font-weight:500;'>({win_pct:.0f}%)</span>"
        if s.matches
        else f"<span style='color:{C_MUTED};'>—</span>"
    )

    if s.win_streak >= 2:
        streak_val = f"<span style='color:{C_WIN};font-weight:700;'>W{s.win_streak}</span>"
    elif s.loss_streak >= 2:
        streak_val = f"<span style='color:{C_LOSS};font-weight:700;'>L{s.loss_streak}</span>"
    else:
        streak_val = f"<span style='color:{C_MUTED};'>—</span>"

    goals_val = (
        f"{s.goals_for}<span style='color:{C_MUTED};'>&ndash;</span>{s.goals_against}"
        if s.matches else f"<span style='color:{C_MUTED};'>—</span>"
    )
    diff = s.goals_for - s.goals_against
    if diff != 0:
        sign = "+" if diff > 0 else ""
        diff_color = C_WIN if diff > 0 else C_LOSS
        goals_val += f" <span style='color:{diff_color};font-size:9pt;'>{sign}{diff}</span>"

    saves_t  = s.statfeed_counts.get(SF_SAVE, 0)
    shots_t  = s.statfeed_counts.get(SF_SHOT, 0)
    demos_t  = s.statfeed_counts.get(SF_DEMOLISH, 0)
    saves_s  = s.statfeed_counts_self.get(SF_SAVE, 0)
    shots_s  = s.statfeed_counts_self.get(SF_SHOT, 0)
    demos_s  = s.statfeed_counts_self.get(SF_DEMOLISH, 0)

    overall_rows = [
        _stat_row("Matches", matches_val),
        _stat_row("Goals", goals_val),
        _stat_row("Streak", streak_val),
        _stat_row(
            "Best run",
            (f"<span style='color:{C_WIN};font-weight:700;'>W{s.best_win_streak}</span>"
             if s.best_win_streak else EM_DASH),
        ),
        _stat_row("Recent", _recent_pips(s.recent) if s.recent else EM_DASH),
    ]
    play_rows = [
        _stat_row("Saves",     _pair_count(saves_t, saves_s)),
        _stat_row("Shots",     _pair_count(shots_t, shots_s)),
        _stat_row("Demos",     _pair_count(demos_t, demos_s)),
        _stat_row("Crossbars", _opt_int(s.crossbars)),
    ]
    fun_rows = [
        _stat_row("Max goal speed",   _pair_max(s.max_goal_speed, s.max_goal_speed_self)),
        _stat_row("Max ball speed",   _pair_max(s.max_ball_speed, s.max_ball_speed_self)),
        _stat_row("Hardest crossbar", _pair_max(s.max_impact_force, s.max_impact_force_self)),
        _stat_row("Fastest goal",     _pair_fastest(s.fastest_goal_time, s.fastest_goal_time_self)),
        _stat_row("Own goals",        _pair_count(s.own_goals, s.own_goals_self)),
    ]

    header = (
        "<table width='100%' cellspacing='0' cellpadding='0' "
        "style='border-collapse:collapse;'>"
        "<tr>"
        f"<td align='left' style='color:{C_TEXT};font-size:10pt;font-weight:700;"
        "letter-spacing:0.18em;'>SESSION</td>"
        f"<td align='right' style='color:{C_MUTED};font-size:8pt;font-weight:500;"
        f"letter-spacing:0.10em;'>{duration.upper()}</td>"
        "</tr>"
        "</table>"
        f"<div style='height:1px;background-color:{C_FAINT};font-size:1px;line-height:1px;"
        "margin-top:8px;'>&nbsp;</div>"
        "<div style='height:6px;font-size:1px;line-height:1px;'>&nbsp;</div>"
    )
    legend = ""
    if with_legend and _session_has_split(s):
        legend = (
            "<table cellpadding='0' cellspacing='0' width='100%'>"
            "<tr><td height='10'>&nbsp;</td></tr></table>"
            "<table width='100%' cellspacing='0' cellpadding='0' "
            "style='border-collapse:collapse;'>"
            "<tr>"
            f"<td align='left'  style='color:{C_MUTED};font-size:8pt;"
            "letter-spacing:0.02em;'>Format</td>"
            f"<td align='right' style='color:{C_MUTED};font-size:8pt;"
            "letter-spacing:0.02em;'><b>session</b> | yours</td>"
            "</tr></table>"
        )
    return (
        header
        + _stat_section("OVERALL", overall_rows)
        + _stat_section("PLAY",    play_rows)
        + _stat_section("FUN",     fun_rows)
        + legend
    )


class MatchStats:
    """Per-match aggregator. Reset on match_initialized, snapshot on match_ended.

    Every metric tracks both the match-wide value (anyone) and the self-only value,
    so the post-match summary can render 'match | yours' pairs.
    """

    def __init__(self, self_name: Optional[str] = None):
        self.reset(self_name)

    def reset(self, self_name: Optional[str] = None) -> None:
        self.self_name = self_name
        # Counters (statfeed-driven and crossbar event)
        self.saves = 0
        self.saves_self = 0
        self.shots = 0
        self.shots_self = 0
        self.demos = 0           # demolitions given, anyone
        self.demos_self = 0      # demolitions given by you
        self.demoed_self = 0     # times you got demolished (no match-wide pair — use total)
        self.crossbars = 0
        self.crossbars_self = 0
        # Maxes
        self.max_goal_speed = 0.0
        self.max_goal_speed_self = 0.0
        self.max_ball_speed = 0.0
        self.max_ball_speed_self = 0.0
        self.max_impact_force = 0.0
        self.max_impact_force_self = 0.0
        # Min (fastest goal time in seconds; None = no goal yet)
        self.fastest_goal_time: Optional[float] = None
        self.fastest_goal_time_self: Optional[float] = None
        self.own_goals = 0
        self.own_goals_self = 0

    def _is_self(self, name) -> bool:
        return bool(name) and name == self.self_name

    def on_event(self, event: str, data: dict) -> None:
        if event == EVT_GOAL_SCORED:
            scorer = data.get("Scorer")
            scorer_name = scorer.get("Name") if isinstance(scorer, dict) else None
            if data.get("bOwnGoal"):
                self.own_goals += 1
                last_name, _ = _last_touch_player(data)
                if self._is_self(last_name):
                    self.own_goals_self += 1
            sp = data.get("GoalSpeed")
            if isinstance(sp, (int, float)):
                self.max_goal_speed = max(self.max_goal_speed, float(sp))
                if self._is_self(scorer_name):
                    self.max_goal_speed_self = max(self.max_goal_speed_self, float(sp))
            gt = data.get("GoalTime")
            if isinstance(gt, (int, float)) and gt > 0:
                if self.fastest_goal_time is None or gt < self.fastest_goal_time:
                    self.fastest_goal_time = float(gt)
                if self._is_self(scorer_name):
                    if self.fastest_goal_time_self is None or gt < self.fastest_goal_time_self:
                        self.fastest_goal_time_self = float(gt)
        elif event == EVT_BALL_HIT:
            ball = data.get("Ball")
            sp = ball.get("PostHitSpeed") if isinstance(ball, dict) else None
            if isinstance(sp, (int, float)):
                self.max_ball_speed = max(self.max_ball_speed, float(sp))
                hitters = data.get("Players")
                if isinstance(hitters, list) and any(
                    self._is_self(h.get("Name")) for h in hitters if isinstance(h, dict)
                ):
                    self.max_ball_speed_self = max(self.max_ball_speed_self, float(sp))
        elif event == EVT_CROSSBAR_HIT:
            self.crossbars += 1
            last_touch = data.get("BallLastTouch")
            toucher = None
            if isinstance(last_touch, dict):
                player = last_touch.get("Player")
                if isinstance(player, dict):
                    toucher = player.get("Name")
            if self._is_self(toucher):
                self.crossbars_self += 1
            ifo = data.get("ImpactForce")
            if isinstance(ifo, (int, float)):
                self.max_impact_force = max(self.max_impact_force, float(ifo))
                if self._is_self(toucher):
                    self.max_impact_force_self = max(self.max_impact_force_self, float(ifo))
        elif event == EVT_STATFEED:
            ev = data.get("EventName")
            main = data.get("MainTarget")
            sec = data.get("SecondaryTarget")
            main_name = main.get("Name") if isinstance(main, dict) else None
            sec_name = sec.get("Name") if isinstance(sec, dict) else None
            if ev == SF_SAVE:
                self.saves += 1
                if self._is_self(main_name):
                    self.saves_self += 1
            elif ev == SF_SHOT:
                self.shots += 1
                if self._is_self(main_name):
                    self.shots_self += 1
            elif ev == SF_DEMOLISH:
                self.demos += 1
                if self._is_self(main_name):
                    self.demos_self += 1
                if self._is_self(sec_name):
                    self.demoed_self += 1


def _first_keyboard_label(keys: list) -> Optional[str]:
    """Pick the friendliest key label from a hotkeys list — prefer keyboard names
    over gamepad bindings (more recognizable in a footer hint)."""
    if not keys:
        return None
    for k in keys:
        if not k.startswith("pad_"):
            return k.upper()
    # All gamepad — render the first as-is.
    return keys[0]


def _session_has_split(s: SessionStats) -> bool:
    """True when at least one stat shows the session-vs-self split, so the
    'Format' legend is informative rather than noise."""
    return (
        s.max_goal_speed > s.max_goal_speed_self
        or s.max_ball_speed > s.max_ball_speed_self
        or s.max_impact_force > s.max_impact_force_self
        or s.own_goals > s.own_goals_self
        or s.statfeed_counts.get(SF_SAVE, 0) > s.statfeed_counts_self.get(SF_SAVE, 0)
        or s.statfeed_counts.get(SF_SHOT, 0) > s.statfeed_counts_self.get(SF_SHOT, 0)
        or s.statfeed_counts.get(SF_DEMOLISH, 0) > s.statfeed_counts_self.get(SF_DEMOLISH, 0)
    )


def _session_footer_html(cfg: dict, view: str) -> str:
    """Hotkey hint row for the session card. View-specific copy: the session
    view advertises F11 to swap to graph; the graph view doesn't reach here
    because it's painted onto a pixmap, not HTML."""
    expand_label = _first_keyboard_label(cfg.get("expand_hotkeys") or [])
    if view != "session" or not expand_label:
        return ""
    return (
        "<table cellpadding='0' cellspacing='0' width='100%'>"
        "<tr><td height='10'>&nbsp;</td></tr></table>"
        "<table width='100%' cellspacing='0' cellpadding='0' "
        "style='border-collapse:collapse;'>"
        "<tr>"
        f"<td align='left' style='color:{C_MUTED};font-size:8pt;"
        f"letter-spacing:0.02em;padding:1px 0;'><b>{expand_label}</b> graph</td>"
        f"<td align='right' style='color:{C_MUTED};font-size:8pt;"
        f"letter-spacing:0.02em;padding:1px 0;'>session</td>"
        "</tr>"
        "</table>"
    )


def _h2h_footer_html(cfg: dict, expanded: bool, session: Optional[SessionStats]) -> str:
    """Single-table footer for the H2H overlay.

    - Always: hotkey hint row (`F11 expand` left, `F12 session` right).
    - When MMR is enabled: a second hotkey row with `F10 cycle MMR` and the
      current category (e.g. "best", "2v2") so the user sees what's selected.
    - When `session` is supplied AND has a split: a `Format | session | yours` row.

    Rows live in the same <table>, so Qt RichText doesn't insert its native
    ~12-20px between-block margin between them.
    """
    expand_label = _first_keyboard_label(cfg.get("expand_hotkeys") or [])
    session_label = _first_keyboard_label(cfg.get("session_hotkeys") or [])
    cycle_label = _first_keyboard_label(cfg.get("cycle_hotkeys") or [])
    mmr_enabled = bool(cfg.get("mmr_enabled", False))
    mmr_category = cfg.get("mmr_category", "best")

    rows: list[str] = []
    cell = (
        "<td align='{align}' style='color:{C_MUTED};font-size:8pt;"
        "letter-spacing:0.02em;padding:1px 0;'>{content}</td>"
    )

    # Format row first — explanatory info above the actionable hotkey row.
    if session is not None and _session_has_split(session):
        rows.append(
            "<tr>"
            + cell.format(align="left",  C_MUTED=C_MUTED, content="Format")
            + cell.format(align="right", C_MUTED=C_MUTED,
                          content="<b>session</b> | yours")
            + "</tr>"
        )

    if mmr_enabled and cycle_label:
        rows.append(
            "<tr>"
            + cell.format(align="left",  C_MUTED=C_MUTED,
                          content=f"<b>{cycle_label}</b> cycle MMR")
            + cell.format(align="right", C_MUTED=C_MUTED,
                          content=f"<b>{mmr_category}</b>")
            + "</tr>"
        )

    h_left = ""
    h_right = ""
    if expand_label:
        verb = "collapse" if expanded else "expand"
        h_left = f"<b>{expand_label}</b> {verb}"
    if session_label:
        h_right = f"<b>{session_label}</b> session"
    if h_left or h_right:
        rows.append(
            "<tr>"
            + cell.format(align="left",  C_MUTED=C_MUTED, content=h_left)
            + cell.format(align="right", C_MUTED=C_MUTED, content=h_right)
            + "</tr>"
        )

    if not rows:
        return ""

    return (
        # 12px gap between the body content and the footer.
        "<table cellpadding='0' cellspacing='0' width='100%'>"
        "<tr><td height='12'>&nbsp;</td></tr></table>"
        "<table width='100%' cellspacing='0' cellpadding='0' "
        "style='border-collapse:collapse;'>"
        + "".join(rows)
        + "</table>"
    )


def make_tray_icon() -> QIcon:
    """Tray icon. Prefers icon.ico/icon.png next to the script; falls back to a
    programmatic dark-circle-with-lime-H if neither is present."""
    for name in ("icon.ico", "icon.png"):
        path = APP_DIR / name
        if path.exists():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
    pix = QPixmap(32, 32)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#101415"))
    p.setPen(QPen(QColor(C_WIN), 2))
    p.drawEllipse(2, 2, 28, 28)
    p.setPen(QColor(C_WIN))
    p.setFont(QFont("Segoe UI", 13, QFont.Bold))
    p.drawText(pix.rect(), Qt.AlignCenter, "H")
    p.end()
    return QIcon(pix)


def render_summary_html(payload: dict, ms: "MatchStats") -> str:
    """Auto-popup card flashed for ~5s when a match ends. Result + score + per-match stats."""
    my_team = payload.get("myTeam")
    winner = payload.get("winner")
    i_won = winner == my_team
    label = "WIN" if i_won else "LOSS"
    accent = C_WIN if i_won else C_LOSS

    score = payload.get("score")
    if isinstance(score, list) and len(score) == 2 and isinstance(my_team, int):
        score_html = (
            f"<span style='font-family:Consolas,\"SF Mono\",monospace;font-size:14pt;"
            f"font-weight:700;color:{C_TEXT};'>"
            f"{score[my_team]}<span style='color:{C_MUTED};'>&ndash;</span>"
            f"{score[1 - my_team]}</span>"
        )
    else:
        score_html = ""

    header = (
        "<table width='100%' cellspacing='0' cellpadding='0' "
        "style='border-collapse:collapse;'>"
        "<tr>"
        f"<td align='left' style='color:{accent};font-size:14pt;font-weight:700;"
        f"letter-spacing:0.18em;'>{label}</td>"
        f"<td align='right'>{score_html}</td>"
        "</tr>"
        "</table>"
    )

    # PLAY rows: hide entirely when both match-wide and self are 0.
    play_rows = []
    if ms.saves:     play_rows.append(_stat_row("Saves",     _pair_count(ms.saves, ms.saves_self, always_pair=True)))
    if ms.shots:     play_rows.append(_stat_row("Shots",     _pair_count(ms.shots, ms.shots_self, always_pair=True)))
    if ms.demos:     play_rows.append(_stat_row("Demos",     _pair_count(ms.demos, ms.demos_self, always_pair=True)))
    if ms.demoed_self:
        play_rows.append(_stat_row("Demoed",    str(ms.demoed_self)))
    if ms.crossbars: play_rows.append(_stat_row("Crossbars", _pair_count(ms.crossbars, ms.crossbars_self, always_pair=True)))

    fun_rows = []
    if ms.max_goal_speed > 0:
        fun_rows.append(_stat_row("Max goal speed",   _pair_max(ms.max_goal_speed, ms.max_goal_speed_self, always_pair=True)))
    if ms.max_ball_speed > 0:
        fun_rows.append(_stat_row("Max ball speed",   _pair_max(ms.max_ball_speed, ms.max_ball_speed_self, always_pair=True)))
    if ms.max_impact_force > 0:
        fun_rows.append(_stat_row("Hardest crossbar",
                                  _pair_max(ms.max_impact_force, ms.max_impact_force_self, always_pair=True)))
    if ms.fastest_goal_time is not None:
        fun_rows.append(_stat_row("Fastest goal",
                                  _pair_fastest(ms.fastest_goal_time, ms.fastest_goal_time_self, always_pair=True)))
    if ms.own_goals > 0:
        fun_rows.append(_stat_row("Own goals",
                                  _pair_count(ms.own_goals, ms.own_goals_self, always_pair=True)))

    body = ""
    if play_rows:
        body += _stat_section("PLAY", play_rows)
    if fun_rows:
        body += _stat_section("FUN", fun_rows)
    if body:
        divider = (
            f"<div style='height:1px;background-color:{C_FAINT};font-size:1px;line-height:1px;"
            "margin-top:8px;'>&nbsp;</div>"
            "<div style='height:6px;font-size:1px;line-height:1px;'>&nbsp;</div>"
        )
        return header + divider + body
    return header


# ---- MMR graph view ---------------------------------------------------------
# Renders an evolution graph of the user's own MMR for one playlist into a
# QPixmap (Qt RichText doesn't support data-URL <img>, so we paint directly
# and feed the pixmap to QLabel.setPixmap). Per-game attribution is derived
# from cumulative TRN snapshots — see attribute_mmr_points() for the math.

# Tier MMR ranges (RL Season 36 ranges, approximate). Anything below the
# bottom is Bronze; anything above the top is SSL.
MMR_RANK_ZONES = [
    (0,    195,  "Bronze",            "#B87333"),
    (195,  395,  "Silver",             "#C0C5CD"),
    (395,  595,  "Gold",               "#F0C674"),
    (595,  795,  "Platinum",           "#6FC8D6"),
    (795,  995,  "Diamond",            "#7FA9F2"),
    (995,  1195, "Champion",           "#B59CEE"),
    (1195, 1565, "Grand Champion",     "#EC4F50"),
    (1565, 2500, "Supersonic Legend",  "#DB2C70"),
]


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def attribute_mmr_points(playlist: str, snapshots: list[dict],
                         matches: list[dict],
                         grace_seconds: int = 120,
                         window: int = 30) -> list[dict]:
    """Walk consecutive snapshot pairs and attribute the cumulative MMR delta
    in each interval to the matches that ended within it.

    Per-game step is *derived* from the data:
      - When W != L:  step = abs(D) / abs(W - L)  (signed by outcome)
      - When W == L:  use the rolling median of past intervals' steps; if no
                      such history exists yet, bootstrap to 10
      - When W + L == 0:  the user played outside our session — plot the
                          snapshot transition as a single "snap" point and
                          move on, no per-game attribution

    Output: list of {"x": iso_ts, "mmr": int, "marker": "W"|"L"|"snap"} in
    chronological order, capped to the last `window` items.
    """
    points: list[dict] = []
    if not snapshots:
        return points
    # Filter snapshots that have data for this playlist; we anchor on the
    # first one and walk from there.
    relevant_snaps = [s for s in snapshots if (s.get("playlists") or {}).get(playlist) is not None]
    if not relevant_snaps:
        return points

    pl_matches_sorted = sorted(
        [m for m in matches if _match_playlist(m) == playlist
         and isinstance(m.get("endedAt"), str)],
        key=lambda m: m["endedAt"],
    )

    s0 = relevant_snaps[0]
    cumulative = (s0.get("playlists") or {}).get(playlist)
    points.append({"x": s0.get("ts"), "mmr": cumulative, "marker": "snap"})

    past_steps: list[float] = []
    grace = grace_seconds

    for s_prev, s_cur in zip(relevant_snaps, relevant_snaps[1:]):
        mmr_prev = (s_prev.get("playlists") or {}).get(playlist)
        mmr_cur = (s_cur.get("playlists") or {}).get(playlist)
        if mmr_prev is None or mmr_cur is None:
            cumulative = mmr_cur if mmr_cur is not None else cumulative
            points.append({"x": s_cur.get("ts"), "mmr": cumulative, "marker": "snap"})
            continue
        delta = mmr_cur - mmr_prev

        t_prev = _parse_iso(s_prev.get("ts"))
        t_cur = _parse_iso(s_cur.get("ts"))
        if t_prev is None or t_cur is None:
            cumulative = mmr_cur
            points.append({"x": s_cur.get("ts"), "mmr": cumulative, "marker": "snap"})
            continue
        # Grace tail: a match that ended a few seconds before the snapshot's
        # timestamp would otherwise be missed if our wall clock and TRN's
        # clock skew, or if Psyonix→TRN propagation is slower than our poll.
        cutoff = t_cur + timedelta(seconds=grace)
        interval_matches = []
        for m in pl_matches_sorted:
            t_m = _parse_iso(m.get("endedAt"))
            if t_m is None:
                continue
            if t_prev <= t_m <= cutoff:
                interval_matches.append(m)

        if not interval_matches:
            if delta == 0:
                # Idle interval for this playlist — no MMR motion and no
                # match in this mode. Skip; emitting a flat snap here is
                # what made the graph draw a horizontal line across days
                # of 2v2-only play when looking at the 3v3 graph.
                continue
            # MMR moved without a recorded match — user played in another
            # session or outside our tracking. Plot the snapshot transition
            # only; no per-game line.
            cumulative = mmr_cur
            points.append({"x": s_cur.get("ts"), "mmr": cumulative, "marker": "snap"})
            continue

        wins = sum(1 for m in interval_matches if m.get("result") == "W")
        losses = len(interval_matches) - wins

        if wins != losses:
            step = abs(delta) / abs(wins - losses)
            past_steps.append(step)
        else:
            # No signal in this interval (net-zero). Use the median of
            # previous derived steps; bootstrap to 10 only if we have nothing.
            if past_steps:
                sorted_steps = sorted(past_steps)
                step = sorted_steps[len(sorted_steps) // 2]
            else:
                step = 10.0

        for m in interval_matches:
            sign = 1 if m.get("result") == "W" else -1
            cumulative = cumulative + sign * step
            points.append({
                "x": m.get("endedAt"),
                "mmr": int(round(cumulative)),
                "marker": m.get("result") or "snap",
            })

        # Reconcile rounding/W==L mismatches by snapping the last point in
        # this interval to the actually-observed MMR. Keeps the line on
        # truth at every snapshot boundary.
        if points[-1]["mmr"] != mmr_cur:
            points[-1] = {**points[-1], "mmr": mmr_cur}
        cumulative = mmr_cur

    return points[-window:] if window > 0 else points


# 4px baseline grid; 4px corner radius matches the design's "soft 0.25rem".
_GRAPH_HEADER_H = 24
_GRAPH_FOOTER_H = 28
_GRAPH_INSET_X = 4
_GRAPH_CHIP_RADIUS = 4

# Why module-level: render fires up to 4× per second from the focus_timer
# while F12 is held; recreating QFonts each tick allocates needlessly.
_GRAPH_MONO_FAMILIES = ["Consolas", "SF Mono", "DejaVu Sans Mono", "Menlo", "Courier New"]
_GRAPH_FONT_CACHE: dict[tuple, QFont] = {}


def _graph_font(family: str, size: int, bold: bool = False) -> QFont:
    key = ("plain", family, size, bold)
    f = _GRAPH_FONT_CACHE.get(key)
    if f is None:
        f = QFont(family, size)
        if bold:
            f.setBold(True)
        _GRAPH_FONT_CACHE[key] = f
    return f


def _graph_mono_font(size: int, bold: bool = False) -> QFont:
    key = ("mono", size, bold)
    f = _GRAPH_FONT_CACHE.get(key)
    if f is None:
        f = QFont()
        f.setFamilies(_GRAPH_MONO_FAMILIES)
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(size)
        if bold:
            f.setBold(True)
        _GRAPH_FONT_CACHE[key] = f
    return f


def _draw_marker(painter: "QPainter", x: int, y: int,
                 color: "QColor", radius: int) -> None:
    from PySide6.QtCore import Qt as QtNs
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QBrush
    painter.setPen(QtNs.NoPen)
    painter.setBrush(QBrush(color))
    painter.drawEllipse(QPoint(x, y), radius, radius)


def render_graph_pixmap(playlist: str, snapshots: list[dict],
                        matches: list[dict], cfg: dict,
                        canvas_width: int = 348, canvas_height: int = 220) -> "QPixmap":
    """Paint the MMR-evolution graph for `playlist` into a QPixmap (header,
    plot region with rank-zone bands and W/L markers, footer with hotkey
    hints). Caller writes the result via Overlay.set_pixmap()."""
    from PySide6.QtGui import QBrush, QColor, QPolygon, QPen
    from PySide6.QtCore import QPoint, Qt as QtNs

    grace = int(cfg.get("graph_match_grace_seconds", 120))
    window = int(cfg.get("graph_match_window", 30))
    points = attribute_mmr_points(playlist, snapshots, matches,
                                  grace_seconds=grace, window=window)

    pix = QPixmap(canvas_width, canvas_height)
    pix.fill(QColor(0, 0, 0, 0))  # QLabel stylesheet shows through
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.TextAntialiasing, True)

    font_family = cfg.get("font_family", "Segoe UI")
    title_font = _graph_font(font_family, 9, bold=True)
    small_font = _graph_font(font_family, 7)
    num_font_md = _graph_mono_font(8, bold=True)
    num_font_sm = _graph_mono_font(7)

    plot_top = _GRAPH_HEADER_H
    plot_bottom = canvas_height - _GRAPH_FOOTER_H
    plot_left = _GRAPH_INSET_X
    plot_right = canvas_width - _GRAPH_INSET_X
    plot_w = plot_right - plot_left
    plot_h = plot_bottom - plot_top

    painter.setFont(title_font)
    painter.setPen(QColor(C_TEXT))
    title = f"MMR · {playlist.upper()}"
    painter.drawText(plot_left, _GRAPH_HEADER_H - 8, title)

    if len(points) >= 2:
        delta = points[-1]["mmr"] - points[0]["mmr"]
        sign = "+" if delta > 0 else ""
        delta_text = f"{sign}{delta}"
        if delta > 0:
            pill_bg, pill_fg = QColor(C_WIN), QColor(C_PILL_TEXT_WIN)
        elif delta < 0:
            pill_bg, pill_fg = QColor(C_LOSS), QColor(C_PILL_TEXT_LOSS)
        else:
            pill_bg, pill_fg = QColor(C_FAINT), QColor(C_DIM)
        painter.setFont(num_font_md)
        fm = painter.fontMetrics()
        text_w = fm.horizontalAdvance(delta_text)
        pad_x, pad_y = 6, 2
        pill_w = text_w + pad_x * 2
        pill_h = fm.height() + pad_y
        pill_x = plot_right - pill_w
        pill_y = (_GRAPH_HEADER_H - pill_h) // 2
        painter.setPen(QtNs.NoPen)
        painter.setBrush(QBrush(pill_bg))
        painter.drawRoundedRect(pill_x, pill_y, pill_w, pill_h,
                                _GRAPH_CHIP_RADIUS, _GRAPH_CHIP_RADIUS)
        painter.setPen(QPen(pill_fg))
        text_baseline = pill_y + pill_h - pad_y - fm.descent() + 1
        painter.drawText(pill_x + pad_x, text_baseline, delta_text)

    sep_pen = QPen(QColor(C_FAINT))
    sep_pen.setWidth(1)
    painter.setPen(sep_pen)
    painter.drawLine(plot_left, _GRAPH_HEADER_H, plot_right, _GRAPH_HEADER_H)

    if len(points) < 2:
        painter.setFont(small_font)
        painter.setPen(QColor(C_MUTED))
        if not snapshots:
            msg = "MMR not tracked yet — enable in tray menu and play a match"
        elif not any((s.get("playlists") or {}).get(playlist) is not None
                     for s in snapshots):
            msg = f"No {playlist} MMR yet — play a ranked {playlist} match"
        elif not any(_match_playlist(m) == playlist for m in matches):
            msg = f"No {playlist} matches yet — play one to start the graph"
        else:
            msg = "Need at least 2 MMR snapshots — keep playing"
        fm = painter.fontMetrics()
        msg_w = fm.horizontalAdvance(msg)
        painter.drawText(
            plot_left + (plot_w - msg_w) // 2,
            plot_top + plot_h // 2,
            msg,
        )
        _draw_graph_footer(painter, cfg, playlist, plot_left, plot_right,
                           canvas_height, _GRAPH_FOOTER_H, small_font)
        painter.end()
        return pix

    mmr_values = [p["mmr"] for p in points if isinstance(p["mmr"], (int, float))]
    mmr_min = min(mmr_values)
    mmr_max = max(mmr_values)
    # Pad the y-range so points don't sit on the edge. Always show at least
    # 100 MMR of vertical span so single-game graphs aren't squashed.
    span = max(mmr_max - mmr_min, 100)
    pad = max(20, int(span * 0.15))
    y_min = max(0, mmr_min - pad)
    y_max = mmr_max + pad

    def to_x(i: int) -> int:
        if len(points) == 1:
            return plot_left + plot_w // 2
        return plot_left + int(i * plot_w / (len(points) - 1))

    def to_y(mmr: float) -> int:
        if y_max == y_min:
            return plot_top + plot_h // 2
        return plot_bottom - int((mmr - y_min) / (y_max - y_min) * plot_h)

    for lo, hi, _name, color in MMR_RANK_ZONES:
        if hi < y_min or lo > y_max:
            continue
        band_lo = max(lo, y_min)
        band_hi = min(hi, y_max)
        y_top = to_y(band_hi)
        y_bot = to_y(band_lo)
        c = QColor(color)
        c.setAlpha(18)
        painter.fillRect(plot_left, y_top, plot_w, y_bot - y_top, QBrush(c))

    start_y = to_y(points[0]["mmr"])
    anchor_pen = QPen(QColor(C_FAINT))
    anchor_pen.setWidth(1)
    anchor_pen.setStyle(QtNs.DashLine)
    painter.setPen(anchor_pen)
    painter.drawLine(plot_left + 24, start_y, plot_right - 12, start_y)

    line_pts = [QPoint(to_x(i), to_y(p["mmr"])) for i, p in enumerate(points)]
    line_pen = QPen(QColor(C_TEXT))
    line_pen.setWidth(2)
    line_pen.setCapStyle(QtNs.RoundCap)
    line_pen.setJoinStyle(QtNs.RoundJoin)
    painter.setPen(line_pen)
    painter.drawPolyline(QPolygon(line_pts))

    last_idx = len(points) - 1
    marker_color = {"W": C_WIN, "L": C_LOSS}
    for i, p in enumerate(points):
        marker = p.get("marker") or "snap"
        base_color = QColor(marker_color.get(marker, C_MUTED))
        x, y = to_x(i), to_y(p["mmr"])
        if i == last_idx:
            ring = QColor(base_color)
            ring.setAlpha(120)
            _draw_marker(painter, x, y, ring, 7)
            _draw_marker(painter, x, y, base_color, 4)
        else:
            _draw_marker(painter, x, y, base_color,
                         3 if marker == "snap" else 4)

    # Place the current MMR clear of the last marker's 7px halo — vertical
    # overlap was bleeding the digits into the glow.
    cur_mmr = points[-1]["mmr"]
    cur_x, cur_y = to_x(last_idx), to_y(cur_mmr)
    painter.setFont(num_font_md)
    painter.setPen(QColor(C_TEXT))
    label = str(cur_mmr)
    fm = painter.fontMetrics()
    label_w = fm.horizontalAdvance(label)
    label_x = max(plot_left, min(plot_right - label_w, cur_x - label_w // 2))
    label_y = cur_y - 12
    if label_y - fm.ascent() < plot_top + 2:
        label_y = cur_y + fm.ascent() + 8
    painter.drawText(label_x, label_y, label)

    painter.setFont(num_font_sm)
    painter.setPen(QColor(C_MUTED))
    painter.drawText(plot_left, plot_top + 8, str(y_max))
    painter.drawText(plot_left, plot_bottom - 2, str(y_min))

    cap_text = f"last {len(points)}"
    painter.setFont(small_font)
    painter.setPen(QColor(C_MUTED))
    cap_w = painter.fontMetrics().horizontalAdvance(cap_text)
    painter.drawText(plot_right - cap_w, plot_top + 14, cap_text)

    painter.setPen(sep_pen)
    painter.drawLine(plot_left, plot_bottom + 1, plot_right, plot_bottom + 1)

    _draw_graph_footer(painter, cfg, playlist, plot_left, plot_right,
                       canvas_height, _GRAPH_FOOTER_H, small_font)
    painter.end()
    return pix


def _draw_graph_footer(painter: QPainter, cfg: dict, playlist: str,
                       left: int, right: int, canvas_height: int, footer_h: int,
                       small_font: QFont) -> None:
    """Footer with hotkey hints (keys bold, verbs muted) and the active
    playlist label right-aligned in a heavier weight, so the key letters
    read first at a glance."""
    cycle_label = _first_keyboard_label(cfg.get("cycle_hotkeys") or [])
    expand_label = _first_keyboard_label(cfg.get("expand_hotkeys") or [])
    base_y = canvas_height - footer_h + 16

    family = cfg.get("font_family", "Segoe UI")
    key_font = _graph_mono_font(7, bold=True)
    verb_font = small_font
    pl_font = _graph_font(family, 7, bold=True)
    key_color = QColor(C_DIM)
    verb_color = QColor(C_MUTED)
    sep_color = QColor(C_FAINT)

    x = left
    pieces: list[tuple[str, QFont, QColor]] = []
    if expand_label:
        pieces.extend([(expand_label, key_font, key_color),
                       (" session", verb_font, verb_color)])
    if cycle_label:
        if pieces:
            pieces.append(("  ·  ", verb_font, sep_color))
        pieces.extend([(cycle_label, key_font, key_color),
                       (" playlist", verb_font, verb_color)])
    for text, font, color in pieces:
        painter.setFont(font)
        painter.setPen(QPen(color))
        painter.drawText(x, base_y, text)
        x += painter.fontMetrics().horizontalAdvance(text)

    pl_label = playlist.upper()
    painter.setFont(pl_font)
    painter.setPen(QPen(QColor(C_TEXT)))
    rect = painter.fontMetrics().boundingRect(pl_label)
    painter.drawText(right - rect.width(), base_y, pl_label)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        with contextlib.suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    cfg = load_config()
    apply_overrides(cfg)
    players_db = load_players()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    overlay = Overlay(cfg)
    stats = StatsClient(cfg["host"], cfg["port"],
                        api_dump_enabled=bool(cfg.get("api_debug_dump", False)))
    session = SessionStats(recent_size=cfg.get("recent_size", 5))
    match_stats = MatchStats()
    mmr_client = MMRClient(enabled=bool(cfg.get("mmr_enabled", False)))
    mmr_client.start()
    hotkey_h2h = HotkeyManager(cfg["hotkeys"])
    hotkey_session = HotkeyManager(cfg.get("session_hotkeys") or [])
    hotkey_expand = HotkeyManager(cfg.get("expand_hotkeys") or [])
    hotkey_cycle = HotkeyManager(cfg.get("cycle_hotkeys") or [])

    # Sanitize the persisted category once at startup — guards against a hand-edited
    # config setting (e.g. "1V1" instead of "1v1"). Falls back to "best".
    if cfg.get("mmr_category") not in MMR_CATEGORIES:
        cfg["mmr_category"] = "best"

    state = {
        "in_match": False,
        "h2h_held": False,
        "session_held": False,
        "summary_visible": False,
        "summary_html": "",
        "h2h_html": idle_html("Waiting for Rocket League…"),
        "h2h_expanded": bool(cfg.get("h2h_default_expanded", False)),
        "session_view": cfg.get("session_view", "session"),
        "graph_playlist": cfg.get("graph_playlist", "2v2"),
        "roster": [],
        "my_team": 0,
        "arena": "",
        "team_colors": {},
    }
    if state["session_view"] not in ("session", "graph"):
        state["session_view"] = "session"
    if state["graph_playlist"] not in ("1v1", "2v2", "3v3"):
        state["graph_playlist"] = "2v2"

    def _any_visible() -> bool:
        return (state["h2h_held"] or state["session_held"]
                or state["summary_visible"])

    def update_overlay():
        if not _any_visible():
            focus_timer.stop()
            overlay.hide()
            return
        if cfg.get("require_rl_focus", True) and not is_rl_focused():
            overlay.hide()
            return
        # Priority: held keys win over the auto-popup. Session > H2H > summary.
        if state["session_held"]:
            if state["session_view"] == "graph":
                _ensure_graph_data_loaded()
                pix = render_graph_pixmap(
                    state["graph_playlist"],
                    mmr_history_cache["snapshots"],
                    mmr_history_cache["matches"],
                    cfg,
                    canvas_width=cfg["width"] - 32,
                )
                overlay.set_pixmap(pix)
            else:
                overlay.set_html(
                    render_session_html(session)
                    + _session_footer_html(cfg, "session")
                )
        elif state["h2h_held"] and state["in_match"]:
            if state["h2h_expanded"]:
                spacer = (
                    "<table cellpadding='0' cellspacing='0' width='100%'>"
                    "<tr><td height='28'>&nbsp;</td></tr></table>"
                )
                # In expanded mode we render the session WITHOUT its own Format legend
                # and recombine it (along with the hotkey hint) into a single-table
                # footer below — that way the two metadata rows are siblings inside
                # one table and Qt can't slip native block spacing between them.
                overlay.set_html(
                    state["h2h_html"]
                    + spacer
                    + render_session_html(session, with_legend=False)
                    + _h2h_footer_html(cfg, expanded=True, session=session)
                )
            else:
                overlay.set_html(
                    state["h2h_html"]
                    + _h2h_footer_html(cfg, expanded=False, session=None)
                )
        elif state["summary_visible"]:
            overlay.set_html(state["summary_html"])
        else:
            overlay.hide()
            return
        overlay.show()
        overlay.raise_()

    focus_timer = QTimer()
    focus_timer.setInterval(250)
    focus_timer.timeout.connect(update_overlay)

    def on_h2h_pressed():
        state["h2h_held"] = True
        focus_timer.start()
        update_overlay()

    def on_h2h_released():
        state["h2h_held"] = False
        if not _any_visible():
            focus_timer.stop()
        update_overlay()

    def on_session_pressed():
        state["session_held"] = True
        focus_timer.start()
        update_overlay()

    def on_session_released():
        state["session_held"] = False
        if not _any_visible():
            focus_timer.stop()
        update_overlay()

    summary_timer = QTimer()
    summary_timer.setSingleShot(True)

    def hide_summary():
        summary_timer.stop()
        state["summary_visible"] = False
        if not _any_visible():
            focus_timer.stop()
        update_overlay()

    summary_timer.timeout.connect(hide_summary)

    def rerender_h2h() -> None:
        """Re-run render_html against the saved roster — used both when a match
        starts and whenever fresh MMR data lands or the user cycles category."""
        if not state["roster"]:
            mmr_log("rerender_h2h: skip (no roster)")
            return
        self_id = cfg.get("self_player_id")
        # Snapshot the cache once per render so all rows see a consistent view
        # even if the worker writes mid-build.
        mmr_db = {p["key"]: mmr_client.get(p["key"]) for p in state["roster"]}
        if mmr_client.is_enabled():
            summary_parts = []
            for p in state["roster"]:
                e = mmr_db.get(p["key"])
                if e is None:
                    summary_parts.append(f"{p['name']}=…")
                elif e.get("not_found"):
                    summary_parts.append(f"{p['name']}=NF")
                else:
                    best = (e.get("best") or {})
                    summary_parts.append(
                        f"{p['name']}={best.get('mmr')}@{best.get('playlist')}"
                    )
            mmr_log(f"rerender_h2h: cat={cfg.get('mmr_category','best')!r} "
                    f"rows=[{', '.join(summary_parts)}]")
        state["h2h_html"] = render_html(
            state["roster"], state["my_team"], state["arena"],
            players_db, state["team_colors"], self_id,
            mmr_db=mmr_db,
            mmr_category=cfg.get("mmr_category", "best"),
            mmr_enabled=mmr_client.is_enabled(),
        )

    def _ensure_graph_data_loaded() -> None:
        """First call (or after `dirty` is set) parses the on-disk files into
        the in-memory cache. Subsequent calls re-stat both files and reparse
        only when their mtime changed — cheap, and means polling-loop writes
        appear in the next graph render without extra plumbing."""
        try:
            hist_mtime = MMR_HISTORY_PATH.stat().st_mtime if MMR_HISTORY_PATH.exists() else 0.0
        except OSError:
            hist_mtime = 0.0
        try:
            match_mtime = MATCHES_PATH.stat().st_mtime if MATCHES_PATH.exists() else 0.0
        except OSError:
            match_mtime = 0.0
        need_load = (
            not mmr_history_cache["loaded"]
            or mmr_history_cache["dirty"]
            or hist_mtime != mmr_history_cache["mtime_history"]
            or match_mtime != mmr_history_cache["mtime_matches"]
        )
        if not need_load:
            return
        mmr_history_cache["snapshots"] = load_mmr_history()
        mmr_history_cache["matches"] = load_matches()
        mmr_history_cache["mtime_history"] = hist_mtime
        mmr_history_cache["mtime_matches"] = match_mtime
        mmr_history_cache["loaded"] = True
        mmr_history_cache["dirty"] = False

    # Post-match self-MMR polling state. The token is bumped on each new poll
    # so callbacks scheduled by an earlier poll can self-cancel — important
    # because back-to-back matches would otherwise produce overlapping polls
    # whose snapshots interleave in the attribution algorithm.
    poll_state = {"token": 0, "baseline": None}

    def start_post_match_mmr_poll(self_player: dict) -> None:
        pid = self_player.get("primaryId") or self_player["key"]
        name = self_player.get("name") or ""
        self_id = cfg.get("self_player_id")
        baseline_entry = mmr_client.get(self_id) if self_id else None
        baseline = (baseline_entry or {}).get("lastUpdated") or ""

        poll_state["token"] += 1
        poll_state["baseline"] = baseline
        my_token = poll_state["token"]
        delays_ms = [0, 120_000, 240_000, 360_000, 480_000, 600_000]
        mmr_log(f"poll: scheduled for self={self_id!r} "
                f"baseline_lastUpdated={baseline!r}")

        def attempt(i: int):
            if poll_state["token"] != my_token:
                mmr_log(f"poll: superseded (token {my_token} != "
                        f"{poll_state['token']}); aborting")
                return
            cur = mmr_client.get(self_id) if self_id else None
            cur_last = (cur or {}).get("lastUpdated") or ""
            if cur_last and baseline and cur_last > baseline:
                mmr_log(f"poll: TRN advanced to {cur_last!r} after {i} attempt(s); stopping")
                return
            if i >= len(delays_ms):
                mmr_log("poll: budget exhausted (10 min, 6 attempts)")
                return
            mmr_log(f"poll: attempt #{i+1}/{len(delays_ms)} (force-refresh)")
            mmr_client.enqueue(pid, name, force=True)
            QTimer.singleShot(120_000, lambda: attempt(i + 1))

        attempt(0)

    def on_initialized(payload: dict):
        state["in_match"] = True
        hide_summary()  # next match starting → drop any in-flight post-match popup
        # Auto-detect self in 1v1: only one teammate on my side = me. Persist to config.
        if not cfg.get("self_player_id"):
            mt = payload["myTeam"]
            same_side = [p for p in payload["players"] if p["team"] == mt]
            if len(same_side) == 1:
                cfg["self_player_id"] = same_side[0]["key"]
                print(f"[self] detected self={same_side[0]['name']!r} "
                      f"({same_side[0]['key']}) — saved to config", file=sys.stderr)
                save_config(cfg)
        # Resolve self_name for session-stat attribution (events only carry Name).
        self_id = cfg.get("self_player_id")
        if self_id:
            for p in payload["players"]:
                if p["key"] == self_id:
                    session.self_name = p["name"]
                    break
        # Reset per-match aggregator using the now-known self_name.
        match_stats.reset(self_name=session.self_name)
        # Persist roster bits we need to re-render asynchronously when MMR
        # data trickles in (or when the user toggles category via F10).
        state["roster"] = payload["players"]
        state["my_team"] = payload["myTeam"]
        state["arena"] = payload["arena"]
        state["team_colors"] = payload.get("teamColors") or {}
        # Self IS included now — we want to see our own MMR in the YOU row,
        # and the post-match refresh in on_ended only works if self has been
        # enqueued at least once. Cached entries serve instantly, fresh ones
        # arrive over the next ~1s per player.
        if mmr_client.is_enabled():
            mmr_log(f"on_initialized: enqueueing {len(payload['players'])} player(s) "
                    f"(including self={self_id!r})")
            mmr_client.enqueue_roster(payload["players"])
        else:
            mmr_log(f"on_initialized: MMR disabled, skipping enqueue "
                    f"(enabled_flag={cfg.get('mmr_enabled', False)}, "
                    f"curl_cffi_loaded={mmr_client._requests is not None})")
        rerender_h2h()
        update_overlay()
        print(f"[match] initialized arena={payload['arena']} myTeam={payload['myTeam']}")

    def on_ended(payload: dict):
        state["in_match"] = False
        session.on_match_ended(payload)
        i_won = payload["winner"] == payload["myTeam"]
        record = {
            "matchGuid": payload.get("matchGuid"),
            "endedAt": now_iso(),
            "arena": payload["arena"],
            "myTeam": payload["myTeam"],
            "winner": payload["winner"],
            "result": "W" if i_won else "L",
            "score": payload.get("score"),
            "playlist": _playlist_from_player_count(len(payload["players"])),
            "players": payload["players"],
        }
        append_match(record)
        update_players_cache(players_db, record)
        save_players(players_db)
        score_str = ""
        if isinstance(record.get("score"), list) and len(record["score"]) == 2:
            mt = payload["myTeam"]
            score_str = f" ({record['score'][mt]}–{record['score'][1 - mt]})"
        print(f"[match] ended {'WIN' if i_won else 'LOSS'}{score_str}")
        # Force-refresh self MMR after the match — TRN's edge cache is sticky
        # (we observed ~8 min of staleness in practice), so we poll every 2 min
        # for up to 10 min and stop early once TRN's lastUpdated actually rolls
        # past where we started. See start_post_match_mmr_poll() below.
        self_id = cfg.get("self_player_id")
        if mmr_client.is_enabled() and self_id:
            self_player = next((p for p in payload["players"]
                                if p["key"] == self_id), None)
            if self_player:
                start_post_match_mmr_poll(self_player)
        # Auto-popup the post-match summary card. Stays up until the next match
        # starts (match_initialized) or the user leaves to the menu (match_destroyed),
        # with a 30s safety net for cases where neither event fires.
        if cfg.get("show_match_summary", True):
            state["summary_html"] = render_summary_html(payload, match_stats)
            state["summary_visible"] = True
            focus_timer.start()
            update_overlay()
            summary_timer.start(int(cfg.get("match_summary_seconds", 30)) * 1000)

    def on_destroyed():
        state["in_match"] = False
        hide_summary()  # leaving the match → drop the post-match popup
        state["h2h_html"] = idle_html("Waiting for next match…")
        update_overlay()

    def on_status(connected: bool):
        if state["in_match"]:
            return
        if connected:
            state["h2h_html"] = idle_html("Connected — waiting for match…")
        else:
            state["h2h_html"] = idle_html(
                "Disconnected — is RL running with the Stats API enabled?")
        update_overlay()

    def on_event_for_session(event: str, data: dict):
        # Only count events while a real match is in progress (between match_initialized
        # and match_ended/match_destroyed). Excludes free practice / training / menus.
        if state["in_match"]:
            session.on_event(event, data)
            match_stats.on_event(event, data)

    stats.match_initialized.connect(on_initialized)
    stats.match_ended.connect(on_ended)
    stats.match_destroyed.connect(on_destroyed)
    stats.connection_status.connect(on_status)
    stats.event_seen.connect(on_event_for_session)
    hotkey_h2h.pressed.connect(on_h2h_pressed)
    hotkey_h2h.released.connect(on_h2h_released)
    hotkey_session.pressed.connect(on_session_pressed)
    hotkey_session.released.connect(on_session_released)

    def toggle_expand():
        # Context-sensitive: when F12 is held, F11 swaps the session sub-view
        # between session card and graph. Otherwise (Tab held or nothing
        # held), it keeps the existing H2H-expand toggle behavior.
        if state["session_held"]:
            nxt = "graph" if state["session_view"] == "session" else "session"
            state["session_view"] = nxt
            cfg["session_view"] = nxt
            save_config(cfg)
            print(f"[overlay] session_view={nxt}", file=sys.stderr)
        else:
            state["h2h_expanded"] = not state["h2h_expanded"]
            cfg["h2h_default_expanded"] = state["h2h_expanded"]
            save_config(cfg)
            print(f"[overlay] expanded={state['h2h_expanded']}", file=sys.stderr)
        update_overlay()

    hotkey_expand.pressed.connect(toggle_expand)


    GRAPH_PLAYLISTS = ("1v1", "2v2", "3v3")

    def cycle_mmr_category():
        # Context-sensitive: while the graph view is showing (F12 held +
        # session_view=="graph"), F10 cycles the graph's playlist instead of
        # the H2H MMR category. Same key, different role per context — same
        # idea as F11 (expand H2H vs swap session subview).
        if state["session_held"] and state["session_view"] == "graph":
            cur_pl = state["graph_playlist"]
            i = GRAPH_PLAYLISTS.index(cur_pl) if cur_pl in GRAPH_PLAYLISTS else -1
            nxt_pl = GRAPH_PLAYLISTS[(i + 1) % len(GRAPH_PLAYLISTS)]
            state["graph_playlist"] = nxt_pl
            cfg["graph_playlist"] = nxt_pl
            save_config(cfg)
            mmr_log(f"F10 cycle_graph_playlist: {cur_pl!r} -> {nxt_pl!r}")
            update_overlay()
            return
        cur = cfg.get("mmr_category", "best")
        try:
            i = MMR_CATEGORIES.index(cur)
        except ValueError:
            i = -1
        nxt = MMR_CATEGORIES[(i + 1) % len(MMR_CATEGORIES)]
        cfg["mmr_category"] = nxt
        save_config(cfg)
        mmr_log(f"F10 cycle_category: {cur!r} -> {nxt!r}")
        rerender_h2h()
        update_overlay()

    hotkey_cycle.pressed.connect(cycle_mmr_category)

    # Tracks the last self entry we logged so we can compute deltas (and skip
    # writes when nothing has changed). Seeded from disk cache below.
    last_self_log = {"playlists": {}, "lastUpdated": None}

    # Lazy cache for mmr_history.jsonl. We don't load at startup — the graph
    # view is opened by maybe 1% of users on any given session, so we pay the
    # parse cost only on first F11-from-session. The `dirty` flag is set by
    # _log_my_mmr after a history append, telling the graph render to reparse
    # before drawing. mtime-based invalidation also guards against external
    # edits.
    mmr_history_cache = {
        "loaded": False, "snapshots": [], "matches": [],
        "mtime_history": 0.0, "mtime_matches": 0.0, "dirty": False,
    }

    def _log_my_mmr(entry: dict):
        """Append one line per *meaningful* refresh to my_mmr.log: per-playlist
        MMR, deltas, and TRN's lastUpdated. Skips the write when no playlist
        moved AND TRN's snapshot hasn't rolled — those entries were just our
        force-refreshes hitting TRN's static edge cache and add no signal."""
        prev = last_self_log["playlists"]
        cur = entry.get("playlists") or {}
        last_updated = entry.get("lastUpdated") or "?"

        any_change = any(
            (cur.get(lbl) or {}).get("mmr") != (prev.get(lbl) or {}).get("mmr")
            for lbl in ("1v1", "2v2", "3v3")
        )
        trn_rolled = last_updated != last_self_log["lastUpdated"]
        first_entry = not last_self_log["lastUpdated"]
        if not (any_change or trn_rolled or first_entry):
            return  # static cache hit; saying so over and over is just noise

        parts = []
        for label in ("1v1", "2v2", "3v3"):
            cv = (cur.get(label) or {}).get("mmr")
            pv = (prev.get(label) or {}).get("mmr")
            if cv is None:
                parts.append(f"{label}=—")
            elif pv is None:
                parts.append(f"{label}={cv}")
            elif cv == pv:
                parts.append(f"{label}={cv} (·)")
            else:
                parts.append(f"{label}={cv} ({cv - pv:+d})")
        best = entry.get("best") or {}
        best_part = (
            f"best={best.get('mmr')}@{best.get('playlist')}"
            if best else "best=—"
        )
        line = (
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
            f"{'  '.join(parts)}  {best_part}  "
            f"trn_lastUpdated={last_updated}"
        )
        try:
            with MY_MMR_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            mmr_log(f"my_mmr.log write failed: {e}")
        # Persist a structured snapshot to mmr_history.jsonl for the graph
        # view. Stricter dedupe than the log: only write when TRN's
        # lastUpdated actually advanced (or first entry ever) — otherwise
        # the attribution algorithm could double-count an interval.
        if trn_rolled or first_entry:
            snap = {
                "ts": now_iso(),
                "trn_lastUpdated": last_updated,
                "playlists": {
                    lbl: (cur.get(lbl) or {}).get("mmr")
                    for lbl in ("1v1", "2v2", "3v3")
                    if (cur.get(lbl) or {}).get("mmr") is not None
                },
            }
            append_mmr_history(snap)
            mmr_history_cache["dirty"] = True
        last_self_log["playlists"] = cur
        last_self_log["lastUpdated"] = last_updated

    # Seed last_self_log from disk cache so deltas span restarts AND so the
    # first refresh after launch doesn't write a noise line if nothing moved.
    self_id = cfg.get("self_player_id")
    if self_id:
        existing_self = mmr_client.get(self_id)
        if existing_self and not existing_self.get("not_found"):
            last_self_log["playlists"] = existing_self.get("playlists") or {}
            last_self_log["lastUpdated"] = existing_self.get("lastUpdated")
            mmr_log(f"seed last_self_log from cache: "
                    f"{list(last_self_log['playlists'].keys())} "
                    f"trn_lastUpdated={last_self_log['lastUpdated']}")

    def on_mmr_updated(key: str):
        # Self MMR refresh? Mirror the snapshot to my_mmr.log for tracking.
        sid = cfg.get("self_player_id")
        if sid and key == sid:
            entry = mmr_client.get(key)
            if entry and not entry.get("not_found"):
                _log_my_mmr(entry)
        # Coalesce repaints — many opponents resolving in quick succession would
        # otherwise re-render once per arrival. The 200ms timer is single-shot
        # and rearmed on every signal, so we only repaint after the queue lulls.
        mmr_repaint_timer.start(200)

    mmr_repaint_timer = QTimer()
    mmr_repaint_timer.setSingleShot(True)

    def _mmr_repaint():
        if state["in_match"]:
            rerender_h2h()
            update_overlay()

    mmr_repaint_timer.timeout.connect(_mmr_repaint)
    mmr_client.updated.connect(on_mmr_updated)

    # System tray icon — gives the user a way to quit when launched via start.bat
    # (which uses pythonw and so has no console window to Ctrl+C).
    tray = None
    status_action = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = QSystemTrayIcon(make_tray_icon())
        tray.setToolTip("Rocket League H2H — starting…")

        menu = QMenu()
        title_action = QAction("Rocket League H2H")
        title_action.setEnabled(False)
        menu.addAction(title_action)
        status_action = QAction("Status: starting…")
        status_action.setEnabled(False)
        menu.addAction(status_action)
        menu.addSeparator()

        open_action = QAction("Open data folder")
        def _open_folder():
            try:
                if sys.platform == "win32":
                    os.startfile(str(APP_DIR))  # type: ignore[attr-defined]
                else:
                    import subprocess
                    opener = "open" if sys.platform == "darwin" else "xdg-open"
                    subprocess.Popen([opener, str(APP_DIR)])
            except Exception as e:
                print(f"[tray] open folder failed: {e}", file=sys.stderr)
        open_action.triggered.connect(_open_folder)
        menu.addAction(open_action)

        reset_session_action = QAction("Reset session stats")
        def _reset_session():
            session.reset()
            match_stats.reset(self_name=session.self_name)
            print("[reset] session stats cleared", file=sys.stderr)
            update_overlay()
        reset_session_action.triggered.connect(_reset_session)
        menu.addAction(reset_session_action)

        wipe_history_action = QAction("Wipe match history…")
        def _wipe_history():
            # Drain any queued match_ended slot first — otherwise a match that
            # ended just before the user clicked Wipe would write its record
            # *after* we've shown the dialog, and then we'd silently delete it.
            QApplication.processEvents()
            reply = QMessageBox.question(
                None,
                "Wipe match history",
                "Permanently delete matches.jsonl and players.json?\n"
                "Your current session stats are kept.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            for path in (MATCHES_PATH, PLAYERS_PATH):
                try:
                    path.unlink(missing_ok=True)
                except OSError as e:
                    print(f"[reset] failed to delete {path.name}: {e}", file=sys.stderr)
            players_db.clear()
            # The cached H2H card was rendered against the now-wiped opponent
            # records — refresh so a held Tab during this match doesn't show
            # stale W/L counts. (Idle text until the next match starts.)
            state["h2h_html"] = idle_html("History wiped — fresh start.")
            print("[reset] match history wiped", file=sys.stderr)
            update_overlay()
        wipe_history_action.triggered.connect(_wipe_history)
        menu.addAction(wipe_history_action)
        menu.addSeparator()

        mmr_action = QAction("Show MMR (sends opponent IDs to tracker.gg)")
        mmr_action.setCheckable(True)
        mmr_action.setChecked(bool(cfg.get("mmr_enabled", False)))
        def _toggle_mmr(checked: bool):
            cfg["mmr_enabled"] = bool(checked)
            save_config(cfg)
            mmr_client.set_enabled(bool(checked))
            mmr_log(f"tray toggle: enabled={cfg['mmr_enabled']} "
                    f"in_match={state['in_match']} "
                    f"roster_size={len(state.get('roster') or [])}")
            # Flipping ON in the middle of a match: enqueue the current roster
            # immediately so the user sees data without waiting for the next
            # match. Flipping OFF: just re-render so the chips disappear.
            if checked and state["in_match"] and state["roster"]:
                mmr_log(f"  in-match enqueue {len(state['roster'])} player(s)")
                mmr_client.enqueue_roster(state["roster"])
            rerender_h2h()
            update_overlay()
        mmr_action.toggled.connect(_toggle_mmr)
        menu.addAction(mmr_action)
        menu.addSeparator()

        auto_update_action = QAction("Auto-update on launch")
        auto_update_action.setCheckable(True)
        auto_update_action.setChecked(bool(cfg.get("auto_update", False)))
        def _toggle_auto_update(checked: bool):
            cfg["auto_update"] = bool(checked)
            save_config(cfg)
            print(f"[update] auto_update={cfg['auto_update']}", file=sys.stderr)
        auto_update_action.toggled.connect(_toggle_auto_update)
        menu.addAction(auto_update_action)
        menu.addSeparator()

        quit_action = QAction("Quit")
        quit_action.triggered.connect(app.quit)
        menu.addAction(quit_action)

        tray.setContextMenu(menu)
        tray.show()

        # Skip the Qt setText/setToolTip churn when the connection state hasn't
        # changed — connection_status emits on every reconnect attempt failure
        # during a backoff storm, and Qt does compare strings, but building the
        # f-string and crossing the C++ boundary is wasted work.
        last_tray_state = [None]  # boxed for closure assignment

        def update_tray_status(connected: bool):
            if last_tray_state[0] == connected:
                return
            last_tray_state[0] = connected
            label = "Connected" if connected else "Disconnected"
            if status_action is not None:
                status_action.setText(f"Status: {label}")
            tray.setToolTip(f"Rocket League H2H — {label}")
        stats.connection_status.connect(update_tray_status)
    else:
        print("[tray] system tray not available; quit via Task Manager / Ctrl+C",
              file=sys.stderr)

    stats.start()
    hotkey_h2h.start()
    hotkey_session.start()
    hotkey_expand.start()
    hotkey_cycle.start()

    print(f"[ready] h2h={cfg['hotkeys']} session={cfg.get('session_hotkeys') or []} "
          f"expand={cfg.get('expand_hotkeys') or []} "
          f"cycle={cfg.get('cycle_hotkeys') or []} "
          f"position={cfg['position']} tcp://{cfg['host']}:{cfg['port']}")
    print(f"        require_rl_focus={cfg.get('require_rl_focus', True)} "
          f"expanded={state['h2h_expanded']} "
          f"mmr={cfg.get('mmr_enabled', False)}/{cfg.get('mmr_category', 'best')} "
          f"self={cfg.get('self_player_id') or '(auto-detect on first 1v1)'}")
    print(f"        matches → {MATCHES_PATH.name}")
    print(f"        players → {PLAYERS_PATH.name}")

    rc = app.exec()
    stats.stop()
    hotkey_h2h.stop()
    hotkey_session.stop()
    hotkey_expand.stop()
    hotkey_cycle.stop()
    mmr_client.stop()
    sys.exit(rc)


if __name__ == "__main__":
    main()
