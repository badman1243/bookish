"""
database_panel.py

Lets the user pick a .pgn database file, builds (or reuses) its position
index in the background, and shows live statistics for whatever position
is currently set on the shared ChessBoardWidget: a move table with a
white/draw/black bar per move, plus a distinct "\u03a3" (sigma) row at the
bottom aggregating all of them -- the position's overall record.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QColor, QBrush, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QFileDialog,
    QProgressBar, QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.pgn_indexer import PgnIndexer, IndexProgress, index_exists_and_matches
from core.position_stats import get_position_stats, format_game_count
from gui.board_widget import ChessBoardWidget
from gui.result_bar import ResultBar

SIGMA_BG = QColor("#1f3a5c")
SIGMA_FG = QColor("#ffffff")
LOW_CONFIDENCE_GAP = 8.0  # percentage points Wilson has to pull the score down by to flag a row
LOW_CONFIDENCE_COLOR = QColor("#b8860b")


def default_index_path(pgn_path: str) -> str:
    p = Path(pgn_path)
    candidate = p.with_suffix(".repidx.sqlite")
    try:
        candidate.touch(exist_ok=True)
        candidate.unlink()
        return str(candidate)
    except OSError:
        import tempfile
        return str(Path(tempfile.gettempdir()) / (p.stem + ".repidx.sqlite"))


class IndexWorker(QThread):
    progress = Signal(object)  # IndexProgress
    finished_ok = Signal(str)  # db_path
    failed = Signal(str)

    def __init__(self, pgn_path: str, db_path: str):
        super().__init__()
        self.pgn_path = pgn_path
        self.db_path = db_path

    def run(self) -> None:
        try:
            indexer = PgnIndexer(self.pgn_path, self.db_path)
            indexer.build(progress_cb=lambda p: self.progress.emit(p))
            self.finished_ok.emit(self.db_path)
        except Exception as exc:  # surface any parse/IO error to the UI
            self.failed.emit(str(exc))


class DatabasePanel(QWidget):
    databaseLoaded = Signal(str)  # emits db_path once an index is ready

    def __init__(self, board: ChessBoardWidget, parent=None):
        super().__init__(parent)
        self.board = board
        self.pgn_path: str | None = None
        self.db_path: str | None = None
        self.conn: sqlite3.Connection | None = None
        self.worker: IndexWorker | None = None
        self.current_stats = None

        layout = QVBoxLayout(self)

        row = QHBoxLayout()
        self.load_btn = QPushButton("Load PGN Database\u2026")
        self.load_btn.clicked.connect(self._pick_file)
        row.addWidget(self.load_btn)
        self.path_label = QLabel("No database loaded")
        self.path_label.setWordWrap(True)
        row.addWidget(self.path_label, stretch=1)
        layout.addLayout(row)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.progress_label = QLabel("")
        self.progress_label.setVisible(False)
        layout.addWidget(self.progress_label)

        self.total_games_label = QLabel("Total games in database: \u2013")
        layout.addWidget(self.total_games_label)

        layout.addWidget(QLabel("Click a row to play that move on the board:"))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Move", "Games", "White / Draw / Black"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.cellClicked.connect(self._on_row_clicked)
        layout.addWidget(self.table, stretch=1)

        self.board.positionChanged.connect(self.refresh_stats)

    # -- loading ---------------------------------------------------------

    def _pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select a PGN database", "", "PGN files (*.pgn)")
        if not path:
            return
        self.load_database(path)

    def load_database(self, pgn_path: str) -> None:
        self.pgn_path = pgn_path
        db_path = default_index_path(pgn_path)
        self.path_label.setText(pgn_path)

        if index_exists_and_matches(db_path, pgn_path):
            self._finish_loading(db_path)
            return

        self.load_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)  # indeterminate until we know game totals
        self.progress_label.setVisible(True)
        self.progress_label.setText("Indexing database\u2026")

        self.worker = IndexWorker(pgn_path, db_path)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_ok.connect(self._finish_loading)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _on_progress(self, prog: IndexProgress) -> None:
        self.progress_label.setText(
            f"Indexed {prog.games_processed:,} games ({prog.elapsed_seconds:.0f}s)\u2026"
        )

    def _on_failed(self, message: str) -> None:
        self.load_btn.setEnabled(True)
        self.progress.setVisible(False)
        self.progress_label.setVisible(False)
        QMessageBox.critical(self, "Failed to load database", message)

    def _finish_loading(self, db_path: str) -> None:
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.load_btn.setEnabled(True)
        self.progress.setVisible(False)
        self.progress_label.setVisible(False)

        total_row = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'total_games'"
        ).fetchone()
        total_games = int(total_row[0]) if total_row else 0

        errors_row = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'games_with_errors'"
        ).fetchone()
        games_with_errors = int(errors_row[0]) if errors_row else 0

        label_text = f"Total games in database: {format_game_count(total_games)} ({total_games:,})"
        if games_with_errors:
            label_text += f"  \u26a0 {games_with_errors:,} had a parsing issue"
            self.total_games_label.setToolTip(
                f"{games_with_errors:,} game(s) hit something PGN couldn't parse (an illegal move, "
                "a malformed tag, etc). Whatever came before the problem in each game is still "
                "counted normally -- only the rest of that one game, from the error onward, is "
                "missing. The file itself is untouched; only the index is affected."
            )
        else:
            self.total_games_label.setToolTip("")
        self.total_games_label.setText(label_text)

        self.databaseLoaded.emit(db_path)
        self.refresh_stats()

    # -- live stats --------------------------------------------------------

    def refresh_stats(self) -> None:
        self.table.setRowCount(0)
        self.current_stats = None
        if self.conn is None:
            return

        stats = get_position_stats(self.conn, self.board.get_board())
        self.current_stats = stats
        if stats is None or not stats.moves:
            return

        self.table.setRowCount(len(stats.moves) + 1)  # +1 for the Sigma summary row

        for row, m in enumerate(stats.moves):
            pct = m.pct_of(stats.game_count)
            self._set_row(
                row, m.san, pct, m.count, m.white_win_pct(), m.draw_pct(), m.black_win_pct(),
                confidence_gap=m.confidence_gap(),
            )

        total_results = stats.total_white_wins() + stats.total_draws() + stats.total_black_wins()
        if total_results:
            w_pct = 100.0 * stats.total_white_wins() / total_results
            d_pct = 100.0 * stats.total_draws() / total_results
            b_pct = 100.0 * stats.total_black_wins() / total_results
        else:
            w_pct = d_pct = b_pct = 0.0
        sigma_row = len(stats.moves)
        self._set_row(sigma_row, "\u03a3", 100.0, stats.game_count, w_pct, d_pct, b_pct, sigma=True)

    def _set_row(
        self, row: int, move_text: str, pct: float, count: int,
        white_pct: float, draw_pct: float, black_pct: float, sigma: bool = False,
        confidence_gap: float | None = None,
    ) -> None:
        low_confidence = confidence_gap is not None and confidence_gap >= LOW_CONFIDENCE_GAP
        games_text = f"{pct:.0f}%   {format_game_count(count)}"
        if low_confidence:
            games_text += "  \u26a0"

        move_item = QTableWidgetItem(move_text)
        games_item = QTableWidgetItem(games_text)
        move_item.setTextAlignment(Qt.AlignCenter)
        games_item.setTextAlignment(Qt.AlignCenter)

        if low_confidence:
            tooltip = (
                f"Only {count:,} games -- a thin sample. Its record could easily shift with "
                f"more data (Wilson-adjusted estimate is ~{confidence_gap:.0f} points lower "
                f"than the raw percentage). Treat this one with some caution."
            )
            games_item.setToolTip(tooltip)
            move_item.setToolTip(tooltip)
            games_item.setForeground(QBrush(LOW_CONFIDENCE_COLOR))

        if sigma:
            bold = QFont()
            bold.setBold(True)
            for item in (move_item, games_item):
                item.setFont(bold)
                item.setBackground(QBrush(SIGMA_BG))
                item.setForeground(QBrush(SIGMA_FG))

        self.table.setItem(row, 0, move_item)
        self.table.setItem(row, 1, games_item)

        bar = ResultBar()
        bar.set_results(white_pct, draw_pct, black_pct)
        if sigma:
            bar.setStyleSheet(f"background-color: {SIGMA_BG.name()};")
        self.table.setCellWidget(row, 2, bar)

    def _on_row_clicked(self, row: int, column: int) -> None:
        if self.current_stats is None or row >= len(self.current_stats.moves):
            return  # the Sigma row (or anything stale) isn't a playable move
        move = self.current_stats.moves[row]
        self.board.push_move_uci(move.move_uci)
