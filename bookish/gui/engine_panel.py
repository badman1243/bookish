"""
engine_panel.py

The "Engine" tab: add a UCI engine (any downloaded executable -- it's
just a program already on disk, so this is an ordinary QFileDialog file
pick, not a special upload mechanism), see the options it reports,
configure them, and open the resizable analysis popup (see
engine_analysis_dialog.py) against whatever's currently on the shared
board.

Loading an engine (spawning the process and completing the UCI
handshake) runs on a QThread -- the same defensive pattern already used
for indexing (database_panel.IndexWorker) and building
(repertoire_panel.BuildWorker) -- so a slow or broken executable can't
freeze the GUI.

Saved engines are remembered (path, display name, last-applied option
values) across restarts, but are never auto-launched on startup --
spawning an arbitrary executable silently, without being asked to in
that session, isn't something this app does. Each saved entry instead
sits in a dropdown with an explicit "Load Saved Engine" button.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QPushButton, QLabel,
    QListWidget, QListWidgetItem, QFileDialog, QMessageBox, QCheckBox,
    QSpinBox, QComboBox, QLineEdit, QGroupBox, QScrollArea,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.engine_manager import EngineManager, EngineInfo, UciOptionSpec, EngineError
from gui.board_widget import ChessBoardWidget
from gui.engine_analysis_dialog import EngineAnalysisDialog


@dataclass
class LoadedEngine:
    name: str
    path: str
    manager: EngineManager
    info: EngineInfo
    values: dict[str, Any] = field(default_factory=dict)  # current option values, keyed by option name


class LoadWorker(QThread):
    loaded = Signal(str, object, object)  # path, EngineManager, EngineInfo
    failed = Signal(str, str)  # path, message

    def __init__(self, path: str):
        super().__init__()
        self.path = path

    def run(self) -> None:
        manager = EngineManager(self.path)
        try:
            info = manager.start()
        except EngineError as exc:
            self.failed.emit(self.path, str(exc))
            return
        self.loaded.emit(self.path, manager, info)


class EnginePanel(QWidget):
    def __init__(self, board: ChessBoardWidget, parent=None):
        super().__init__(parent)
        self.board = board
        self.engines: dict[str, LoadedEngine] = {}  # keyed by display name
        self._saved_not_loaded: list[dict] = []  # entries restored from settings, not yet started
        self._pending_values: dict[str, Any] = {}  # option values to restore once the in-flight load finishes
        self._load_worker: LoadWorker | None = None
        self._open_dialogs: dict[str, EngineAnalysisDialog] = {}

        layout = QVBoxLayout(self)

        add_row = QHBoxLayout()
        self.add_btn = QPushButton("Add Engine\u2026")
        self.add_btn.clicked.connect(self._pick_engine)
        add_row.addWidget(self.add_btn)
        self.status_label = QLabel("")
        add_row.addWidget(self.status_label, stretch=1)
        layout.addLayout(add_row)

        self.saved_row_widget = QWidget()
        saved_row = QHBoxLayout(self.saved_row_widget)
        saved_row.setContentsMargins(0, 0, 0, 0)
        saved_row.addWidget(QLabel("Saved:"))
        self.saved_combo = QComboBox()
        saved_row.addWidget(self.saved_combo, stretch=1)
        self.load_saved_btn = QPushButton("Load Saved Engine")
        self.load_saved_btn.clicked.connect(self._load_saved_selected)
        saved_row.addWidget(self.load_saved_btn)
        self.saved_row_widget.setVisible(False)
        layout.addWidget(self.saved_row_widget)

        self.list_widget = QListWidget()
        self.list_widget.currentItemChanged.connect(self._on_selection_changed)
        layout.addWidget(self.list_widget)

        action_row = QHBoxLayout()
        self.analyze_btn = QPushButton("Open Analysis Panel")
        self.analyze_btn.clicked.connect(self._open_analysis)
        self.analyze_btn.setEnabled(False)
        action_row.addWidget(self.analyze_btn)
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self._remove_selected)
        self.remove_btn.setEnabled(False)
        action_row.addWidget(self.remove_btn)
        layout.addLayout(action_row)

        self.options_box = QGroupBox("Engine options")
        self.options_form = QFormLayout()
        self.options_box.setLayout(self.options_form)
        self.managed_note = QLabel(
            "Multi-PV is set in the analysis window. Ponder, Chess960 and analysis mode are "
            "managed automatically, so they aren't listed here."
        )
        self.managed_note.setWordWrap(True)
        self.managed_note.setVisible(False)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.options_box)
        layout.addWidget(scroll, stretch=1)
        layout.addWidget(self.managed_note)
        self._option_widgets: dict[str, QWidget] = {}

        apply_row = QHBoxLayout()
        self.apply_btn = QPushButton("Apply Options")
        self.apply_btn.clicked.connect(self._apply_options)
        self.apply_btn.setEnabled(False)
        apply_row.addWidget(self.apply_btn)
        self.apply_status_label = QLabel("")
        apply_row.addWidget(self.apply_status_label, stretch=1)
        layout.addLayout(apply_row)

    # -- adding / loading --------------------------------------------------

    def _pick_engine(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select a UCI engine executable", "", "All files (*)")
        if not path:
            return
        self._load_path(path, display_name=None, pending_values=None)

    def _load_saved_selected(self) -> None:
        idx = self.saved_combo.currentIndex()
        if idx < 0 or idx >= len(self._saved_not_loaded):
            return
        entry = self._saved_not_loaded.pop(idx)
        self._refresh_saved_combo()
        self._load_path(entry["path"], entry["name"], entry.get("values", {}))

    def _refresh_saved_combo(self) -> None:
        self.saved_combo.clear()
        self.saved_combo.addItems([e["name"] for e in self._saved_not_loaded])
        self.saved_row_widget.setVisible(bool(self._saved_not_loaded))

    def _load_path(self, path: str, display_name: str | None, pending_values: dict[str, Any] | None) -> None:
        self.add_btn.setEnabled(False)
        self.load_saved_btn.setEnabled(False)
        self.status_label.setText(f"Starting {Path(path).name}\u2026")
        self._pending_values = pending_values or {}
        self._load_worker = LoadWorker(path)
        self._load_worker.loaded.connect(lambda p, mgr, info: self._on_loaded(p, mgr, info, display_name))
        self._load_worker.failed.connect(self._on_load_failed)
        self._load_worker.start()

    def _on_loaded(self, path: str, manager: EngineManager, info: EngineInfo, display_name: str | None) -> None:
        self.add_btn.setEnabled(True)
        self.load_saved_btn.setEnabled(True)
        self.status_label.setText("")

        name = display_name or info.name
        base_name, n = name, 2
        while name in self.engines:
            name = f"{base_name} ({n})"
            n += 1

        # Only options a user can actually set: managed ones (MultiPV,
        # Ponder, ...) are refused by python-chess, and buttons have no
        # value to remember -- keeping either here would make every
        # later save/restore round trip fail on them.
        values = {
            opt.name: opt.default for opt in info.options.values()
            if not opt.managed and opt.type != "button"
        }
        # Also drops anything a settings file remembers that this engine
        # no longer has (an updated build that renamed or removed an
        # option), and anything already at its default.
        restore = {k: v for k, v in self._pending_values.items() if k in values and v != values[k]}
        self._pending_values = {}
        if restore:
            values.update(restore)
            try:
                manager.configure(restore)
            except EngineError as exc:
                QMessageBox.warning(self, "Couldn't restore saved options", str(exc))

        loaded = LoadedEngine(name=name, path=path, manager=manager, info=info, values=values)
        self.engines[name] = loaded
        item = QListWidgetItem(name)
        item.setToolTip(f"{path}\n{info.author}" if info.author else path)
        self.list_widget.addItem(item)
        self.list_widget.setCurrentItem(item)

    def _on_load_failed(self, path: str, message: str) -> None:
        self.add_btn.setEnabled(True)
        self.load_saved_btn.setEnabled(True)
        self.status_label.setText("")
        self._pending_values = {}
        QMessageBox.critical(self, "Couldn't load engine", message)

    # -- selection / options -----------------------------------------------

    def _current_engine(self) -> LoadedEngine | None:
        item = self.list_widget.currentItem()
        if item is None:
            return None
        return self.engines.get(item.text())

    def _on_selection_changed(self, *_args) -> None:
        engine = self._current_engine()
        has_selection = engine is not None
        self.analyze_btn.setEnabled(has_selection)
        self.remove_btn.setEnabled(has_selection)
        self.apply_btn.setEnabled(has_selection)
        self._rebuild_options_form(engine)

    def _rebuild_options_form(self, engine: LoadedEngine | None) -> None:
        while self.options_form.rowCount():
            self.options_form.removeRow(0)
        self._option_widgets.clear()
        self.managed_note.setVisible(False)
        self.apply_status_label.setText("")
        if engine is None:
            return
        self.managed_note.setVisible(any(o.managed for o in engine.info.options.values()))
        for opt in engine.info.options.values():
            if opt.managed:
                continue  # python-chess refuses to set these (configure() raises) -- never offer them
            widget = self._make_option_widget(opt, engine.values.get(opt.name, opt.default))
            if widget is not None:
                self._option_widgets[opt.name] = widget
                self.options_form.addRow(opt.name, widget)

    def _make_option_widget(self, opt: UciOptionSpec, current: Any) -> QWidget | None:
        if opt.type == "check":
            w = QCheckBox()
            w.setChecked(bool(current))
            return w
        if opt.type == "spin":
            w = QSpinBox()
            w.setRange(opt.min if opt.min is not None else -1_000_000,
                       opt.max if opt.max is not None else 1_000_000)
            w.setValue(int(current) if current is not None else 0)
            return w
        if opt.type == "combo":
            w = QComboBox()
            w.addItems(opt.var)
            if current in opt.var:
                w.setCurrentText(str(current))
            return w
        if opt.type == "button":
            w = QPushButton("Trigger")
            w.clicked.connect(lambda _checked=False, name=opt.name: self._trigger_button(name))
            return w
        if opt.type == "string":
            w = QLineEdit()
            w.setText(str(current) if current is not None else "")
            return w
        return None  # an option type this UCI version doesn't define -- skip rather than guess

    def _with_paused_analysis(self, engine: LoadedEngine, action) -> None:
        """Sending the engine a command cancels any analysis running on
        it (see EngineManager.configure), so if this engine's popup is
        open, pause it around `action` and resume afterward."""
        dialog = self._open_dialogs.get(engine.name)
        if dialog is not None:
            dialog.run_while_paused(action)
        else:
            action()

    def _flash_button(self, button: QPushButton, flash_text: str, duration_ms: int = 1200) -> None:
        """Swaps a button's label to `flash_text` for a moment, then puts
        it back -- a visible pulse on the exact control that was pressed,
        alongside (not instead of) the status text, since a message
        appearing elsewhere in the panel is easy to miss."""
        original = button.text()
        button.setText(flash_text)
        QTimer.singleShot(duration_ms, lambda: button.setText(original))

    def _trigger_button(self, option_name: str) -> None:
        engine = self._current_engine()
        if engine is None:
            return
        try:
            self._with_paused_analysis(engine, lambda: engine.manager.configure({option_name: None}))
        except EngineError as exc:
            self.apply_status_label.setText(f"{option_name} failed.")
            QMessageBox.warning(self, "Engine option failed", str(exc))
            return
        self.apply_status_label.setText(f"Sent: {option_name}")
        widget = self._option_widgets.get(option_name)
        if isinstance(widget, QPushButton):
            self._flash_button(widget, "Sent \u2713")

    def _apply_options(self) -> None:
        engine = self._current_engine()
        if engine is None:
            return
        values: dict[str, Any] = {}
        for name, widget in self._option_widgets.items():
            if isinstance(widget, QCheckBox):
                values[name] = widget.isChecked()
            elif isinstance(widget, QSpinBox):
                values[name] = widget.value()
            elif isinstance(widget, QComboBox):
                values[name] = widget.currentText()
            elif isinstance(widget, QLineEdit):
                values[name] = widget.text()
            # QPushButton ("button" options) fire immediately on click, not here.
        # Only send what actually changed -- re-sending every option on
        # every Apply is noise at best, and at worst re-triggers options
        # with side effects (e.g. re-pointing EvalFile or reopening a log).
        changed = {k: v for k, v in values.items() if v != engine.values.get(k)}
        if not changed:
            self.apply_status_label.setText("No changes to apply.")
            return
        try:
            self._with_paused_analysis(engine, lambda: engine.manager.configure(changed))
        except EngineError as exc:
            self.apply_status_label.setText("Apply failed.")
            QMessageBox.warning(self, "Engine option failed", str(exc))
            return
        engine.values.update(changed)
        self.apply_status_label.setText(f"Applied: {', '.join(sorted(changed))}")
        self._flash_button(self.apply_btn, "Applied \u2713")

    # -- analysis / removal -------------------------------------------------

    def _open_analysis(self) -> None:
        engine = self._current_engine()
        if engine is None:
            return
        dialog = self._open_dialogs.get(engine.name)
        if dialog is not None:
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
            return
        dialog = EngineAnalysisDialog(engine.name, engine.manager, self.board, parent=self.window())
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.destroyed.connect(lambda *_args, nm=engine.name: self._open_dialogs.pop(nm, None))
        self._open_dialogs[engine.name] = dialog
        dialog.show()

    def _remove_selected(self) -> None:
        engine = self._current_engine()
        if engine is None:
            return
        dialog = self._open_dialogs.pop(engine.name, None)
        if dialog is not None:
            dialog.close()
        engine.manager.quit()
        del self.engines[engine.name]
        self.list_widget.takeItem(self.list_widget.currentRow())

    def shutdown(self) -> None:
        """Called from MainWindow.closeEvent -- makes sure no engine
        process is left running as an orphan after the app exits."""
        for dialog in list(self._open_dialogs.values()):
            dialog.close()
        for engine in self.engines.values():
            engine.manager.quit()

    # -- settings persistence (same shape as RepertoirePanel) --------------

    def get_settings(self) -> dict:
        return {
            "engines": [
                {"name": e.name, "path": e.path, "values": e.values}
                for e in self.engines.values()
            ]
        }

    def apply_settings(self, s: dict) -> None:
        entries = s.get("engines", [])
        self._saved_not_loaded = [e for e in entries if Path(e.get("path", "")).exists()]
        self._refresh_saved_combo()
