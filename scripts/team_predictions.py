"""
Frozen team-market predictions (moneyline, spread, total) -- the same
"freeze once, never silently recompute, timestamp it" principle as
scripts/predictions.py, applied to game-level outcomes instead of
player stats.

Keyed by f"{away_team}@{home_team}|{market}|{week}" so a rematch in a
later week gets its own real record. Each record freezes:
  - our real projection (from dataio.project_matchup)
  - the real FanDuel line at freeze time (from oddsapi_client, already
    being pulled for the player-prop side of the site)
  - which real signals were active (home/away is implicit in the key;
    real recent-form trend and real injury counts are stored so
    grade.py can later check whether they actually predict anything,
    same pattern as signal_effectiveness() for player props)

Grading (did our call and/or FanDuel's line get it right, once the
real final score exists) is intentionally NOT done in this module --
same separation as predictions.py vs grade.py, so freezing logic never
has to know how grading works and vice versa.
"""

import json
from pathlib import Path
from datetime import datetime, timezone

DATA_DIR = Path(__file__).parent.parent / "data"
PATH = DATA_DIR / "team_predictions.json"

MARKETS = ("team_moneyline", "team_spread", "team_total")


def _key(away_team: str, home_team: str, market: str, week: int) -> str:
    return f"{away_team}@{home_team}|{market}|{week}"


def load_team_predictions() -> dict:
    try:
        return json.loads(PATH.read_text()) if PATH.exists() else {}
    except Exception:
        return {}


def save_team_predictions(preds: dict) -> None:
    PATH.write_text(json.dumps(preds, indent=2))


def freeze_team_prediction(
    away_team: str, home_team: str, week: int, market: str,
    our_call: str, our_value: float, market_line: float | None,
    market_source: str | None, signals: dict,
) -> bool:
    """
    Freezes ONE real prediction for (away@home, market, week) if it
    doesn't already exist. Returns True if a new record was written,
    False if one was already there (matching predictions.py's
    freeze_prediction return convention) -- callers use this to count
    "N new predictions frozen this build" without re-deriving it.

    our_call: "HOME"/"AWAY" for moneyline and spread (which side we
    project to win / cover), "OVER"/"UNDER" for total.
    our_value: the real number behind the call (win probability % for
    moneyline, projected margin for spread, projected total for total).
    """
    if market not in MARKETS:
        raise ValueError(f"unknown market {market!r}")
    key = _key(away_team, home_team, market, week)
    preds = load_team_predictions()
    if key in preds:
        return False
    preds[key] = {
        "away_team": away_team, "home_team": home_team, "week": week, "market": market,
        "our_call": our_call, "our_value": our_value,
        "market_line": market_line, "market_source": market_source,
        "signals": signals or {},
        "frozen_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "result": None, "graded": False,
    }
    save_team_predictions(preds)
    return True


def ungraded_predictions() -> list[dict]:
    """Every real frozen team prediction not yet graded -- what
    grade.py's team-market pass will iterate over once built."""
    return [p for p in load_team_predictions().values() if not p.get("graded")]


def grade_all(real_schedule) -> dict:
    """
    Real grading pass: for every ungraded frozen team prediction whose
    real game now has a real final score, determines whether our call
    and (separately) FanDuel's line were correct, and marks it graded.
    real_schedule is a polars DataFrame with home_team/away_team/week/
    home_score/away_score/result -- passed in rather than imported, so
    this module doesn't need to know how the schedule is loaded (same
    separation as the rest of this site's grade.py takes a data source
    as an argument rather than reaching for a global).

    Returns {"newly_graded": int, "still_pending": int}.
    """
    preds = load_team_predictions()
    newly_graded = 0
    for key, p in preds.items():
        if p.get("graded"):
            continue
        row = real_schedule.filter(
            (real_schedule["home_team"] == p["home_team"])
            & (real_schedule["away_team"] == p["away_team"])
            & (real_schedule["week"] == p["week"])
        )
        if row.height == 0:
            continue
        r = row.row(0, named=True)
        if r.get("home_score") is None or r.get("away_score") is None:
            continue

        home_score, away_score = r["home_score"], r["away_score"]
        market = p["market"]
        if market == "team_moneyline":
            actual_winner = "HOME" if home_score > away_score else "AWAY" if away_score > home_score else "TIE"
            our_correct = (p["our_call"] == actual_winner) if actual_winner != "TIE" else None
            market_correct = None
            if p.get("market_line") is not None and actual_winner != "TIE":
                # market_line here is FanDuel's real implied HOME win probability (%)
                fd_call = "HOME" if p["market_line"] >= 50 else "AWAY"
                market_correct = fd_call == actual_winner
            p["actual_result"] = {"home_score": home_score, "away_score": away_score, "winner": actual_winner}
        elif market == "team_spread":
            actual_margin = home_score - away_score
            actual_side = "HOME" if actual_margin > -(p["market_line"] or 0) else "AWAY"
            # "Covered" relative to FanDuel's real home spread line, not our own projected margin --
            # our_call already reflects our real disagreement/agreement with that same real line.
            our_correct = (p["our_call"] == actual_side) if p["market_line"] is not None else None
            market_correct = None  # the market has no "call" of its own to grade against itself
            p["actual_result"] = {"home_score": home_score, "away_score": away_score, "actual_margin": actual_margin}
        elif market == "team_total":
            actual_total = home_score + away_score
            actual_side = "OVER" if actual_total > (p["market_line"] or 0) else "UNDER"
            our_correct = (p["our_call"] == actual_side) if p["market_line"] is not None else None
            market_correct = None
            p["actual_result"] = {"home_score": home_score, "away_score": away_score, "actual_total": actual_total}
        else:
            continue

        p["our_correct"] = our_correct
        p["market_correct"] = market_correct
        p["graded"] = True
        newly_graded += 1

    if newly_graded:
        save_team_predictions(preds)
    still_pending = sum(1 for p in preds.values() if not p.get("graded"))
    return {"newly_graded": newly_graded, "still_pending": still_pending}


def track_record(market: str | None = None) -> dict:
    """
    Real, honest accuracy summary from graded team predictions -- our
    real hit rate, and (moneyline only, since spread/total have no
    independent market "call" to grade) FanDuel's real implied hit rate
    for real, direct comparison. Returns an honest zero/None state when
    there isn't enough graded history yet, same as every other accuracy
    summary on this site.
    """
    preds = [p for p in load_team_predictions().values() if p.get("graded")]
    if market:
        preds = [p for p in preds if p["market"] == market]
    graded_with_call = [p for p in preds if p.get("our_correct") is not None]
    n = len(graded_with_call)
    our_hits = sum(1 for p in graded_with_call if p["our_correct"])
    market_graded = [p for p in preds if p.get("market_correct") is not None]
    market_hits = sum(1 for p in market_graded if p["market_correct"])
    return {
        "n": n,
        "our_hit_rate": round(100 * our_hits / n, 1) if n else None,
        "market_n": len(market_graded),
        "market_hit_rate": round(100 * market_hits / len(market_graded), 1) if market_graded else None,
    }
