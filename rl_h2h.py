#!/usr/bin/env python3
"""Rocket League head-to-head overlay.

Connects to the Rocket League Stats API local WebSocket, tracks per-player
W/L history across matches, and shows a transparent always-on-top overlay
while the configured hotkey is held.
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
import sys
import threading
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import websockets
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QFont, QGuiApplication
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget
from pynput import keyboard


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
MATCHES_PATH = APP_DIR / "matches.jsonl"
PLAYERS_PATH = APP_DIR / "players.json"


EVT_MATCH_CREATED = "MatchCreated"
EVT_MATCH_INITIALIZED = "MatchInitialized"
EVT_ROUND_STARTED = "RoundStarted"
EVT_UPDATE_STATE = "UpdateState"
EVT_MATCH_ENDED = "MatchEnded"
EVT_MATCH_DESTROYED = "MatchDestroyed"
EVT_REPLAY_CREATED = "ReplayCreated"

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
        "  Bumpers:  pad_lb (LB / L1), pad_rb (RB / R1)",
        "  Triggers: pad_lt (LT / L2), pad_rt (RT / R2)",
        "  D-pad:    pad_dpad_up, pad_dpad_down, pad_dpad_left, pad_dpad_right",
        "  Other:    pad_back (Xbox View / PS Share — default scoreboard button),",
        "            pad_start (Menu / Options), pad_lstick, pad_rstick",
        "Gamepad bindings require: pip install inputs",
        "Note: stock RL maps D-pad directions to quickchat. To avoid sending a quickchat",
        "      every time you check the overlay, use 'pad_back' on controller.",
        "require_rl_focus: only show the overlay when Rocket League has focus (Windows).",
        "self_player_id:   auto-filled after your first 1v1 match — used to hide your own",
        "                  W/L stat row. Set manually if you only play 2v2/3v3.",
        "position: top-left | top-center | top-right | bottom-left | bottom-right"
    ],
    "host": "127.0.0.1",
    "port": 49123,
    "hotkeys": ["tab", "pad_back"],
    "session_hotkeys": ["f12"],
    "require_rl_focus": True,
    "self_player_id": None,
    "position": "top-right",
    "margin": 24,
    "width": 380,
    "background_rgba": [16, 20, 21, 200],
    "text_color": "#E0E3E5",
    "font_family": "Segoe UI",
    "font_size": 11,
}


def is_rl_focused() -> bool:
    """True when Rocket League is the foreground window. Always True on non-Windows."""
    if sys.platform != "win32":
        return True
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h_proc = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h_proc:
            return False
        try:
            buf = (ctypes.c_wchar * 260)()
            size = wintypes.DWORD(260)
            if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                return buf.value.lower().endswith("rocketleague.exe")
        finally:
            kernel32.CloseHandle(h_proc)
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
    except Exception as e:
        print(f"[config] could not save: {e}", file=sys.stderr)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    loaded: Optional[dict] = None
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except Exception as e:
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


def update_players_cache(players: dict, match: dict) -> None:
    my_team = match["myTeam"]
    i_won = match["winner"] == my_team
    when = match["endedAt"]
    score = match.get("score")  # [blue, orange]
    if isinstance(score, list) and len(score) == 2:
        my_pov = [score[my_team], score[1 - my_team]]
    else:
        my_pov = None
    for p in match["players"]:
        rec = players.setdefault(p["key"], {
            "name": p["name"],
            "aliases": [p["name"]],
            BUCKET_VS:   {"wins": 0, "losses": 0, "lastSeenAt": None, "lastResult": None, "lastScore": None},
            BUCKET_WITH: {"wins": 0, "losses": 0, "lastSeenAt": None, "lastResult": None, "lastScore": None},
        })
        rec["name"] = p["name"]
        if p["name"] not in rec["aliases"]:
            rec["aliases"].append(p["name"])
        bucket = BUCKET_WITH if p["team"] == my_team else BUCKET_VS
        if i_won:
            rec[bucket]["wins"] += 1
        else:
            rec[bucket]["losses"] += 1
        rec[bucket]["lastSeenAt"] = when
        rec[bucket]["lastResult"] = "W" if i_won else "L"
        rec[bucket]["lastScore"] = my_pov


class StatsClient(QObject):
    """Reads the Rocket League Stats API local TCP socket and emits match-lifecycle signals.

    Verbose-debug mode: lots of [stats]/[tcp]/[evt]/[state]/[emit] logging on stderr to
    help diagnose remaining issues. To be cleaned up once stable.
    """

    match_initialized = Signal(dict)
    match_ended = Signal(dict)
    match_destroyed = Signal()
    connection_status = Signal(bool)
    event_seen = Signal(str, dict)  # raw event name + decoded data, for downstream consumers

    def __init__(self, host: str, port: int):
        super().__init__()
        self.host = host
        self.port = port
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="StatsClient")
        # Cross-match diagnostics — survive _reset().
        self._event_counts: dict[str, int] = {}
        self._first_seen: set[str] = set()
        self._sample_remaining = 2
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
        self._in_replay: bool = False

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
        # The official doc calls it a "web socket" but the wire format is raw TCP NDJSON.
        # We confirmed this end-to-end; skip the WS probe (it costs a 10s open_timeout
        # when RL hasn't pushed bytes yet, with no path to fall back).
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
        print(f"[tcp] connected to {self.host}:{self.port}", file=sys.stderr)
        bytes_total = 0
        msgs_total = 0
        loop = asyncio.get_running_loop()
        last_alive = loop.time()
        try:
            decoder = json.JSONDecoder()
            buf = ""
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    print(f"[tcp] EOF after {bytes_total} bytes / {msgs_total} msgs",
                          file=sys.stderr)
                    return
                bytes_total += len(chunk)
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
                    msgs_total += 1
                    self._safe_handle(obj)
                now = loop.time()
                if now - last_alive >= 60.0:
                    last_alive = now
                    print(f"[tcp] alive: bytes={bytes_total} msgs={msgs_total} "
                          f"events={{{self._event_summary()}}}", file=sys.stderr)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            print(f"[tcp] closed after {bytes_total} bytes / {msgs_total} msgs",
                  file=sys.stderr)

    def _event_summary(self) -> str:
        if not self._event_counts:
            return ""
        return ", ".join(f"{k}={v}" for k, v in sorted(self._event_counts.items()))

    def _safe_handle(self, msg) -> None:
        if not isinstance(msg, dict):
            if self._sample_remaining > 0:
                self._sample_remaining -= 1
                print(f"[stats] non-dict msg: {repr(msg)[:200]}", file=sys.stderr)
            return
        event = msg.get("Event", "?")
        self._event_counts[event] = self._event_counts.get(event, 0) + 1
        if event not in self._first_seen:
            self._first_seen.add(event)
            try:
                snippet = json.dumps(msg)[:600]
            except Exception:
                snippet = repr(msg)[:600]
            print(f"[stats] first {event}: {snippet}", file=sys.stderr)
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
        if event:
            self.event_seen.emit(event, data)
        if event == EVT_UPDATE_STATE:
            self._on_update_state(data)
        elif event == EVT_MATCH_CREATED:
            guid = data.get("MatchGuid") or "(empty)"
            print(f"[evt] MatchCreated guid={guid!r}", file=sys.stderr)
            self._reset()
            self._match_guid = data.get("MatchGuid")
        elif event == EVT_MATCH_INITIALIZED:
            print(f"[evt] {event}", file=sys.stderr)
            self._maybe_emit_initialized()
        elif event == EVT_ROUND_STARTED:
            # Lock the roster at kickoff. One last emit if anyone joined since the
            # previous emit; from this point we stop accumulating players.
            self._maybe_emit_initialized()
            if self._initialized_emitted and len(self._roster) > self._last_emitted_roster_size:
                self._emit_match_initialized(reason="round started, final roster")
            self._round_started = True
        elif event == EVT_MATCH_ENDED:
            print(f"[evt] MatchEnded data={data}", file=sys.stderr)
            self._on_match_ended(data)
        elif event == EVT_MATCH_DESTROYED:
            print("[evt] MatchDestroyed", file=sys.stderr)
            self.match_destroyed.emit()
            self._reset()
        elif event == EVT_REPLAY_CREATED:
            print("[evt] ReplayCreated — entering replay mode, recording paused", file=sys.stderr)
            self._in_replay = True

    def _on_update_state(self, data: dict):
        if not isinstance(data, dict) or self._in_replay:
            return
        game = data.get("Game")
        if isinstance(game, dict):
            arena = game.get("Arena")
            if isinstance(arena, str) and arena and arena != self._arena:
                print(f"[state] arena={arena!r}", file=sys.stderr)
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
            is_new = key not in self._roster
            name_raw = p.get("Name")
            name = name_raw if isinstance(name_raw, str) else "?"
            self._roster[key] = {
                "key": key,
                "primaryId": pid,
                "name": name,
                "team": int(team),
            }
            if is_new:
                print(f"[state] +player {name!r} team={team} ({pid})", file=sys.stderr)
            if any(k in p for k in SPECTATOR_FIELDS):
                spectator_team_hits.add(int(team))
        # Spectator-only fields appear iff the local client is on this player's team.
        # If both teams report them, the user is spectating — leave my_team unset.
        if self._my_team is None and len(spectator_team_hits) == 1:
            (self._my_team,) = spectator_team_hits
            print(f"[state] my_team={self._my_team}", file=sys.stderr)
        elif self._my_team is None and len(spectator_team_hits) > 1 and not self._spectator_warned:
            self._spectator_warned = True
            print(f"[state] spectator mode? both teams report spectator fields: "
                  f"{spectator_team_hits}", file=sys.stderr)
        # First emit when both teams have a player; re-emit on every roster growth so
        # late joiners (typical in 3v3) appear in the H2H card before kickoff.
        if not self._initialized_emitted:
            self._maybe_emit_initialized()
        elif len(self._roster) > self._last_emitted_roster_size:
            self._emit_match_initialized(reason="roster grew")

    def _on_match_ended(self, data: dict):
        if self._in_replay:
            print("[emit] MatchEnded skipped: in replay", file=sys.stderr)
            return
        winner = data.get("WinnerTeamNum")
        if winner is None or self._my_team is None or not self._roster:
            print(f"[emit] MatchEnded skipped: winner={winner} my_team={self._my_team} "
                  f"roster={len(self._roster)}", file=sys.stderr)
            return
        print(f"[emit] match_ended winner={winner} my_team={self._my_team} "
              f"roster={len(self._roster)} score={self._score}", file=sys.stderr)
        self.match_ended.emit({
            "winner": int(winner),
            "myTeam": self._my_team,
            "arena": self._arena,
            "matchGuid": self._match_guid,
            "players": list(self._roster.values()),
            "score": list(self._score),
            "teamColors": dict(self._team_colors),
        })

    def _maybe_emit_initialized(self):
        if (self._initialized_emitted or self._in_replay
                or self._my_team is None or not self._roster):
            return
        teams = {p["team"] for p in self._roster.values()}
        if len(teams) < 2:
            return
        self._initialized_emitted = True
        self._emit_match_initialized(reason="first emit")

    def _emit_match_initialized(self, reason: str = ""):
        teams = sorted({p["team"] for p in self._roster.values()})
        self._last_emitted_roster_size = len(self._roster)
        tag = f" ({reason})" if reason else ""
        print(f"[emit] match_initialized my_team={self._my_team} "
              f"roster={len(self._roster)} teams={teams} arena={self._arena!r}{tag}",
              file=sys.stderr)
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
        rgba = ",".join(str(v) for v in cfg["background_rgba"])
        self._label.setStyleSheet(
            "QLabel {"
            f"  color: {cfg['text_color']};"
            f"  background-color: rgba({rgba});"
            "  border: 1px solid rgba(255, 255, 255, 28);"
            "  border-radius: 4px;"
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

# Palette mapped from DESIGN(1).md, restricted to primary/secondary/tertiary tokens
# (no lime/gold primary-container — user pref).
C_TEXT     = "#E0E3E5"  # on-surface
C_DIM      = "#C4C9AC"  # on-surface-variant
C_MUTED    = "#8E9379"  # outline
C_FAINT    = "#444933"  # outline-variant (dividers, NEW pill border)
C_BLUE     = "#3B9EFF"  # fallback only — wire ColorPrimary preferred
C_ORANGE   = "#FF7A29"  # fallback only
C_WIN      = "#D7E3F8"  # tertiary-container — cool ice-blue accent
C_LOSS     = "#BFC7D8"  # secondary — muted for de-emphasis


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
    except Exception:
        return ""
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _player_row(p: dict, my_team: int, players_db: dict, self_id: Optional[str] = None) -> str:
    rec = players_db.get(p["key"])
    bucket = BUCKET_WITH if p["team"] == my_team else BUCKET_VS
    name = p["name"]
    if len(name) > 16:
        name = name[:15] + "…"
    is_self = self_id is not None and p["key"] == self_id

    if is_self:
        stat_cell = (
            f"<span style='color:{C_WIN};font-size:8pt;font-weight:700;"
            "letter-spacing:0.16em;'>YOU</span>"
        )
        sub_row = ""
        return (
            "<table width='100%' cellspacing='0' cellpadding='0' "
            "style='border-collapse:collapse;margin:0;'>"
            "<tr>"
            f"<td align='left' style='color:{C_TEXT};font-size:10pt;font-weight:600;"
            f"padding:3px 0 0 0;'>{name}</td>"
            "<td align='right' style='padding:3px 0 0 0;white-space:nowrap;'>"
            f"{stat_cell}</td>"
            "</tr>"
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

    sub_row = (
        f"<tr><td colspan='2' style='padding:0 0 2px 0;'>{sub}</td></tr>" if sub else ""
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
                  my_team: int, players_db: dict, self_id: Optional[str] = None) -> str:
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
        body = "".join(_player_row(p, my_team, players_db, self_id) for p in players)
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
                self_id: Optional[str] = None) -> str:
    blue = sorted([p for p in roster if p["team"] == 0], key=lambda x: x["name"].lower())
    orange = sorted([p for p in roster if p["team"] == 1], key=lambda x: x["name"].lower())

    blue_color = team_colors.get(0) if isinstance(team_colors, dict) else None
    orange_color = team_colors.get(1) if isinstance(team_colors, dict) else None
    if not isinstance(blue_color, str) or not blue_color.startswith("#"):
        blue_color = C_BLUE
    if not isinstance(orange_color, str) or not orange_color.startswith("#"):
        orange_color = C_ORANGE

    arena_label = pretty_arena(arena)
    if arena_label:
        a = arena_label if len(arena_label) <= 22 else arena_label[:21] + "…"
        arena_cell = (
            f"<td align='right' style='color:{C_MUTED};font-size:8pt;font-weight:500;"
            f"letter-spacing:0.10em;'>{a.upper()}</td>"
        )
    else:
        arena_cell = "<td></td>"

    header = (
        "<table width='100%' cellspacing='0' cellpadding='0' "
        "style='border-collapse:collapse;'>"
        "<tr>"
        f"<td align='left' style='color:{C_TEXT};font-size:10pt;font-weight:700;"
        "letter-spacing:0.18em;'>HEAD&middot;TO&middot;HEAD</td>"
        f"{arena_cell}"
        "</tr>"
        "</table>"
        f"<div style='height:1px;background-color:{C_FAINT};font-size:1px;line-height:1px;"
        "margin-top:8px;'>&nbsp;</div>"
        "<div style='height:10px;font-size:1px;line-height:1px;'>&nbsp;</div>"
    )
    spacer = "<div style='height:10px;font-size:1px;line-height:1px;'>&nbsp;</div>"
    return (
        header
        + _team_section("BLUE",   blue_color,   blue,   my_team == 0, my_team, players_db, self_id)
        + spacer
        + _team_section("ORANGE", orange_color, orange, my_team == 1, my_team, players_db, self_id)
    )


class SessionStats:
    """Aggregates stats from script start until kill. Updated from StatsClient signals."""

    def __init__(self):
        self.started_at = datetime.now(timezone.utc)
        self.self_name: Optional[str] = None
        self.matches = 0
        self.wins = 0
        self.losses = 0
        self.win_streak = 0
        self.loss_streak = 0
        self.best_win_streak = 0
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
        self.statfeed_counts: dict[str, int] = {}
        self.statfeed_counts_self: dict[str, int] = {}

    def _is_self(self, name: Optional[str]) -> bool:
        return bool(name) and name == self.self_name

    def on_event(self, event: str, data: dict) -> None:
        if event == "GoalScored":
            scorer_name = (data.get("Scorer") or {}).get("Name") if isinstance(data.get("Scorer"), dict) else None
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
        elif event == "CrossbarHit":
            self.crossbars += 1
            last_touch = data.get("BallLastTouch") or {}
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
        elif event == "BallHit":
            ball = data.get("Ball")
            sp = None
            if isinstance(ball, dict):
                sp = ball.get("PostHitSpeed")
            if isinstance(sp, (int, float)):
                if sp > self.max_ball_speed:
                    self.max_ball_speed = float(sp)
                # BallHit can have multiple players in the same frame — count if any is self.
                hitters = data.get("Players")
                if isinstance(hitters, list):
                    if any(self._is_self((h or {}).get("Name")) for h in hitters if isinstance(h, dict)):
                        if sp > self.max_ball_speed_self:
                            self.max_ball_speed_self = float(sp)
        elif event == "StatfeedEvent":
            ev_name = data.get("EventName")
            if isinstance(ev_name, str) and ev_name:
                self.statfeed_counts[ev_name] = self.statfeed_counts.get(ev_name, 0) + 1
                main = data.get("MainTarget") or {}
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


def render_session_html(s: SessionStats) -> str:
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

    em_dash = f"<span style='color:{C_MUTED};'>—</span>"

    def _opt_int(v):
        return str(int(v)) if v else em_dash

    def _pair_int(session_v: float, self_v: float) -> str:
        # Show one number when you own the session max; otherwise "session | yours".
        if session_v <= 0:
            return em_dash
        if self_v >= session_v:
            return str(int(session_v))
        sep = f"<span style='color:{C_MUTED};font-weight:500;'>&nbsp;|&nbsp;</span>"
        right = str(int(self_v)) if self_v > 0 else f"<span style='color:{C_MUTED};'>—</span>"
        return f"{int(session_v)}{sep}{right}"

    def _pair_int_self(self_v: int, session_v: int) -> str:
        # For pure counters (saves/shots/demos), show "session | yours".
        if session_v == 0:
            return em_dash
        if self_v >= session_v:
            return str(int(session_v))
        sep = f"<span style='color:{C_MUTED};font-weight:500;'>&nbsp;|&nbsp;</span>"
        right = str(int(self_v)) if self_v > 0 else f"<span style='color:{C_MUTED};'>—</span>"
        return f"{int(session_v)}{sep}{right}"

    def _pair_fastest(session_v: Optional[float], self_v: Optional[float]) -> str:
        if not session_v:
            return em_dash
        if self_v is not None and self_v <= session_v:
            return f"{session_v:.1f}s"
        sep = f"<span style='color:{C_MUTED};font-weight:500;'>&nbsp;|&nbsp;</span>"
        right = f"{self_v:.1f}s" if self_v else f"<span style='color:{C_MUTED};'>—</span>"
        return f"{session_v:.1f}s{sep}{right}"

    saves_t  = s.statfeed_counts.get("Save", 0)
    shots_t  = s.statfeed_counts.get("Shot", 0)
    demos_t  = s.statfeed_counts.get("Demolish", 0)
    saves_s  = s.statfeed_counts_self.get("Save", 0)
    shots_s  = s.statfeed_counts_self.get("Shot", 0)
    demos_s  = s.statfeed_counts_self.get("Demolish", 0)

    overall_rows = [
        _stat_row("Matches", matches_val),
        _stat_row("Goals", goals_val),
        _stat_row("Streak", streak_val),
        _stat_row(
            "Best run",
            (f"<span style='color:{C_WIN};font-weight:700;'>W{s.best_win_streak}</span>"
             if s.best_win_streak else em_dash),
        ),
    ]
    play_rows = [
        _stat_row("Saves",     _pair_int_self(saves_s, saves_t)),
        _stat_row("Shots",     _pair_int_self(shots_s, shots_t)),
        _stat_row("Demos",     _pair_int_self(demos_s, demos_t)),
        _stat_row("Crossbars", _opt_int(s.crossbars)),
    ]
    fun_rows = [
        _stat_row("Max goal speed",   _pair_int(s.max_goal_speed, s.max_goal_speed_self)),
        _stat_row("Max ball speed",   _pair_int(s.max_ball_speed, s.max_ball_speed_self)),
        _stat_row("Hardest crossbar", _pair_int(s.max_impact_force, s.max_impact_force_self)),
        _stat_row("Fastest goal",     _pair_fastest(s.fastest_goal_time, s.fastest_goal_time_self)),
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
    show_legend = (
        s.max_goal_speed > s.max_goal_speed_self
        or s.max_ball_speed > s.max_ball_speed_self
        or s.max_impact_force > s.max_impact_force_self
        or shots_t > shots_s or saves_t > saves_s or demos_t > demos_s
    )
    legend = ""
    if show_legend:
        legend = (
            "<div style='height:8px;font-size:1px;line-height:1px;'>&nbsp;</div>"
            f"<div style='color:{C_MUTED};font-size:8pt;letter-spacing:0.04em;'>"
            "Format: <b>session</b> | yours</div>"
        )
    return (
        header
        + _stat_section("OVERALL", overall_rows)
        + _stat_section("PLAY",    play_rows)
        + _stat_section("FUN",     fun_rows)
        + legend
    )


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    cfg = load_config()
    players_db = load_players()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    overlay = Overlay(cfg)
    stats = StatsClient(cfg["host"], cfg["port"])
    session = SessionStats()
    hotkey_h2h = HotkeyManager(cfg["hotkeys"])
    hotkey_session = HotkeyManager(cfg.get("session_hotkeys") or [])

    state = {
        "in_match": False,
        "h2h_held": False,
        "session_held": False,
        "h2h_html": idle_html("Waiting for Rocket League…"),
        "idle_html": idle_html("Waiting for Rocket League…"),
    }

    def update_overlay():
        held = state["h2h_held"] or state["session_held"]
        if not held:
            overlay.hide()
            return
        if cfg.get("require_rl_focus", True) and not is_rl_focused():
            overlay.hide()
            return
        if state["session_held"]:
            overlay.set_html(render_session_html(session))
            overlay.show()
            overlay.raise_()
            return
        # H2H is only meaningful while a real match is in progress.
        if not state["in_match"]:
            overlay.hide()
            return
        overlay.set_html(state["h2h_html"])
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
        if not (state["h2h_held"] or state["session_held"]):
            focus_timer.stop()
        update_overlay()

    def on_session_pressed():
        state["session_held"] = True
        focus_timer.start()
        update_overlay()

    def on_session_released():
        state["session_held"] = False
        if not (state["h2h_held"] or state["session_held"]):
            focus_timer.stop()
        update_overlay()

    def on_initialized(payload: dict):
        state["in_match"] = True
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
        html = render_html(
            payload["players"], payload["myTeam"], payload["arena"],
            players_db, payload.get("teamColors") or {},
            self_id,
        )
        state["h2h_html"] = html
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

    def on_destroyed():
        state["in_match"] = False
        state["h2h_html"] = idle_html("Waiting for next match…")
        state["idle_html"] = state["h2h_html"]
        update_overlay()

    def on_status(connected: bool):
        if state["in_match"]:
            return
        if connected:
            state["h2h_html"] = idle_html("Connected — waiting for match…")
        else:
            state["h2h_html"] = idle_html(
                "Disconnected — is RL running with the Stats API enabled?")
        state["idle_html"] = state["h2h_html"]
        update_overlay()

    def on_event_for_session(event: str, data: dict):
        # Only count events while a real match is in progress (between match_initialized
        # and match_ended/match_destroyed). Excludes free practice / training / menus.
        if state["in_match"]:
            session.on_event(event, data)

    stats.match_initialized.connect(on_initialized)
    stats.match_ended.connect(on_ended)
    stats.match_destroyed.connect(on_destroyed)
    stats.connection_status.connect(on_status)
    stats.event_seen.connect(on_event_for_session)
    hotkey_h2h.pressed.connect(on_h2h_pressed)
    hotkey_h2h.released.connect(on_h2h_released)
    hotkey_session.pressed.connect(on_session_pressed)
    hotkey_session.released.connect(on_session_released)

    stats.start()
    hotkey_h2h.start()
    hotkey_session.start()

    print(f"[ready] h2h={cfg['hotkeys']} session={cfg.get('session_hotkeys') or []} "
          f"position={cfg['position']} tcp://{cfg['host']}:{cfg['port']}")
    print(f"        require_rl_focus={cfg.get('require_rl_focus', True)} "
          f"self={cfg.get('self_player_id') or '(auto-detect on first 1v1)'}")
    print(f"        matches → {MATCHES_PATH.name}")
    print(f"        players → {PLAYERS_PATH.name}")

    rc = app.exec()
    stats.stop()
    hotkey_h2h.stop()
    hotkey_session.stop()
    sys.exit(rc)


if __name__ == "__main__":
    main()
