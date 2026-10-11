"""
settings.py

Tiny JSON-based settings persistence -- lives right next to the app
(bookish_settings.json next to main.py), the same "everything visible,
nothing hidden" spirit as presets.yaml, the Surveys folder, and
CHANGELOG.md. Loaded once at startup, saved once on clean exit -- not a
live-sync system, so a crash loses only whatever changed since the last
normal close, same tradeoff Bookish already makes for index-build safety
(see pgn_indexer.py's synchronous=OFF pragma).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SETTINGS_PATH = Path(__file__).resolve().parent.parent / "bookish_settings.json"
_LEGACY_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "vantage_settings.json"


def load_settings() -> dict[str, Any]:
    """Returns {} if the file doesn't exist yet or can't be parsed --
    callers always get *something* usable rather than needing their own
    fallback logic. If this is the first launch since the app was
    renamed from Vantage, transparently picks up the old
    vantage_settings.json instead of silently discarding it -- a rename
    shouldn't cost you your settings."""
    path = SETTINGS_PATH if SETTINGS_PATH.exists() else _LEGACY_SETTINGS_PATH
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}


def save_settings(data: dict[str, Any]) -> None:
    """Best-effort: a failed settings save isn't worth crashing the app
    over (e.g. a read-only install directory), so this swallows errors
    rather than raising. Atomic write (tmp + os.replace) so a save that's
    interrupted mid-write can't leave a corrupt file behind for next
    launch to choke on. Always writes to the current (bookish_settings.json)
    path even if this session loaded from the legacy vantage_settings.json
    name -- the old file is left in place untouched rather than deleted."""
    try:
        tmp_path = SETTINGS_PATH.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, SETTINGS_PATH)
    except OSError:
        pass
