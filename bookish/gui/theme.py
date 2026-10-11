"""
theme.py

Simple app-wide light/dark mode using Qt's Fusion style + a custom
QPalette (the standard, reliable way to get a real dark mode that applies
consistently across platforms in Qt, rather than relying on whatever the
OS theme happens to do). Chess-semantic colors (board squares, the
white/draw/black result bars) intentionally do NOT change with this --
they represent the game, not the app chrome, same as a physical board
doesn't change color because you dimmed the room lights.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


def apply_light(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setPalette(app.style().standardPalette())


def apply_dark(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    window = QColor(45, 45, 48)
    base = QColor(35, 35, 38)
    text = QColor(220, 220, 220)
    disabled_text = QColor(127, 127, 127)
    highlight = QColor(60, 110, 170)

    palette.setColor(QPalette.Window, window)
    palette.setColor(QPalette.WindowText, text)
    palette.setColor(QPalette.Base, base)
    palette.setColor(QPalette.AlternateBase, window)
    palette.setColor(QPalette.ToolTipBase, text)
    palette.setColor(QPalette.ToolTipText, text)
    palette.setColor(QPalette.Text, text)
    palette.setColor(QPalette.Disabled, QPalette.Text, disabled_text)
    palette.setColor(QPalette.Button, window)
    palette.setColor(QPalette.ButtonText, text)
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, disabled_text)
    palette.setColor(QPalette.BrightText, QColor(255, 80, 80))
    palette.setColor(QPalette.Link, QColor(100, 160, 220))
    palette.setColor(QPalette.Highlight, highlight)
    palette.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    palette.setColor(QPalette.PlaceholderText, disabled_text)

    app.setPalette(palette)


# Colors the hand-painted widgets (ChessBoardWidget's margin/coordinates)
# need to pick between explicitly, since they don't come from QPalette.
BOARD_MARGIN = {
    "light": {"bg": QColor("#e8e8e8"), "text": QColor("#333333")},
    "dark": {"bg": QColor("#2d2d30"), "text": QColor("#cccccc")},
}
