"""
Local-only usage metrics — never uploaded anywhere.

Tracks command runs, emails trashed, undo count, and first run date.
Used only to understand how mailtrim is being used locally and to inform
future product decisions.

Data is stored in ~/.mailtrim/usage.json  (plain JSON, human-readable).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from mailtrim.config import DATA_DIR

logger = logging.getLogger(__name__)

_STATS_PATH = DATA_DIR / "usage.json"

_DEFAULT: dict = {
    "first_run": None,  # ISO date string
    "total_runs": 0,
    "command_counts": {},  # {"stats": 12, "purge": 3, ...}
    "emails_trashed": 0,
    "emails_restored": 0,
    "undo_count": 0,
    "version_first_seen": {},  # {"0.2.0": "2026-04-11"}
}


def _load() -> dict:
    try:
        if _STATS_PATH.exists():
            return json.loads(_STATS_PATH.read_text())
    except Exception as exc:
        logger.debug("Could not load usage stats: %s", exc)
    return dict(_DEFAULT)


def _save(data: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _STATS_PATH.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        logger.debug("Could not save usage stats: %s", exc)


def record_run(command: str) -> None:
    """Record that a command was run. Called at the start of each command."""
    from mailtrim import __version__

    data = _load()
    now = datetime.now(timezone.utc).date().isoformat()

    if data.get("first_run") is None:
        data["first_run"] = now

    data["total_runs"] = data.get("total_runs", 0) + 1
    counts = data.get("command_counts", {})
    counts[command] = counts.get(command, 0) + 1
    data["command_counts"] = counts

    versions = data.get("version_first_seen", {})
    if __version__ not in versions:
        versions[__version__] = now
    data["version_first_seen"] = versions

    _save(data)


def record_emails_trashed(count: int) -> None:
    """Record how many emails were moved to Trash."""
    data = _load()
    data["emails_trashed"] = data.get("emails_trashed", 0) + count
    _save(data)


def record_undo(restored: int = 0) -> None:
    """Record an undo operation."""
    data = _load()
    data["undo_count"] = data.get("undo_count", 0) + 1
    data["emails_restored"] = data.get("emails_restored", 0) + restored
    _save(data)


def get_stats() -> dict:
    """Return current usage stats as a plain dict."""
    return _load()


def format_summary() -> str:
    """Return a single human-readable summary line."""
    data = _load()
    trashed = data.get("emails_trashed", 0)
    runs = data.get("total_runs", 0)
    first = data.get("first_run", "unknown")
    return f"{trashed:,} emails trashed across {runs} runs since {first}"
