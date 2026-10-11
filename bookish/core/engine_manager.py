"""
engine_manager.py

Thin wrapper around python-chess's own UCI implementation
(``chess.engine.SimpleEngine``) so the GUI layer never talks UCI
directly -- it only ever sees ``EngineInfo`` / ``UciOptionSpec`` /
``AnalysisLine`` dataclasses, the same "read-only queries return small
dataclasses, not raw protocol objects" shape already used by
position_stats.py.

python-chess already implements the full UCI protocol (handshake,
option parsing, "go"/"stop", info-line parsing including
score/pv/multipv) -- there's no reason to hand-roll any of that here.
``SimpleEngine`` is the synchronous wrapper: it runs a real
``chess.engine.Protocol`` on a background asyncio event loop thread
internally and marshals every call across that boundary, so every
method here is an ordinary blocking call, safe to invoke from a worker
QThread, and -- per python-chess's own documentation of SimpleEngine --
safe to call from a *different* thread than the one that started the
engine. That's exactly what's needed for a Stop button on the GUI
thread to interrupt an `analyse_stream()` that's blocked in a
background QThread.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import chess
import chess.engine


class EngineError(Exception):
    """Anything that goes wrong starting, configuring, or talking to an
    engine process -- wraps whatever python-chess/OS exception actually
    fired so the GUI only ever needs to catch one type."""


@dataclass
class UciOptionSpec:
    """One entry from the engine's own ``option name ... type ...``
    handshake lines, decoupled from python-chess's ``chess.engine.Option``
    so the GUI (and settings persistence) only ever depend on plain
    data, not on a specific python-chess release's internal shape."""
    name: str
    type: str  # "check" | "spin" | "combo" | "button" | "string"
    default: Any
    min: int | None = None
    max: int | None = None
    var: list[str] = field(default_factory=list)
    # True for options python-chess (and this app) control automatically --
    # MultiPV, Ponder, UCI_Chess960, UCI_Variant, UCI_AnalyseMode. python-chess
    # actively refuses to let callers set most of them (configure() raises),
    # so the GUI must not offer them as editable controls.
    managed: bool = False


@dataclass
class EngineInfo:
    name: str
    author: str
    options: dict[str, UciOptionSpec]


@dataclass
class AnalysisLine:
    """One line (one Multi-PV rank) of a single streaming analysis
    update. Score is always from White's point of view -- score_cp XOR
    score_mate is set, never both, matching how python-chess itself
    represents a score -- the same convention Lichess's own analysis
    board uses for both its eval bar and its per-line numbers, so the
    GUI never has to flip a sign depending on whose turn it is."""
    multipv: int
    depth: int
    seldepth: int | None
    score_cp: int | None
    score_mate: int | None
    nodes: int
    nps: int | None
    time_s: float
    pv_uci: list[str]
    pv_san: list[str]


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _ensure_executable(path: str) -> None:
    """Downloaded engine binaries on Linux/macOS often arrive without
    the executable bit set -- a plain download never has it -- so set
    it ourselves rather than making the user go find a terminal first.
    Windows has no such bit, and any permission failure here (e.g. a
    read-only mount) is non-fatal: popen_uci below will simply raise
    its own clear error if the file genuinely can't be executed."""
    if _is_windows():
        return
    try:
        current = os.stat(path).st_mode
        os.chmod(path, current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


class EngineManager:
    """One loaded UCI engine process. Create one per engine the user
    has added in the Engine tab; call `start()` once, `quit()` when
    removed or on app shutdown."""

    def __init__(self, path: str):
        self.path = path
        self._engine: chess.engine.SimpleEngine | None = None
        self._current_analysis: chess.engine.SimpleAnalysisResult | None = None

    @property
    def running(self) -> bool:
        return self._engine is not None

    def start(self) -> EngineInfo:
        _ensure_executable(self.path)
        try:
            self._engine = chess.engine.SimpleEngine.popen_uci(self.path)
        except (OSError, chess.engine.EngineError) as exc:
            raise EngineError(f"Couldn't start engine at {self.path}: {exc}") from exc

        id_info = self._engine.id
        options = {
            name: UciOptionSpec(
                name=opt.name,
                type=opt.type,
                default=opt.default,
                min=opt.min,
                max=opt.max,
                var=list(opt.var) if opt.var else [],
                managed=opt.is_managed() or name.lower() == "uci_analysemode",
            )
            for name, opt in self._engine.options.items()
        }
        return EngineInfo(
            name=id_info.get("name") or Path(self.path).stem,
            author=id_info.get("author", ""),
            options=options,
        )

    def configure(self, options: dict[str, Any]) -> None:
        """Sends the given options to the engine right away. A UCI
        "button" option is fired by passing `None` as its value (python-chess
        sends the bare `setoption name <X>` line; it just doesn't remember
        a None afterward).

        Not safe to call while `analyse_stream()` is running: python-chess
        runs this as a command of its own, which cancels the analysis in
        progress -- the stream then just ends, with no error. Callers
        with an analysis open should pause it first (see
        EngineAnalysisDialog.run_while_paused)."""
        if self._engine is None:
            raise EngineError("Engine is not running.")
        try:
            self._engine.configure(options)
        except chess.engine.EngineError as exc:
            raise EngineError(str(exc)) from exc

    def quit(self) -> None:
        self.stop()
        if self._engine is not None:
            try:
                self._engine.quit()
            except chess.engine.EngineError:
                pass  # already dead / didn't respond -- nothing more we can do
            self._engine = None

    def stop(self) -> None:
        """Ends whatever `analyse_stream()` is currently in progress
        (if any), from any thread. Safe to call even when nothing is
        running."""
        if self._current_analysis is not None:
            self._current_analysis.stop()

    def analyse_stream(
        self, board: chess.Board, multipv: int = 1, cancel: threading.Event | None = None,
    ) -> Iterator[list[AnalysisLine]]:
        """Blocking generator: analyzes `board` with no time/depth
        limit (matches Lichess's own "computer analysis" panel -- it
        keeps thinking, and getting deeper, until told to stop),
        yielding the current best-known set of lines -- one per
        Multi-PV rank, sorted by rank -- every time the engine reports
        a fresh update. Runs forever until `stop()` is called (from any
        thread) or the caller simply stops iterating. Only one analysis
        at a time per engine is supported here -- the caller
        (EngineAnalysisDialog) always stops the previous one first.

        `cancel` closes a real race that a plain `stop()` can't: if a
        stop request lands just *before* this generator has registered
        its analysis (which is exactly what rapid position changes --
        holding an arrow key, say -- cause), `stop()` finds nothing to
        stop and is silently lost, leaving an infinite analysis running
        with nobody listening. The caller sets `cancel` *before* calling
        `stop()`; this generator checks it right after registering the
        analysis and on every update, so whichever side "wins" the race,
        the analysis still ends.
        """
        if self._engine is None:
            raise EngineError("Engine is not running.")

        best_by_pv: dict[int, AnalysisLine] = {}
        analysis_result = self._engine.analysis(board, multipv=multipv)
        self._current_analysis = analysis_result
        try:
            with analysis_result as analysis:
                if cancel is not None and cancel.is_set():
                    return  # __exit__ stops the analysis we just started
                for info in analysis:
                    if cancel is not None and cancel.is_set():
                        break
                    line = self._info_to_line(info, board)
                    if line is None:
                        continue
                    best_by_pv[line.multipv] = line
                    yield [best_by_pv[i] for i in sorted(best_by_pv)]
        except chess.engine.EngineError as exc:
            raise EngineError(str(exc)) from exc
        finally:
            # Only clear our own registration -- a stale generator
            # finishing late must never wipe out a newer analysis that
            # has already registered itself.
            if self._current_analysis is analysis_result:
                self._current_analysis = None

    @staticmethod
    def _info_to_line(info: dict, board: chess.Board) -> AnalysisLine | None:
        # A "currmove"/"currline"-only info line (no depth/pv yet) isn't
        # a real update to show -- skip it rather than displaying a
        # half-populated row.
        if "depth" not in info or not info.get("pv"):
            return None

        score_cp = score_mate = None
        score = info.get("score")
        if score is not None:
            pov = score.white()
            score_mate = pov.mate()
            if score_mate is None:
                score_cp = pov.score()

        b = board.copy(stack=False)
        pv_uci: list[str] = []
        pv_san: list[str] = []
        for mv in info["pv"]:
            try:
                pv_san.append(b.san(mv))
            except (AssertionError, ValueError):
                break  # an engine reporting an illegal continuation -- stop, keep what parsed so far
            pv_uci.append(mv.uci())
            b.push(mv)

        return AnalysisLine(
            multipv=info.get("multipv", 1),
            depth=info["depth"],
            seldepth=info.get("seldepth"),
            score_cp=score_cp,
            score_mate=score_mate,
            nodes=info.get("nodes", 0),
            nps=info.get("nps"),
            time_s=info.get("time", 0.0),
            pv_uci=pv_uci,
            pv_san=pv_san,
        )
