# Rocket League Head-to-Head Overlay

A small Python overlay that connects to Rocket League's local Stats API, tracks your match history, and shows a transparent always-on-top card with per-opponent W/L records and live session stats.

> Runs on **Windows**. The Rocket League Stats API is Windows-only.

## Setup

1. **Enable the RL Stats API.** Edit `<RL Install>\TAGame\Config\DefaultStatsAPI.ini`:
   ```
   PacketSendRate=2
   Port=49123
   ```
   Save the file and **fully exit Rocket League** — the .ini is read once at launch.

2. **Set RL to Borderless** (Settings → Video → Display Mode → Borderless). Overlays can't render over true-fullscreen DirectX.

3. **Clone and install.** Requires Python 3.10+.
   ```powershell
   git clone https://github.com/Florentde29/rl-h2h.git
   cd rl-h2h
   python -m pip install -r requirements.txt
   ```

## Run

```powershell
python rl_h2h.py
```

Or double-click `start.bat` for a windowless launch. The script can be started before or after Rocket League — it auto-reconnects.

A small **Octane icon appears in the Windows system tray** (notification area, bottom-right). Right-click it for a menu: connection status, "Open data folder", and **Quit**. If you don't see it, expand the chevron `^` next to the clock — Windows hides new tray icons by default. You can pin it: drag the icon onto the taskbar.

To update later: `git pull`.

## Hotkeys (defaults)

The overlay is **held**, not toggled. Defaults match Rocket League's stock
scoreboard binding so you can naturally peek at both at the same time.

| Action          | PC      | Xbox    | PlayStation |
|-----------------|---------|---------|-------------|
| Head-to-head    | **Tab** | **LB**  | **L1**      |
| Session stats   | **F12** | —       | —           |

Releasing the key hides the overlay. Multiple bindings can be combined; the
overlay shows while *any* of them is held.

## Config

Settings live in `config.json` (created on first run). The file is **gitignored**, so `git pull` never overwrites your local edits. When new options are added in a future version, your existing values are preserved and only the new keys are merged in. The top of the file has comments listing every supported keyboard and gamepad key name. Common tweaks:

- `hotkeys` / `session_hotkeys` — lists of triggers, e.g. `["tab", "pad_lb"]`. Avoid D-pad bindings — stock RL maps them to quickchat.
- `position` — `top-right` (default), `top-left`, `top-center`, `bottom-right`, `bottom-left`
- `require_rl_focus` — set to `false` to also show the overlay on the desktop
- `self_player_id` — auto-filled after your first 1v1; set manually if you only play 2v2/3v3 (copy your `Platform|Uid` from `players.json`)

To wipe your match history, delete `matches.jsonl` and `players.json`.

## Privacy

Everything runs locally. `matches.jsonl`, `players.json`, and `config.json` never leave your machine — they're written next to the script. The script connects to `127.0.0.1:49123` only; no remote network calls anywhere in the code. The data is yours.

## Troubleshooting

- **Overlay never shows / Stats API never connects.** Make sure you fully exited Rocket League before saving `DefaultStatsAPI.ini`. The .ini is read once at launch — if RL was already running, your edits are ignored. Confirm the file contains `PacketSendRate=2` (or any non-zero) and `Port=49123`.
- **Overlay invisible during a match.** Set RL to **Borderless** display mode (Settings → Video → Display Mode). True fullscreen owns the whole screen; no overlay can render over it.
- **Tray icon missing.** Windows hides newly-installed tray icons behind the chevron `^` next to the clock by default. Click the chevron, drag the Octane icon onto the taskbar to pin it. Or: Settings → Personalization → Taskbar → Other system tray icons → enable `pythonw.exe`.
- **Gamepad bindings silently ignored.** The `inputs` package needs to be installed (`pip install -r requirements.txt`). The console will say `[hotkey] gamepad listener watching: [...]` on startup if it's working.
- **Port 49123 already in use.** Pick a different port in `DefaultStatsAPI.ini` *and* in `config.json` (`"port": 49123`) — they must match.

## License

[MIT](LICENSE).
