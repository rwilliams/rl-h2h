"""H2H card rendering: the per-opponent W/L overlay + its idle and footer states."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import colors
from .arenas import pretty_arena
from .constants import BUCKET_VS, BUCKET_WITH
from .mmr import tier_color


def first_keyboard_label(keys: list) -> Optional[str]:
    """Pick the friendliest key label from a hotkeys list — prefer keyboard names
    over gamepad bindings (more recognizable in a footer hint)."""
    if not keys:
        return None
    for k in keys:
        if not k.startswith("pad_"):
            return k.upper()
    # All gamepad — render the first as-is.
    return keys[0]


def idle_html(message: str) -> str:
    is_disconnected = "disconnect" in message.lower() or "stats api" in message.lower()
    dot_color = "#E5484D" if is_disconnected else colors.C_BLUE
    label = "OFFLINE" if is_disconnected else "STANDBY"
    return (
        "<table width='100%' cellspacing='0' cellpadding='0' style='border-collapse:collapse;'>"
        "<tr>"
        f"<td align='left' style='color:{colors.C_TEXT};font-size:10pt;font-weight:700;"
        "letter-spacing:0.18em;'>HEAD&middot;TO&middot;HEAD</td>"
        "<td align='right' style='font-size:8pt;font-weight:700;letter-spacing:0.16em;'>"
        f"<span style='color:{dot_color};'>&#9679;</span>"
        f"&nbsp;<span style='color:#7A8290;'>{label}</span>"
        "</td>"
        "</tr>"
        "</table>"
        "<div style='height:10px;font-size:1px;line-height:1px;'>&nbsp;</div>"
        f"<div style='color:{colors.C_DIM};font-size:10pt;letter-spacing:0.01em;line-height:140%;'>"
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
            f"<span style='color:{colors.C_MUTED};font-size:8pt;letter-spacing:0.04em;'>"
            "…</span>"
        )
    if entry.get("not_found"):
        return (
            f"<span style='color:{colors.C_MUTED};font-size:8pt;letter-spacing:0.04em;'>"
            "—</span>"
        )
    pick = _mmr_pick(entry, category)
    if not pick:
        return (
            f"<span style='color:{colors.C_MUTED};font-size:8pt;letter-spacing:0.04em;'>"
            "—</span>"
        )
    tier = pick.get("tier") or "Unranked"
    mmr = pick.get("mmr")
    color = tier_color(tier)
    parts = [
        f"<span style='color:{color};font-size:8pt;font-weight:700;"
        f"letter-spacing:0.02em;'>{tier}</span>",
        f"<span style='color:{colors.C_DIM};font-family:Consolas,\"SF Mono\",monospace;"
        f"font-size:8pt;font-weight:600;'>{mmr}</span>",
    ]
    if category == "best":
        playlist = pick.get("playlist")
        if playlist:
            parts.append(
                f"<span style='color:{colors.C_MUTED};font-size:7pt;font-weight:600;"
                f"letter-spacing:0.06em;text-transform:uppercase;'>{playlist}</span>"
            )
    sep = (
        f"<span style='color:{colors.C_FAINT};font-size:8pt;'>&nbsp;·&nbsp;</span>"
    )
    return sep.join(parts)


def _player_row(p: dict, my_team: int, players_db: dict, self_id: Optional[str] = None,
                mmr_db: Optional[dict] = None, mmr_category: str = "best",
                mmr_enabled: bool = False) -> str:
    rec = players_db.get(p["key"])
    bucket = BUCKET_WITH if p["team"] == my_team else BUCKET_VS
    name = p["name"]
    if len(name) > colors.NAME_MAX_LEN:
        name = name[:colors.NAME_MAX_LEN - 1] + "…"
    is_self = self_id is not None and p["key"] == self_id

    if is_self:
        stat_cell = (
            f"<span style='color:{colors.C_WIN};font-size:8pt;font-weight:700;"
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
            f"<td align='left' style='color:{colors.C_TEXT};font-size:10pt;font-weight:600;"
            f"padding:3px 0 0 0;'>{name}</td>"
            "<td align='right' style='padding:3px 0 0 0;white-space:nowrap;'>"
            f"{stat_cell}</td>"
            "</tr>"
            f"{sub_row}"
            "</table>"
        )

    if not rec or (rec[bucket]["wins"] == 0 and rec[bucket]["losses"] == 0):
        stat_cell = (
            f"<span style='color:{colors.C_DIM};font-size:8pt;font-weight:700;"
            f"letter-spacing:0.14em;border:1px solid {colors.C_FAINT};"
            "padding:1px 6px;'>NEW</span>"
        )
        sub = ""
    else:
        b = rec[bucket]
        stat_cell = (
            f"<span style='font-family:Consolas,\"SF Mono\",monospace;"
            f"font-size:10pt;font-weight:600;color:{colors.C_TEXT};'>"
            f"{b['wins']}<span style='color:{colors.C_MUTED};'>&ndash;</span>{b['losses']}"
            "</span>"
        )
        when = humanize_when(b["lastSeenAt"])
        res = b["lastResult"]
        last_score = b.get("lastScore")
        if when and res:
            res_color = colors.C_WIN if res == "W" else colors.C_LOSS
            score_part = ""
            if isinstance(last_score, list) and len(last_score) == 2:
                score_part = (
                    f"&nbsp;<span style='color:{colors.C_MUTED};"
                    "font-family:Consolas,\"SF Mono\",monospace;'>"
                    f"({last_score[0]}&ndash;{last_score[1]})</span>"
                )
            sub = (
                f"<span style='color:{colors.C_MUTED};font-size:8pt;'>"
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
            + f"<span style='color:{colors.C_FAINT};font-size:8pt;'>&nbsp;·&nbsp;</span>"
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
        f"<td align='left' style='color:{colors.C_TEXT};font-size:10pt;font-weight:500;"
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
    label_color = colors.C_TEXT if is_you else colors.C_DIM

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
            f"<div style='color:{colors.C_MUTED};font-size:9pt;"
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
        blue_color = colors.C_BLUE
    if not isinstance(orange_color, str) or not orange_color.startswith("#"):
        orange_color = colors.C_ORANGE

    arena_label = pretty_arena(arena)
    # When MMR is enabled, the right cell shows the active category pill
    # ("MMR · BEST", "MMR · 2V2") so the user always sees what F10 is set to.
    # We show arena on a second header row so neither piece of info is lost.
    if mmr_enabled:
        cat = (mmr_category or "best").upper()
        right_cell = (
            f"<td align='right' style='color:{colors.C_DIM};font-size:8pt;font-weight:700;"
            f"letter-spacing:0.18em;white-space:nowrap;'>"
            f"<span style='color:{colors.C_MUTED};font-weight:500;'>MMR&nbsp;·&nbsp;</span>"
            f"{cat}"
            "</td>"
        )
    elif arena_label:
        a = arena_label if len(arena_label) <= 22 else arena_label[:21] + "…"
        right_cell = (
            f"<td align='right' style='color:{colors.C_MUTED};font-size:8pt;font-weight:500;"
            f"letter-spacing:0.10em;'>{a.upper()}</td>"
        )
    else:
        right_cell = "<td></td>"

    arena_subline = ""
    if mmr_enabled and arena_label:
        a = arena_label if len(arena_label) <= 22 else arena_label[:21] + "…"
        arena_subline = (
            f"<div style='color:{colors.C_MUTED};font-size:8pt;font-weight:500;"
            f"letter-spacing:0.10em;text-align:right;padding-top:1px;'>"
            f"{a.upper()}</div>"
        )

    header = (
        "<table width='100%' cellspacing='0' cellpadding='0' "
        "style='border-collapse:collapse;'>"
        "<tr>"
        f"<td align='left' style='color:{colors.C_TEXT};font-size:10pt;font-weight:700;"
        "letter-spacing:0.18em;'>HEAD&middot;TO&middot;HEAD</td>"
        f"{right_cell}"
        "</tr>"
        "</table>"
        f"{arena_subline}"
        f"<div style='height:1px;background-color:{colors.C_FAINT};font-size:1px;line-height:1px;"
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


def session_footer_html(cfg: dict, view: str) -> str:
    """Hotkey hint row for the session card. View-specific copy: the session
    view advertises F11 to swap to graph; the graph view doesn't reach here
    because it's painted onto a pixmap, not HTML."""
    expand_label = first_keyboard_label(cfg.get("expand_hotkeys") or [])
    if view != "session" or not expand_label:
        return ""
    return (
        "<table cellpadding='0' cellspacing='0' width='100%'>"
        "<tr><td height='10'>&nbsp;</td></tr></table>"
        "<table width='100%' cellspacing='0' cellpadding='0' "
        "style='border-collapse:collapse;'>"
        "<tr>"
        f"<td align='left' style='color:{colors.C_MUTED};font-size:8pt;"
        f"letter-spacing:0.02em;padding:1px 0;'><b>{expand_label}</b> graph</td>"
        f"<td align='right' style='color:{colors.C_MUTED};font-size:8pt;"
        f"letter-spacing:0.02em;padding:1px 0;'>session</td>"
        "</tr>"
        "</table>"
    )


def h2h_footer_html(cfg: dict, expanded: bool, session) -> str:
    """Single-table footer for the H2H overlay.

    - Always: hotkey hint row (`F11 expand` left, `F12 session` right).
    - When MMR is enabled: a second hotkey row with `F10 cycle MMR` and the
      current category (e.g. "best", "2v2") so the user sees what's selected.
    - When `session` is supplied AND has a split: a `Format | session | yours` row.

    Rows live in the same <table>, so Qt RichText doesn't insert its native
    ~12-20px between-block margin between them.
    """
    from .session_stats import session_has_split  # local import: avoids cycle

    expand_label = first_keyboard_label(cfg.get("expand_hotkeys") or [])
    session_label = first_keyboard_label(cfg.get("session_hotkeys") or [])
    cycle_label = first_keyboard_label(cfg.get("cycle_hotkeys") or [])
    mmr_enabled = bool(cfg.get("mmr_enabled", False))
    mmr_category = cfg.get("mmr_category", "best")

    rows: list[str] = []
    cell = (
        "<td align='{align}' style='color:{C_MUTED};font-size:8pt;"
        "letter-spacing:0.02em;padding:1px 0;'>{content}</td>"
    )

    # Format row first — explanatory info above the actionable hotkey row.
    if session is not None and session_has_split(session):
        rows.append(
            "<tr>"
            + cell.format(align="left",  C_MUTED=colors.C_MUTED, content="Format")
            + cell.format(align="right", C_MUTED=colors.C_MUTED,
                          content="<b>session</b> | yours")
            + "</tr>"
        )

    if mmr_enabled and cycle_label:
        rows.append(
            "<tr>"
            + cell.format(align="left",  C_MUTED=colors.C_MUTED,
                          content=f"<b>{cycle_label}</b> cycle MMR")
            + cell.format(align="right", C_MUTED=colors.C_MUTED,
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
            + cell.format(align="left",  C_MUTED=colors.C_MUTED, content=h_left)
            + cell.format(align="right", C_MUTED=colors.C_MUTED, content=h_right)
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
