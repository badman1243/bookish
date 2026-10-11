"""
board_widget.py

An interactive chessboard used to set/input the position the rest of the
app looks at. Click a piece then click a destination, or press-drag-drop
it -- both work interchangeably. Supports FEN import, undo/redo, flip,
reset, having a move pushed onto it externally (e.g. by clicking a row
in the Database panel's move table), light/dark margin styling to match
the app theme, resizing (square size is computed live from whatever
space the widget's actually been given, staying square and centered
rather than stretching), smooth piece-move animation (see "Animation"
below), and drawing engine-suggested move arrows on top of the board
(see "Arrows" below).

Pieces are rendered from Lichess's "Companion" SVG set (by David L.
Brown, distributed as freeware for chess software -- see
gui/pieces/companion/ and the project README for the source/license
note), loaded once at startup and rendered with QSvgRenderer.

Animation
---------
Moves made by clicking, by an external call (push_move_uci -- Database
row clicks, the Right-arrow "play the top move" fallback), or by
undo/redo all slide the moved piece from its old square to its new one
over a short, configurable duration, instead of the position just
snapping. A move completed by *dragging* does not additionally
animate -- the piece is already sitting where you dropped it, so
sliding it again would look like a correction, not a smooth move.

Implementation: the board's own position (self.board) is always updated
immediately when a move happens -- nothing about the *logical* state is
deferred, only the *painting* of it. A short-lived QTimer advances
progress (measured against wall-clock time via time.monotonic(), not a
fixed step count, so the animation still takes the same real duration
even if a frame or two gets skipped under load) and repaints each tick;
paintEvent skips drawing the moving piece(s) at their normal, final
square while an animation is in flight, drawing them at an interpolated
position instead (eased -- fast start, slow finish -- rather than
linear, which is what most chess sites do and reads as noticeably more
natural). Castling animates both the king and the rook; a promotion
animates as the pawn sliding, then simply appears as the promoted piece
the instant it lands, matching how most chess UIs handle it.

Arrows
------
A separate, independent overlay from everything else painted here:
set_arrows()/clear_arrows() let another widget (currently just
EngineAnalysisDialog, drawing its Multi-PV candidate moves) hand this
widget a list of (from_square, to_square, color) tuples to draw on top
of the board and pieces, Lichess-style. Nothing here assumes who's
setting them or why -- the board doesn't know or care that they
currently only ever come from engine analysis.
"""

from __future__ import annotations

import math
import time
from pathlib import Path

import chess
from PySide6.QtCore import Qt, QRectF, QPointF, QSize, QTimer, Signal
from PySide6.QtGui import QPainter, QColor, QFont, QMouseEvent, QPen, QPolygonF
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QWidget, QDialog, QVBoxLayout, QPushButton, QLabel

from gui import theme

PIECES_DIR = Path(__file__).resolve().parent / "pieces" / "companion"

# a1 is a dark square: light iff (file + rank) is odd.
LIGHT_SQUARE = QColor("#f0d9b5")
DARK_SQUARE = QColor("#b58863")
SELECTED_COLOR = QColor(255, 255, 0, 140)
TARGET_COLOR = QColor(0, 0, 0, 60)
LAST_MOVE_COLOR = QColor(255, 165, 0, 90)
MARGIN = 22
DRAG_THRESHOLD = 6  # pixels of movement before a press-drag counts as a drag rather than a click
DEFAULT_SQUARE_SIZE = 56  # used for the initial/preferred size before any manual resize
MIN_SQUARE_SIZE = 20  # floor so the board never collapses into something unusable/inverted

# Named speeds shown in the GUI -> animation duration in milliseconds.
# "Off" (0) means moves snap instantly, same as before this existed.
ANIMATION_DURATIONS_MS = {"Off": 0, "Fast": 100, "Normal": 200, "Slow": 350}
DEFAULT_ANIMATION_SPEED = "Normal"
_ANIMATION_TICK_MS = 16  # ~60fps
# Modeled on lichess's own chessground: its default animation duration is
# confirmed at 200ms exactly (matching "Normal" below) from chessground's
# own published config. Its easing is applied as a CSS transition on a
# `.anim` class rather than a value visible directly in the TypeScript
# source, so the exact curve isn't independently confirmable the same
# way the duration is -- CSS_EASE below is the standard CSS `ease` curve,
# what an unstyled transition defaults to, and the best-supported
# reconstruction of that feel: gentle throughout rather than accelerating
# hard immediately the way a plain ease-out (this file's previous
# approach) does.
CSS_EASE = (0.25, 0.1, 0.25, 1.0)


def _cubic_bezier(t: float, control: tuple[float, float, float, float]) -> float:
    """Evaluates a CSS-style cubic-bezier(x1,y1,x2,y2) timing function at
    time t in [0, 1]. Both axes of a CSS easing curve are parametric
    functions of an auxiliary variable u (not a simple y=f(t)), so this
    solves bezier_x(u) = t via Newton-Raphson -- the same approach
    browsers use internally -- then evaluates bezier_y(u)."""
    x1, y1, x2, y2 = control

    def bezier(u: float, p1: float, p2: float) -> float:
        return 3 * (1 - u) ** 2 * u * p1 + 3 * (1 - u) * u ** 2 * p2 + u ** 3

    u = t
    for _ in range(8):
        x = bezier(u, x1, x2) - t
        if abs(x) < 1e-6:
            break
        dx = 3 * (1 - u) ** 2 * x1 + 6 * (1 - u) * u * (x2 - x1) + 3 * u ** 2 * (1 - x2)
        if abs(dx) < 1e-6:
            break
        u -= x / dx
    return bezier(u, y1, y2)


def _piece_file_stem(symbol: str) -> str:
    color_prefix = "w" if symbol.isupper() else "b"
    return f"{color_prefix}{symbol.upper()}"


class PromotionDialog(QDialog):
    """Small popup asking which piece to promote a pawn to."""

    def __init__(self, color: chess.Color, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Promote to")
        self.chosen = chess.QUEEN
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Promote pawn to:"))
        for piece, label in ((chess.QUEEN, "Queen"), (chess.ROOK, "Rook"),
                              (chess.BISHOP, "Bishop"), (chess.KNIGHT, "Knight")):
            btn = QPushButton(label)
            btn.clicked.connect(lambda checked=False, p=piece: self._choose(p))
            layout.addWidget(btn)

    def _choose(self, piece: int) -> None:
        self.chosen = piece
        self.accept()


class ChessBoardWidget(QWidget):
    positionChanged = Signal()
    # Fired specifically when a move is *made* (click, drag, an external
    # push_move_uci call, or redo) -- never for undo, set_fen, or reset,
    # since those are navigation, not making a move. Exists so anything
    # that cares specifically about "a move was just made" (as opposed
    # to positionChanged, which fires uniformly for all of the above)
    # doesn't have to infer that from context.
    moveMade = Signal()

    _svg_cache: dict[str, QSvgRenderer] = {}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.board = chess.Board()
        self.flipped = False
        self.selected_square: int | None = None
        self.legal_targets: set[int] = set()
        self.last_move: chess.Move | None = None
        self._dark = False
        self._redo_stack: list[chess.Move] = []
        self._arrows: list[tuple[int, int, QColor]] = []

        self._dragging = False
        self._press_pos: QPointF | None = None
        self._drag_pos: QPointF | None = None

        # Animation state -- see module docstring.
        self._anim_duration_ms = ANIMATION_DURATIONS_MS[DEFAULT_ANIMATION_SPEED]
        self._anim_pieces: list[tuple[str, int, int]] = []  # (symbol, from_square, to_square)
        self._anim_progress = 1.0
        self._anim_start_time = 0.0
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._on_animation_tick)

        self._load_piece_svgs()
        self.setMinimumSize(MIN_SQUARE_SIZE * 8 + MARGIN * 2, MIN_SQUARE_SIZE * 8 + MARGIN * 2)
        # Needs real keyboard focus (not just mouse clicks) so arrow-key
        # undo/redo has somewhere sensible to go by default -- see
        # MainWindow, which explicitly focuses this on startup rather than
        # leaving the FEN text box holding default focus.
        self.setFocusPolicy(Qt.StrongFocus)

    def sizeHint(self) -> QSize:
        # Only matters for the very first layout pass, before anyone's
        # dragged the splitter -- after that, actual allocated size wins.
        return QSize(DEFAULT_SQUARE_SIZE * 8 + MARGIN * 2, DEFAULT_SQUARE_SIZE * 8 + MARGIN * 2)

    @classmethod
    def _load_piece_svgs(cls) -> None:
        if cls._svg_cache:
            return
        for color_prefix in ("w", "b"):
            for piece_letter in ("P", "N", "B", "R", "Q", "K"):
                stem = f"{color_prefix}{piece_letter}"
                path = PIECES_DIR / f"{stem}.svg"
                if path.exists():
                    cls._svg_cache[stem] = QSvgRenderer(str(path))

    # -- public API ----------------------------------------------------

    def set_dark_mode(self, dark: bool) -> None:
        self._dark = dark
        self.update()

    def set_animation_speed(self, speed_name: str) -> None:
        """speed_name is one of ANIMATION_DURATIONS_MS's keys ("Off",
        "Fast", "Normal", "Slow"); unrecognized names are treated as
        Off, rather than raising, since this is a cosmetic setting."""
        self._anim_duration_ms = ANIMATION_DURATIONS_MS.get(speed_name, 0)

    def set_arrows(self, arrows: list[tuple[int, int, QColor]]) -> None:
        """`arrows` is a list of (from_square, to_square, color) tuples,
        drawn on top of the board and pieces. Used today by the engine
        analysis popup to show its top candidate move(s), one color per
        Multi-PV rank -- kept general rather than hardcoded to "the
        engine's arrows" specifically, since nothing here needs to know
        who's setting them."""
        self._arrows = arrows
        self.update()

    def clear_arrows(self) -> None:
        self.set_arrows([])

    def set_fen(self, fen: str) -> bool:
        try:
            new_board = chess.Board(fen)
        except ValueError:
            return False
        self.board = new_board
        self._clear_interaction_state()
        self._redo_stack.clear()
        self._cancel_animation()
        self.last_move = None
        self.clear_arrows()
        self.update()
        self.positionChanged.emit()
        return True

    def reset(self) -> None:
        self.board.reset()
        self._clear_interaction_state()
        self._redo_stack.clear()
        self._cancel_animation()
        self.last_move = None
        self.clear_arrows()
        self.update()
        self.positionChanged.emit()

    def undo(self) -> bool:
        """Steps back one move. Returns whether there was one to undo."""
        if not self.board.move_stack:
            return False
        mv = self.board.peek()
        moves_info = self._piece_moves_for(mv, self.board, reverse=True)
        self.board.pop()
        self._redo_stack.append(mv)
        self.last_move = self.board.peek() if self.board.move_stack else None
        self._clear_interaction_state()
        self.positionChanged.emit()
        self._start_animation(moves_info)
        return True

    def redo(self) -> bool:
        """Re-applies the most recently undone move, if any. Cleared by
        any genuinely new move (same as a text editor's undo/redo)."""
        if not self._redo_stack:
            return False
        mv = self._redo_stack.pop()
        moves_info = self._piece_moves_for(mv, self.board, reverse=False)
        self.board.push(mv)
        self.last_move = mv
        self._clear_interaction_state()
        self.positionChanged.emit()
        self.moveMade.emit()
        self._start_animation(moves_info)
        return True

    def flip(self) -> None:
        self.flipped = not self.flipped
        self.update()

    def get_board(self) -> chess.Board:
        return self.board.copy()

    def push_move_uci(self, uci: str) -> bool:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            return False
        if move not in self.board.legal_moves:
            return False
        moves_info = self._piece_moves_for(move, self.board, reverse=False)
        self.board.push(move)
        self.last_move = move
        self._redo_stack.clear()
        self._clear_interaction_state()
        self.positionChanged.emit()
        self.moveMade.emit()
        self._start_animation(moves_info)
        return True

    def move_list_san(self) -> list[str]:
        b = chess.Board()
        sans = []
        for mv in self.board.move_stack:
            sans.append(b.san(mv))
            b.push(mv)
        return sans

    def _clear_interaction_state(self) -> None:
        self.selected_square = None
        self.legal_targets = set()
        self._dragging = False
        self._press_pos = None
        self._drag_pos = None

    # -- animation ---------------------------------------------------------

    def _piece_moves_for(self, move: chess.Move, board_snapshot: chess.Board, reverse: bool) -> list[tuple[str, int, int]]:
        """(symbol, from_square, to_square) for every piece that visibly
        relocates for this move -- normally just the one, plus the rook
        too for castling. `board_snapshot` must be a board where the
        moving piece is still actually sitting on its *source* square for
        this direction: the current board (pre-push) for a forward move,
        or the current board (pre-pop, i.e. the move is still applied) for
        an undo. Works for both directions symmetrically since castling
        detection here is by move geometry (a king moving two files), not
        by querying castling rights, which wouldn't reflect the same
        squares once undone anyway."""
        src_sq = move.to_square if reverse else move.from_square
        dst_sq = move.from_square if reverse else move.to_square
        piece = board_snapshot.piece_at(src_sq)
        if piece is None:
            return []
        info = [(piece.symbol(), src_sq, dst_sq)]

        is_castle = (
            piece.piece_type == chess.KING
            and abs(chess.square_file(dst_sq) - chess.square_file(src_sq)) == 2
        )
        if is_castle:
            rank = chess.square_rank(src_sq)
            # The post-castle king square is dst_sq going forward, but
            # src_sq in reverse (undo) -- src_sq/dst_sq swap which one
            # holds "the real square the king ends up on" depending on
            # direction, so comparing them against each other for
            # kingside-vs-queenside silently inverts under reverse=True.
            # Checking the actual post-castle square against the g-file
            # directly is unambiguous either way.
            post_castle_king_sq = src_sq if reverse else dst_sq
            kingside = chess.square_file(post_castle_king_sq) == 6  # g-file
            rook_from_file, rook_to_file = (7, 5) if kingside else (0, 3)
            rook_from = chess.square(rook_from_file, rank)
            rook_to = chess.square(rook_to_file, rank)
            r_src, r_dst = (rook_to, rook_from) if reverse else (rook_from, rook_to)
            rook_piece = board_snapshot.piece_at(r_src)
            if rook_piece is not None:
                info.append((rook_piece.symbol(), r_src, r_dst))
        return info

    def _start_animation(self, moves_info: list[tuple[str, int, int]]) -> None:
        if self._anim_duration_ms <= 0 or not moves_info:
            self._cancel_animation()
            self.update()
            return
        self._anim_pieces = moves_info
        self._anim_progress = 0.0
        self._anim_start_time = time.monotonic()
        self._anim_timer.start(_ANIMATION_TICK_MS)
        self.update()

    def _cancel_animation(self) -> None:
        if self._anim_timer.isActive():
            self._anim_timer.stop()
        self._anim_pieces = []
        self._anim_progress = 1.0

    def _on_animation_tick(self) -> None:
        elapsed_ms = (time.monotonic() - self._anim_start_time) * 1000.0
        self._anim_progress = min(1.0, elapsed_ms / self._anim_duration_ms)
        self.update()
        if self._anim_progress >= 1.0:
            self._anim_timer.stop()
            self._anim_pieces = []

    # -- dynamic sizing ---------------------------------------------------

    def _square_size(self) -> float:
        available = min(self.width(), self.height()) - 2 * MARGIN
        return max(MIN_SQUARE_SIZE, available / 8)

    def _board_origin(self) -> tuple[float, float]:
        sq = self._square_size()
        board_extent = sq * 8 + 2 * MARGIN
        return (self.width() - board_extent) / 2, (self.height() - board_extent) / 2

    # -- coordinate mapping ------------------------------------------------

    def _col_for_file(self, file: int) -> int:
        return 7 - file if self.flipped else file

    def _row_for_rank(self, rank: int) -> int:
        return rank if self.flipped else 7 - rank

    def _file_for_col(self, col: int) -> int:
        return 7 - col if self.flipped else col

    def _rank_for_row(self, row: int) -> int:
        return row if self.flipped else 7 - row

    def _screen_pos(self, square: int) -> tuple[float, float]:
        file = chess.square_file(square)
        rank = chess.square_rank(square)
        col = self._col_for_file(file)
        row = self._row_for_rank(rank)
        ox, oy = self._board_origin()
        sq = self._square_size()
        return ox + MARGIN + col * sq, oy + MARGIN + row * sq

    def _square_at(self, pos: QPointF) -> int | None:
        ox, oy = self._board_origin()
        sq = self._square_size()
        col = int((pos.x() - ox - MARGIN) // sq)
        row = int((pos.y() - oy - MARGIN) // sq)
        if not (0 <= col < 8 and 0 <= row < 8):
            return None
        file = self._file_for_col(col)
        rank = self._rank_for_row(row)
        return chess.square(file, rank)

    # -- painting --------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        margin_colors = theme.BOARD_MARGIN["dark" if self._dark else "light"]
        painter.fillRect(self.rect(), margin_colors["bg"])
        sq = self._square_size()

        for rank in range(8):
            for file in range(8):
                square = chess.square(file, rank)
                x, y = self._screen_pos(square)
                color = DARK_SQUARE if (file + rank) % 2 == 0 else LIGHT_SQUARE
                painter.fillRect(QRectF(x, y, sq, sq), color)

        if self.last_move is not None:
            for s in (self.last_move.from_square, self.last_move.to_square):
                x, y = self._screen_pos(s)
                painter.fillRect(QRectF(x, y, sq, sq), LAST_MOVE_COLOR)

        if self.selected_square is not None:
            x, y = self._screen_pos(self.selected_square)
            painter.fillRect(QRectF(x, y, sq, sq), SELECTED_COLOR)

        for s in self.legal_targets:
            x, y = self._screen_pos(s)
            painter.setBrush(TARGET_COLOR)
            painter.setPen(Qt.NoPen)
            r = sq * 0.15
            cx, cy = x + sq / 2, y + sq / 2
            painter.drawEllipse(QPointF(cx, cy), r, r)

        dragged_square = self.selected_square if self._dragging else None
        animating_to_squares = {to_sq for (_, _, to_sq) in self._anim_pieces}
        pad = sq * 0.06
        for square, piece in self.board.piece_map().items():
            if square == dragged_square or square in animating_to_squares:
                continue
            x, y = self._screen_pos(square)
            self._draw_piece(painter, piece.symbol(), QRectF(x + pad, y + pad, sq - 2 * pad, sq - 2 * pad))

        if dragged_square is not None and self._drag_pos is not None:
            piece = self.board.piece_at(dragged_square)
            if piece is not None:
                half = (sq - 2 * pad) / 2
                rect = QRectF(self._drag_pos.x() - half, self._drag_pos.y() - half, half * 2, half * 2)
                self._draw_piece(painter, piece.symbol(), rect)

        if self._anim_pieces:
            eased = _cubic_bezier(self._anim_progress, CSS_EASE)
            for symbol, from_sq, to_sq in self._anim_pieces:
                fx, fy = self._screen_pos(from_sq)
                tx, ty = self._screen_pos(to_sq)
                ix = fx + (tx - fx) * eased
                iy = fy + (ty - fy) * eased
                self._draw_piece(painter, symbol, QRectF(ix + pad, iy + pad, sq - 2 * pad, sq - 2 * pad))

        if self._arrows:
            self._draw_arrows(painter)

        ox, oy = self._board_origin()
        label_font = QFont()
        label_font.setPointSize(max(6, min(9, int(sq * 0.16))))
        painter.setFont(label_font)
        painter.setPen(margin_colors["text"])
        for col in range(8):
            file_letter = chess.FILE_NAMES[self._file_for_col(col)]
            x = ox + MARGIN + col * sq
            painter.drawText(QRectF(x, oy + MARGIN + 8 * sq, sq, MARGIN), Qt.AlignHCenter | Qt.AlignTop, file_letter)
        for row in range(8):
            rank_number = str(self._rank_for_row(row) + 1)
            y = oy + MARGIN + row * sq
            painter.drawText(QRectF(ox, y, MARGIN - 4, sq), Qt.AlignVCenter | Qt.AlignRight, rank_number)

    def _draw_piece(self, painter: QPainter, symbol: str, rect: QRectF) -> None:
        renderer = self._svg_cache.get(_piece_file_stem(symbol))
        if renderer is not None and renderer.isValid():
            renderer.render(painter, rect)

    def _draw_arrows(self, painter: QPainter) -> None:
        """Draws each (from_square, to_square, color) arrow -- see
        set_arrows() -- from square-center to square-center, on top of
        everything else. The shaft is stopped short of the destination
        center so the arrowhead's *point*, not the line, is what lands
        exactly there, the same visual convention Lichess's own board
        uses for its move arrows."""
        sq = self._square_size()
        painter.setRenderHint(QPainter.Antialiasing)
        for from_sq, to_sq, color in self._arrows:
            fx, fy = self._screen_pos(from_sq)
            tx, ty = self._screen_pos(to_sq)
            cx1, cy1 = fx + sq / 2, fy + sq / 2
            cx2, cy2 = tx + sq / 2, ty + sq / 2
            dx, dy = cx2 - cx1, cy2 - cy1
            length = math.hypot(dx, dy)
            if length < 1e-6:
                continue  # a "null" arrow (from == to) -- nothing sensible to draw
            ux, uy = dx / length, dy / length

            head_len = sq * 0.35
            shaft_end_x = cx2 - ux * head_len
            shaft_end_y = cy2 - uy * head_len

            painter.setPen(QPen(color, sq * 0.12, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(QPointF(cx1, cy1), QPointF(shaft_end_x, shaft_end_y))

            head_w = sq * 0.20
            perp_x, perp_y = -uy, ux
            tip = QPointF(cx2, cy2)
            left = QPointF(shaft_end_x + perp_x * head_w, shaft_end_y + perp_y * head_w)
            right = QPointF(shaft_end_x - perp_x * head_w, shaft_end_y - perp_y * head_w)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawPolygon(QPolygonF([tip, left, right]))

    # -- interaction --------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.setFocus(Qt.MouseFocusReason)
        if event.button() != Qt.LeftButton:
            return
        square = self._square_at(event.position())
        if square is None:
            return

        if self.selected_square is not None and square != self.selected_square:
            if self._try_move(self.selected_square, square):
                self._clear_interaction_state()
                self.update()
                return
            piece = self.board.piece_at(square)
            if not (piece and piece.color == self.board.turn):
                self._clear_interaction_state()
                self.update()
                return

        piece = self.board.piece_at(square)
        if piece and piece.color == self.board.turn:
            self.selected_square = square
            self.legal_targets = {m.to_square for m in self.board.legal_moves if m.from_square == square}
            self._press_pos = event.position()
            self._drag_pos = event.position()
            self._dragging = False
            self.update()
        else:
            self._clear_interaction_state()
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.selected_square is None or not (event.buttons() & Qt.LeftButton):
            return
        if self._press_pos is not None and not self._dragging:
            delta = event.position() - self._press_pos
            if abs(delta.x()) > DRAG_THRESHOLD or abs(delta.y()) > DRAG_THRESHOLD:
                self._dragging = True
        self._drag_pos = event.position()
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.LeftButton or self.selected_square is None:
            return
        if self._dragging:
            square = self._square_at(event.position())
            if square is not None and square != self.selected_square:
                self._try_move(self.selected_square, square)
            self._clear_interaction_state()
            self.update()

    def _try_move(self, from_sq: int, to_sq: int) -> bool:
        candidate = chess.Move(from_sq, to_sq)
        moving_piece = self.board.piece_at(from_sq)
        is_promotion = (
            moving_piece is not None
            and moving_piece.piece_type == chess.PAWN
            and chess.square_rank(to_sq) in (0, 7)
        )
        if is_promotion:
            dlg = PromotionDialog(self.board.turn, self)
            if dlg.exec():
                candidate = chess.Move(from_sq, to_sq, promotion=dlg.chosen)
            else:
                candidate = chess.Move(from_sq, to_sq, promotion=chess.QUEEN)

        if candidate in self.board.legal_moves:
            # A move completed by dragging is already visually at its
            # destination -- animating it again would look like an
            # unwanted correction, not a smooth move, so only animate
            # click-completed moves.
            moves_info = [] if self._dragging else self._piece_moves_for(candidate, self.board, reverse=False)
            self.board.push(candidate)
            self.last_move = candidate
            self._redo_stack.clear()
            self.positionChanged.emit()
            self.moveMade.emit()
            self._start_animation(moves_info)
            return True
        return False
