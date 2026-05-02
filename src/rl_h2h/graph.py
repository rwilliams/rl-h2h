"""MMR-evolution graph painted into a QPixmap (Qt RichText can't render it)."""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPixmap, QPolygon

from . import colors
from .mmr import MMR_RANK_ZONES, attribute_mmr_points
from .render_h2h import first_keyboard_label
from .storage import match_playlist


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


def _draw_marker(painter: QPainter, x: int, y: int, color: QColor, radius: int) -> None:
    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(color))
    painter.drawEllipse(QPoint(x, y), radius, radius)


def render_graph_pixmap(playlist: str, snapshots: list[dict],
                        matches: list[dict], cfg: dict,
                        canvas_width: int = 348, canvas_height: int = 220) -> QPixmap:
    """Paint the MMR-evolution graph for ``playlist`` into a QPixmap (header,
    plot region with rank-zone bands and W/L markers, footer with hotkey
    hints). Caller writes the result via Overlay.set_pixmap()."""
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
    painter.setPen(QColor(colors.C_TEXT))
    title = f"MMR · {playlist.upper()}"
    painter.drawText(plot_left, _GRAPH_HEADER_H - 8, title)

    if len(points) >= 2:
        delta = points[-1]["mmr"] - points[0]["mmr"]
        sign = "+" if delta > 0 else ""
        delta_text = f"{sign}{delta}"
        if delta > 0:
            pill_bg, pill_fg = QColor(colors.C_WIN), QColor(colors.C_PILL_TEXT_WIN)
        elif delta < 0:
            pill_bg, pill_fg = QColor(colors.C_LOSS), QColor(colors.C_PILL_TEXT_LOSS)
        else:
            pill_bg, pill_fg = QColor(colors.C_FAINT), QColor(colors.C_DIM)
        painter.setFont(num_font_md)
        fm = painter.fontMetrics()
        text_w = fm.horizontalAdvance(delta_text)
        pad_x, pad_y = 6, 2
        pill_w = text_w + pad_x * 2
        pill_h = fm.height() + pad_y
        pill_x = plot_right - pill_w
        pill_y = (_GRAPH_HEADER_H - pill_h) // 2
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(pill_bg))
        painter.drawRoundedRect(pill_x, pill_y, pill_w, pill_h,
                                _GRAPH_CHIP_RADIUS, _GRAPH_CHIP_RADIUS)
        painter.setPen(QPen(pill_fg))
        text_baseline = pill_y + pill_h - pad_y - fm.descent() + 1
        painter.drawText(pill_x + pad_x, text_baseline, delta_text)

    sep_pen = QPen(QColor(colors.C_FAINT))
    sep_pen.setWidth(1)
    painter.setPen(sep_pen)
    painter.drawLine(plot_left, _GRAPH_HEADER_H, plot_right, _GRAPH_HEADER_H)

    if len(points) < 2:
        painter.setFont(small_font)
        painter.setPen(QColor(colors.C_MUTED))
        if not snapshots:
            msg = "MMR not tracked yet — enable in tray menu and play a match"
        elif not any((s.get("playlists") or {}).get(playlist) is not None
                     for s in snapshots):
            msg = f"No {playlist} MMR yet — play a ranked {playlist} match"
        elif not any(match_playlist(m) == playlist for m in matches):
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
    anchor_pen = QPen(QColor(colors.C_FAINT))
    anchor_pen.setWidth(1)
    anchor_pen.setStyle(Qt.DashLine)
    painter.setPen(anchor_pen)
    painter.drawLine(plot_left + 24, start_y, plot_right - 12, start_y)

    line_pts = [QPoint(to_x(i), to_y(p["mmr"])) for i, p in enumerate(points)]
    line_pen = QPen(QColor(colors.C_TEXT))
    line_pen.setWidth(2)
    line_pen.setCapStyle(Qt.RoundCap)
    line_pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(line_pen)
    painter.drawPolyline(QPolygon(line_pts))

    last_idx = len(points) - 1
    marker_color = {"W": colors.C_WIN, "L": colors.C_LOSS}
    for i, p in enumerate(points):
        marker = p.get("marker") or "snap"
        base_color = QColor(marker_color.get(marker, colors.C_MUTED))
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
    painter.setPen(QColor(colors.C_TEXT))
    label = str(cur_mmr)
    fm = painter.fontMetrics()
    label_w = fm.horizontalAdvance(label)
    label_x = max(plot_left, min(plot_right - label_w, cur_x - label_w // 2))
    label_y = cur_y - 12
    if label_y - fm.ascent() < plot_top + 2:
        label_y = cur_y + fm.ascent() + 8
    painter.drawText(label_x, label_y, label)

    painter.setFont(num_font_sm)
    painter.setPen(QColor(colors.C_MUTED))
    painter.drawText(plot_left, plot_top + 8, str(y_max))
    painter.drawText(plot_left, plot_bottom - 2, str(y_min))

    cap_text = f"last {len(points)}"
    painter.setFont(small_font)
    painter.setPen(QColor(colors.C_MUTED))
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
    cycle_label = first_keyboard_label(cfg.get("cycle_hotkeys") or [])
    expand_label = first_keyboard_label(cfg.get("expand_hotkeys") or [])
    base_y = canvas_height - footer_h + 16

    family = cfg.get("font_family", "Segoe UI")
    key_font = _graph_mono_font(7, bold=True)
    verb_font = small_font
    pl_font = _graph_font(family, 7, bold=True)
    key_color = QColor(colors.C_DIM)
    verb_color = QColor(colors.C_MUTED)
    sep_color = QColor(colors.C_FAINT)

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
    painter.setPen(QPen(QColor(colors.C_TEXT)))
    rect = painter.fontMetrics().boundingRect(pl_label)
    painter.drawText(right - rect.width(), base_y, pl_label)
