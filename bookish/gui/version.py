"""
version.py

The version number shown in the window title is read straight from
CHANGELOG.md's own "## vN -- ..." headings, rather than a separate
number maintained by hand somewhere else that could drift out of sync
with what the changelog actually says. Falls back to no version suffix
at all if CHANGELOG.md is missing or has no recognizable heading --
never blocks the app from starting over a cosmetic detail.
"""

from __future__ import annotations

import re
from pathlib import Path

CHANGELOG_PATH = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
_VERSION_RE = re.compile(r"^##\s*v(\d+)\b", re.MULTILINE)


def current_version() -> str | None:
    """Returns e.g. "20" for the highest "## v20 -- ..." heading found,
    or None if CHANGELOG.md doesn't exist or has no such heading."""
    try:
        text = CHANGELOG_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    matches = _VERSION_RE.findall(text)
    if not matches:
        return None
    return str(max(int(m) for m in matches))
