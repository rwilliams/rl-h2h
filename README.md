# Rocket League Head-to-Head Overlay

A small Python overlay that connects to Rocket League's local Stats API, tracks your match history, and shows a transparent always-on-top card with per-opponent W/L records and live session stats.

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

To update later: `git pull`.

## Hotkeys (defaults)

| Bind | Action |
|---|---|
| Hold **Tab** | Head-to-head card (only appears during a match) |
| Hold **View / Share** (`pad_back` on controller) | Same as Tab |
| Hold **F12** | Session stats overlay |

Releasing the key hides the overlay.

## Config

Settings live in `config.json` (created on first run). The top of the file has comments listing every supported keyboard and gamepad key name. Common tweaks:

- `hotkeys` / `session_hotkeys` — lists of triggers, e.g. `["tab", "pad_back"]`
- `position` — `top-right` (default), `top-left`, `top-center`, `bottom-right`, `bottom-left`
- `require_rl_focus` — set to `false` to also show the overlay on the desktop
- `self_player_id` — auto-filled after your first 1v1; set manually if you only play 2v2/3v3 (copy your `Platform|Uid` from `players.json`)

To wipe your match history, delete `matches.jsonl` and `players.json`.
