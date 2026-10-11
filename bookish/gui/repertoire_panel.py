"""
repertoire_panel.py

Configuration controls for building a repertoire from whatever position is
currently set on the shared board, plus a background worker that runs the
(potentially slow, deeply recursive) build off the UI thread.

Layout is two-tier: Color / Preset / Scouting Mode are the only controls
shown by default -- picking a Preset alone is enough to get a sensible
build. Everything else (Depth, Rarity Cutoff, Ignore/Requirement, Move
Ranges, Minimum Games) lives under a collapsed "Advanced Settings"
section for anyone who wants to fine-tune past what a Preset sets.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from pathlib import Path

import chess
from PySide6.QtCore import QThread, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QComboBox, QSpinBox, QDoubleSpinBox,
    QPushButton, QLabel, QProgressBar, QMessageBox, QGroupBox,
    QHBoxLayout, QCheckBox,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.repertoire_builder import (
    RepertoireBuilder, RepertoireConfig, RepNode, BuildSummary, to_pgn_game,
    SCOUTING_MODES, compute_coverage_pct, describe_for_filename, classify_repertoire,
)
from core.presets import load_presets, PRESETS_PATH
from gui.board_widget import ChessBoardWidget

DEPTH_CHOICES = [5, 10, 15, 20, 25, 30]
# Display label -> Wilson z-score. Curated, standard confidence levels
# rather than a raw z-score spinbox, since "95% confidence" means
# something to look up; "z=1.96" doesn't, to most people using this.
CONFIDENCE_Z_SCORES = {
    "80% (quicker to trust small samples)": 1.2816,
    "90%": 1.6449,
    "95% (standard)": 1.9600,
    "98%": 2.3263,
    "99% (very skeptical of small samples)": 2.5758,
}
SURVEYS_DIR = Path(__file__).resolve().parent.parent / "Surveys"


class BuildWorker(QThread):
    finished_ok = Signal(object, object)  # RepNode, BuildSummary
    failed = Signal(str)

    def __init__(self, db_path: str, config: RepertoireConfig, root_board: chess.Board):
        super().__init__()
        self.db_path = db_path
        self.config = config
        self.root_board = root_board  # full board incl. move_stack, used later for PGN export

    def run(self) -> None:
        try:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            builder = RepertoireBuilder(conn, self.config)
            root = builder.build(self.root_board.copy())
            conn.close()
            self.finished_ok.emit(root, builder.summary)
        except Exception as exc:
            self.failed.emit(str(exc))


class RepertoirePanel(QWidget):
    def __init__(self, board: ChessBoardWidget, parent=None):
        super().__init__(parent)
        self.board = board
        self.db_path: str | None = None
        self.worker: BuildWorker | None = None
        self.result_root: RepNode | None = None
        self.result_root_board: chess.Board | None = None
        self._applying_preset = False

        layout = QVBoxLayout(self)

        # -- Main: the only controls shown by default ------------------------

        main_box = QGroupBox("Repertoire settings")
        main_form = QFormLayout(main_box)

        self.color_combo = QComboBox()
        self.color_combo.addItems(["White", "Black"])
        main_form.addRow("Color:", self.color_combo)

        self.presets, presets_error = load_presets()

        self.preset_combo = QComboBox()
        self.preset_combo.addItems(list(self.presets.keys()) + ["Custom"])
        self.preset_combo.setToolTip(
            "Sets Depth, Rarity Cutoff, and the other shape settings together as a "
            "starting point -- everything remains individually adjustable under "
            "Advanced Settings, which switches this to \"Custom\". Defined in "
            f"{PRESETS_PATH.name}, editable directly -- see the buttons alongside."
        )
        preset_row = QHBoxLayout()
        preset_row.addWidget(self.preset_combo, 1)
        self.reload_presets_btn = QPushButton("\u27f3")
        self.reload_presets_btn.setToolTip(f"Reload {PRESETS_PATH.name} from disk")
        self.reload_presets_btn.setFixedWidth(28)
        self.reload_presets_btn.clicked.connect(self._reload_presets)
        preset_row.addWidget(self.reload_presets_btn)
        self.edit_presets_btn = QPushButton("Edit\u2026")
        self.edit_presets_btn.setToolTip(f"Open {PRESETS_PATH} in your default text editor")
        self.edit_presets_btn.clicked.connect(self._open_presets_file)
        preset_row.addWidget(self.edit_presets_btn)
        main_form.addRow("Preset:", preset_row)

        self.scouting_combo = QComboBox()
        self.scouting_combo.addItems(list(SCOUTING_MODES.keys()))
        self.scouting_combo.setToolTip(
            "Neutral: standard scoring (win=1, draw=0.5, loss=0).\n"
            "Aggressive: draws count for nothing -- only decisive results are rewarded.\n"
            "Solid: not losing counts fully as good as winning."
        )
        main_form.addRow("Scouting Mode:", self.scouting_combo)

        layout.addWidget(main_box)

        # -- Advanced: collapsed by default -----------------------------------

        self.advanced_toggle = QPushButton("\u25b8 Advanced Settings")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setFlat(True)
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        layout.addWidget(self.advanced_toggle)

        self.advanced_box = QGroupBox()
        self.advanced_box.setVisible(False)
        adv_form = QFormLayout(self.advanced_box)

        self.depth_combo = QComboBox()
        self.depth_combo.addItems([str(d) for d in DEPTH_CHOICES])
        self.depth_combo.setCurrentText("15")
        self.depth_combo.setToolTip("A hard ceiling regardless of Rarity Cutoff below.")
        adv_form.addRow("Depth:", self.depth_combo)

        rarity_row = QHBoxLayout()
        self.population_enabled = QCheckBox("Enabled")
        self.population_enabled.setChecked(True)
        self.population_spin = QSpinBox()
        self.population_spin.setRange(2, 1_000_000)
        self.population_spin.setValue(200)
        self.population_spin.setPrefix("1 in ")
        self.population_spin.setSuffix(" games")
        self.population_spin.setToolTip(
            "Stops a branch once it's become rarer than this, rather than always running\n"
            "to the fixed Depth above -- true mainline theory (which stays common) still\n"
            "rides however deep Depth allows; offbeat tries get cut short automatically.\n"
            "Depth still applies as a hard ceiling underneath this regardless."
        )
        rarity_row.addWidget(self.population_enabled)
        rarity_row.addWidget(self.population_spin)
        adv_form.addRow("Rarity Cutoff:", rarity_row)

        self.ignore_spin = QDoubleSpinBox()
        self.ignore_spin.setRange(0, 100)
        self.ignore_spin.setSuffix(" %")
        self.ignore_spin.setValue(5)
        adv_form.addRow("Ignore opponent moves below:", self.ignore_spin)

        self.requirement_spin = QDoubleSpinBox()
        self.requirement_spin.setRange(0, 100)
        self.requirement_spin.setSuffix(" %")
        self.requirement_spin.setValue(50)
        adv_form.addRow("Minimum opponent coverage required:", self.requirement_spin)

        self.user_range_spin = QSpinBox()
        self.user_range_spin.setRange(1, 20)
        self.user_range_spin.setValue(3)
        adv_form.addRow("User Move Range:", self.user_range_spin)

        self.opp_range_spin = QSpinBox()
        self.opp_range_spin.setRange(1, 20)
        self.opp_range_spin.setValue(3)
        adv_form.addRow("Opponent Move Range:", self.opp_range_spin)

        self.user_games_spin = QSpinBox()
        self.user_games_spin.setRange(0, 10_000_000)
        self.user_games_spin.setValue(0)
        adv_form.addRow("User Minimum Games:", self.user_games_spin)

        self.opp_games_spin = QSpinBox()
        self.opp_games_spin.setRange(0, 10_000_000)
        self.opp_games_spin.setValue(0)
        adv_form.addRow("Opponent Minimum Games:", self.opp_games_spin)

        self.confidence_combo = QComboBox()
        self.confidence_combo.addItems(list(CONFIDENCE_Z_SCORES.keys()))
        self.confidence_combo.setCurrentText("95% (standard)")
        self.confidence_combo.setToolTip(
            "How skeptical the whole algorithm is of thin samples, on both your own move\n"
            "and the opponent's replies -- this is the dial behind Wilson-adjusted scoring,\n"
            "the champion-scan tiebreak, and Ignore Threshold/Rarity Cutoff alike. Higher\n"
            "demands more data before trusting a percentage (favors well-tested, forcing\n"
            "moves more strongly); lower is quicker to trust a smaller sample. This is a\n"
            "personal-preference dial independent of Preset -- it doesn't change when you\n"
            "switch presets, and switching it doesn't flip the Preset dropdown to Custom."
        )
        adv_form.addRow("Confidence Level:", self.confidence_combo)

        layout.addWidget(self.advanced_box)

        self.preset_combo.currentTextChanged.connect(self._apply_preset)
        for w, signal_name in (
            (self.depth_combo, "currentTextChanged"),
            (self.population_spin, "valueChanged"),
            (self.population_enabled, "toggled"),
            (self.ignore_spin, "valueChanged"),
            (self.requirement_spin, "valueChanged"),
            (self.user_range_spin, "valueChanged"),
            (self.opp_range_spin, "valueChanged"),
            (self.user_games_spin, "valueChanged"),
            (self.opp_games_spin, "valueChanged"),
        ):
            getattr(w, signal_name).connect(self._on_manual_override)
        self.preset_combo.setCurrentText("Tournament")
        self._apply_preset("Tournament")

        self.root_label = QLabel("Repertoire will be built starting from the current board position.")
        self.root_label.setWordWrap(True)
        layout.addWidget(self.root_label)

        btn_row = QHBoxLayout()
        self.build_btn = QPushButton("Build Repertoire")
        self.build_btn.clicked.connect(self._start_build)
        self.build_btn.setEnabled(False)
        btn_row.addWidget(self.build_btn)
        self.save_btn = QPushButton("Save Repertoire")
        self.save_btn.clicked.connect(self._save_pgn)
        self.save_btn.setEnabled(False)
        btn_row.addWidget(self.save_btn)
        layout.addLayout(btn_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        layout.addStretch(1)

        self.board.positionChanged.connect(self._update_root_label)
        self._update_root_label()

        if presets_error:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: QMessageBox.warning(self, "Presets file issue", presets_error))

    # -- wiring ---------------------------------------------------------

    def set_database(self, db_path: str) -> None:
        self.db_path = db_path
        self.build_btn.setEnabled(True)

    def _toggle_advanced(self, checked: bool) -> None:
        self.advanced_box.setVisible(checked)
        self.advanced_toggle.setText(("\u25be" if checked else "\u25b8") + " Advanced Settings")

    def _open_presets_file(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(PRESETS_PATH)))

    def _reload_presets(self) -> None:
        current = self.preset_combo.currentText()
        self.presets, err = load_presets()

        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItems(list(self.presets.keys()) + ["Custom"])
        self.preset_combo.blockSignals(False)

        target = current if (current in self.presets or current == "Custom") else next(iter(self.presets), "Custom")
        self.preset_combo.setCurrentText(target)
        if target in self.presets:
            self._apply_preset(target)  # re-apply explicitly in case the values themselves changed

        if err:
            QMessageBox.warning(self, "Presets file issue", err)
        else:
            QMessageBox.information(
                self, "Presets reloaded", f"Reloaded {len(self.presets)} preset(s) from {PRESETS_PATH.name}."
            )

    def _apply_preset(self, name: str) -> None:
        preset = self.presets.get(name)
        if preset is None:
            # "Custom" (or anything else not recognized): nothing to set --
            # the whole point of Custom is "whatever's currently dialed
            # in" -- but that's meaningless if Advanced Settings isn't
            # even open to see or change those values, so open it.
            self.advanced_toggle.setChecked(True)
            return
        self._applying_preset = True
        self.depth_combo.setCurrentText(str(preset.depth))
        self.population_enabled.setChecked(bool(preset.population_threshold))
        self.population_spin.setValue(preset.population_threshold or 200)
        self.ignore_spin.setValue(preset.ignore_threshold_pct)
        self.requirement_spin.setValue(preset.requirement_pct)
        self.opp_range_spin.setValue(preset.opponent_move_range)
        self.user_range_spin.setValue(preset.user_move_range)
        self.user_games_spin.setValue(preset.user_minimum_games)
        self.opp_games_spin.setValue(preset.opponent_minimum_games)
        self._applying_preset = False
        rarity_text = f"1 in {preset.population_threshold}" if preset.population_threshold else "disabled"
        self.preset_combo.setToolTip(
            f"{preset.description}\n\n"
            f"Depth {preset.depth}, Rarity Cutoff {rarity_text}, "
            f"Opponent Move Range {preset.opponent_move_range}, "
            f"User/Opponent Minimum Games {preset.user_minimum_games}/{preset.opponent_minimum_games}."
        )

    def _on_manual_override(self, *args) -> None:
        if self._applying_preset:
            return
        if self.preset_combo.currentText() != "Custom":
            self.preset_combo.setCurrentText("Custom")

    def get_settings(self) -> dict:
        """Serializes everything a settings file needs to restore this
        panel exactly -- the resolved preset name (or "Custom"), plus
        every individual value regardless, so a Custom state round-trips
        completely and a named preset is robust even if presets.yaml
        changes between sessions."""
        return {
            "color": self.color_combo.currentText(),
            "preset": self.preset_combo.currentText(),
            "scouting_mode": self.scouting_combo.currentText(),
            "confidence_level": self.confidence_combo.currentText(),
            "advanced_expanded": self.advanced_toggle.isChecked(),
            "depth": self.depth_combo.currentText(),
            "population_enabled": self.population_enabled.isChecked(),
            "population_threshold": self.population_spin.value(),
            "ignore_threshold_pct": self.ignore_spin.value(),
            "requirement_pct": self.requirement_spin.value(),
            "opponent_move_range": self.opp_range_spin.value(),
            "user_move_range": self.user_range_spin.value(),
            "user_minimum_games": self.user_games_spin.value(),
            "opponent_minimum_games": self.opp_games_spin.value(),
        }

    def apply_settings(self, s: dict) -> None:
        """Restores a previously-saved state. A named preset just gets
        re-selected (letting _apply_preset do the work, so it stays in
        sync if presets.yaml itself changed since); "Custom" restores
        each individual value directly, since there's no preset to
        re-derive them from."""
        if not s:
            return
        if "color" in s:
            self.color_combo.setCurrentText(s["color"])
        if "scouting_mode" in s and s["scouting_mode"] in SCOUTING_MODES:
            self.scouting_combo.setCurrentText(s["scouting_mode"])
        if "confidence_level" in s and s["confidence_level"] in CONFIDENCE_Z_SCORES:
            self.confidence_combo.setCurrentText(s["confidence_level"])

        preset_name = s.get("preset", "Tournament")
        if preset_name in self.presets:
            self.preset_combo.setCurrentText(preset_name)  # _apply_preset does the rest
        else:
            self._applying_preset = True
            if "depth" in s:
                self.depth_combo.setCurrentText(str(s["depth"]))
            if "population_enabled" in s:
                self.population_enabled.setChecked(bool(s["population_enabled"]))
            if "population_threshold" in s:
                self.population_spin.setValue(int(s["population_threshold"]))
            if "ignore_threshold_pct" in s:
                self.ignore_spin.setValue(float(s["ignore_threshold_pct"]))
            if "requirement_pct" in s:
                self.requirement_spin.setValue(float(s["requirement_pct"]))
            if "opponent_move_range" in s:
                self.opp_range_spin.setValue(int(s["opponent_move_range"]))
            if "user_move_range" in s:
                self.user_range_spin.setValue(int(s["user_move_range"]))
            if "user_minimum_games" in s:
                self.user_games_spin.setValue(int(s["user_minimum_games"]))
            if "opponent_minimum_games" in s:
                self.opp_games_spin.setValue(int(s["opponent_minimum_games"]))
            self._applying_preset = False
            self.preset_combo.setCurrentText("Custom")

        if "advanced_expanded" in s:
            self.advanced_toggle.setChecked(bool(s["advanced_expanded"]))

    def _update_root_label(self) -> None:
        moves = self.board.move_list_san()
        if moves:
            preview = " ".join(moves[-10:])
            self.root_label.setText(f"Repertoire will be built from the current position ( \u2026{preview} ).")
        else:
            self.root_label.setText("Repertoire will be built from the starting position.")

    # -- build ------------------------------------------------------------

    def _start_build(self) -> None:
        if not self.db_path:
            QMessageBox.warning(self, "No database", "Load a PGN database first.")
            return

        config = RepertoireConfig(
            owner_color=chess.WHITE if self.color_combo.currentText() == "White" else chess.BLACK,
            depth=int(self.depth_combo.currentText()),
            ignore_threshold_pct=self.ignore_spin.value(),
            requirement_pct=self.requirement_spin.value(),
            user_move_range=self.user_range_spin.value(),
            opponent_move_range=self.opp_range_spin.value(),
            user_game_amount=self.user_games_spin.value(),
            opponent_game_amount=self.opp_games_spin.value(),
            draw_weight=SCOUTING_MODES[self.scouting_combo.currentText()],
            population_threshold=self.population_spin.value() if self.population_enabled.isChecked() else None,
            confidence_z=CONFIDENCE_Z_SCORES[self.confidence_combo.currentText()],
        )
        root_board_snapshot = self.board.get_board()

        self.build_btn.setEnabled(False)
        self.save_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.summary_label.setText("Building\u2026")

        self.worker = BuildWorker(self.db_path, config, root_board_snapshot)
        self.worker.finished_ok.connect(self._on_build_done)
        self.worker.failed.connect(self._on_build_failed)
        self.worker.start()

    def _on_build_done(self, root: RepNode, summary: BuildSummary) -> None:
        self.result_root = root
        self.result_root_board = self.worker.root_board if self.worker else self.board.get_board()
        owner_color = self.worker.config.owner_color if self.worker else chess.WHITE
        self.progress.setVisible(False)
        self.build_btn.setEnabled(True)
        self.save_btn.setEnabled(True)

        coverage = compute_coverage_pct(root, owner_color)
        text = (
            f"Done. {summary.total_lines} line(s), {summary.total_nodes} node(s), "
            f"max depth reached: {summary.max_depth_reached} of your own moves.\n"
            f"Covers an estimated {coverage:.1f}% of real games reaching this position "
            f"all the way to one of this repertoire's own moves.\n"
        )
        if summary.dead_ends:
            text += f"{summary.dead_ends} line(s) ended early (no viable move / no data). "
        if summary.transpositions_found:
            text += f"{summary.transpositions_found} transposition(s) found (noted in the PGN, not re-expanded). "
        if summary.population_pruned:
            text += f"{summary.population_pruned} opponent repl(y/ies) excluded for being rarer than the Rarity Cutoff. "
        if summary.narrowed_branches:
            text += f"{summary.narrowed_branches} opponent decision(s) hit the branching backstop. "
        if summary.shortfall_branches:
            text += (
                f"{summary.shortfall_branches} branch point(s) hit the opponent move-range cap "
                f"before reaching your coverage requirement."
            )
        self.summary_label.setText(text)

    def _on_build_failed(self, message: str) -> None:
        self.progress.setVisible(False)
        self.build_btn.setEnabled(True)
        QMessageBox.critical(self, "Build failed", message)

    _SURVEY_NAME_RE = re.compile(r"^Survey_(\d+)(?:_.*)?\.pgn$")

    def _next_survey_index(self) -> int:
        SURVEYS_DIR.mkdir(parents=True, exist_ok=True)
        max_index = -1
        for f in SURVEYS_DIR.iterdir():
            m = self._SURVEY_NAME_RE.match(f.name)
            if m:
                max_index = max(max_index, int(m.group(1)))
        return max_index + 1

    def _save_pgn(self) -> None:
        if self.result_root is None or self.result_root_board is None:
            return

        index = self._next_survey_index()
        slug = describe_for_filename(self.result_root, self.result_root_board)
        filename = f"Survey_{index}_{slug}.pgn" if slug else f"Survey_{index}.pgn"
        target = SURVEYS_DIR / filename
        color_name = self.color_combo.currentText()
        mode_name = self.scouting_combo.currentText()
        preset_name = self.preset_combo.currentText()

        headers = {
            "Event": f"{color_name} Repertoire ({mode_name}, {preset_name})",
            # ChessBase (and most viewers) always display the "White"
            # tag as the game's primary/left-hand name, regardless of
            # which color the repertoire is actually for -- so the
            # "Repertoire" label always goes on White here, with the
            # real target color kept in the Event tag above.
            "White": "Repertoire",
            "Black": "?",
            "Result": "*",
        }
        eco_entry = classify_repertoire(self.result_root, self.result_root_board)
        if eco_entry is not None:
            # Standard PGN tags -- ECO is the code, Opening is the name.
            # Any real viewer (ChessBase, lichess, etc.) already knows to
            # display these.
            headers["ECO"] = eco_entry.eco
            headers["Opening"] = eco_entry.name

        game = to_pgn_game(
            self.result_root,
            self.result_root_board,
            headers=headers,
        )

        # Write to a temp file in the same directory, then atomically
        # replace the target -- avoids handing a viewer a half-written
        # file if it happens to be watching/re-reading that path already.
        tmp_path = target.with_suffix(target.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(str(game) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)

        QMessageBox.information(self, "Saved", f"Repertoire saved to {target}")
