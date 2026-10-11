"""
presets.py

Repertoire presets now live in an editable file (presets.yaml, next to
main.py -- same "everything visible, nothing hidden" spirit as the
Surveys folder and CHANGELOG.md) instead of being hardcoded in Python.
Open it in any text editor, change a number, save, and click "Reload
Presets" in the app (or just relaunch) -- no need to ask for a code
change to retune a preset.

If the file doesn't exist yet (first run), a commented default file is
written automatically so there's always something there to edit. If the
file exists but fails to parse (bad YAML, a missing field, wrong type),
loading falls back to the same built-in defaults and reports the error
back to the caller rather than crashing -- the app stays usable while
you fix the file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

PRESETS_PATH = Path(__file__).resolve().parent.parent / "presets.yaml"


@dataclass(frozen=True)
class Preset:
    depth: int
    population_threshold: int | None  # the "X" in "1 in X games"; None/0 disables Rarity Cutoff
    ignore_threshold_pct: float
    requirement_pct: float
    opponent_move_range: int
    user_move_range: int
    user_minimum_games: int
    opponent_minimum_games: int
    description: str = ""


# Built-in fallback, used both to write the initial presets.yaml on first
# run and as a safety net if the file exists but fails to parse.
DEFAULT_PRESETS: dict[str, Preset] = {
    "Casual": Preset(5, 50, 10, 70, 3, 1, 300, 300,
                      "A repertoire guide that covers only the most important lines."),
    "Club": Preset(10, 100, 10, 80, 3, 2, 250, 250,
                   "Covers all of the mainlines and some important sidelines."),
    "Tournament": Preset(15, 200, 5, 90, 5, 3, 200, 200,
                          "Serious tournament preparation looking to reach 2000 USCF/FIDE; "
                          "covers all the mainlines and most sidelines."),
    "Ambitious": Preset(20, 400, 5, 90, 5, 5, 150, 150,
                         "For 1800-2000 FIDE/USCF players working toward National Master or FIDE Master."),
    "Master": Preset(25, 800, 5, 90, 5, 5, 100, 100,
                      "For FIDE Masters aiming at International Master, or IMs aiming at Grandmaster."),
}

_YAML_HEADER = """\
# Bookish repertoire presets.
#
# Edit these directly in any text editor -- changes take effect the next
# time you launch Bookish, or immediately if you click "Reload Presets"
# in the Repertoire Builder tab. If this file has a syntax error or a
# preset is missing a field, Bookish falls back to its built-in defaults
# and tells you what went wrong rather than crashing -- fix the file and
# reload/relaunch to pick your edits back up.
#
# Fields for each preset:
#   depth                    Hard ceiling on how many of your own moves
#                            a line can contain, no matter what.
#   rarity_cutoff            The "X" in "1 in X games" -- a branch gets
#                            excluded once it's rarer than this. Set to
#                            0 or null to disable Rarity Cutoff for this
#                            preset (falls back to pure Depth-based
#                            building).
#   ignore_threshold_pct     Opponent replies below this share of the
#                            position's games are dropped by default.
#   requirement_pct          If what clears ignore_threshold_pct doesn't
#                            add up to this much cumulative coverage,
#                            smaller replies get added back in until it
#                            does.
#   opponent_move_range      Hard cap on how many branches the tree can
#                            split into at any one opponent turn.
#   user_move_range          How many of your own top candidate moves
#                            (by popularity) get considered before
#                            picking the best-scoring one.
#   user_minimum_games       A candidate move of yours needs at least
#                            this many games in the database to be
#                            eligible at all.
#   opponent_minimum_games   Same idea, for the opponent's replies.
#   description              Shown in the Preset dropdown's tooltip.
#
# Presets are applied in the order they appear here. "Custom" is always
# added automatically after whatever's listed below -- don't add your
# own entry named Custom.

"""


def _preset_to_yaml_dict(p: Preset) -> dict:
    return {
        "depth": p.depth,
        "rarity_cutoff": p.population_threshold,
        "ignore_threshold_pct": p.ignore_threshold_pct,
        "requirement_pct": p.requirement_pct,
        "opponent_move_range": p.opponent_move_range,
        "user_move_range": p.user_move_range,
        "user_minimum_games": p.user_minimum_games,
        "opponent_minimum_games": p.opponent_minimum_games,
        "description": p.description,
    }


def write_default_file(path: Path = PRESETS_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(_YAML_HEADER)
        for i, (name, p) in enumerate(DEFAULT_PRESETS.items()):
            if i > 0:
                f.write("\n")
            yaml.safe_dump({name: _preset_to_yaml_dict(p)}, f, sort_keys=False, default_flow_style=False)


_REQUIRED_FIELDS = (
    "depth", "ignore_threshold_pct", "requirement_pct", "opponent_move_range",
    "user_move_range", "user_minimum_games", "opponent_minimum_games",
)


def load_presets(path: Path = PRESETS_PATH) -> tuple[dict[str, Preset], str | None]:
    """Returns (presets, error_message). error_message is None on a clean
    load; if it's set, `presets` is DEFAULT_PRESETS (the file exists but
    couldn't be used as-is) so the caller always gets something usable."""
    if not path.exists():
        try:
            write_default_file(path)
        except OSError as exc:
            return DEFAULT_PRESETS, f"Couldn't create {path.name} ({exc}); using built-in defaults."
        return DEFAULT_PRESETS, None

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        return DEFAULT_PRESETS, f"{path.name} has a YAML syntax error ({exc}); using built-in defaults until it's fixed."
    except OSError as exc:
        return DEFAULT_PRESETS, f"Couldn't read {path.name} ({exc}); using built-in defaults."

    if not isinstance(raw, dict) or not raw:
        return DEFAULT_PRESETS, f"{path.name} is empty or not structured as expected; using built-in defaults."

    presets: dict[str, Preset] = {}
    for name, fields in raw.items():
        if name == "Custom":
            continue  # reserved; silently skip if someone added it by hand
        if not isinstance(fields, dict):
            return DEFAULT_PRESETS, f"{path.name}: preset \"{name}\" isn't structured correctly; using built-in defaults."
        missing = [f for f in _REQUIRED_FIELDS if f not in fields]
        if missing:
            return DEFAULT_PRESETS, f"{path.name}: preset \"{name}\" is missing {', '.join(missing)}; using built-in defaults."
        try:
            rarity = fields.get("rarity_cutoff")
            presets[name] = Preset(
                depth=int(fields["depth"]),
                population_threshold=int(rarity) if rarity else None,
                ignore_threshold_pct=float(fields["ignore_threshold_pct"]),
                requirement_pct=float(fields["requirement_pct"]),
                opponent_move_range=int(fields["opponent_move_range"]),
                user_move_range=int(fields["user_move_range"]),
                user_minimum_games=int(fields["user_minimum_games"]),
                opponent_minimum_games=int(fields["opponent_minimum_games"]),
                description=str(fields.get("description", "")),
            )
        except (TypeError, ValueError) as exc:
            return DEFAULT_PRESETS, f"{path.name}: preset \"{name}\" has a bad value ({exc}); using built-in defaults."

    if not presets:
        return DEFAULT_PRESETS, f"{path.name} doesn't define any presets; using built-in defaults."

    return presets, None
