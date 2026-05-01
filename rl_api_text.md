# Rocket League Stats API — Reference

Source: <https://www.rocketleague.com/en/developer/stats-api> (captured 2026-05-01).

The Stats API is a **local WebSocket** that the Rocket League client opens
while a match is in progress. It broadcasts JSON messages at a configurable
periodic rate plus event-driven messages. Third-party tools (broadcaster
HUDs, overlays, trackers, etc.) ingest these messages.

---

## 1. Enabling the API (player-side setup)

Edit `<Install Dir>\TAGame\Config\DefaultStatsAPI.ini` **before launching the
client**. Changes made while the game is running require a restart.

| Setting          | Type  | Default       | Description                                                                                  |
| ---------------- | ----- | ------------- | -------------------------------------------------------------------------------------------- |
| `PacketSendRate` | float | `0` (disabled)| Number of `UpdateState` packets per second. Must be > 0 to enable the socket. Capped at 120. |
| `Port`           | int   | `49123`       | Local TCP port the WebSocket listens on.                                                     |

Connect to: `ws://127.0.0.1:<Port>` (default `ws://127.0.0.1:49123`).

---

## 2. Message envelope

Every message follows the same envelope:

```json
{
  "Event": "EventName",
  "Data":  { /* event-specific payload */ }
}
```

### Field-visibility conventions

- **`CONDITIONAL`** — only present when relevant (e.g. `Attacker` only when a
  player is demolished, `Assister` only when a goal had an assist).
- **`SPECTATOR`** — only present if the local client is spectating or on the
  same team as the player. Not visible for opponents during normal play.

### Gotcha: double-encoded `Data` payload (observed in practice)

In this project we found the `Data` payload arrives as a **JSON string**
inside the envelope rather than an inline object — i.e. it must be
`json.loads`'d a second time. See commit `f7d6344` ("Decode the
double-encoded Data payload") and the parser in `rl_h2h.py`. Always be
defensive: try parsing `Data` as a string first, fall back to using it
directly if it's already a dict.

### Connection gotcha

The websockets handshake probe sometimes fails or hangs; in this project we
skip the WS probe and connect via raw TCP (`c9c27ea`).

---

## 3. Tick message

### `UpdateState`

Sent `PacketSendRate` times per second. This is the "current state of the
match" snapshot used to drive HUDs/overlays.

Top-level shape:

```jsonc
{
  "Event": "UpdateState",
  "Data": {
    "MatchGuid": "…",            // online/LAN matches only
    "Players": [ /* per-player */ ],
    "Game":    { /* match-level state */ }
  }
}
```

#### `Players[]` fields

| Field            | Type   | Visibility   | Description                                                              |
| ---------------- | ------ | ------------ | ------------------------------------------------------------------------ |
| `Name`           | string |              | Display name.                                                            |
| `PrimaryId`      | string |              | `Platform\|Uid\|Splitscreen` (e.g. `Steam\|123\|0`, `Epic\|456\|0`).     |
| `Shortcut`       | int    |              | Spectator shortcut number.                                               |
| `TeamNum`        | int    |              | `0` = Blue, `1` = Orange.                                                |
| `Score`          | int    |              | Total match score.                                                       |
| `Goals`          | int    |              | Goals this match.                                                        |
| `Shots`          | int    |              | Shot attempts.                                                           |
| `Assists`        | int    |              | Assists.                                                                 |
| `Saves`          | int    |              | Saves.                                                                   |
| `Touches`        | int    |              | Total ball touches.                                                      |
| `CarTouches`     | int    |              | Touches by car body (not ball).                                          |
| `Demos`          | int    |              | Demolitions inflicted.                                                   |
| `bHasCar`        | bool   | SPECTATOR    | Player currently has a vehicle.                                          |
| `Speed`          | float  | SPECTATOR    | Vehicle speed in Unreal Units/second.                                    |
| `Boost`          | int    | SPECTATOR    | Boost amount 0–100.                                                      |
| `bBoosting`      | bool   | SPECTATOR    | Currently boosting.                                                      |
| `bOnGround`      | bool   | SPECTATOR    | At least 3 wheels touching the world.                                    |
| `bOnWall`        | bool   | SPECTATOR    | Vehicle is on a wall.                                                    |
| `bPowersliding`  | bool   | SPECTATOR    | Holding handbrake.                                                       |
| `bDemolished`    | bool   | SPECTATOR    | Vehicle currently destroyed.                                             |
| `bSupersonic`    | bool   | SPECTATOR    | At supersonic speed.                                                     |
| `Attacker`       | object | CONDITIONAL  | The demolisher; present only while `bDemolished`. `{Name, Shortcut, TeamNum}`. |

#### `Game` fields

| Field          | Type    | Visibility   | Description                                                                         |
| -------------- | ------- | ------------ | ----------------------------------------------------------------------------------- |
| `Teams[]`      | array   |              | One entry per team, ordered by `TeamNum`.                                           |
| ↳ `Name`       | string  |              | Team name.                                                                          |
| ↳ `TeamNum`    | int     |              | Team index.                                                                         |
| ↳ `Score`      | int     |              | Team goal count.                                                                    |
| ↳ `ColorPrimary`   | string |          | Hex color (no `#`) for primary color.                                               |
| ↳ `ColorSecondary` | string |          | Hex color for secondary color.                                                      |
| `TimeSeconds`  | int     |              | Seconds remaining in the match.                                                     |
| `bOvertime`    | bool    |              | Match is in overtime.                                                               |
| `Ball`         | object  |              | `{Speed: float, TeamNum: int}` — `TeamNum` of last team to touch (`255` if none).   |
| `bReplay`      | bool    |              | A goal replay or history replay is active.                                          |
| `bHasWinner`   | bool    |              | A team has won.                                                                     |
| `Winner`       | string  |              | Winning team name; empty string if no winner yet.                                   |
| `Arena`        | string  |              | Asset name of the current map (e.g. `Stadium_P`). Map to friendly names client-side.|
| `bHasTarget`   | bool    |              | Client is viewing a specific vehicle.                                               |
| `Target`       | object  | CONDITIONAL  | `{Name, Shortcut, TeamNum}` of viewed player; empty/0 fields if no target.          |
| `Frame`        | int     | CONDITIONAL  | Current frame number while a replay is active.                                      |
| `Elapsed`      | float   | CONDITIONAL  | Seconds elapsed since game start while a replay is active.                          |

---

## 4. Events

All events share `MatchGuid` (string, set only for **online or LAN** matches).
Fields below are the additional payload beyond `MatchGuid`.

### Match lifecycle

| Event              | When fired                                                       | Extra fields                  |
| ------------------ | ---------------------------------------------------------------- | ----------------------------- |
| `MatchCreated`     | All teams created and replicated.                                | —                             |
| `MatchInitialized` | First countdown starts.                                          | —                             |
| `CountdownBegin`   | Start of each round when countdown begins.                       | —                             |
| `RoundStarted`     | Game enters active state (after countdown finishes).             | —                             |
| `MatchPaused`      | Match admin pauses.                                              | —                             |
| `MatchUnpaused`    | Match admin unpauses.                                            | —                             |
| `MatchEnded`       | Match ends, winner chosen.                                       | `WinnerTeamNum: int`          |
| `PodiumStart`      | Podium state after match end.                                    | —                             |
| `MatchDestroyed`   | Leaving the game.                                                | —                             |
| `ReplayCreated`    | Replay loaded from Match History (NOT goal replays).             | —                             |

### Goal-replay lifecycle

| Event              | When fired                                                                              |
| ------------------ | --------------------------------------------------------------------------------------- |
| `GoalReplayStart`  | Goal replay starts.                                                                     |
| `GoalReplayWillEnd`| Ball explodes during the goal replay. **Not fired if the replay is skipped.**           |
| `GoalReplayEnd`    | Goal replay ends.                                                                       |

### Gameplay events

#### `BallHit`
Sent **one frame after** the ball is hit.

```jsonc
{
  "Players": [ { "Name": "...", "Shortcut": 1, "TeamNum": 0 } ],   // players who hit on that frame
  "Ball": {
    "PreHitSpeed":  0.0,
    "PostHitSpeed": 1450.2,
    "Location": { "X": -512, "Y": 100, "Z": 200 }
  }
}
```

#### `ClockUpdatedSeconds`
In-game clock changed.

```jsonc
{ "TimeSeconds": 180, "bOvertime": false }
```

#### `CrossbarHit`
Ball hit a crossbar.

```jsonc
{
  "BallLocation": { "X": 120, "Y": -2944, "Z": 320 },
  "BallSpeed":   870.3,
  "ImpactForce": 127.5,                 // relative to crossbar normal
  "BallLastTouch": {
    "Player": { "Name": "...", "Shortcut": 1, "TeamNum": 0 },
    "Speed":  120
  }
}
```

#### `GoalScored`

```jsonc
{
  "GoalSpeed":      87.3,               // ball speed when crossing line
  "GoalTime":      127.5,               // length of previous round, seconds
  "ImpactLocation": { "X": 0, "Y": -2944, "Z": 320 },
  "Scorer":   { "Name": "...", "Shortcut": 1, "TeamNum": 0 },
  "Assister": { "Name": "...", "Shortcut": 3, "TeamNum": 0 },   // CONDITIONAL
  "BallLastTouch": {
    "Player": { "Name": "...", "Shortcut": 1, "TeamNum": 0 },
    "Speed":  125
  }
}
```

##### Detecting own-goals

The API has **no own-goal flag**. `Scorer.TeamNum` is the team of the
player who put the ball into the net, regardless of whose net it went
into. There are two viable derivations:

1. **Score-delta (canonical, robust):** snapshot
   `Game.Teams[i].Score` from the latest `UpdateState` at the moment
   `GoalScored` fires, then watch the *next* `UpdateState`. Whichever
   team's count actually went up is the beneficiary. If that's not
   `Scorer.TeamNum`, it's an own-goal. Works on every arena (standard,
   hoops, dropshot, snowday, labs).
2. **Coordinate-Y (instant, arena-dependent):** in standard arenas
   Blue's net is at `Y ≈ −5120` and Orange's at `Y ≈ +5120`. So
   `ImpactLocation.Y < 0` means Orange scored (or Blue own-goaled);
   `Y > 0` means Blue scored (or Orange own-goaled). Compare to
   `Scorer.TeamNum`. Faster than #1 (no waiting) but assumes the Y-axis
   convention, which can break on non-standard layouts.

Note that `GoalScored` and the corresponding score-bumped `UpdateState`
can arrive in either order, so a **deferred-attribution queue** is the
clean implementation: queue every `GoalScored` with its
score-snapshot at queue time, drain on every `UpdateState` (and on
`MatchEnded`/`MatchDestroyed`), and emit the enriched event with a
synthetic `bOwnGoal: bool` field. This is what `rl_h2h.py` does in
`StatsClient._drain_pending_goals`.

Edge case: two goals in the same `UpdateState` window (rare, possible at
low `PacketSendRate`) are ambiguous via score-delta alone — both teams'
scores went up. The pragmatic fallback is to default to
`bOwnGoal=False` for these pile-ups; the coordinate-Y check could
disambiguate but adds complexity for a corner case.

#### `StatfeedEvent`
Fires whenever a player earns a stat (demos, saves, etc).

```jsonc
{
  "EventName":    "Demolish",           // asset name (e.g. "Demolish", "Save")
  "Type":         "Demolition",         // localized display label
  "MainTarget":      { "Name": "PlayerA", "Shortcut": 1, "TeamNum": 0 },
  "SecondaryTarget": { "Name": "PlayerB", "Shortcut": 2, "TeamNum": 1 }   // CONDITIONAL
}
```

---

## 5. Common shapes

**Player ref** (used in `Scorer`, `Assister`, `MainTarget`, `Attacker`, etc.):
```jsonc
{ "Name": "string", "Shortcut": 1, "TeamNum": 0 }
```

**Vector**:
```jsonc
{ "X": 0.0, "Y": 0.0, "Z": 0.0 }
```

**`PrimaryId`** is `Platform|Uid|Splitscreen` — split on `|` to get the
platform (`Steam`, `Epic`, `PS4`, `XboxOne`, `Switch`, …) and persistent uid.
Only `UpdateState.Players[].PrimaryId` carries this; event payloads only
expose `Name`/`Shortcut`/`TeamNum`.

**Units**: speeds are in **Unreal Units/second** (~100 UU = 1 metre).
Supersonic threshold ≈ `2200` UU/s.

---

## 6. Practical notes for this project

- The "websocket" is local-only; no auth. Despite the official wording, **the
  wire format is raw TCP NDJSON** — RFC 6455 WebSocket handshake is not
  honored. Connect with `asyncio.open_connection`, parse with
  `json.JSONDecoder().raw_decode()` over a rolling buffer.
- `Data` is **double-encoded JSON** in practice — parse the envelope, then
  `json.loads` the `Data` field again. (`f7d6344`)
- Be defensive in the `UpdateState` parser: spectator-only fields and
  conditional objects (`Attacker`, `Target`, `Assister`) may be missing —
  type-check every nested access. (`6e38e7f`)
- One bad message must not kill the client — wrap per-message handling
  in a try/except and keep the loop alive. (`3b3776d`)
- Event order during a goal:
  `BallHit` → `GoalScored` → `GoalReplayStart` → `GoalReplayWillEnd` →
  `GoalReplayEnd` → `CountdownBegin` → `RoundStarted`.
- **Doc/wire mismatch**: the doc lists `GoalReplayWillEnd`, the wire emits
  `ReplayWillEnd` (no `Goal` prefix). We don't dispatch on it; flagging in
  case Psyonix fixes one or the other later.
- Match end signal for persisting H2H records: `MatchEnded` (carries
  `WinnerTeamNum`); `MatchDestroyed` is a fallback when the user quits early.
- `Arena` is the asset name (`Stadium_P`, `TrainStation_Night_P`, …). Pretty
  names live in `rl_h2h.py` (`ARENA_BASE` + variant suffixes).
- `MatchGuid` is **only present for online/LAN matches** — don't rely on it
  for offline/freeplay/exhibition.
- `Game.Teams[].Score` is the live team score; capture it on every
  `UpdateState` and snapshot at `MatchEnded` for the final tally.
- `Game.Teams[].ColorPrimary` is the in-game team color (no `#`). May be
  gray (`959595`) in private/training maps; fall back to defaults in that case.
- **Self detection.** The wire has no "this is me" flag. Heuristic:
  spectator-only fields appear only for the local client's team. In 1v1, the
  single player on `my_team` is "me" — record their `PrimaryId` once
  (`Platform|Uid` after dropping the splitscreen suffix) and reuse forever.
  In 2v2/3v3 we can't auto-detect from one match without prior data.
- **PrimaryId platforms observed**: `Epic|<32-char-hex>|0`, `PS4|<numeric>|0`,
  `XboxOne|<numeric>|0`, `Steam|<steamid64>|0`. The third segment is
  splitscreen index — strip it for cross-session identity.
- **What's NOT in the API**: MMR, rank, division, playlist/queue type, party
  composition, replay file path. For MMR you have to scrape Tracker Network
  by `PrimaryId`.
