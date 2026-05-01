#!/usr/bin/env python3
"""Rocket League head-to-head overlay.

Connects to the Rocket League Stats API local WebSocket, tracks per-player
W/L history across matches, and shows a transparent always-on-top overlay
while the configured hotkey is held.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import websockets
from PySide6.QtCore import QObject, Qt, Signal
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

BUCKET_VS = "vs"
BUCKET_WITH = "with"

VALID_POSITIONS = ("top-left", "top-center", "top-right", "bottom-left", "bottom-right")
SPECTATOR_FIELDS = ("Boost", "bBoosting", "bOnGround", "bOnWall", "bSupersonic")


DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 49123,
    "hotkey": "tab",
    "position": "top-right",
    "margin": 24,
    "width": 380,
    "background_rgba": [14, 16, 20, 225],
    "text_color": "#F2F4F7",
    "font_family": "Segoe UI",
    "font_size": 11,
}


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"[config] failed to parse {CONFIG_PATH}: {e}", file=sys.stderr)
    else:
        atomic_write_text(CONFIG_PATH, json.dumps(DEFAULT_CONFIG, indent=2))
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
    for p in match["players"]:
        rec = players.setdefault(p["key"], {
            "name": p["name"],
            "aliases": [p["name"]],
            BUCKET_VS:   {"wins": 0, "losses": 0, "lastSeenAt": None, "lastResult": None},
            BUCKET_WITH: {"wins": 0, "losses": 0, "lastSeenAt": None, "lastResult": None},
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


class StatsClient(QObject):
    """Reads the Rocket League Stats API local TCP socket and emits match-lifecycle signals.

    Verbose-debug mode: lots of [stats]/[tcp]/[evt]/[state]/[emit] logging on stderr to
    help diagnose remaining issues. To be cleaned up once stable.
    """

    match_initialized = Signal(dict)
    match_ended = Signal(dict)
    match_destroyed = Signal()
    connection_status = Signal(bool)

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
        self._spectator_warned = False

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
                if now - last_alive >= 10.0:
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
        if event == EVT_UPDATE_STATE:
            self._on_update_state(data)
        elif event == EVT_MATCH_CREATED:
            guid = data.get("MatchGuid") or "(empty)"
            print(f"[evt] MatchCreated guid={guid!r}", file=sys.stderr)
            self._reset()
            self._match_guid = data.get("MatchGuid")
        elif event in (EVT_MATCH_INITIALIZED, EVT_ROUND_STARTED):
            print(f"[evt] {event}", file=sys.stderr)
            self._maybe_emit_initialized()
        elif event == EVT_MATCH_ENDED:
            print(f"[evt] MatchEnded data={data}", file=sys.stderr)
            self._on_match_ended(data)
        elif event == EVT_MATCH_DESTROYED:
            print("[evt] MatchDestroyed", file=sys.stderr)
            self.match_destroyed.emit()
            self._reset()

    def _on_update_state(self, data: dict):
        # Hot path. Once the roster + side are locked in, skip everything.
        if self._initialized_emitted:
            return
        if not isinstance(data, dict):
            return
        game = data.get("Game")
        if isinstance(game, dict):
            arena = game.get("Arena")
            if isinstance(arena, str) and arena and arena != self._arena:
                print(f"[state] arena={arena!r}", file=sys.stderr)
                self._arena = arena
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
            print(f"[state] my_team={self._my_team} "
                  f"(spectator-hit teams: {spectator_team_hits})", file=sys.stderr)
        elif self._my_team is None and len(spectator_team_hits) > 1 and not self._spectator_warned:
            self._spectator_warned = True
            print(f"[state] spectator mode? both teams report spectator fields: "
                  f"{spectator_team_hits}", file=sys.stderr)
        self._maybe_emit_initialized()

    def _on_match_ended(self, data: dict):
        winner = data.get("WinnerTeamNum")
        if winner is None or self._my_team is None or not self._roster:
            print(f"[emit] MatchEnded skipped: winner={winner} my_team={self._my_team} "
                  f"roster={len(self._roster)}", file=sys.stderr)
            return
        print(f"[emit] match_ended winner={winner} my_team={self._my_team} "
              f"roster={len(self._roster)}", file=sys.stderr)
        self.match_ended.emit({
            "winner": int(winner),
            "myTeam": self._my_team,
            "arena": self._arena,
            "matchGuid": self._match_guid,
            "players": list(self._roster.values()),
        })

    def _maybe_emit_initialized(self):
        if self._initialized_emitted or self._my_team is None or not self._roster:
            return
        teams = {p["team"] for p in self._roster.values()}
        if len(teams) < 2:
            return
        self._initialized_emitted = True
        print(f"[emit] match_initialized my_team={self._my_team} "
              f"roster={len(self._roster)} teams={sorted(teams)} arena={self._arena!r}",
              file=sys.stderr)
        self.match_initialized.emit({
            "arena": self._arena,
            "myTeam": self._my_team,
            "players": list(self._roster.values()),
        })


class HotkeyBridge(QObject):
    pressed = Signal()
    released = Signal()

    def __init__(self, hotkey_name: str):
        super().__init__()
        self._target = self._parse(hotkey_name.lower())
        self._down = False
        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)

    @staticmethod
    def _parse(name: str):
        if hasattr(keyboard.Key, name):
            return getattr(keyboard.Key, name)
        if len(name) != 1:
            raise ValueError(
                f"Unknown hotkey '{name}'. Use a single char (e.g. 'h') or a "
                "pynput special key name (e.g. 'tab', 'f1', 'caps_lock', 'shift')."
            )
        return keyboard.KeyCode.from_char(name)

    def _matches(self, key) -> bool:
        if isinstance(self._target, keyboard.Key):
            return key == self._target
        return getattr(key, "char", None) == self._target.char

    def _on_press(self, key):
        if self._matches(key) and not self._down:
            self._down = True
            self.pressed.emit()

    def _on_release(self, key):
        if self._matches(key) and self._down:
            self._down = False
            self.released.emit()

    def start(self):
        self._listener.start()

    def stop(self):
        self._listener.stop()


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
            "  border: 1px solid #2A2F38;"
            "  border-radius: 8px;"
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

C_TEXT   = "#F2F4F7"  # primary
C_DIM    = "#9098A4"  # secondary
C_MUTED  = "#5A6270"  # tertiary
C_FAINT  = "#3D434E"  # quaternary
C_BLUE   = "#3B9EFF"
C_ORANGE = "#FF7A29"
C_WIN    = "#4ADE80"
C_LOSS   = "#F87171"


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


def _player_row(p: dict, my_team: int, players_db: dict) -> str:
    rec = players_db.get(p["key"])
    bucket = BUCKET_WITH if p["team"] == my_team else BUCKET_VS
    name = p["name"]
    if len(name) > 16:
        name = name[:15] + "…"

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
        if when and res:
            res_color = C_WIN if res == "W" else C_LOSS
            sub = (
                f"<span style='color:{C_MUTED};font-size:8pt;'>"
                f"{when}&nbsp;<span style='color:{res_color};font-weight:700;'>{res}</span>"
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


def _team_section(label: str, color: str, players: list, is_you: bool, my_team: int, players_db: dict) -> str:
    bar_color = color if is_you else C_FAINT
    label_color = C_TEXT if is_you else C_DIM
    you_tag = (
        f"<span style='color:{color};font-size:8pt;font-weight:700;"
        "letter-spacing:0.16em;'>&nbsp;&nbsp;YOU</span>"
        if is_you else ""
    )

    header = (
        "<table width='100%' cellspacing='0' cellpadding='0' "
        "style='border-collapse:collapse;margin:0;'>"
        "<tr>"
        f"<td width='3' bgcolor='{bar_color}' style='font-size:1px;line-height:1px;'>&nbsp;</td>"
        "<td width='8' style='font-size:1px;'>&nbsp;</td>"
        f"<td align='left' style='color:{label_color};font-size:9pt;font-weight:700;"
        f"letter-spacing:0.18em;'>{label}{you_tag}</td>"
        "</tr>"
        "</table>"
    )

    if players:
        body = "".join(_player_row(p, my_team, players_db) for p in players)
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


def render_html(roster: list[dict], my_team: int, arena: str, players_db: dict) -> str:
    blue = sorted([p for p in roster if p["team"] == 0], key=lambda x: x["name"].lower())
    orange = sorted([p for p in roster if p["team"] == 1], key=lambda x: x["name"].lower())

    if arena:
        a = arena if len(arena) <= 22 else arena[:21] + "…"
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
        + _team_section("BLUE",   C_BLUE,   blue,   my_team == 0, my_team, players_db)
        + spacer
        + _team_section("ORANGE", C_ORANGE, orange, my_team == 1, my_team, players_db)
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
    hotkey = HotkeyBridge(cfg["hotkey"])

    state = {"in_match": False}

    def on_initialized(payload: dict):
        state["in_match"] = True
        html = render_html(payload["players"], payload["myTeam"], payload["arena"], players_db)
        overlay.set_html(html)
        print(f"[match] initialized arena={payload['arena']} myTeam={payload['myTeam']}")

    def on_ended(payload: dict):
        state["in_match"] = False
        i_won = payload["winner"] == payload["myTeam"]
        record = {
            "matchGuid": payload.get("matchGuid"),
            "endedAt": now_iso(),
            "arena": payload["arena"],
            "myTeam": payload["myTeam"],
            "winner": payload["winner"],
            "result": "W" if i_won else "L",
            "players": payload["players"],
        }
        append_match(record)
        update_players_cache(players_db, record)
        save_players(players_db)
        print(f"[match] ended {'WIN' if i_won else 'LOSS'}")

    def on_destroyed():
        state["in_match"] = False
        overlay.set_html(idle_html("Waiting for next match…"))

    def on_status(connected: bool):
        if state["in_match"]:
            return
        if connected:
            overlay.set_html(idle_html("Connected — waiting for match…"))
        else:
            overlay.set_html(idle_html("Disconnected — is RL running with the Stats API enabled?"))

    def on_press():
        overlay.show()
        overlay.raise_()

    def on_release():
        overlay.hide()

    stats.match_initialized.connect(on_initialized)
    stats.match_ended.connect(on_ended)
    stats.match_destroyed.connect(on_destroyed)
    stats.connection_status.connect(on_status)
    hotkey.pressed.connect(on_press)
    hotkey.released.connect(on_release)

    stats.start()
    hotkey.start()

    print(f"[ready] hotkey='{cfg['hotkey']}' position={cfg['position']} ws://{cfg['host']}:{cfg['port']}")
    print(f"        matches → {MATCHES_PATH.name}")
    print(f"        players → {PLAYERS_PATH.name}")

    rc = app.exec()
    stats.stop()
    hotkey.stop()
    sys.exit(rc)


if __name__ == "__main__":
    main()
