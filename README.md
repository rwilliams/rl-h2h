# Rocket League Head-to-Head Overlay

A small Python overlay that connects to Rocket League's local Stats API, tracks your match history, and shows a transparent always-on-top card with per-opponent W/L records and live session stats.

> Runs on **Windows**. The Rocket League Stats API is Windows-only.

## Setup

This setup is done entirely in PowerShell. Press **Win + X** and click **Terminal** (Windows 11) or **Windows PowerShell** (Windows 10) to open it. Every command block below is something you copy and paste — in order, top to bottom.

### 1. Install Python and Git

```powershell
winget install --id Python.Python.3.12 -e
winget install --id Git.Git -e
```

After both finish, **close and reopen PowerShell** so the new tools land on your PATH.

> If `winget` isn't recognised (older Windows 10 builds), install the **App Installer** package from the Microsoft Store first — that ships winget. As a manual alternative, download Python from <https://www.python.org/downloads/> (tick *"Add python.exe to PATH"*) and Git from <https://git-scm.com/download/win>.

### 2. Download this project

```powershell
cd $env:USERPROFILE\Documents
git clone https://github.com/Florentde29/rl-h2h.git
cd rl-h2h
```

That clones the project into `Documents\rl-h2h\` and `cd`s into it. Stay in this folder for the next command.

### 3. Install the Python dependencies

```powershell
python -m pip install -r requirements.txt
```

About 30 seconds. You only do this once.

### 4. Enable the Rocket League Stats API

Open this file in Notepad (Rocket League ships it):

```
<Rocket League install folder>\TAGame\Config\DefaultStatsAPI.ini
```

Make sure it contains these two lines:

```
PacketSendRate=2
Port=49123
```

Save it, then **fully exit Rocket League** if it's running. RL only reads this `.ini` at launch — saving while the game is open does nothing.

### 5. Set Rocket League to Borderless

In RL: **Settings → Video → Display Mode → Borderless**. Overlays can't render over true-fullscreen DirectX.

### 6. First run

In the same PowerShell window:

```powershell
python rl_h2h.py
```

The next section explains what to expect.

## Run

```powershell
python rl_h2h.py
```

Or double-click `start.bat` in the project folder for a windowless launch. The script can be started before or after Rocket League — it auto-reconnects.

A small **Rocket League H2H icon appears in the Windows system tray** (notification area, bottom-right). Right-click it for a menu: connection status, "Open data folder", and **Quit**. If you don't see it, expand the chevron `^` next to the clock — Windows hides new tray icons by default. You can pin it: drag the icon onto the taskbar.

To launch automatically every time Windows boots, press **Win + R**, type `shell:startup`, press Enter, and drop a shortcut to `start.bat` into the folder that opens.

## Hotkeys (defaults)

The overlay is **held**, not toggled. Defaults match Rocket League's stock scoreboard binding so you can naturally peek at both at the same time.

| Action               | PC      | Xbox    | PlayStation |
|----------------------|---------|---------|-------------|
| Head-to-head         | **Tab** | **LB**  | **L1**      |
| Session stats        | **F12** | —       | —           |
| Toggle expanded H2H  | **F11** | —       | —           |

Hold to show; release to hide. Multiple bindings can be combined; the overlay shows while *any* of them is held.

**F11** toggles whether the H2H overlay also shows the session stats card underneath. The choice persists across launches (saved to `config.json`).

## Config

Settings live in `config.json` (created on first run). The file is **gitignored**, so `git pull` never overwrites your local edits. When new options are added in a future version, your existing values are preserved and only the new keys are merged in. The top of the file has comments listing every supported keyboard and gamepad key name. Common tweaks:

- `hotkeys` / `session_hotkeys` — lists of triggers, e.g. `["tab", "pad_lb"]`. Avoid D-pad bindings — stock RL maps them to quickchat.
- `position` — `top-right` (default), `top-left`, `top-center`, `bottom-right`, `bottom-left`
- `require_rl_focus` — set to `false` to also show the overlay on the desktop
- `self_player_id` — auto-filled after your first 1v1; set manually if you only play 2v2/3v3 (copy your `Platform|Uid` from `players.json`)

To wipe your match history, delete `matches.jsonl` and `players.json`.

## Privacy

Everything runs locally. `matches.jsonl`, `players.json`, and `config.json` never leave your machine — they're written next to the script. The script connects to `127.0.0.1:49123` only; no remote network calls anywhere in the code. The data is yours.

## Updates

Auto-update is **opt-in**. To enable it: right-click the tray icon (bottom-right, the Rocket League H2H icon) and tick **Auto-update on launch**. From then on, every time you run `start.bat`, the launcher checks GitHub for a newer version and applies it silently before starting the app. Untick it any time to go back to manual updates (`git pull`).

When auto-update is enabled:

- **Network failures don't block launch.** Offline? The updater times out after 5 s and the app starts on whatever's on disk.
- **Local edits are preserved.** If you've modified `rl_h2h.py` (or any other tracked file), the updater detects it and skips that run, leaving your customisations alone. You'll fall behind upstream until you reset.
- **Your data is never touched.** `config.json`, `matches.jsonl`, and `players.json` are gitignored and ignored by the updater too.
- **Two modes, picked automatically:**
  - If you cloned with `git`, the updater runs `git pull --ff-only`.
  - If you downloaded the repo as a ZIP from GitHub (no git installed), the updater fetches the latest archive directly from GitHub.
- **Run logs** are written to `update.log` next to the script if you want to see what happened.

The setting persists in `config.json` (`auto_update: true|false`). Launching with `python rl_h2h.py` instead of `start.bat` always skips the updater, regardless of the toggle.

## Troubleshooting

- **Overlay never shows / Stats API never connects.** Make sure you fully exited Rocket League before saving `DefaultStatsAPI.ini`. The .ini is read once at launch — if RL was already running, your edits are ignored. Confirm the file contains `PacketSendRate=2` (or any non-zero) and `Port=49123`.
- **Overlay invisible during a match.** Set RL to **Borderless** display mode (Settings → Video → Display Mode). True fullscreen owns the whole screen; no overlay can render over it.
- **Tray icon missing.** Windows hides newly-installed tray icons behind the chevron `^` next to the clock by default. Click the chevron, drag the Rocket League H2H icon onto the taskbar to pin it. Or: Settings → Personalization → Taskbar → Other system tray icons → enable `pythonw.exe`.
- **Gamepad bindings silently ignored.** The `inputs` package needs to be installed (`pip install -r requirements.txt`). The console will say `[hotkey] gamepad listener watching: [...]` on startup if it's working.
- **Port 49123 already in use.** Pick a different port in `DefaultStatsAPI.ini` *and* in `config.json` (`"port": 49123`) — they must match.

## License

[MIT](LICENSE).
