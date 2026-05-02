"""System tray icon (loaded from assets/, with a programmatic fallback)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap

from . import colors
from .paths import ASSETS_DIR


def make_tray_icon() -> QIcon:
    """Tray icon. Prefers ``assets/icon.ico`` / ``assets/icon.png``; falls back
    to a programmatic dark-circle-with-lime-H if neither is present."""
    for name in ("icon.ico", "icon.png"):
        path = ASSETS_DIR / name
        if path.exists():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
    pix = QPixmap(32, 32)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#101415"))
    p.setPen(QPen(QColor(colors.C_WIN), 2))
    p.drawEllipse(2, 2, 28, 28)
    p.setPen(QColor(colors.C_WIN))
    p.setFont(QFont("Segoe UI", 13, QFont.Bold))
    p.drawText(pix.rect(), Qt.AlignCenter, "H")
    p.end()
    return QIcon(pix)
