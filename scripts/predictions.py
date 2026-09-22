"""
Frozen prediction store. One record per (player, stat, week), written
ONCE and never overwritten -- this is what makes grading meaningful.

Earlier versions of this site logged a new CSV row on every single
build, including the odds-only refresh that runs every 20 minutes --
that would have produced thousands of duplicate rows per player per
week instead of one real, checkable prediction. This fixes that: a key
that already exists is left completely alone on every subsequent call,
exactly like the MLB site's "frozen prediction" pattern (projectedK/
projectedOuts/reason preserved across rebuilds in run_daily.py).

Each record captures the REAL market line (PrizePicks, where available)
at the moment it was frozen, not just our own projection -- without a
real line, there's nothing to call a hit or a miss against.
"""

import json
from pathlib import Path
from datetime import datetime, timezone

DATA_DIR = Path(__file__).parent.parent / "data"
PREDICTIONS_PATH = DATA_DIR / "predictions.json"


def load_predictions() -> dict:
    if not PREDICTIONS_PATH.exists():
        return {}
    try:
        return json.loads(PREDICTIONS_PATH.read_text())
    except Exception:
        return {}


def save_predictions(preds: dict) -> None:
    PREDICTIONS_PATH.write_text(json.dumps(preds, indent=2, default=str))


def migrate_stale_tiers() -> int:
    """
    One-time self-healing pass: any prediction frozen under the OLD flat
    tier logic (labeled exactly "No data", back when the tier only
    looked at n_games_2026 and ignored career history) gets its tier
    label recomputed using real, static 2023-2025 historical data --
    never touching projected/actual/market_line/hit, since those are the
    actual frozen prediction and must never silently change. Only the
    informational label was wrong; this fixes the label, once, safely.
    Safe to run every build -- already-migrated entries won't match the
    old label string again, so this naturally becomes a no-op after the
    first run.
    """
    import dataio

    preds = load_predictions()
    migrated = 0
    for p in preds.values():
        if p.get("tier_at_freeze") != "No data":
            continue
        _baseline, source, n_career = dataio._get_baseline(p["player"], p["stat"])
        has_career = source == "career (2023-2025)"
        # These were all frozen for a player's next (at-the-time-unplayed)
        # game, so n_games_2026 at freeze time was 0 in every real case
        # this migration applies to.
        new_tier = dataio.provisional_tier(0, has_career_history=has_career, n_career_games=n_career)
        p["tier_at_freeze"] = new_tier["label"]
        migrated += 1
    if migrated:
        save_predictions(preds)
    return migrated


def _key(player: str, stat: str, week: int) -> str:
    return f"{player}|{stat}|{week}"


def tag_best5(player: str, stat: str, week: int, list_name: str) -> bool:
    """
    Marks an ALREADY-frozen prediction as having been part of a specific
    week's Best 5 list (e.g. "highest_confidence" or "best_value") --
    metadata only, never touches the actual projected/market_line/call/
    actual/hit fields, since Best 5 selection happens after individual
    predictions are already frozen for the week. Safe to call multiple
    times (idempotent) and safe if the prediction doesn't exist yet
    (returns False rather than erroring, matching this module's
    fail-soft pattern).
    """
    preds = load_predictions()
    key = _key(player, stat, week)
    if key not in preds:
        return False
    tags = preds[key].setdefault("best5_tags", [])
    if list_name not in tags:
        tags.append(list_name)
        save_predictions(preds)
    return True


def freeze_prediction(
    player: str, team: str, opponent: str, week: int, stat: str,
    projected: float, tier_label: str, market_line: float | None = None,
    market_source: str | None = None,
) -> bool:
    """
    Writes a new frozen prediction ONLY if this (player, stat, week)
    combination doesn't already exist. Returns True if a new record was
    written, False if one already existed (and was therefore left
    untouched). Call/edge are only set when a real market line exists --
    no market line means no call, since there's nothing to grade against.
    """
    preds = load_predictions()
    key = _key(player, stat, week)
    if key in preds:
        return False

    call = None
    edge = None
    if market_line is not None:
        edge = round(projected - market_line, 1)
        call = "OVER" if edge > 0 else "UNDER" if edge < 0 else "PUSH"

    preds[key] = {
        "player": player,
        "team": team,
        "opponent": opponent,
        "week": week,
        "stat": stat,
        "projected": projected,
        "tier_at_freeze": tier_label,
        "market_line": market_line,
        "market_source": market_source,
        "call": call,
        "edge": edge,
        "frozen_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "graded": False,
        "actual": None,
        "hit": None,
        "graded_at": None,
    }
    save_predictions(preds)
    return True


def player_prop_history(player: str, stat: str) -> list[dict]:
    """
    Every real frozen prediction for this player/stat, across every
    week, sorted by week -- the real data behind a PrizePicks-style
    'value vs. line, per game' chart, except it grows across the WHOLE
    season instead of staying a fixed last-5. Each entry carries its own
    real, frozen market line (the actual line that was posted that
    specific week, not today's current one) and real hit/miss status
    once that week's game is graded.
    """
    preds = load_predictions()
    matches = [p for p in preds.values() if p["player"] == player and p["stat"] == stat]
    matches.sort(key=lambda p: p["week"])
    out = []
    for p in matches:
        if not p.get("graded"):
            status = "pending"
        elif p.get("hit") is None:
            status = "no_line"
        elif p["hit"]:
            status = "hit"
        else:
            status = "miss"
        out.append({
            "week": p["week"], "value": p.get("actual"), "threshold": p.get("market_line"),
            "call": p.get("call"), "status": status,
        })
    return out
