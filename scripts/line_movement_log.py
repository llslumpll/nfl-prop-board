"""
Tracks real market line movement over time, per player/stat/week.

Every prediction freeze locks in ONE market_line at first freeze and
never updates it (the "freeze predictions, never let them drift"
principle) -- which means the frozen line is effectively the real
OPENING line, but nothing tracks whether the market has moved since.

This module logs the real line every time it's observed (every build,
including the 20-minute odds-refresh runs), keeping the real first-seen
(opening) value and the real most-recent (current) value, each with a
real timestamp. Movement (current - opening) is only ever derived from
two real observations -- never fabricated, never estimated.
"""

import json
from pathlib import Path
from datetime import datetime, timezone

DATA_DIR = Path(__file__).parent.parent / "data"
LOG_PATH = DATA_DIR / "line_movement_log.json"

_cache = None


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(LOG_PATH.read_text()) if LOG_PATH.exists() else {}
        except Exception:
            _cache = {}
    return _cache


def _save() -> None:
    LOG_PATH.write_text(json.dumps(_cache, indent=2))


def _key(player: str, stat: str, week: int) -> str:
    return f"{player}|{stat}|{week}"


def log_line(player: str, stat: str, week: int, market_line: float) -> dict:
    """
    Records a real line observation. First time this (player, stat,
    week) is seen, that real value becomes the real opening line.
    Every observation after that updates the real current line and
    timestamp -- the opening value is never touched again once set.
    """
    log = _load()
    key = _key(player, stat, week)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if key not in log:
        log[key] = {
            "opening_line": market_line, "opening_seen_at": now,
            "current_line": market_line, "current_seen_at": now,
        }
    else:
        log[key]["current_line"] = market_line
        log[key]["current_seen_at"] = now
    _save()
    return log[key]


def get_line_movement(player: str, stat: str, week: int) -> dict | None:
    """
    Real movement for this player/stat/week, from two real logged
    observations -- None if we haven't logged this combination at all
    yet (never a fabricated "no movement" default).
    """
    log = _load()
    entry = log.get(_key(player, stat, week))
    if not entry:
        return None
    change = round(entry["current_line"] - entry["opening_line"], 1)
    return {**entry, "moved": change != 0, "change": change}


def log_all(pp_props: dict, players_this_week: list[tuple]) -> int:
    """
    Logs the real current line for every (player, stat, week) combo
    that has a real PrizePicks line right now. players_this_week is a
    list of (player, stat, week) tuples to check against pp_props.
    Returns how many real lines were logged this build.
    """
    count = 0
    for player, stat, week in players_this_week:
        line = pp_props.get(player, {}).get(stat)
        if line is not None:
            log_line(player, stat, week, line)
            count += 1
    return count
