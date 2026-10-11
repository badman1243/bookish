"""
position_stats.py

Read-only queries against an index built by pgn_indexer.PgnIndexer.
Used by both the Database viewer GUI panel and the repertoire builder.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

import chess


@dataclass
class MoveStat:
    move_uci: str
    san: str
    count: int
    white_wins: int
    draws: int
    black_wins: int

    @property
    def total(self) -> int:
        return self.white_wins + self.draws + self.black_wins

    def pct_of(self, denominator: int) -> float:
        return 100.0 * self.count / denominator if denominator else 0.0

    def white_win_pct(self) -> float:
        return 100.0 * self.white_wins / self.total if self.total else 0.0

    def draw_pct(self) -> float:
        return 100.0 * self.draws / self.total if self.total else 0.0

    def black_win_pct(self) -> float:
        return 100.0 * self.black_wins / self.total if self.total else 0.0

    def win_pct_for(self, color: chess.Color) -> float:
        """Win rate for whichever color is about to play this move's owner."""
        return self.white_win_pct() if color == chess.WHITE else self.black_win_pct()

    def weighted_points_pct(self, color: chess.Color, draw_weight: float) -> float:
        """Score for `color`'s side as a percentage, treating a win as 1
        point and a draw as `draw_weight` points. This one knob covers the
        three scouting modes:
          - draw_weight=0.0 ("Aggressive"): draws count for nothing -- only
            decisive results are rewarded, so drawish moves lose out to
            sharper ones even at an equal or lower win rate.
          - draw_weight=0.5 ("Neutral"): standard chess scoring.
          - draw_weight=1.0 ("Solid"): not losing is as good as winning --
            rewards moves that hold firm against strong opposition even
            when they don't convert to a win.
        Symmetric for either color: it's always "how well did *this* side
        do", never assuming White or Black specifically.
        """
        if self.total == 0:
            return 0.0
        wins = self.white_wins if color == chess.WHITE else self.black_wins
        return 100.0 * (wins + draw_weight * self.draws) / self.total

    def confidence_adjusted_pct(self, color: chess.Color, draw_weight: float, z: float = 1.96) -> float:
        """`weighted_points_pct()`, pulled down toward 50% in proportion to
        how little data supports it (see `wilson_lower_bound` below). A
        move at 83% from 12 games and a move at 52% from 50,000 games
        should not be compared at face value -- this puts them on the
        same, sample-size-aware footing. `z` controls how skeptical this
        is (higher = more conservative); see RepertoireConfig.confidence_z."""
        p = self.weighted_points_pct(color, draw_weight) / 100.0
        return 100.0 * wilson_lower_bound(p, self.count, z)

    def confidence_interval_pct(self, color: chess.Color, draw_weight: float, z: float = 1.96) -> tuple[float, float]:
        """(lower, upper) confidence bound as percentages for this move's
        weighted score. Useful for display/inspection; for actually
        deciding whether one move is confidently better than another, use
        `is_confidently_better` below instead of comparing these bounds
        directly against each other (see its docstring for why)."""
        p = self.weighted_points_pct(color, draw_weight) / 100.0
        lower, upper = wilson_interval(p, self.count, z)
        return 100.0 * lower, 100.0 * upper

    def confidence_adjusted_pct_of(self, denominator: int, z: float = 1.96) -> float:
        """Wilson lower bound of this move's *share of games* at this
        position (not its win/draw/loss record) -- a conservative estimate
        of how common it really is, not just the observed count/denominator
        ratio. Used on the opponent side (Ignore Threshold, Rarity Cutoff,
        Requirement top-up) for the same reason confidence_adjusted_pct is
        used for the owner's own move: a reply that's 6% from 15 games
        isn't confidently "6%" any more than a candidate move that's 83%
        from 12 games is confidently "83%"."""
        if denominator <= 0:
            return 0.0
        p = self.count / denominator
        return 100.0 * wilson_lower_bound(p, denominator, z)

    def confidence_gap(self) -> float:
        """How many percentage points Wilson-adjustment pulls this move's
        (neutral-weighted) score down by. Large gap = thin sample, treat
        the raw percentage with real skepticism; near-zero gap = plenty of
        games back this number up. Used to flag low-confidence rows in the
        Database viewer regardless of which repertoire mode you're in."""
        p = self.weighted_points_pct(chess.WHITE, 0.5) / 100.0
        return abs(p - wilson_lower_bound(p, self.count)) * 100.0


def _wilson_center_margin(p: float, n: int, z: float) -> tuple[float, float, float]:
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return center, margin, denom


def wilson_lower_bound(p: float, n: int, z: float = 1.96) -> float:
    """Wilson score interval lower bound for a proportion `p` estimated
    from `n` samples, as a fraction in [0, 1]. Conservative: with little
    data it pulls the estimate a long way toward 50% (maximum
    uncertainty); with a lot of data it barely moves the observed value
    at all. z=1.96 is the standard ~95%-confidence choice -- see
    RepertoireConfig.confidence_z for making this configurable."""
    if n <= 0:
        return 0.0
    center, margin, denom = _wilson_center_margin(p, n, z)
    return max(0.0, min(1.0, (center - margin) / denom))


def wilson_upper_bound(p: float, n: int, z: float = 1.96) -> float:
    """The mirror image of wilson_lower_bound: how high the true rate
    could plausibly be, given the same sample."""
    if n <= 0:
        return 1.0
    center, margin, denom = _wilson_center_margin(p, n, z)
    return max(0.0, min(1.0, (center + margin) / denom))


def wilson_interval(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """(lower, upper) in one call, sharing the computation."""
    if n <= 0:
        return 0.0, 1.0
    center, margin, denom = _wilson_center_margin(p, n, z)
    return max(0.0, min(1.0, (center - margin) / denom)), max(0.0, min(1.0, (center + margin) / denom))


def is_confidently_better(challenger: "MoveStat", champion: "MoveStat", color: chess.Color, draw_weight: float, z: float = 1.96) -> bool:
    """Is `challenger`'s weighted score significantly higher than
    `champion`'s, accounting for both samples' uncertainty together?

    This is a proper two-proportion comparison (a difference-of-proportions
    z-test on p_challenger - p_champion), not a check of whether their
    individual confidence intervals happen not to overlap -- that's a
    well-known statistical pitfall: requiring two separate intervals to
    not overlap is a substantially stricter (over-conservative) bar than
    testing whether the difference between them is actually significant,
    and using it here would wrongly favor the more-tested move even in
    cases with a real, meaningful, statistically-supported edge for the
    less-tested one.
    """
    p1 = champion.weighted_points_pct(color, draw_weight) / 100.0
    p2 = challenger.weighted_points_pct(color, draw_weight) / 100.0
    n1, n2 = champion.count, challenger.count
    if n1 <= 0 or n2 <= 0:
        return False
    se_diff = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    if se_diff <= 0:
        return p2 > p1
    return (p2 - p1) - z * se_diff > 0


@dataclass
class PositionStats:
    pos_hash: int
    game_count: int
    moves: list[MoveStat]  # sorted by count desc

    def total_white_wins(self) -> int:
        return sum(m.white_wins for m in self.moves)

    def total_draws(self) -> int:
        return sum(m.draws for m in self.moves)

    def total_black_wins(self) -> int:
        return sum(m.black_wins for m in self.moves)


def position_key(board: chess.Board) -> tuple:
    """The raw, exact comparison key for a position: piece placement (as
    six piece-type bitboards plus the two color-occupancy bitboards),
    side to move, legally relevant castling rights, and legally relevant
    en-passant square. Two positions are the identical position if and
    only if this tuple matches exactly -- there's no approximation here,
    this *is* the position. `position_hash` below just hashes it for fast
    dict/SQLite lookups; anywhere that needs a guarantee (not just "very
    likely the same"), compare this tuple directly instead of the hash.
    """
    return (
        board.pawns, board.knights, board.bishops, board.rooks,
        board.queens, board.kings,
        board.occupied_co[chess.WHITE], board.occupied_co[chess.BLACK],
        board.turn, board.clean_castling_rights(),
        board.ep_square if board.has_legal_en_passant() else None,
    )


def position_hash(board: chess.Board) -> int:
    """A cheap, internally-consistent position hash, built from
    `position_key()` above (the same idea as python-chess's own
    ``Board._transposition_key()``, reimplemented directly here so we can
    hash it ourselves).

    This is deliberately NOT a Polyglot-compatible zobrist hash -- we
    don't need opening-book compatibility, only internal consistency
    between the indexer and every later query -- and it's substantially
    cheaper to compute: Polyglot's hash XORs together one random-table
    entry per piece on the board (a Python-level loop over every
    occupied square), while this is a single hash of a small tuple of
    already-available bitboard integers. Confirmed deterministic across
    separate process runs: Python does not randomize the hash of ints
    (only str/bytes/datetime), so this is safe to persist in SQLite and
    look up again in a later session.

    Note this is a *hash*, not a guarantee: two different positions could
    in principle collide to the same integer (astronomically unlikely --
    for the bulk position database, at real-world scale, that risk is
    negligible and re-verifying every lookup against the full key would
    undo the speed work that went into indexing). Anywhere a false
    positive would actually matter -- like confirming a transposition is
    truly the same position -- compare `position_key()` directly instead
    of relying on the hash alone; see `RepertoireBuilder` for that.
    """
    h = hash(position_key(board))
    # SQLite INTEGER is signed 64-bit; wrap into that range so it always fits.
    if h >= 2**63:
        h -= 2**64
    elif h < -2**63:
        h += 2**64
    return h


def format_game_count(n: int) -> str:
    """693 -> '693', 12345 -> '12.3K', 693000000 -> '693M', 1500000000 -> '1.5B'."""
    if n < 1000:
        return str(n)
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= divisor:
            value = n / divisor
            # Drop the decimal if it's a whole number (e.g. "2B" not "2.0B")
            text = f"{value:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return str(n)


def get_position_stats(conn: sqlite3.Connection, board: chess.Board) -> PositionStats | None:
    pos_hash = position_hash(board)
    pos_row = conn.execute(
        "SELECT game_count FROM positions WHERE pos_hash = ?", (pos_hash,)
    ).fetchone()
    if pos_row is None:
        return None

    rows = conn.execute(
        """
        SELECT move_uci, san, count, white_wins, draws, black_wins
        FROM moves WHERE pos_hash = ?
        ORDER BY count DESC
        """,
        (pos_hash,),
    ).fetchall()

    moves = [MoveStat(*row) for row in rows]
    return PositionStats(pos_hash=pos_hash, game_count=pos_row[0], moves=moves)


def get_stats_by_hash(conn: sqlite3.Connection, pos_hash: int) -> PositionStats | None:
    pos_row = conn.execute(
        "SELECT game_count FROM positions WHERE pos_hash = ?", (pos_hash,)
    ).fetchone()
    if pos_row is None:
        return None
    rows = conn.execute(
        """
        SELECT move_uci, san, count, white_wins, draws, black_wins
        FROM moves WHERE pos_hash = ?
        ORDER BY count DESC
        """,
        (pos_hash,),
    ).fetchall()
    moves = [MoveStat(*row) for row in rows]
    return PositionStats(pos_hash=pos_hash, game_count=pos_row[0], moves=moves)


def get_meta(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT key, value FROM meta").fetchall()
    return dict(rows)
