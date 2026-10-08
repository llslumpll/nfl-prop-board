"""
Pre-kickoff market-line attach for frozen player-prop predictions.

Why this exists: a prediction is frozen ONCE per (player, stat, week), usually
early in the week, and PrizePicks / FanDuel often haven't posted that player's
line yet. Without a line there is no OVER/UNDER call, so the pick can never be
scored hit/miss. This lets a lineless pick pick up its FIRST real line, but only
under rules that keep the scoreboard honest:

  * the projection, tier and signals are NEVER touched -- only the market side
    is filled in, from the frozen projection vs the real line;
  * only before the game's real kickoff (a line seen after kickoff is never
    attached, so a call is never made with the game already under way);
  * only once -- a pick that already has a line is never changed;
  * every attach is stamped (`line_attached_at`, `line_attach_note`) and the
    `market_source` records which book the line came from.

Safe to run every build: picks already carrying a line, graded picks, and picks
whose kickoff has passed (or can't be determined) are skipped.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import polars as pl

import dataio
import predictions

ET = ZoneInfo("America/New_York")

# Prediction `stat` -> the stat key used in the line dicts. any_td is excluded:
# neither PrizePicks nor FanDuel (as pulled here) gives a comparable line for it.
ATTACHABLE_STATS = ("passing_yards", "receiving_yards", "receptions", "rushing_yards")


def _kickoff_utc(team: str, week: int, sched: pl.DataFrame) -> datetime | None:
    """Real kickoff for `team`'s game that week, in UTC, from the schedule
    (gameday + gametime, which nflverse publishes in US Eastern). None when it
    can't be determined -- callers treat None as 'do not attach'."""
    rows = sched.filter(
        ((pl.col("home_team") == team) | (pl.col("away_team") == team)) & (pl.col("week") == week)
    )
    if rows.height == 0:
        return None
    r = rows.row(0, named=True)
    day, tm = r.get("gameday"), r.get("gametime")
    if not day or not tm:
        return None
    try:
        local = datetime.strptime(f"{day} {tm}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    except ValueError:
        return None
    return local.astimezone(timezone.utc)


def attach_lines(line_sources: list[tuple[str, dict]], now: datetime | None = None) -> dict:
    """
    line_sources: ordered [(source_name, {player: {stat: line}}), ...]. The first
    source with a line for a (player, stat) wins, so PrizePicks (listed first)
    is preferred over FanDuel and the two are never mixed for one pick.

    Returns {"attached": {source: n}, "skipped_after_kickoff": n,
             "skipped_no_kickoff": n, "still_no_line": n}.
    """
    now = now or datetime.now(timezone.utc)
    preds = predictions.load_predictions()
    sched = dataio.load_schedule()

    attached: dict[str, int] = {}
    after_kickoff = no_kickoff = still_no_line = 0
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")

    for p in preds.values():
        if p.get("graded") or p.get("market_line") is not None:
            continue
        if p.get("stat") not in ATTACHABLE_STATS:
            continue

        found = None
        for name, lines in line_sources:
            line = (lines.get(p["player"]) or {}).get(p["stat"])
            if line is not None:
                found = (name, float(line))
                break
        if found is None:
            still_no_line += 1
            continue

        kickoff = _kickoff_utc(p["team"], p["week"], sched)
        if kickoff is None:
            no_kickoff += 1
            continue
        if now >= kickoff:
            after_kickoff += 1
            continue

        source, line = found
        edge = round(p["projected"] - line, 1)
        p["market_line"] = line
        p["market_source"] = source
        p["edge"] = edge
        p["call"] = "OVER" if edge > 0 else "UNDER" if edge < 0 else "PUSH"
        p["line_attached_at"] = stamp
        p["line_attach_note"] = (
            f"Projection frozen {p.get('frozen_at')}; this {source} line was first seen "
            f"{stamp}, before kickoff. Call set from the frozen projection vs this line."
        )
        attached[source] = attached.get(source, 0) + 1

    if attached:
        predictions.save_predictions(preds)

    return {
        "attached": attached,
        "skipped_after_kickoff": after_kickoff,
        "skipped_no_kickoff": no_kickoff,
        "still_no_line": still_no_line,
    }
