"""
Logs the real, live cushion-based coverage-tendency proxy (see
dataio.team_cushion_profile_2026) once per team per week -- not because
we can verify it's right yet (real 2026 man/zone charting isn't
released by nflverse yet, checked and confirmed), but so there's an
honest, timestamped trail to check it against once that real data does
land. Same "log now, validate later" discipline as calibrate.py.

One entry per (team, week), most recent write wins for that pair, kept
indefinitely for the season since the whole point is comparing week 1's
proxy against a full season of real charting once it exists.
"""

import json
from pathlib import Path
from datetime import datetime, timezone

DATA_DIR = Path(__file__).parent.parent / "data"
LOG_PATH = DATA_DIR / "coverage_proxy_log.json"


def log_coverage_proxy(team: str, week: int, avg_cushion_allowed: float, games: int) -> None:
    try:
        log = json.loads(LOG_PATH.read_text()) if LOG_PATH.exists() else {"entries": []}
    except Exception:
        log = {"entries": []}

    entry = {
        "team": team,
        "week": week,
        "avg_cushion_allowed": avg_cushion_allowed,
        "games": games,
        "logged_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }

    existing_idx = next(
        (i for i, e in enumerate(log["entries"]) if e.get("team") == team and e.get("week") == week), None
    )
    if existing_idx is not None:
        log["entries"][existing_idx] = entry
    else:
        log["entries"].append(entry)
    log["entries"] = sorted(log["entries"], key=lambda e: (e["team"], e["week"]))

    LOG_PATH.write_text(json.dumps(log, indent=2))


def load_coverage_log() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    try:
        return json.loads(LOG_PATH.read_text()).get("entries", [])
    except Exception:
        return []


def log_all_teams(team_list: list[str], cushion_profile_fn) -> int:
    """Logs the current cushion proxy for every real team with data
    this week. Returns how many were logged."""
    count = 0
    for team in team_list:
        profile = cushion_profile_fn(team)
        if profile:
            log_coverage_proxy(team, profile["games"], profile["avg_cushion_allowed"], profile["games"])
            count += 1
    return count
