"""Auto-popup card flashed for ~5s when a match ends. Result + score + per-match stats."""
from __future__ import annotations

from . import colors
from .session_stats import (
    MatchStats,
    pair_count,
    pair_fastest,
    pair_max,
    stat_row,
    stat_section,
)


def render_summary_html(payload: dict, ms: MatchStats) -> str:
    my_team = payload.get("myTeam")
    winner = payload.get("winner")
    i_won = winner == my_team
    label = "WIN" if i_won else "LOSS"
    accent = colors.C_WIN if i_won else colors.C_LOSS

    score = payload.get("score")
    if isinstance(score, list) and len(score) == 2 and isinstance(my_team, int):
        score_html = (
            f"<span style='font-family:Consolas,\"SF Mono\",monospace;font-size:14pt;"
            f"font-weight:700;color:{colors.C_TEXT};'>"
            f"{score[my_team]}<span style='color:{colors.C_MUTED};'>&ndash;</span>"
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
    if ms.saves:     play_rows.append(stat_row("Saves",     pair_count(ms.saves, ms.saves_self, always_pair=True)))
    if ms.shots:     play_rows.append(stat_row("Shots",     pair_count(ms.shots, ms.shots_self, always_pair=True)))
    if ms.demos:     play_rows.append(stat_row("Demos",     pair_count(ms.demos, ms.demos_self, always_pair=True)))
    if ms.demoed_self:
        play_rows.append(stat_row("Demoed",    str(ms.demoed_self)))
    if ms.crossbars: play_rows.append(stat_row("Crossbars", pair_count(ms.crossbars, ms.crossbars_self, always_pair=True)))

    # ACTIVITY rows: derived from UpdateState ticks. Touches is exact (cumulative
    # API counter); distance and boost-used are integrated locally at ~2 Hz, so
    # they're prefixed with ~ to flag them as approximations.
    t_scope, t_self = ms.touches_leader()
    d_scope, d_self = ms.distance_leader_m()
    b_scope, b_self = ms.boost_used_leader()
    activity_rows = []
    if t_scope:
        activity_rows.append(stat_row("Touches", pair_count(t_scope, t_self, always_pair=True)))
    if d_scope:
        activity_rows.append(stat_row("Distance", f"~{d_scope}m{colors.PAIR_SEP}~{d_self}m"))
    if b_scope:
        activity_rows.append(stat_row("Boost used", f"~{b_scope}{colors.PAIR_SEP}~{b_self}"))

    fun_rows = []
    if ms.max_goal_speed > 0:
        fun_rows.append(stat_row("Max goal speed",   pair_max(ms.max_goal_speed, ms.max_goal_speed_self, always_pair=True)))
    if ms.max_ball_speed > 0:
        fun_rows.append(stat_row("Max ball speed",   pair_max(ms.max_ball_speed, ms.max_ball_speed_self, always_pair=True)))
    if ms.max_impact_force > 0:
        fun_rows.append(stat_row("Hardest crossbar",
                                 pair_max(ms.max_impact_force, ms.max_impact_force_self, always_pair=True)))
    if ms.fastest_goal_time is not None:
        fun_rows.append(stat_row("Fastest goal",
                                 pair_fastest(ms.fastest_goal_time, ms.fastest_goal_time_self, always_pair=True)))
    if ms.own_goals > 0:
        fun_rows.append(stat_row("Own goals",
                                 pair_count(ms.own_goals, ms.own_goals_self, always_pair=True)))

    body = ""
    if play_rows:
        body += stat_section("PLAY", play_rows)
    if activity_rows:
        body += stat_section("ACTIVITY", activity_rows)
    if fun_rows:
        body += stat_section("FUN", fun_rows)
    if body:
        divider = (
            f"<div style='height:1px;background-color:{colors.C_FAINT};font-size:1px;line-height:1px;"
            "margin-top:8px;'>&nbsp;</div>"
            "<div style='height:6px;font-size:1px;line-height:1px;'>&nbsp;</div>"
        )
        return header + divider + body
    return header
