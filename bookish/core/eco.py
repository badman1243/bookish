"""
eco.py

Classifies a sequence of moves against the standard Encyclopaedia of
Chess Openings (ECO) codes and names -- e.g. "C60 Ruy Lopez".

The data (core/eco_openings.tsv) is sourced from lichess-org's own
chess-openings dataset (https://github.com/lichess-org/chess-openings),
the same one Lichess's own site uses for this -- not a third-party
package of unknown provenance. Preprocessed once (see the module
docstring below for how) from the raw eco/name/pgn TSV files into a
position-keyed lookup: each row's PGN move list is replayed with
python-chess into the resulting board's EPD (piece placement, side to
move, castling rights, en passant square -- everything that defines "is
this the same position", deliberately excluding move counters), so
lookups are by *position*, not literal move-order text. This is what
lets it recognize an opening reached by transposition -- a different
move order into the same position -- rather than only exact, literal
move sequences.

Regenerating the bundled data (only needed if lichess-org's own dataset
is updated and you want the refresh):

    import csv, chess
    seen = {}
    for vol in "abcde":
        with open(f"{vol}.tsv", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f, delimiter="\t"):
                board = chess.Board()
                tokens = [t for t in row["pgn"].replace(".", ". ").split()
                          if not t.rstrip(".").isdigit()]
                ply = 0
                for tok in tokens:
                    board.push_san(tok)
                    ply += 1
                epd = board.epd()
                existing = seen.get(epd)
                if existing is None or existing[2] < ply:
                    seen[epd] = (row["eco"], row["name"], ply)
    # then write epd/eco/name/ply as tab-separated rows to eco_openings.tsv
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import chess

ECO_DATA_PATH = Path(__file__).resolve().parent / "eco_openings.tsv"
_ECO_CODE_RE = re.compile(r"^[A-Ea-e]\d{2}$")


@dataclass(frozen=True)
class EcoEntry:
    eco: str
    name: str
    ply: int


_lookup: dict[str, EcoEntry] | None = None


def _load() -> dict[str, EcoEntry]:
    global _lookup
    if _lookup is not None:
        return _lookup
    table: dict[str, EcoEntry] = {}
    try:
        with open(ECO_DATA_PATH, "r", encoding="utf-8") as f:
            next(f)  # header row
            for line in f:
                epd, eco, name, ply = line.rstrip("\n").split("\t")
                table[epd] = EcoEntry(eco=eco, name=name, ply=int(ply))
    except OSError:
        table = {}
    _lookup = table
    return table


def classify_board(board: chess.Board) -> EcoEntry | None:
    """Looks up a single position directly. Most callers want
    `classify_game` instead, which finds the deepest (most specific)
    match along a whole move sequence."""
    return _load().get(board.epd())


def classify_game(moves: list[chess.Move], start_board: chess.Board | None = None) -> EcoEntry | None:
    """Walks the given moves from `start_board` (the starting position if
    not given), checking every position reached along the way, and
    returns the *deepest* (most specific/longest) ECO match found -- e.g.
    for the Ruy Lopez, matching after 3.Bb5 rather than settling for the
    more generic "Open Game" match after 1.e4 e5. Returns None if no
    position along the way matches anything in the database (e.g. a very
    offbeat try with no recognized name)."""
    board = start_board.copy() if start_board is not None else chess.Board()
    table = _load()
    best: EcoEntry | None = table.get(board.epd())
    for mv in moves:
        board.push(mv)
        entry = table.get(board.epd())
        if entry is not None:
            best = entry
    return best


def find_position_by_query(query: str) -> str | None:
    """The reverse of classify_game -- given an ECO code ("C60") or an
    opening name/substring ("Ruy Lopez", "najdorf"), returns the EPD of
    the most general (shortest) matching position, or None if nothing
    matches. A code is tried first (it's a precise, structured
    identifier, so an exact match should win outright); a bare/partial
    name falls back to a case-insensitive substring match against every
    entry's name. Picking the *shortest* match among ties is what lands
    you on the main tabiya for a code/name that covers many
    sub-variations (e.g. "C60" or "Ruy Lopez" alone) rather than some
    arbitrary deep line that happens to share the same code or contain
    the same words."""
    query = query.strip()
    if not query:
        return None

    table = _load()
    if _ECO_CODE_RE.match(query):
        code = query.upper()
        matches = [(epd, entry) for epd, entry in table.items() if entry.eco == code]
        if matches:
            matches.sort(key=lambda pair: pair[1].ply)
            return matches[0][0]
        return None

    needle = query.lower()
    matches = [(epd, entry) for epd, entry in table.items() if needle in entry.name.lower()]
    if not matches:
        return None
    matches.sort(key=lambda pair: pair[1].ply)
    return matches[0][0]
