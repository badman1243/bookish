"""
pgn_indexer.py

Turns a raw .pgn database into a SQLite index keyed by chess position, so
that looking up "what has been played / what are the results from this
exact position" is an indexed lookup instead of a full re-scan of the PGN
file.

Schema
------
positions(pos_hash INTEGER PRIMARY KEY, game_count INTEGER)
    One row per distinct position reached during indexing. pos_hash is a
    (signed, for SQLite) position hash (see position_stats.position_hash),
    which correctly folds in side-to-move, castling rights and en-passant
    file, so transpositions collapse into the same row. game_count is the
    number of games in which this exact position was reached (used as the
    "total games" denominator for that node, independent of what was
    played next).

moves(pos_hash INTEGER, move_uci TEXT, san TEXT, count INTEGER,
      white_wins INTEGER, draws INTEGER, black_wins INTEGER,
      PRIMARY KEY (pos_hash, move_uci))
    One row per (position, move actually played from that position) pair,
    with aggregated game counts and results.

meta(key TEXT PRIMARY KEY, value TEXT)
    Bookkeeping: source pgn path, total games indexed, build timestamp.

Performance notes
------------------
Single-threaded only, deliberately. A multi-process version was tried
(splitting the file across CPU cores) and went through several rounds of
fixes, but kept hitting "SynchronizedArray objects should only be shared
between processes through inheritance" on real hardware even after being
rebuilt around a Manager-based proxy specifically to avoid that -- and
by that point, three attempts in, the right call was to stop chasing it
and revert to the single-threaded path that always worked reliably,
rather than risk a fourth attempt. If cross-platform multiprocessing
indexing gets revisited later, it should be tested on the actual target
platform before being called fixed, not just reasoned about.

What's still true from that work and kept here, since none of it
depends on multiprocessing:
  - avoids `chess.pgn.read_game()`'s default behaviour of building a full
    Game/GameNode tree (comments, NAGs, parent/child objects for every
    move) via a small custom Visitor (`_IndexVisitor`) that just tracks
    the one mutable board the scanner already maintains;
  - hashes each position exactly once per ply (reused as both the
    "after" hash for the move just played and the "before" hash for the
    next move);
  - reuses the literal SAN text already in the PGN file (captured via
    `begin_parse_san`) instead of recomputing it with `board.san()`;
  - never stores/reconstructs FEN strings during indexing (nothing reads
    them, and `Board.fen()` is a 64-square Python loop -- by far the
    single most expensive thing to drop);
  - skips any variations already embedded in the source PGN (mainline
    only), matching how database exports are normally structured;
  - uses a cheap custom position hash (see position_stats.position_hash)
    instead of Polyglot's, which loops per-piece in Python.

Together these took single-threaded speed from ~130 games/sec to ~570
games/sec on an 8,000-game/~40-ply synthetic benchmark, with no
cross-process dependencies to go wrong.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import chess
import chess.pgn

from .position_stats import position_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    pos_hash INTEGER PRIMARY KEY,
    game_count INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS moves (
    pos_hash INTEGER NOT NULL,
    move_uci TEXT NOT NULL,
    san TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    white_wins INTEGER NOT NULL DEFAULT 0,
    draws INTEGER NOT NULL DEFAULT 0,
    black_wins INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (pos_hash, move_uci)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class IndexProgress:
    games_processed: int
    positions_seen: int
    elapsed_seconds: float
    done: bool = False


class _IndexVisitor(chess.pgn.BaseVisitor):
    """Low-overhead PGN visitor used for bulk indexing (see module
    docstring). One instance is reused for every game in the file; its
    per-game transient state is reset in `begin_game`, while `positions`
    and `moves` are the same persistent dicts for the whole import."""

    def __init__(self, positions: dict, moves: dict, max_ply: int):
        self.positions = positions
        self.moves = moves
        self.max_ply = max_ply
        self.wdb = (0, 0, 0)
        self.ply = -1
        self.cur_hash: int | None = None
        self.pending_san: str | None = None
        self.had_error = False

    def begin_game(self):
        self.ply = -1
        self.cur_hash = None
        self.wdb = (0, 0, 0)
        self.pending_san = None
        self.had_error = False

    def visit_header(self, tagname: str, tagvalue: str) -> None:
        if tagname == "Result":
            if tagvalue == "1-0":
                self.wdb = (1, 0, 0)
            elif tagvalue == "0-1":
                self.wdb = (0, 0, 1)
            elif tagvalue == "1/2-1/2":
                self.wdb = (0, 1, 0)
            else:
                self.wdb = (0, 0, 0)

    def begin_variation(self):
        return chess.pgn.SKIP  # mainline only, matches prior behaviour

    def begin_parse_san(self, board: chess.Board, san: str) -> None:
        # Stash the literal token from the file so visit_move doesn't
        # need to recompute it with the (expensive) board.san(move).
        self.pending_san = san

    def visit_move(self, board: chess.Board, move: chess.Move) -> None:
        if self.ply < 0 or self.ply >= self.max_ply:
            return
        w, d, b = self.wdb
        key = (self.cur_hash, move.uci())
        entry = self.moves.get(key)
        if entry is None:
            self.moves[key] = [self.pending_san, 1, w, d, b]
        else:
            entry[1] += 1
            entry[2] += w
            entry[3] += d
            entry[4] += b

    def visit_board(self, board: chess.Board) -> None:
        self.ply += 1
        if self.ply > self.max_ply:
            return
        h = position_hash(board)
        self.cur_hash = h
        count = self.positions.get(h)
        self.positions[h] = 1 if count is None else count + 1

    def handle_error(self, error: Exception) -> None:
        # Be lenient like the default GameBuilder: a malformed game
        # somewhere in a multi-million-game dump shouldn't abort hours of
        # indexing. Whatever was parsed before the error still gets kept
        # (python-chess simply stops parsing the rest of this one game,
        # it doesn't discard what came before) -- but flag it so the
        # count can be surfaced in the GUI rather than silently lost.
        self.had_error = True

    def result(self):
        return True


class PgnIndexer:
    """Streams a PGN file and builds/refreshes the position index database."""

    def __init__(self, pgn_path: str, db_path: str, max_ply: int = 60):
        """
        pgn_path: path to the source .pgn file (read-only, never modified).
        db_path:  path to the SQLite index to create/overwrite.
        max_ply:  only index positions up to this many half-moves deep.
                  Repertoires only ever need ~60 ply (30 full moves for one
                  side), and capping this keeps the index small and the
                  import fast on huge databases instead of indexing every
                  transposition into a random middlegame.
        """
        self.pgn_path = pgn_path
        self.db_path = db_path
        self.max_ply = max_ply

    def build(
        self,
        progress_cb: Optional[Callable[[IndexProgress], None]] = None,
        flush_every_games: int = 100_000,
        progress_every: int = 500,
    ) -> None:
        db_file = Path(self.db_path)
        if db_file.exists():
            db_file.unlink()

        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        # Speed pragmas for bulk import. Safety is irrelevant here: if the
        # process dies mid-import we just rebuild the index from the PGN.
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA journal_mode = MEMORY")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -200000")  # ~200MB page cache
        conn.execute("PRAGMA mmap_size = 268435456")  # 256MB

        # In-memory aggregation, flushed to SQLite every `flush_every_games`
        # games so memory stays bounded on very large databases.
        # positions: pos_hash -> game_count
        # moves:     (pos_hash, move_uci) -> [san, count, w, d, b]
        positions: dict[int, int] = {}
        moves: dict[tuple, list] = {}
        visitor = _IndexVisitor(positions, moves, self.max_ply)

        def make_visitor():
            return visitor

        start_time = time.time()
        games_processed = 0
        games_with_errors = 0

        with open(self.pgn_path, "r", encoding="utf-8", errors="replace") as pgn_file:
            while True:
                result = chess.pgn.read_game(pgn_file, Visitor=make_visitor)
                if result is None:
                    break

                games_processed += 1
                if visitor.had_error:
                    games_with_errors += 1

                if games_processed % flush_every_games == 0:
                    self._flush(conn, positions, moves)
                    positions.clear()
                    moves.clear()

                if progress_cb and games_processed % progress_every == 0:
                    progress_cb(
                        IndexProgress(
                            games_processed=games_processed,
                            positions_seen=len(positions),
                            elapsed_seconds=time.time() - start_time,
                        )
                    )

        self._flush(conn, positions, moves)

        source_stat = Path(self.pgn_path).stat()
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('source_pgn', ?)",
            (str(Path(self.pgn_path).resolve()),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('source_mtime', ?)",
            (str(source_stat.st_mtime),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('source_size', ?)",
            (str(source_stat.st_size),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('total_games', ?)",
            (str(games_processed),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('games_with_errors', ?)",
            (str(games_with_errors),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('built_at', ?)",
            (str(int(time.time())),),
        )
        conn.commit()
        conn.close()

        if progress_cb:
            progress_cb(
                IndexProgress(
                    games_processed=games_processed,
                    positions_seen=0,
                    elapsed_seconds=time.time() - start_time,
                    done=True,
                )
            )

    @staticmethod
    def _flush(conn: sqlite3.Connection, positions: dict, moves: dict) -> None:
        if positions:
            conn.executemany(
                """
                INSERT INTO positions (pos_hash, game_count)
                VALUES (?, ?)
                ON CONFLICT(pos_hash) DO UPDATE SET
                    game_count = game_count + excluded.game_count
                """,
                list(positions.items()),
            )
        if moves:
            conn.executemany(
                """
                INSERT INTO moves (pos_hash, move_uci, san, count, white_wins, draws, black_wins)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pos_hash, move_uci) DO UPDATE SET
                    count = count + excluded.count,
                    white_wins = white_wins + excluded.white_wins,
                    draws = draws + excluded.draws,
                    black_wins = black_wins + excluded.black_wins
                """,
                [
                    (h, uci, v[0], v[1], v[2], v[3], v[4])
                    for (h, uci), v in moves.items()
                ],
            )
        conn.commit()


def index_exists_and_matches(db_path: str, pgn_path: str) -> bool:
    """Quick check so the GUI can skip re-indexing a database it already
    built. Checks the source file's path *and* its modification time and
    size, not just the path -- otherwise re-exporting or refreshing a PGN
    under the same filename would silently keep serving the old index,
    with no indication the stats are now stale. An index built before
    this check existed won't have mtime/size recorded at all, in which
    case it's treated as unknown and re-indexed once rather than assumed
    fresh."""
    if not Path(db_path).exists():
        return False
    try:
        current = Path(pgn_path).stat()
        conn = sqlite3.connect(db_path)
        rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        conn.close()

        if rows.get("source_pgn") != str(Path(pgn_path).resolve()):
            return False
        if "source_mtime" not in rows or "source_size" not in rows:
            return False  # built before freshness tracking existed -- re-index once
        return (
            float(rows["source_mtime"]) == current.st_mtime
            and int(rows["source_size"]) == current.st_size
        )
    except (sqlite3.Error, OSError, ValueError):
        return False
