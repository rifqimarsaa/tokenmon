"""Claude Code's own credit accounting: what a token of each model costs, and how many credits a plan holds.

Claude's limits are not counted in raw tokens. Claude Code keeps the rates in its config (`~/.claude.json`),
and its 5-hour credit allowance per plan tier; this module reads only those few values and never stores
anything else from that file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# What Claude Code listed when this was written (credits per million tokens). Used only if the config
# cannot be read; a model missing from the table counts as 0 credits.
DEFAULT_RATES = {
    "claude-opus-5-5": 1833, "claude-opus-5": 2292, "claude-opus-4-8": 2750, "claude-opus-4-7": 3338,
    "claude-opus-4-6": 5000, "claude-sonnet-5-5": 1250, "claude-sonnet-5": 1250, "claude-sonnet-4-6": 2500,
    "claude-haiku-4-5": 833, "claude-fable-5-1": 8333, "claude-fable-5": 8333,
}
# shortcut: the weekly capacity is not published. 5 x the 5-hour credits matched 10 weekly readings of one
# Max 20x account within 2.6 points; replace this if Anthropic publishes the figure.
WEEKLY_PER_FIVE_HOUR = 5

_cache: tuple[Path, int, dict[str, Any]] | None = None


def config_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base) / ".claude.json" if base else Path.home() / ".claude.json"


def _find(obj: Any, key: str) -> Any:
    """First non-empty value stored under `key`, wherever it sits (its place in the file has moved before)."""
    if isinstance(obj, dict):
        for name, value in obj.items():
            if name == key and value:
                return value
            found = _find(value, key)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find(value, key)
            if found:
                return found
    return None


def _config() -> dict[str, Any]:
    """The three values we need, re-read only when the file changes (it is rewritten often)."""
    global _cache
    path = config_path()
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {}
    if _cache and _cache[0] == path and _cache[1] == stamp:
        return _cache[2]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    tally = _find(data, "tengu_amber_tally") or {}
    values = {
        "rates": tally.get("creditsPerMillionTokens") if isinstance(tally, dict) else None,
        "five_hour": tally.get("fiveHourCredits") if isinstance(tally, dict) else None,
        "tier": _find(data, "organizationRateLimitTier"),
    }
    _cache = (path, stamp, values)
    return values


def credit_rates() -> dict[str, float]:
    rates = _config().get("rates")
    return rates if isinstance(rates, dict) and rates else DEFAULT_RATES


def weekly_capacity() -> int | None:
    """Credits in one weekly window for this account's plan, or None when the plan is unknown."""
    config = _config()
    five = config.get("five_hour")
    five = five.get(config.get("tier")) if isinstance(five, dict) else None
    return int(five * WEEKLY_PER_FIVE_HOUR) if isinstance(five, (int, float)) and five > 0 else None
