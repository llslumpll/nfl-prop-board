"""
Tracks real match-rate snapshots over time -- is the PIPELINE working
(are PrizePicks lines actually matching to our players), separate from
whether the predictions themselves are any good. Same distinction the
MLB site's Pipeline Health tab makes. One entry per calendar day, most
recent write wins for that day (matches MLB's refresh_odds.py pattern),
keeping the last ~90 days so this doesn't grow forever.
"""

import json
from pathlib import Path
from datetime import datetime, timezone

DATA_DIR = Path(__file__).parent.parent / "data"
HEALTH_PATH = DATA_DIR / "pipeline_health.json"


def log_health(frozen_this_build: int, matched_with_market_line: int, total_frozen_all_time: int) -> None:
    try:
        health = json.loads(HEALTH_PATH.read_text()) if HEALTH_PATH.exists() else {"entries": []}
    except Exception:
        health = {"entries": []}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    match_rate = round(100 * matched_with_market_line / frozen_this_build, 1) if frozen_this_build else None

    entry = {
        "date": today,
        "frozen_this_build": frozen_this_build,
        "matched_with_market_line": matched_with_market_line,
        "match_rate": match_rate,
        "total_frozen_all_time": total_frozen_all_time,
    }

    existing_idx = next((i for i, e in enumerate(health["entries"]) if e.get("date") == today), None)
    if existing_idx is not None:
        health["entries"][existing_idx] = entry
    else:
        health["entries"].append(entry)
    health["entries"] = sorted(health["entries"], key=lambda e: e["date"])[-90:]

    HEALTH_PATH.write_text(json.dumps(health, indent=2))


def load_health() -> list[dict]:
    if not HEALTH_PATH.exists():
        return []
    try:
        return json.loads(HEALTH_PATH.read_text()).get("entries", [])
    except Exception:
        return []
