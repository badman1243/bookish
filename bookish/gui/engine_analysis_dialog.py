"""
engine_analysis_dialog.py

The resizable "computer analysis" popup, in the same spirit as
Lichess's own analysis board and most standalone chess GUIs: an
evaluation bar plus a live Multi-PV list that keeps re-analyzing
whatever position is currently on the shared board, until you pause it
or close the window. A genuinely non-modal, resizable top-level window
(not a fixed-size dialog) -- you can keep moving pieces on the board
while it's open, and it follows along.

The engine process itself is owned by EnginePanel, not this dialog --
closing the popup only stops the in-flight analysis, so reopening it
for the same engine is instant rather than relaunching the process.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import chess
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QSplitter, QLabel, QPushButton,
    QSpinBox, QCheckBox, QTableWidget, QTableWidgetItem, QHeaderView,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.engine_manager import EngineManager, AnalysisLine, EngineError
from gui.board_widget import ChessBoardWidget
from gui.eval_bar import EvalBar

# One arrow color per Multi-PV rank, brightest/thickest for the top line
# -- matches how Lichess draws its own candidate-move arrows when more
# than one line is shown at once.
ARROW_COLORS = [
    QColor(0, 150, 60, 190),
    QColor(60, 110, 200, 170),
    QColor(200, 140, 0, 150),
    QColor(160, 60, 160, 130),
]
_MAX_PV_MOVES_SHOWN = 12  # a full line can run long; this is a display cap, not an analysis one


class AnalysisWorker(QThread):
    # Every emission carries the generation this worker was started
    # under, so the dialog can drop anything that arrives late from a
    # superseded analysis (queued cross-thread signals can still be in
    # flight after the worker has been told to stop) instead of
    # repainting stale lines/arrows for a position that's no longer the
    # one on the board.
    lines_updated = Signal(int, object)  # generation, list[AnalysisLine]
    failed = Signal(int, str)  # generation, message
    ended = Signal(int)  # generation -- the stream finished without anyone asking it to

    def __init__(self, manager: EngineManager, board: chess.Board, multipv: int, generation: int):
        super().__init__()
        self.manager = manager
        self.board = board
        self.multipv = multipv
        self.generation = generation
        self._cancel = threading.Event()

    def request_stop(self) -> None:
        # Order matters: set the token *before* asking the manager to
        # stop, so a stop that lands before this worker has registered
        # its analysis still gets honored (see EngineManager.analyse_stream).
        self._cancel.set()
        self.manager.stop()

    def run(self) -> None:
        try:
            for lines in self.manager.analyse_stream(self.board, multipv=self.multipv, cancel=self._cancel):
                self.lines_updated.emit(self.generation, lines)
        except EngineError as exc:
            self.failed.emit(self.generation, str(exc))
            return
        if not self._cancel.is_set():
            # Something other than us ended the analysis (python-chess
            # cancels a running analysis whenever another command, like
            # configure(), reaches the engine). Say so, rather than
            # leaving the window claiming "Analyzing..." over a dead stream.
            self.ended.emit(self.generation)


class EngineAnalysisDialog(QDialog):
    def __init__(self, engine_name: str, manager: EngineManager, board: ChessBoardWidget, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.board = board
        self.worker: AnalysisWorker | None = None
        # Workers told to stop that hadn't actually finished yet when we
        # moved on -- kept referenced until they do, since destroying a
        # QThread object while its thread is still running crashes Qt.
        self._retired_workers: set[AnalysisWorker] = set()
        self._generation = 0
        self._running = False
        self._show_arrows = True

        self.setWindowTitle(f"Analysis \u2014 {engine_name}")
        self.setModal(False)
        self.setSizeGripEnabled(True)  # a visible corner handle, on top of the ordinary resizable border
        self.resize(480, 340)

        layout = QVBoxLayout(self)

        top_row = QHBoxLayout()
        self.toggle_btn = QPushButton("Stop")
        self.toggle_btn.clicked.connect(self._toggle_running)
        top_row.addWidget(self.toggle_btn)

        top_row.addWidget(QLabel("Multi-PV:"))
        self.multipv_spin = QSpinBox()
        self.multipv_spin.setRange(1, 8)
        self.multipv_spin.setValue(3)
        self.multipv_spin.valueChanged.connect(self._restart_analysis)
        top_row.addWidget(self.multipv_spin)

        self.arrows_check = QCheckBox("Arrows")
        self.arrows_check.setChecked(True)
        self.arrows_check.toggled.connect(self._on_arrows_toggled)
        top_row.addWidget(self.arrows_check)
        top_row.addStretch(1)
        layout.addLayout(top_row)

        splitter = QSplitter(Qt.Horizontal)
        self.eval_bar = EvalBar()
        splitter.addWidget(self.eval_bar)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["#", "Score", "Depth", "Line"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setWordWrap(True)
        splitter.addWidget(self.table)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, stretch=1)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

        self.board.positionChanged.connect(self._on_position_changed)
        self._start_analysis()

    # -- lifecycle -----------------------------------------------------

    def _toggle_running(self) -> None:
        if self._running:
            self._stop_analysis()
        else:
            self._start_analysis()

    def _restart_analysis(self) -> None:
        if self._running:
            self._stop_analysis()
            self._start_analysis()

    def _on_position_changed(self) -> None:
        if self._running:
            self._stop_analysis()
            self._start_analysis()

    def _on_arrows_toggled(self, checked: bool) -> None:
        self._show_arrows = checked
        if not checked:
            self.board.clear_arrows()

    def _start_analysis(self) -> None:
        self.table.setRowCount(0)
        self.status_label.setText("Analyzing\u2026")
        self.toggle_btn.setText("Stop")
        self._running = True
        self._generation += 1
        snapshot = self.board.get_board()
        self.worker = AnalysisWorker(self.manager, snapshot, self.multipv_spin.value(), self._generation)
        self.worker.lines_updated.connect(self._on_lines)
        self.worker.failed.connect(self._on_failed)
        self.worker.ended.connect(self._on_ended)
        self.worker.start()

    def _stop_analysis(self) -> None:
        self._generation += 1  # anything still in flight from the old worker is now stale
        worker, self.worker = self.worker, None
        if worker is not None:
            worker.request_stop()
            # A stop normally lands in milliseconds; the short wait just
            # keeps a following start from overlapping it. If the engine
            # is unusually slow to answer, don't freeze the GUI over it --
            # retire the worker and let it finish on its own.
            if not worker.wait(1000):
                self._retired_workers.add(worker)
                worker.finished.connect(lambda w=worker: self._retired_workers.discard(w))
        self._running = False
        self.toggle_btn.setText("Start")
        self.status_label.setText("Stopped.")
        self.board.clear_arrows()

    def run_while_paused(self, action) -> None:
        """Runs `action()` with this dialog's analysis stopped, then
        resumes it on the current position if it had been running.
        Anything that sends a command to the engine (changing options,
        pressing a UCI button) has to go through here while a popup is
        open -- see EngineManager.configure for why."""
        was_running = self._running
        if was_running:
            self._stop_analysis()
        try:
            action()
        finally:
            if was_running:
                self._start_analysis()

    def _on_ended(self, generation: int) -> None:
        if generation != self._generation:
            return
        self._running = False
        self.toggle_btn.setText("Start")
        self.status_label.setText("Analysis ended \u2014 press Start to resume.")

    def _on_failed(self, generation: int, message: str) -> None:
        if generation != self._generation:
            return  # a superseded analysis erroring out on its way down -- not news
        self._running = False
        self.toggle_btn.setText("Start")
        self.status_label.setText(f"Engine error: {message}")

    def closeEvent(self, event) -> None:
        self._stop_analysis()
        for worker in list(self._retired_workers):
            worker.wait(2000)  # let any straggler finish before this dialog's objects go away
        try:
            self.board.positionChanged.disconnect(self._on_position_changed)
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)

    # -- display ---------------------------------------------------------

    def _on_lines(self, generation: int, lines: list[AnalysisLine]) -> None:
        if generation != self._generation or not lines:
            return
        top = lines[0]
        self.eval_bar.set_score(top.score_cp, top.score_mate)

        self.table.setRowCount(len(lines))
        for row, line in enumerate(lines):
            score_text = self._format_score(line)
            pv_text = " ".join(line.pv_san[:_MAX_PV_MOVES_SHOWN])
            for col, text in enumerate((str(line.multipv), score_text, str(line.depth), pv_text)):
                item = QTableWidgetItem(text)
                if col in (0, 1, 2):
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, col, item)
        self.table.resizeRowsToContents()

        if self._show_arrows:
            arrows = []
            for rank, line in enumerate(lines):
                if not line.pv_uci:
                    continue
                mv = chess.Move.from_uci(line.pv_uci[0])
                color = ARROW_COLORS[min(rank, len(ARROW_COLORS) - 1)]
                arrows.append((mv.from_square, mv.to_square, color))
            self.board.set_arrows(arrows)

        elapsed = f"{top.time_s:.1f}s" if top.time_s else "\u2013"
        nps_text = f"{top.nps:,} nps \u00b7 " if top.nps else ""
        self.status_label.setText(f"depth {top.depth} \u00b7 {top.nodes:,} nodes \u00b7 {nps_text}{elapsed}")

    @staticmethod
    def _format_score(line: AnalysisLine) -> str:
        # Kept in White's POV throughout (see AnalysisLine's docstring),
        # matching Lichess's own convention -- never flipped based on
        # whose turn it is.
        if line.score_mate is not None:
            return f"#{line.score_mate}"
        if line.score_cp is None:
            return "\u2013"
        return f"{line.score_cp / 100:+.2f}"
