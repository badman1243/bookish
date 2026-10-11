# Changelog

All notable changes to Bookish, in the order they were built.

## v1 -- Initial build

Chessboard (click-only), PGN indexer, position stats, the first
repertoire-building algorithm (ignore/requirement thresholds), a
Database tab with a result bar and a per-move score column, and a
Repertoire tab that saved via a plain file dialog.

Indexing used python-chess's default `chess.pgn.read_game()` (builds a
full game-tree object per move), stored a FEN for every position, and
hashed with Polyglot's zobrist scheme -- the slow baseline everything
after this is measured against.

## v2 -- First bug-fix round

Fixed board square colors and click-to-move when flipped, added
coordinate labels, added drag-and-drop, added transposition detection,
fixed the exported PGN to include real lead-in moves instead of jumping
straight to a FEN, introduced the Surveys folder, made clicking a
Database row play that move on the board, and dropped the redundant
per-move Score column now that the result bar already shows it.

Indexing was rewritten for speed: a custom lightweight PGN visitor
(skips the expensive default game-tree building), no more FEN storage,
and a cheap custom position hash instead of Polyglot's. Roughly
130 -> 570 games/sec on an 8,000-game benchmark, still single-threaded.

## v3 -- Reference-image round

Database tab restructured to match a reference screenshot: Move /
Games / a white-draw-black bar per row, with a bold "Σ" summary row at
the bottom (replacing the separate result bar). Added dark/light mode
and switched pieces to Lichess's Companion set. Renamed several
settings to shorter labels. Saving switched to automatic
`Survey_0.pgn`, `Survey_1.pgn`, ... numbering. Fixed the White/Black PGN
header so "Repertoire" always shows on the White side regardless of
which color you built for.

## v4 -- Precision round

Transposition matching is now double-checked against the exact
position, not just a hash match (astronomically unlikely to collide,
but no longer left to chance). Depth options expanded to
5/10/15/20/25/30.

## v5 -- Scouting modes round

Move scoring changed from raw win-rate to a points percentage
(win = 1, draw = 0.5, loss = 0), pulled toward 50% for thin samples via
a Wilson confidence interval. Added three Scouting Modes (Neutral /
Aggressive / Solid). Added a whole-repertoire coverage percentage to
the build summary, and a low-confidence warning icon to the Database
tab.

## v6 -- Structural fix, speed, and naming round

"Every line ends on your own move" became a hard guarantee rather than
a best effort (two bugs fixed: the depth check firing one ply too late,
and non-viable opponent replies not being excluded). Added adaptive
branch narrowing. Renamed the app to Vantage.

Indexing gained parallel (multi-process) support -- splitting the file
across worker processes. This made the machine sluggish while indexing
and lost the smooth progress readout; it gets revisited in v7-v9.

## v7 -- Worker count and streaming fixed, but not completely

Parallel indexing kept, but improved: worker count now defaults to half
your CPU cores (and is user-adjustable) instead of using every core,
each worker streams its slice of the file instead of loading it into
memory, and progress climbs continuously again via a shared counter.
Renamed throughout to Vantage.

Turned out to still be incomplete -- the progress counter used
`multiprocessing.Array`, which crashed on real hardware with
"SynchronizedArray objects should only be shared through inheritance."
Fixed in v8.

## v8 -- Indexing actually fixed, plus Rarity Cutoff

Replaced the crashing progress counter with a
`multiprocessing.Manager().list()` proxy, and wrapped the whole parallel
attempt in a try/except that falls back to single-core indexing on any
failure rather than crashing the build.

Added **Rarity Cutoff** ("1 in X games"): an alternative to a fixed
Depth ceiling that tracks how likely a real opponent's play is to reach
each position, and stops a branch once that gets too rare to be worth
preparing for. Added six presets that set Depth, Rarity Cutoff,
Ignore/Requirement, Move Ranges, and Minimum Games together.

Simplified the Repertoire panel: twelve always-visible controls cut
down to three (Color, Preset, Scouting Mode), with everything else
moved under a collapsed "Advanced Settings" section.

## v9 -- Parallel indexing reverted

Three attempts at parallel indexing (v6, v7, v8) all eventually hit the
same kind of platform-specific failure. Rather than try a fourth fix,
indexing went back to the reliable single-threaded path from v2
(~570-700 games/sec), with none of the multiprocessing code left in.

## v10 -- Renaming, Custom preset fix, preset tuning

Renamed "Pressure" to "Aggressive" throughout. Selecting the "Custom"
preset now actually does something (it opens Advanced Settings, since
Custom is meaningless if you can't see what it's set to). Tuned preset
values: the top tier's Rarity Cutoff raised, and Minimum Games raised
across the board.

## v11 -- Index staleness bug fixed

The database freshness check only ever compared the source PGN's
*path*, so re-exporting a PGN under the same filename could silently
keep serving a stale index. Now the file's modification time and size
are checked too.

## v12 -- Preset values reconsidered

Ignore Threshold flattened to a clean two-value split (10% for the
casual tiers, 5% for everything more serious) instead of a five-step
gradient. Opponent Move Range now grows alongside User Move Range for
more thorough presets, instead of shrinking.

## v13 -- Presets moved to an editable file

Presets no longer live in Python -- they're now in `presets.yaml`,
created automatically on first run with commented defaults. Edit it in
any text editor and reload without restarting. A broken or missing file
falls back to built-in defaults and explains what's wrong. Removed the
"Full Surveys" tier, leaving five: Casual, Club, Tournament, Ambitious,
Master.

## v14 -- Arrow-key navigation and a resizable board

Left/Right arrow keys step through move history (Right auto-plays the
Database tab's top move once there's nothing left to redo). The
board/tabs divider is now a real draggable splitter, and the board
itself resizes live instead of staying a fixed pixel size. Added a
visible Redo button.

## v15 -- Statistically sound move selection

Your own move selection now uses a real two-proportion statistical
test instead of comparing confidence intervals for overlap (a
well-known, overly conservative substitute for actually testing the
difference between two samples). Added a "breakthrough" mechanism so a
less-popular but genuinely better move can still be found outside the
usual candidate pool. Extended the same Wilson-adjusted, sample-size-
aware scoring to the opponent's side (Ignore Threshold, Rarity Cutoff,
Requirement), which had previously only used raw percentages.

## v16 -- Confidence Level exposed in the UI

The statistical strictness setting from v15 (`confidence_z`) is now a
real Advanced Settings control -- a curated 80/90/95/98/99% choice
rather than a raw z-score. Independent of Preset: switching one doesn't
reset the other.

## v17 -- Descriptive Survey filenames

Saved files are now named `Survey_N_<opening>.pgn` instead of just
`Survey_N.pgn`, using the lead-in moves (or the repertoire's own
opening moves, if built from scratch), capped to 6 plies. Auto-
numbering recognizes both old- and new-style filenames.

## v18 -- ECO opening classification

Saved Surveys are now named with the real ECO opening name when one is
found (e.g. `Survey_0_C60_Ruy_Lopez.pgn`), and the PGN itself gets
`[ECO]`/`[Opening]` header tags. Data comes from lichess-org's own
chess-openings dataset, bundled with the app. Classification is by
position, not literal move order, so a transposition into a named
opening is recognized correctly, and the deepest/most specific match
along the way always wins.

## v19 -- Settings persistence

Color, Preset (or individual values if Custom), Scouting Mode,
Confidence Level, Advanced Settings' expanded state, dark mode, board
orientation, and the last database loaded all now survive a restart,
written to a settings file on close and read back on launch.

## v20 -- Renamed to Bookish

App renamed from Vantage to Bookish throughout. The settings file
renamed too; an old settings file is picked up transparently on first
launch rather than losing your settings to the rename.

## v21 -- Version number, parsing visibility, reorderable tabs, animation

The window title now reads the current version straight from this
file's own headings. A malformed game in the PGN no longer disappears
silently -- whatever parsed before the error is still counted, and the
Database tab shows how many games hit a parsing issue. Tabs can be
dragged to reorder, and the order persists. Added piece-move animation
(Off/Fast/Normal/Slow).

## v22 -- FEN/ECO combined search

The position search box now also accepts an ECO code ("C60") or an
opening name/substring ("Najdorf") -- it tries FEN first, then falls
back to a reverse lookup against the bundled ECO data, landing on the
shortest/most general match. Clicking into an empty box also checks the
clipboard and offers to pre-fill it if the contents look like a FEN
(never auto-loads).

## v23-v27 -- Opening Tree, several attempts

An "Opening Tree" tab went through four different designs in a row --
a nested list widget, a rendered-survey rewrite, a persistence layer
("My Tree") recording your own games, and an in-memory version merging
your play with finished Repertoire Builder runs. A real exponential-
blowup bug was caught and fixed along the way (a hard query budget).
None of the four designs quite landed.

## v28 -- Opening Tree removed; a real animation bug fixed

Removed the Opening Tree feature entirely, planning to rebuild it later
with clearer direction rather than attempt a fifth design on the same
foundation. Fixed a genuine bug where undoing a castling move animated
the wrong rook -- the kingside/queenside check compared the move's
from/to squares directly, which silently inverts under undo; fixed by
checking the actual post-castle king square instead.

## v29 -- UCI engine integration

**New "Engine" tab.** Add any downloaded UCI engine (Stockfish, Lc0,
anything else that speaks UCI) via a normal file picker. Loading runs on
a background thread so a slow or broken executable can't freeze the
GUI. Every option the engine reports (Threads, Hash, Skill Level,
UCI_Elo, etc.) shows up automatically as the right kind of control --
nothing is hand-curated per engine. A handful of options (MultiPV,
Ponder, Chess960, analysis mode) are hidden, since python-chess manages
them itself and refuses to let anything else set them directly. Saved
engines persist across restarts but are never auto-launched -- each
shows up as a "Load Saved Engine" entry you click yourself.

**Resizable analysis popup**, in the spirit of Lichess's own analysis
board: an evaluation bar plus a live Multi-PV table, always analyzing
the current position and re-analyzing the moment it changes. A
genuinely resizable, non-modal window -- you can keep moving pieces
while it's open. The top candidate moves also draw as colored arrows on
the board.

Built entirely on python-chess's own `chess.engine` module, no new
dependency. Two real bugs were caught and fixed before release:
changing any option while analysis was running silently cancelled it
(the popup now pauses and resumes analysis automatically around option
changes), and a stop request landing just before a new analysis
registered itself could leave that analysis running forever in the
background (fixed with a cancellation token). Verified end-to-end
against a real Stockfish binary through 34 automated checks.

## v30 -- Evaluation bar visibility, and clearer options feedback

The evaluation bar's White-colored fill had almost no contrast against
a light-mode window background, making the bar hard to see whenever
White was doing well. Fixed with a visible border, the same fix
`result_bar.py`'s bar already used for the same reason.

Applying engine options, or pressing a UCI "button" option (like
Stockfish's "Clear Hash"), now gives clear feedback instead of changing
silently: a status message next to the button ("Applied: Threads,
Hash", "Sent: Clear Hash", or "No changes to apply."), plus the pressed
button itself briefly flashes its own confirmation before reverting.
