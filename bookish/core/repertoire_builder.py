"""
repertoire_builder.py

Builds a repertoire tree from a position index (see pgn_indexer.py /
position_stats.py) according to these rules:

At a node where it's the REPERTOIRE OWNER's turn to move:
    1. Take the top `user_move_range` moves by popularity as the core
       candidate pool, and drop any below `user_game_amount` games.
    2. Champion-scan: start from whichever core candidate has the most
       games (the "forcing"/most-tested move), then only switch to a
       different core candidate when it's *confidently* better -- a
       proper two-proportion statistical comparison (see
       position_stats.is_confidently_better), not just a higher point
       estimate. This is what lets a heavily-tested move win over a
       thinner-sampled one with a merely-higher raw score, while still
       giving way to a genuinely, statistically-supported edge.
    3. Breakthrough: a bounded extension beyond the core pool (the next
       `user_move_range` moves by popularity) gets the same chance to
       confidently beat the champion -- so a good but less-popular move
       just outside the core pool isn't invisible to the algorithm, while
       User Move Range's own meaning (the guaranteed pool size) doesn't
       change.
    4. If nothing in the core pool clears the game-amount floor, the line
       dead-ends here.

At a node where it's the OPPONENT's turn to move (i.e. branching):
    1. Drop any move with fewer than `opponent_game_amount` games, or
       that wouldn't leave a valid move for the owner to answer with
       (see `_has_viable_continuation` -- this is what guarantees every
       line ends on the owner's move, never a dangling opponent reply).
    2. Drop any move that would push the running "how common is this
       exact line" probability below 1 / `population_threshold` (if set)
       -- see below. Confidence-adjusted (Wilson lower bound of the
       move's own share), same reasoning as the owner's side: a reply
       sitting at 6% from 15 games isn't confidently "6%".
    3. If we've already branched `full_branching_limit` times earlier in
       this same line, stop branching altogether and just take their
       single most popular (viable, population-threshold-passing) reply
       -- real opening prep doesn't stay five-wide forever; it narrows to
       "the main line" a few moves in. Past this limit every opponent
       turn becomes a single choice, same as the owner's. This is no
       longer exposed as its own GUI control -- `population_threshold`
       does the same job more smoothly now, so this is just a generous
       backstop (default 8) for when population_threshold is disabled.
    4. Otherwise: independently include every remaining move whose
       confidence-adjusted share of the node's total games is >=
       `ignore_threshold_pct`.
    5. If the included moves don't yet add up to `requirement_pct` of the
       node's total games (using the same confidence-adjusted shares),
       keep adding the next most popular remaining moves (even if
       individually below the ignore threshold) until the cumulative
       share reaches the requirement, or moves run out.
    6. Finally, cap the branch count to the top `opponent_move_range`
       moves (by popularity) from what steps 4-5 produced. NOTE: if the
       move range is smaller than what's needed to satisfy the
       requirement, the range wins and coverage will fall short of the
       requirement -- this is intentional (the range is a hard ceiling on
       branching factor) and is reported back in the build summary.

Confidence throughout ("how skeptical to be of thin samples") is governed
by a single setting, `confidence_z` -- the Wilson z-score, applied
identically on both the owner's side (champion-scan/breakthrough) and the
opponent's side (Ignore Threshold/Rarity Cutoff/Requirement). Higher is
more conservative (demands more data before trusting a percentage);
1.96 (the default) is the standard ~95%-confidence choice.

Population threshold ("prepare down to 1 in X games"): tracks, for every
node, the probability that a real opponent's natural play would lead you
to exactly this position, *assuming you always play your prepared
script*. This never changes across the owner's own moves (there's no
chance involved -- the repertoire always specifies exactly what to play)
and multiplies by a reply's local (confidence-adjusted) share of games
every time the opponent branches. Once continuing into some reply would
push this below 1 / population_threshold, that reply isn't included at
all: the position has become too rare to be worth deeper prep, so the
line stops right there on the owner's last move. This tapers branching
*organically* -- true mainline theory (which stays common at every step)
rides however deep `depth` allows, while offbeat tries get cut short
automatically, without having to hand-tune ignore/requirement/narrowing
at every level to get that same shape. `depth` remains a hard ceiling
underneath it regardless (a genuinely mainline system could otherwise
run very deep).

Every line stops once the owner has made `depth` moves in that line --
stopping *immediately* on that move, without inviting the opponent's
reply to it -- or once it reaches a position already reached via a
different move order earlier in the same tree, in which case it's marked
as a transposition (see `RepNode.transposition_path`) and not expanded
again. Either way, and combined with the opponent-viability check above,
every single line in the export ends on the repertoire owner's own move.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field

import chess
import chess.pgn

from .position_stats import (
    PositionStats, get_stats_by_hash, MoveStat, position_hash, position_key,
    is_confidently_better,
)
from .presets import Preset, DEFAULT_PRESETS, load_presets, PRESETS_PATH, write_default_file
from . import eco


SCOUTING_MODES = {
    # name -> draw_weight, fed straight into MoveStat.weighted_points_pct
    "Neutral": 0.5,   # standard chess scoring: win=1, draw=0.5, loss=0
    "Aggressive": 0.0,  # draws count for nothing -- only decisive results rewarded
    "Solid": 1.0,     # not losing counts fully as good as winning
}


@dataclass
class RepertoireConfig:
    owner_color: chess.Color  # chess.WHITE or chess.BLACK
    depth: int  # hard ceiling: number of the owner's own moves before every line stops
    ignore_threshold_pct: float = 5.0
    requirement_pct: float = 50.0
    user_move_range: int = 3
    opponent_move_range: int = 3
    user_game_amount: int = 0
    opponent_game_amount: int = 0
    draw_weight: float = 0.5  # see SCOUTING_MODES
    full_branching_limit: int = 8  # opponent decisions before narrowing to their single most popular reply (a backstop -- see module docstring; Rarity Cutoff is the primary tapering mechanism now)
    population_threshold: int | None = None  # "X" in "1 in X games"; None disables this check entirely
    confidence_z: float = 1.96  # Wilson z-score; higher = more skeptical of thin samples on both sides of the tree


@dataclass
class RepNode:
    fen_before: str
    move_uci: str | None  # None only for the synthetic root
    san: str | None
    to_move_before: chess.Color  # who was to move at fen_before
    stat: MoveStat | None  # popularity/result stats for this move, if any
    node_total_games: int  # denominator used for this move's percentage
    children: list["RepNode"] = field(default_factory=list)
    dead_end_reason: str | None = None  # set on leaves that stopped early
    transposition_path: list[str] | None = None  # set if this position was
    # already reached earlier in the tree via a different move order; the
    # list is the earlier path (SAN moves relative to the repertoire root)


@dataclass
class BuildSummary:
    total_lines: int = 0
    total_nodes: int = 0
    dead_ends: int = 0  # lines that stopped before reaching full depth (no data / no qualifying move)
    transpositions_found: int = 0  # lines that stopped early because they transpose elsewhere in the tree
    max_depth_reached: int = 0
    shortfall_branches: int = 0  # opponent nodes where move_range cut off coverage
    narrowed_branches: int = 0  # opponent nodes where the branching limit collapsed to a single reply
    population_pruned: int = 0  # opponent replies excluded for being rarer than 1/population_threshold
    breakthroughs: int = 0  # owner picks that came from outside the popularity pool, confidently beating it


class RepertoireBuilder:
    def __init__(self, conn: sqlite3.Connection, config: RepertoireConfig):
        self.conn = conn
        self.cfg = config
        self.summary = BuildSummary()
        self._seen_positions: dict[int, list[tuple[tuple, list[str]]]] = {}

    def build(self, root_board: chess.Board) -> RepNode:
        root = RepNode(
            fen_before=root_board.fen(),
            move_uci=None,
            san=None,
            to_move_before=root_board.turn,
            stat=None,
            node_total_games=0,
        )
        self._seen_positions = {position_hash(root_board): [(position_key(root_board), [])]}
        self._expand(
            root_board.copy(), root, owner_moves_played=0, path=[],
            opponent_branches_used=0, cumulative_pct=100.0,
        )
        return root

    # -- internals ---------------------------------------------------

    def _expand(
        self, board: chess.Board, parent: RepNode, owner_moves_played: int,
        path: list[str], opponent_branches_used: int, cumulative_pct: float,
    ) -> None:
        self.summary.total_nodes += 1
        self.summary.max_depth_reached = max(self.summary.max_depth_reached, owner_moves_played)

        stats = get_stats_by_hash(self.conn, position_hash(board))
        if stats is None or not stats.moves:
            parent.dead_end_reason = "no games in database from this position"
            self._count_leaf(owner_moves_played)
            return

        is_owner_turn = board.turn == self.cfg.owner_color

        if is_owner_turn:
            if owner_moves_played >= self.cfg.depth:
                parent.dead_end_reason = "depth limit reached"
                self._count_leaf(owner_moves_played)
                return

            chosen, broke_through = self._pick_owner_move_and_source(stats)
            if chosen is None:
                parent.dead_end_reason = (
                    f"no move meets the {self.cfg.user_game_amount}-game minimum "
                    f"within the top {self.cfg.user_move_range} moves"
                )
                self._count_leaf(owner_moves_played)
                return
            if broke_through:
                self.summary.breakthroughs += 1

            next_owner_moves_played = owner_moves_played + 1
            reached_depth = next_owner_moves_played >= self.cfg.depth
            self._add_child_and_recurse(
                board, parent, chosen, stats.game_count, next_owner_moves_played,
                path, opponent_branches_used, stop_here=reached_depth, is_owner_move=True,
                cumulative_pct=cumulative_pct,
            )

        else:
            force_single = opponent_branches_used >= self.cfg.full_branching_limit
            chosen_moves, shortfall, pruned = self._pick_opponent_moves(board, stats, force_single, cumulative_pct)
            self.summary.population_pruned += pruned
            if not chosen_moves:
                parent.dead_end_reason = (
                    "no opponent move meets the game-amount floor and leaves a viable move for you"
                )
                self._count_leaf(owner_moves_played)
                return
            if shortfall:
                self.summary.shortfall_branches += 1
            if force_single:
                self.summary.narrowed_branches += 1

            denom = stats.game_count or sum(m.count for m in stats.moves)
            for mv in chosen_moves:
                child_cumulative_pct = cumulative_pct * (mv.pct_of(denom) / 100.0)
                self._add_child_and_recurse(
                    board, parent, mv, stats.game_count, owner_moves_played,
                    path, opponent_branches_used + 1, stop_here=False, is_owner_move=False,
                    cumulative_pct=child_cumulative_pct,
                )

    def _add_child_and_recurse(
        self,
        board: chess.Board,
        parent: RepNode,
        mv: MoveStat,
        node_total_games: int,
        next_owner_moves_played: int,
        path: list[str],
        opponent_branches_used: int,
        stop_here: bool,
        is_owner_move: bool,
        cumulative_pct: float,
    ) -> None:
        child = self._make_child(board, mv, node_total_games)
        parent.children.append(child)

        board.push(chess.Move.from_uci(mv.move_uci))
        child_path = path + [mv.san]
        child_hash = position_hash(board)
        child_key = position_key(board)

        # Bucket by hash for speed, but only ever confirm a transposition
        # on an exact match of the full position key -- a hash collision
        # alone is not proof it's the same position (astronomically
        # unlikely here given how few nodes a single repertoire tree has,
        # but "unlikely" isn't "guaranteed", and a wrongly-flagged
        # transposition can't be double-checked against anything).
        bucket = self._seen_positions.setdefault(child_hash, [])
        earlier_path = None
        for stored_key, stored_path in bucket:
            if stored_key == child_key:
                earlier_path = stored_path
                break

        # A transposition can only be *detected* here -- it can only ever
        # *end the line* when this move is ours. Chess turns strictly
        # alternate, so a converging position with the opponent to move
        # was necessarily just reached by OUR move (and vice versa); if
        # we stopped on an opponent-move match, every transposition would
        # dangle on their move, which is exactly what's not allowed. So
        # on an opponent-move match we register it but keep recursing --
        # one forced ply later, our own (deterministically identical)
        # reply hits the same match and ends the line there instead.
        if is_owner_move and earlier_path is not None and earlier_path != child_path:
            child.transposition_path = earlier_path
            self.summary.transpositions_found += 1
            self.summary.total_nodes += 1
            self.summary.total_lines += 1
        elif stop_here:
            # Depth fully used up on exactly this move -- a complete,
            # valid line ending on the owner's own move. Don't recurse
            # into the opponent's reply to it; every line ends on OUR
            # move, never theirs.
            if earlier_path is None:
                bucket.append((child_key, child_path))
            self.summary.total_nodes += 1
            self.summary.max_depth_reached = max(self.summary.max_depth_reached, next_owner_moves_played)
            self._count_leaf(next_owner_moves_played)
        else:
            if earlier_path is None:
                bucket.append((child_key, child_path))
            self._expand(board, child, next_owner_moves_played, child_path, opponent_branches_used, cumulative_pct)

        board.pop()

    def _pick_owner_move(self, stats: PositionStats) -> MoveStat | None:
        return self._pick_owner_move_and_source(stats)[0]

    def _pick_owner_move_and_source(self, stats: PositionStats) -> tuple[MoveStat | None, bool]:
        """Returns (chosen move, whether it came from outside the
        popularity-based pool via a "breakthrough").

        Champion-scan: start from whichever candidate in the popularity
        pool has the most games (matching "prioritize forcing/well-tested
        moves" as the default), then only switch to a different candidate
        when it's confidently -- not just numerically -- better, using a
        proper two-proportion comparison (see
        position_stats.is_confidently_better). Processed in descending
        game-count order so we're always comparing against whichever pick
        currently has the most evidence behind it.

        Breakthrough: a bounded extension beyond the core pool (the next
        `user_move_range` moves by popularity) gets the same chance --
        but only to confidently beat the champion, never merely to
        outscore it. User Move Range's own size as "the guaranteed pool"
        doesn't change; this is strictly a safety net for a genuinely
        standout alternative just outside it.
        """
        color, draw_weight, z = self.cfg.owner_color, self.cfg.draw_weight, self.cfg.confidence_z
        n = self.cfg.user_move_range

        core_pool = [m for m in stats.moves[:n] if m.count >= self.cfg.user_game_amount]
        if not core_pool:
            return None, False

        ranked = sorted(core_pool, key=lambda m: m.count, reverse=True)
        champion = ranked[0]
        for m in ranked[1:]:
            if is_confidently_better(m, champion, color, draw_weight, z):
                champion = m

        extension = [m for m in stats.moves[n : 2 * n] if m.count >= self.cfg.user_game_amount]
        broke_through = False
        for m in sorted(extension, key=lambda m: m.count, reverse=True):
            if is_confidently_better(m, champion, color, draw_weight, z):
                champion = m
                broke_through = True

        return champion, broke_through

    def _has_viable_continuation(self, board: chess.Board, mv: MoveStat) -> bool:
        """Would playing this opponent reply leave a valid move for the
        owner to answer with? An opponent reply that leads nowhere for us
        (no data past it, or nothing clears the game-amount floor) is
        excluded from consideration entirely, rather than included as a
        branch that dead-ends on their move -- this is what guarantees
        every line in the export ends on the owner's own move."""
        board.push(chess.Move.from_uci(mv.move_uci))
        try:
            stats = get_stats_by_hash(self.conn, position_hash(board))
            if stats is None or not stats.moves:
                return False
            return self._pick_owner_move(stats) is not None
        finally:
            board.pop()

    def _pick_opponent_moves(
        self, board: chess.Board, stats: PositionStats, force_single: bool, cumulative_pct: float,
    ) -> tuple[list[MoveStat], bool, int]:
        z = self.cfg.confidence_z
        denom = stats.game_count or sum(m.count for m in stats.moves)

        candidates = [
            m for m in stats.moves
            if m.count >= self.cfg.opponent_game_amount and self._has_viable_continuation(board, m)
        ]

        # Confidence-adjusted throughout below, not raw counts/denom -- a
        # reply sitting at 6% from 15 games isn't confidently "6%" any
        # more than a candidate move at 83% from 12 games is confidently
        # "83%" (see MoveStat.confidence_adjusted_pct_of). Same reasoning
        # applied to the opponent's side that already governs the
        # owner's own move, instead of only protecting one side of the
        # tree with real statistics and the other with just a flat
        # game-count floor.
        pruned = 0
        if self.cfg.population_threshold:
            min_child_pct = 100.0 / self.cfg.population_threshold
            pool = []
            for m in candidates:
                if cumulative_pct * (m.confidence_adjusted_pct_of(denom, z) / 100.0) >= min_child_pct:
                    pool.append(m)
                else:
                    pruned += 1
        else:
            pool = candidates

        if not pool:
            return [], False, pruned

        if force_single:
            # Branching limit already used up in this line: stop widening
            # and just follow their single most popular (viable) try from
            # here on, same as the owner's own moves.
            return [pool[0]], False, pruned

        included: list[MoveStat] = []
        remaining: list[MoveStat] = []
        for m in pool:
            if m.confidence_adjusted_pct_of(denom, z) >= self.cfg.ignore_threshold_pct:
                included.append(m)
            else:
                remaining.append(m)

        cumulative = sum(m.confidence_adjusted_pct_of(denom, z) for m in included)
        i = 0
        while cumulative < self.cfg.requirement_pct and i < len(remaining):
            m = remaining[i]
            included.append(m)
            cumulative += m.confidence_adjusted_pct_of(denom, z)
            i += 1

        # included may not be in popularity order anymore (ignore-pass
        # order followed by top-up order); re-sort before applying the cap.
        included.sort(key=lambda m: m.count, reverse=True)

        shortfall = False
        if len(included) > self.cfg.opponent_move_range:
            included = included[: self.cfg.opponent_move_range]
            if sum(m.confidence_adjusted_pct_of(denom, z) for m in included) < self.cfg.requirement_pct:
                shortfall = True

        return included, shortfall, pruned

    def _make_child(self, board: chess.Board, mv: MoveStat, node_total_games: int) -> RepNode:
        new_board = board.copy()
        new_board.push(chess.Move.from_uci(mv.move_uci))
        return RepNode(
            fen_before=board.fen(),
            move_uci=mv.move_uci,
            san=mv.san,
            to_move_before=board.turn,
            stat=mv,
            node_total_games=node_total_games,
        )

    def _count_leaf(self, owner_moves_played: int) -> None:
        self.summary.total_lines += 1
        if owner_moves_played < self.cfg.depth:
            self.summary.dead_ends += 1


def compute_coverage_pct(root: RepNode, owner_color: chess.Color) -> float:
    """What fraction of real games reaching the root position stay inside
    this repertoire tree all the way to one of its leaves.

    Only opponent branch points can leak coverage: at an opponent's turn,
    any of their historical replies that the tree *didn't* include is a
    gap (weighted by how often that reply is actually played). The
    owner's own moves never reduce coverage -- the repertoire always
    specifies exactly what to play there, so there's nothing to "miss".
    Dead ends (depth limit, no data, transposition) simply stop
    accumulating further loss; whatever fraction of games made it that
    far still counts as covered up to that point.
    """
    total = 0.0

    def walk(node: RepNode, weight: float) -> None:
        nonlocal total
        if not node.children:
            total += weight
            return
        # A node's own outgoing turn isn't node.to_move_before -- that
        # field records who moved *into* this node (root is the one
        # exception, where it legitimately means "who moves from here").
        # Every child was necessarily played by whoever's turn it was at
        # this node, so children[0].to_move_before is the reliable way to
        # get it for any node, root or not.
        is_owner_node = node.children[0].to_move_before == owner_color
        for child in node.children:
            if is_owner_node or not child.stat or not child.node_total_games:
                branch_weight = weight
            else:
                branch_weight = weight * (child.stat.count / child.node_total_games)
            walk(child, branch_weight)

    walk(root, 1.0)
    return total * 100.0


def _format_variation(start_board: chess.Board, sans: list[str]) -> str:
    """Renders a list of SAN moves (relative to start_board) as a properly
    numbered string, continuing move numbers from start_board's own
    fullmove_number/turn (so a path starting mid-game numbers correctly)."""
    board = start_board.copy()
    parts = []
    move_no = board.fullmove_number
    to_move = board.turn
    for i, san in enumerate(sans):
        if to_move == chess.WHITE:
            parts.append(f"{move_no}.{san}")
        else:
            parts.append(f"{move_no}...{san}" if i == 0 else san)
            move_no += 1
        to_move = not to_move
    return " ".join(parts)


def _lead_in_or_tree_moves(root: RepNode, root_board: chess.Board, max_plies: int | None) -> tuple[chess.Board, list[chess.Move]]:
    """Shared helper: the moves that got to root_board if there were any
    (that's the opening this repertoire is *for*), else the built tree's
    own first moves (always taking the first/most relevant branch at each
    step) so a from-scratch build still has something to classify/name.
    Returns (the board those moves start from, the moves themselves)."""
    base_board = root_board.copy(stack=True)
    lead_in = list(root_board.move_stack)
    for _ in lead_in:
        base_board.pop()

    if lead_in:
        return base_board, lead_in[:max_plies] if max_plies is not None else lead_in

    moves: list[chess.Move] = []
    board = root_board.copy()
    node = root
    limit = max_plies if max_plies is not None else 20
    while node.children and len(moves) < limit:
        child = node.children[0]
        moves.append(chess.Move.from_uci(child.move_uci))
        node = child
    return root_board, moves


def classify_repertoire(root: RepNode, root_board: chess.Board) -> "eco.EcoEntry | None":
    """ECO classification for this repertoire, e.g. "C60 Ruy Lopez" --
    based on the full lead-in if there was one (not capped short; more
    context only ever makes classification more specific, never less
    accurate), or a generously-capped 20 plies of the tree's own first
    moves for a from-scratch build."""
    start_board, moves = _lead_in_or_tree_moves(root, root_board, max_plies=None)
    return eco.classify_game(moves, start_board)


def describe_for_filename(root: RepNode, root_board: chess.Board, max_plies: int = 6, max_chars: int = 40) -> str:
    """A short, filename-safe label for what this repertoire covers.
    Prefers the real ECO name when one is found -- e.g. "C60_Ruy_Lopez"
    -- since that's far more recognizable than a move list; falls back
    to the first few plies as raw moves (e.g. "e4_e5_Nf3_Nc6_Bb5",
    capped short so it stays a quick fingerprint rather than an unwieldy
    wall of text) only when nothing in the ECO database matches. Returns
    "" if there's nothing to describe at all (the caller falls back to
    just the survey number in that case)."""
    entry = classify_repertoire(root, root_board)
    if entry is not None:
        ascii_name = unicodedata.normalize("NFKD", entry.name).encode("ascii", "ignore").decode("ascii")
        name_slug = re.sub(r"[^A-Za-z0-9]+", "_", ascii_name).strip("_")
        slug = f"{entry.eco}_{name_slug}"
        return slug[:max_chars].rstrip("_")

    start_board, moves = _lead_in_or_tree_moves(root, root_board, max_plies=max_plies)
    if not moves:
        return ""
    sans = []
    b = start_board.copy()
    for mv in moves:
        sans.append(b.san(mv))
        b.push(mv)

    slug = re.sub(r"[^A-Za-z0-9_+#=-]", "_", "_".join(sans))
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug[:max_chars].rstrip("_")


def to_pgn_game(root: RepNode, root_board: chess.Board, headers: dict | None = None) -> chess.pgn.Game:
    """Converts a RepNode tree into a single chess.pgn.Game with proper PGN
    variation syntax for every branch. Replays whatever real moves were
    made to reach `root_board` (its move_stack) from their true base
    position first, so the export shows the actual move order that got
    there instead of jumping straight into a FEN -- then attaches the
    repertoire's own branches from that point on. Only transposition
    nodes get a comment; nothing else is annotated."""
    base_board = root_board.copy(stack=True)
    lead_in = list(root_board.move_stack)
    for _ in lead_in:
        base_board.pop()

    game = chess.pgn.Game()
    game.setup(base_board)
    for k, v in (headers or {}).items():
        game.headers[k] = v

    node: chess.pgn.GameNode = game
    for mv in lead_in:
        node = node.add_variation(mv)

    def recurse(rep_node: RepNode, pgn_node: chess.pgn.GameNode) -> None:
        for child in rep_node.children:
            move = chess.Move.from_uci(child.move_uci)
            comment = ""
            if child.transposition_path is not None:
                comment = f"= {_format_variation(root_board, child.transposition_path)}"
            child_pgn_node = pgn_node.add_variation(move, comment=comment)
            recurse(child, child_pgn_node)

    recurse(root, node)
    return game
