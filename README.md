# Bookish

A personal desktop tool that reads a `.pgn` database, shows opening
statistics for any position, automatically builds a repertoire tree
for one color, and lets you check any position against a real UCI
engine -- all in one app.

## Setup

```bash
pip install -r requirements.txt
python main.py
```

Tested with Python 3.10+ on Linux; PySide6 and python-chess are both
cross-platform (Windows/macOS/Linux).

## Using it

1. **Set a position.** Click or drag pieces to make moves, or start
   from the initial position. The search box next to the board accepts
   a FEN, an ECO code ("C60"), or an opening name/substring ("Ruy
   Lopez", "najdorf") -- press "Jump" (or Enter) and it lands on the
   matching position. Clicking into an empty box offers to pre-fill it
   from your clipboard if it looks like a FEN (never auto-loads).
   Undo/Redo, Flip Board, New Game, and Dark mode are all there too.
2. **Load a database.** Database tab -> "Load PGN Database…" and pick a
   `.pgn` file. The first time, it's indexed into a small SQLite file
   next to it -- a one-time pass so every later lookup is instant.
   Reopening the same, unchanged file reuses that index; a changed file
   (checked by modification time and size, not just name) triggers a
   fresh index automatically.
3. **Read the stats.** As you move pieces, the Database tab shows every
   move played from the current position: its game count and a
   white/draw/black bar. Rows backed by very few games get a small
   warning icon. The bottom "Σ" row is the position's overall record.
4. **Build a repertoire.** Repertoire Builder tab. Pick a **Color** and
   a **Preset** (Casual through Master -- see the table below) and
   click **Build Repertoire**; that alone is enough for a sensible
   result. Everything else lives under **Advanced Settings**. The
   summary reports how many lines came out and an estimated coverage
   percentage -- roughly, "of the real games reaching this position,
   how much of it does this repertoire have prepared for." **Save
   Repertoire** writes straight to `Surveys/Survey_0_C60_Ruy_Lopez.pgn`
   (auto-numbered, named from the real ECO opening when one's found).
5. **Check it with an engine.** Engine tab -> "Add Engine…" and pick a
   downloaded UCI engine executable, then "Open Analysis Panel" for a
   live, resizable evaluation window. See **Engine analysis** below.

### Repertoire Builder settings

Only three controls show by default:

- **Color** -- which side the repertoire is for.
- **Preset** -- sets every other value below as a starting point.
  Defined in `presets.yaml`; edit it directly and click the reload
  button, or relaunch.
- **Scouting Mode** -- how a draw counts when picking *your* moves:
  Neutral (standard scoring), Aggressive (draws worth nothing, rewards
  sharp play), or Solid (a draw counts as fully as a win).

Advanced Settings (collapsed by default) adds:

- **Depth** -- a hard ceiling on how many of your own moves a line can
  contain.
- **Rarity Cutoff** ("1 in X games") -- stops a branch once it's become
  rarer than this, instead of always running to a fixed Depth. See
  below for why both exist.
- **Ignore threshold / Coverage requirement** -- which of the
  opponent's replies at each position get included by default, topped
  up if needed to hit the coverage requirement.
- **User/Opponent Move Range** -- how many candidate moves are
  considered for you, and how many branches the tree can split into for
  the opponent.
- **User/Opponent Minimum Games** -- a move needs at least this many
  games to be eligible at all.
- **Confidence Level** -- how skeptical the algorithm is of thin
  samples, on both sides of the tree (see below).

## Rarity Cutoff and Depth

Rarity Cutoff tracks, at every node, how likely a real opponent's
natural play would be to reach that exact position -- and stops
preparing a line once that gets too rare to be worth it. This tapers
branching naturally: true mainline theory (which stays common) still
runs however deep Depth allows, while offbeat tries get cut short
automatically.

Both exist because Rarity Cutoff can only prune something that actually
varies. A fully forced sequence played in 100% of games never triggers
it, no matter how strict, since there's nothing rare about it to catch
-- Depth is what stops a line like that. They answer different
questions: Depth is "how many of my own moves am I willing to prepare,
no matter what"; Rarity Cutoff is "how rare does a reply need to get
before it's not worth answering."

| Preset | Depth | Rarity Cutoff | Ignore % | Requirement % | Move Range (You/Opp) | Min. Games (You/Opp) |
|---|---|---|---|---|---|---|
| Casual | 5 | 1 in 50 | 10% | 70% | 1 / 3 | 300 / 300 |
| Club | 10 | 1 in 100 | 10% | 80% | 2 / 3 | 250 / 250 |
| Tournament | 15 | 1 in 200 | 5% | 90% | 3 / 5 | 200 / 200 |
| Ambitious | 20 | 1 in 400 | 5% | 90% | 5 / 5 | 150 / 150 |
| Master | 25 | 1 in 800 | 5% | 90% | 5 / 5 | 100 / 100 |

## Scoring: points, confidence, and Scouting Modes

Picking your move at each of your own turns isn't just raw win rate --
that ignores draws entirely. Instead:

1. **Weighted points**: a win counts as 1, a loss as 0, a draw as
   whatever the Scouting Mode says (0, 0.5, or 1 -- see above).
2. **Confidence adjustment**: that score is pulled toward 50% in
   proportion to how little data backs it up (a Wilson score interval),
   so a move at 83% from 12 games isn't taken at face value the way a
   move at 52% from 50,000 games is.
3. **Champion-scan tiebreak**: among your candidates, the most-tested
   move is the default pick -- it only loses to a different one when
   that move is *confidently* better, via a real statistical
   comparison, not just a higher raw score. A bounded extension beyond
   your usual candidate pool (a "breakthrough") gets the same chance, so
   a genuinely better but less-popular option isn't invisible to the
   algorithm.

**Confidence Level** is the strictness dial behind all of this,
applied identically to your own move selection and to the opponent's
side (Ignore Threshold, Rarity Cutoff, Requirement). Higher (99%)
demands more data before trusting a percentage -- sticking with
well-tested moves more stubbornly and pruning thin opponent replies
more readily. Lower (80%) acts on a smaller sample sooner. For example,
a forcing move with 5,000 games at 52% beats a 150-game try at 58% at
every level from 90% up, but loses to it at 80% -- the difference
between "trust the bigger sample" and "that lead looks real even from
here." It matters most at Ambitious/Master, where more candidates are
being compared over a lower game floor; at Casual, User Move Range is
just 1, so there's rarely a second candidate to weigh in the first
place.

## Every line ends on your own move

This is a hard guarantee: no exported line ever dangles on the
opponent's reply. Hitting the depth limit is checked the instant your
own move uses the last slot, before the opponent's reply to it is ever
considered; and before an opponent reply is included as a branch, the
builder confirms there's actually a viable move for you after it. If
not, that reply is quietly excluded rather than left as a dead end --
which can mean lower coverage than configured when the database
genuinely runs out of data for a given try, an honest reflection of
what's backed by real games. A converging transposition is detected on
the opponent's move (that's just how turns alternate) but the note
always lands one forced ply later, on your own reply to it.

## ECO opening classification

Saved Surveys and PGN headers are labeled with the real ECO code and
name (e.g. "C60 Ruy Lopez") when one is found, using
[lichess-org's own chess-openings dataset](https://github.com/lichess-org/chess-openings)
bundled with Bookish. Classification is by position, not literal move
text, so a repertoire that transposes into a named opening via a
different move order is still recognized -- and whichever position
along the way matches most specifically wins (e.g. "C88 Ruy Lopez:
Closed" rather than settling for the more generic "C60 Ruy Lopez" an
earlier position in the same line would also match).

## Engine analysis

Engine tab -> "Add Engine…" and pick any UCI-speaking executable you've
already got (Stockfish, Lc0, anything else) -- a normal file picker, no
special install step. On Linux/macOS, a plain downloaded binary is made
executable automatically if it isn't already.

Whatever options the engine declares (Threads, Hash, Skill Level,
UCI_Elo, etc.) appear automatically as the right kind of control --
there's no fixed list per engine. A few options (MultiPV, Ponder,
Chess960, analysis mode) aren't shown, since they're managed
automatically and python-chess refuses to let anything else set them.
"Apply Options" sends only what changed, confirms it with a status
message and a brief flash on the button, and remembers it for next
time. If that engine's analysis popup is open, applying options (or
pressing a button-type option, like "Clear Hash") briefly pauses the
running analysis and resumes it right after -- sending any command
mid-analysis would otherwise silently end it.

"Open Analysis Panel" opens a resizable, non-modal popup: an evaluation
bar plus a live Multi-PV table, following whatever position is on the
board and restarting analysis automatically the moment it changes, even
through a rapid run of moves. Stop/Start pauses and resumes without
unloading the engine; closing the popup does the same (the process
stays loaded, so reopening it is instant). The top candidate move(s)
also draw as colored arrows on the board, one color per Multi-PV rank.

Saved engines (path, name, last-applied options) persist across
restarts like the last-loaded database does, but are never silently
relaunched -- each shows up under "Load Saved Engine" for you to start
explicitly. No new dependency: this is all built on `chess.engine`,
part of the `chess` package already in `requirements.txt`.

## Suggested settings by playing strength

A starting point, not a rulebook:

| Level | Preset | Scouting Mode | Notes |
|---|---|---|---|
| Casual club player | Casual | Neutral or Solid | Games leave theory fast either way, so deep memorization pays off less; a wider Opponent Move Range is worth it since lower-rated opponents try a wider spread of moves. |
| Strong club player | Club | Neutral | Enough real theory to hold an edge without turning prep into a second job. |
| Tournament player | Tournament or Ambitious | Neutral or Aggressive | Opponents mostly know real theory, so a narrower Opponent Move Range keeps prep focused. Aggressive suits anyone chasing results, not just titled players. |
| Master/GM level | Master | Neutral, Solid in reserve | Rarity Cutoff lets true mainline theory run deep while sidelines taper off automatically. Switch to Solid as a dedicated underdog repertoire against much stronger players. |

Two things worth treating separately from rating: Aggressive vs. Solid
is really about what result you need that day, not your level. And
Rarity Cutoff matters more than Depth for keeping a repertoire *usable*
-- a deep tree that branches wide everywhere is much harder to actually
learn than one that's allowed to taper naturally.

## Small quality-of-life things

- **Version number** in the window title is read straight from this
  CHANGELOG's own headings, so it can't drift out of sync.
- **Parsing visibility**: a malformed game in the PGN doesn't abort
  indexing or disappear silently -- whatever parsed before the error is
  still counted, and the Database tab shows a count plus a tooltip
  explaining it.
- **Reorderable tabs**, remembered across restarts by label (so a tab
  added later just takes its default spot).
- **Piece animation** (Off/Fast/Normal/Slow) on clicked moves, Database
  row clicks, and undo/redo; a dragged move doesn't additionally
  animate, since it's already sitting where you dropped it.

## Settings persistence

Color, Preset (or individual Advanced values), Scouting Mode,
Confidence Level, dark mode, board orientation, animation speed, tab
order, saved engines, and the last database loaded are all remembered
across restarts, written to `bookish_settings.json` next to `main.py`
on a normal close and read back on next launch -- a plain JSON file
you're welcome to open or delete for a clean slate.

## Project layout

```
main.py                     entry point
presets.yaml                editable repertoire presets (created on first run)
core/
  pgn_indexer.py            streams a .pgn -> SQLite position index
  position_stats.py         per-position lookups, weighted/confidence-adjusted scoring
  presets.py                loads/writes presets.yaml
  eco.py                    ECO opening classification (position-based lookup)
  eco_openings.tsv          bundled ECO data (source: lichess-org/chess-openings)
  repertoire_builder.py     the move-selection algorithm, branching, transpositions, PGN export
  engine_manager.py         UCI engine process wrapper (python-chess chess.engine)
gui/
  board_widget.py           click-or-drag chessboard, piece animation, engine-arrow overlay
  pieces/companion/*.svg    Lichess's "Companion" piece set (see license note below)
  result_bar.py             white/draw/black stacked bar
  eval_bar.py               vertical evaluation-bar widget
  theme.py                  light/dark palette switching
  database_panel.py         load DB + live position stats
  repertoire_panel.py       repertoire settings + background build + auto-save
  engine_panel.py           Engine tab -- add/configure/remove engines
  engine_analysis_dialog.py resizable live analysis popup
  main_window.py            ties it all together, loads/saves settings
  settings.py               bookish_settings.json read/write
  version.py                reads the current version from CHANGELOG.md
```

## Notes / known limits

- The indexer caps at 60 ply (30 full moves) per game -- plenty for any
  repertoire depth this tool offers.
- Only the PGN mainline of each game is indexed, matching how database
  exports (Lichess, ChessBase, etc.) are normally structured.
- Promotion defaults to Queen unless you pick otherwise in the popup
  that appears when a pawn move reaches the last rank.
- An engine needs to be a genuine UCI-speaking executable; anything
  else fails to load with a clear error rather than hanging.
- The board's piece images are Lichess's "Companion" set, created by
  David L. Brown and listed as freeware in Lichess's own `COPYING.md`.
  Fine for personal use; if you ever redistribute this app publicly,
  double-check the freeware terms at enpassant.dk (linked from
  Lichess's COPYING.md) for anything beyond personal/chess-software use.
