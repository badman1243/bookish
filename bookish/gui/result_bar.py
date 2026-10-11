"""A thin horizontal bar showing white-win / draw / black-win proportions."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

WHITE_COLOR = QColor("#e8e8e8")
DRAW_COLOR = QColor("#9a9a9a")
BLACK_COLOR = QColor("#3a3a3a")


class ResultBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.white_pct = 0.0
        self.draw_pct = 0.0
        self.black_pct = 0.0
        self.setFixedHeight(26)
        self.setMinimumWidth(200)

    def set_results(self, white_pct: float, draw_pct: float, black_pct: float) -> None:
        self.white_pct = white_pct
        self.draw_pct = draw_pct
        self.black_pct = black_pct
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        h = self.height()

        x = 0.0
        for pct, color in (
            (self.white_pct, WHITE_COLOR),
            (self.draw_pct, DRAW_COLOR),
            (self.black_pct, BLACK_COLOR),
        ):
            seg_w = w * (pct / 100.0) if (self.white_pct + self.draw_pct + self.black_pct) else 0
            if seg_w <= 0:
                continue
            painter.fillRect(QRectF(x, 0, seg_w, h), color)
            if seg_w > 28:
                painter.setPen(Qt.black if color == WHITE_COLOR else Qt.white)
                painter.drawText(QRectF(x, 0, seg_w, h), Qt.AlignCenter, f"{pct:.0f}%")
            x += seg_w

        painter.setPen(QColor("#666"))
        painter.drawRect(QRectF(0, 0, w - 1, h - 1))
