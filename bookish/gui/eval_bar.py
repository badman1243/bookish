"""
eval_bar.py

A small hand-painted vertical evaluation bar -- the same "just paint it
with QPainter" spirit as result_bar.py's white/draw/black bar. Fills
from the bottom in White's color up to a fraction of the widget's
height that reflects how good the position is for White, always from
White's point of view regardless of whose turn it is (Lichess's own
convention, so the bar doesn't visually "flip" every other ply).
"""

from __future__ import annotations

import math

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

WHITE_COLOR = QColor("#e8e8e8")
BLACK_COLOR = QColor("#3a3a3a")
MIDLINE_COLOR = QColor("#808080")

# How many centipawns the logistic curve treats as "one full swing" --
# not a real evaluation cutoff, just the curve's shape. A logistic curve
# (rather than a hard clamp) is what makes +2 and +9 visually
# distinguishable near the middle while both still read as "White is
# much better" out toward the ends, matching how Lichess's own bar
# behaves.
_SCALE_CP = 300.0


class EvalBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._cp: int | None = 0
        self._mate: int | None = None
        self.setMinimumWidth(30)

    def set_score(self, cp: int | None, mate: int | None) -> None:
        self._cp = cp
        self._mate = mate
        self.update()

    def clear(self) -> None:
        self.set_score(None, None)

    def _white_fraction(self) -> float:
        if self._mate is not None:
            return 1.0 if self._mate > 0 else 0.0
        if self._cp is None:
            return 0.5
        return 1.0 / (1.0 + math.exp(-self._cp / _SCALE_CP))

    def _label_text(self) -> str:
        if self._mate is not None:
            return f"M{abs(self._mate)}"
        if self._cp is None:
            return "\u2013"
        return f"{self._cp / 100.0:+.1f}"

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        fraction = self._white_fraction()
        white_h = h * fraction

        painter.fillRect(QRectF(0, 0, w, h - white_h), BLACK_COLOR)
        painter.fillRect(QRectF(0, h - white_h, w, white_h), WHITE_COLOR)
        painter.setPen(MIDLINE_COLOR)
        painter.drawLine(0, int(h / 2), w, int(h / 2))

        # Keep the label readable against whichever color it lands on:
        # near the White end it sits low in white territory (dark text),
        # near the Black end it sits high in black territory (light text).
        in_white_area = fraction >= 0.5
        painter.setPen(Qt.black if in_white_area else Qt.white)
        text_y = (h - 16) if in_white_area else 2
        painter.drawText(QRectF(0, text_y, w, 16), Qt.AlignCenter, self._label_text())

        # A near-white fill (WHITE_COLOR) has almost no contrast against a
        # light-mode window background, so without a border the bar reads
        # as "not there" whenever White's doing well -- exactly the report
        # that prompted this. A visible outline (same convention
        # result_bar.py already uses) fixes it regardless of theme,
        # rather than picking a fill color that only works in one mode.
        painter.setPen(QColor("#666"))
        painter.drawRect(QRectF(0, 0, w - 1, h - 1))
