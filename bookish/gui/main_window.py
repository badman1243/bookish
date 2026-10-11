from __future__ import annotations

from pathlib import Path

import chess
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QPushButton, QLineEdit,
    QLabel, QTabWidget, QCheckBox, QComboBox, QApplication, QSplitter,
    QAbstractSpinBox, QTableWidget,
)

from core import eco
from gui import theme
from gui import settings as app_settings
from gui.version import current_version
from gui.board_widget import ChessBoardWidget, ANIMATION_DURATIONS_MS, DEFAULT_ANIMATION_SPEED
from gui.database_panel import DatabasePanel
from gui.repertoire_panel import RepertoirePanel
from gui.engine_panel import EnginePanel


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        version = current_version()
        self.setWindowTitle(f"Bookish v{version}" if version else "Bookish")

        self.board = ChessBoardWidget()

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(self.board, stretch=1)

        fen_row = QHBoxLayout()
        self.fen_input = QLineEdit()
        self.fen_input.setPlaceholderText("Paste a FEN, or type an ECO code / opening name, to jump there\u2026")
        self.fen_input.returnPressed.connect(self._load_fen)
        fen_row.addWidget(self.fen_input)
        load_fen_btn = QPushButton("Jump")
        load_fen_btn.setToolTip("Loads a FEN, or looks up an ECO code (\"C60\") or opening name/substring (\"Najdorf\")")
        load_fen_btn.clicked.connect(self._load_fen)
        fen_row.addWidget(load_fen_btn)
        left_layout.addLayout(fen_row)

        btn_row = QHBoxLayout()
        new_btn = QPushButton("New Game")
        new_btn.clicked.connect(self.board.reset)
        btn_row.addWidget(new_btn)
        undo_btn = QPushButton("Undo \u2190")
        undo_btn.setToolTip("Step back one move (or press the Left arrow key anywhere except a text field)")
        undo_btn.clicked.connect(self.board.undo)
        btn_row.addWidget(undo_btn)
        redo_btn = QPushButton("Redo \u2192")
        redo_btn.setToolTip(
            "Step forward again (or press the Right arrow key). Once there's nothing left to "
            "redo, Right arrow instead plays the current Database tab's top move, if one is loaded."
        )
        redo_btn.clicked.connect(self.board.redo)
        btn_row.addWidget(redo_btn)
        flip_btn = QPushButton("Flip Board")
        flip_btn.clicked.connect(self.board.flip)
        btn_row.addWidget(flip_btn)
        left_layout.addLayout(btn_row)

        self.dark_mode_checkbox = QCheckBox("Dark mode")
        self.dark_mode_checkbox.toggled.connect(self._toggle_dark_mode)
        left_layout.addWidget(self.dark_mode_checkbox)

        anim_row = QHBoxLayout()
        anim_row.addWidget(QLabel("Piece animation:"))
        self.animation_combo = QComboBox()
        self.animation_combo.addItems(list(ANIMATION_DURATIONS_MS.keys()))
        self.animation_combo.setCurrentText(DEFAULT_ANIMATION_SPEED)
        self.animation_combo.setToolTip("How fast pieces slide into place on a move -- \"Off\" snaps instantly, same as before this existed.")
        self.animation_combo.currentTextChanged.connect(self.board.set_animation_speed)
        anim_row.addWidget(self.animation_combo)
        anim_row.addStretch(1)
        left_layout.addLayout(anim_row)

        self.moves_label = QLabel("")
        self.moves_label.setWordWrap(True)
        left_layout.addWidget(self.moves_label)
        self.board.positionChanged.connect(self._update_moves_label)

        self.database_panel = DatabasePanel(self.board)
        self.repertoire_panel = RepertoirePanel(self.board)
        self.engine_panel = EnginePanel(self.board)
        self.database_panel.databaseLoaded.connect(self.repertoire_panel.set_database)

        self.tabs = QTabWidget()
        self.tabs.setMovable(True)  # drag-to-reorder; order is persisted, see _load_startup_settings/closeEvent
        self.tabs.addTab(self.database_panel, "Database")
        self.tabs.addTab(self.repertoire_panel, "Repertoire Builder")
        self.tabs.addTab(self.engine_panel, "Engine")

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        board_natural_width = self.board.sizeHint().width() + 40
        splitter.setSizes([board_natural_width, max(400, 900 - board_natural_width)])

        central = QWidget()
        central_layout = QHBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.addWidget(splitter)
        self.setCentralWidget(central)

        # The FEN box is the first focusable widget Qt would otherwise
        # hand default keyboard focus to on startup -- explicitly give it
        # to the board instead, so arrow-key undo/redo works immediately
        # without requiring an extra click first.
        self.board.setFocus(Qt.OtherFocusReason)

        QApplication.instance().installEventFilter(self)

        self._load_startup_settings()

    def _load_startup_settings(self) -> None:
        s = app_settings.load_settings()
        if not s:
            return
        if s.get("dark_mode"):
            self.dark_mode_checkbox.setChecked(True)  # triggers _toggle_dark_mode via its own signal
        if s.get("board_flipped"):
            self.board.flip()
        if s.get("animation_speed") in ANIMATION_DURATIONS_MS:
            self.animation_combo.setCurrentText(s["animation_speed"])  # triggers board.set_animation_speed via its own signal
        self.repertoire_panel.apply_settings(s.get("repertoire", {}))
        self.engine_panel.apply_settings(s.get("engine", {}))
        self._restore_tab_order(s.get("tab_order", []))
        last_db = s.get("last_database_path")
        if last_db and Path(last_db).exists():
            # Reuses the existing index instantly if the file's unchanged
            # since last time (see pgn_indexer.index_exists_and_matches) --
            # this only actually re-indexes if the file itself changed.
            self.database_panel.load_database(last_db)

    def _restore_tab_order(self, saved_order: list[str]) -> None:
        """Moves tabs to match a previously-saved left-to-right order, by
        label rather than index -- so a label no longer present (a tab
        that was removed) is just skipped, and any tab NOT in the saved
        list (e.g. a new one added in a later version) keeps whatever
        position it landed in by default, rather than the restore logic
        needing to already know about it."""
        for target_index, label in enumerate(saved_order):
            current_index = None
            for i in range(self.tabs.count()):
                if self.tabs.tabText(i) == label:
                    current_index = i
                    break
            if current_index is not None and current_index != target_index:
                self.tabs.tabBar().moveTab(current_index, target_index)

    def closeEvent(self, event) -> None:
        data = {
            "dark_mode": self.dark_mode_checkbox.isChecked(),
            "board_flipped": self.board.flipped,
            "animation_speed": self.animation_combo.currentText(),
            "last_database_path": self.database_panel.pgn_path,
            "repertoire": self.repertoire_panel.get_settings(),
            "engine": self.engine_panel.get_settings(),
            "tab_order": [self.tabs.tabText(i) for i in range(self.tabs.count())],
        }
        app_settings.save_settings(data)
        # Make sure no engine process is left running as an orphan after
        # the app exits -- this has to happen after get_settings() above
        # reads the engines' current state, but before the app actually
        # goes away.
        self.engine_panel.shutdown()
        super().closeEvent(event)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.FocusIn and obj is self.fen_input:
            if not self.fen_input.text():
                clipboard_text = QApplication.clipboard().text().strip()
                if self._looks_like_fen(clipboard_text):
                    self.fen_input.setText(clipboard_text)
            return False  # never consume -- the box still needs to actually receive focus
        if event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Left, Qt.Key_Right):
            focused = QApplication.focusWidget()
            if isinstance(focused, (QLineEdit, QAbstractSpinBox, QTableWidget)):
                return False  # let the focused widget handle it natively
            if event.key() == Qt.Key_Left:
                self.board.undo()
            else:
                if not self.board.redo():
                    self._play_top_database_move()
            return True
        return super().eventFilter(obj, event)

    @staticmethod
    def _looks_like_fen(text: str) -> bool:
        # A real FEN is always short; skip pathologically long clipboard
        # content without even trying to parse it. python-chess itself is
        # the actual arbiter of "is this valid" -- no separate hand-rolled
        # regex to keep in sync with what it actually accepts.
        if not text or len(text) > 100:
            return False
        try:
            chess.Board(text)
            return True
        except ValueError:
            return False

    def _play_top_database_move(self) -> bool:
        stats = self.database_panel.current_stats
        if not stats or not stats.moves:
            return False
        return self.board.push_move_uci(stats.moves[0].move_uci)

    def _load_fen(self) -> None:
        text = self.fen_input.text().strip()
        if not text:
            return
        if self.board.set_fen(text):
            return
        # Not a valid FEN -- try it as an ECO code ("C60") or opening
        # name/substring ("Ruy Lopez", "najdorf") instead. Silently does
        # nothing if neither matches, same as an invalid FEN already did
        # before this existed -- no new interruption for a typo.
        epd = eco.find_position_by_query(text)
        if epd is not None:
            self.board.set_fen(epd)

    def _toggle_dark_mode(self, checked: bool) -> None:
        app = QApplication.instance()
        if checked:
            theme.apply_dark(app)
        else:
            theme.apply_light(app)
        self.board.set_dark_mode(checked)

    def _update_moves_label(self) -> None:
        moves = self.board.move_list_san()
        if not moves:
            self.moves_label.setText("Starting position")
            return
        pairs = []
        for i in range(0, len(moves), 2):
            move_no = i // 2 + 1
            if i + 1 < len(moves):
                pairs.append(f"{move_no}.{moves[i]} {moves[i+1]}")
            else:
                pairs.append(f"{move_no}.{moves[i]}")
        self.moves_label.setText(" ".join(pairs))
