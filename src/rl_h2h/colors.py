"""Palette + name-truncation knobs used by every renderer.

These are module attributes so ``apply_overrides`` can mutate them at startup
from user config and have every consumer see the new values. Renderers must
access them as ``colors.C_TEXT`` (not ``from colors import C_TEXT``) — binding
the name at import time would freeze the default and ignore overrides.
"""
from __future__ import annotations

# Palette baseline. Each constant can be overridden via cfg["colors"].
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

EM_DASH = f"<span style='color:{C_MUTED};'>—</span>"
PAIR_SEP = f"<span style='color:{C_MUTED};font-weight:500;'>&nbsp;|&nbsp;</span>"


def apply_overrides(cfg: dict) -> None:
    """Patch palette + truncation globals from cfg. Called once at startup.

    Color constants are referenced directly by render functions, so the cleanest
    way to honor user overrides is to mutate the module globals before any
    rendering happens. EM_DASH / PAIR_SEP also need to be recomputed because
    they bake C_MUTED in at import time.
    """
    global C_TEXT, C_DIM, C_MUTED, C_FAINT, C_BLUE, C_ORANGE, C_WIN, C_LOSS
    global EM_DASH, PAIR_SEP, NAME_MAX_LEN
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
    PAIR_SEP = f"<span style='color:{C_MUTED};font-weight:500;'>&nbsp;|&nbsp;</span>"
    nml = cfg.get("name_max_length")
    if isinstance(nml, int) and nml > 0:
        NAME_MAX_LEN = nml
