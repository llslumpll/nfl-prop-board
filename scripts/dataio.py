"""
Data access layer for the NFL Prop Edge Board.

IMPORTANT HONESTY NOTES (per project brief):
- All stats here come from real nflreadpy snapshots (live schedule +
  2026 Week 1 stats + 2023-2025 historical stats), not fabricated data.
- No live market (Kalshi / PrizePicks) data is wired in this build --
  this sandbox's network allowlist does not include those APIs. Market
  columns are shown as "not connected" rather than invented.
- MIN_SAMPLE / DAMPEN below are a reasoned first-cut proposal for NFL's
  weekly cadence, not an empirically-fitted model -- see the docstring
  on those constants. They resolve the brief's #1 open question, but
  should be revisited once real graded predictions exist to check them
  against.
"""

import polars as pl
from pathlib import Path
from datetime import datetime, timezone

DATA_DIR = Path(__file__).parent.parent / "data"

# Captured once when this module loads (i.e. once per build run) so every
# projection computed in a single build shares the same frozen timestamp,
# per the brief's "freeze predictions, timestamp every freeze" principle.
FROZEN_AT = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

_stats_cache = None
_injuries_cache = None
_historical_cache = None
_schedules_cache = None

# ---------------------------------------------------------------------------
# MIN_SAMPLE / DAMPEN -- NFL-specific, per brief's open question #1
# ---------------------------------------------------------------------------
# MLB reasoning: ~162 games/season, played daily -> a confidence tier can
# plausibly accumulate MIN_SAMPLE=100 graded picks within a single week
# across the full slate, so MLB tuned around that number.
#
# NFL reasoning: ~16 games/week, played once a week, 18-week season.
# A rough estimate of gradeable prop lines per week across positions
# (QB passing, WR/TE/RB receiving, RB rushing, K kicking) for a full
# 16-game slate is on the order of 150-250 lines/week. Copying MLB's
# MIN_SAMPLE=100 *per confidence tier* would mean a tier might not
# accumulate enough sample until many weeks in -- for the higher/rarer
# tiers, possibly not before the season ends. That silently defeats the
# "gets better over time" premise the whole site is built on.
#
# First-cut NFL values (reasoned, not yet empirically validated against
# real graded results -- to be revisited once History has real data):
#   MIN_SAMPLE = 30   -- reachable inside 3-5 weeks for a typical tier,
#                        comfortably inside an 18-week season even for a
#                        narrower/rarer confidence bucket.
#   DAMPEN     = 0.20 -- slightly more conservative than MLB's ~0.30.
#                        Each NFL game is a much larger fraction of a
#                        player's season sample than an MLB game is
#                        (1/18 vs 1/162), so a single week's result
#                        should move a number less, not more.
MIN_SAMPLE = 30
DAMPEN = 0.20

# ---------------------------------------------------------------------------
# Team badges
# ---------------------------------------------------------------------------
# Real NFL logos are trademarked and can't be embedded. These are each
# team's well-known primary brand color (a fact, not artwork) used behind
# a plain text abbreviation -- a scannable stand-in, not a reproduction
# of any team's actual logo.
TEAM_COLORS = {
    "ARI": "#97233F", "ATL": "#A71930", "BAL": "#241773", "BUF": "#00338D",
    "CAR": "#0085CA", "CHI": "#0B162A", "CIN": "#FB4F14", "CLE": "#311D00",
    "DAL": "#041E42", "DEN": "#FB4F14", "DET": "#0076B6", "GB": "#203731",
    "HOU": "#03202F", "IND": "#002C5F", "JAX": "#101820", "KC": "#E31837",
    "LA": "#003594", "LAC": "#0080C6", "LV": "#000000", "MIA": "#008E97",
    "MIN": "#4F2683", "NE": "#002244", "NO": "#D3BC8D", "NYG": "#0B2265",
    "NYJ": "#125740", "PHI": "#004C54", "PIT": "#FFB612", "SEA": "#69BE28",
    "SF": "#AA0000", "TB": "#D50A0A", "TEN": "#4B92DB", "WAS": "#5A1414",
}


def team_badge(team: str) -> dict:
    """Color + abbreviation for a team badge chip. Falls back to a neutral
    gray for anything unrecognized rather than guessing a color."""
    return {"abbr": team, "color": TEAM_COLORS.get(team, "#5f6862")}


def next_game_for_team(team: str) -> dict | None:
    """Returns {"opponent", "week", "gameday"} for a team's next
    unplayed game, or None if the season has none left. Per-team lookup
    (not a single global "next week") so this stays correct once bye
    weeks make different teams' next games fall on different weeks."""
    sched = load_schedule()
    team_games = sched.filter(
        ((pl.col("home_team") == team) | (pl.col("away_team") == team))
        & (pl.col("result").is_null())
    ).sort("week")
    if team_games.height == 0:
        return None
    row = team_games.row(0, named=True)
    opponent = row["away_team"] if row["home_team"] == team else row["home_team"]
    return {"opponent": opponent, "week": row["week"], "gameday": row.get("gameday")}


def next_game_projection(player_name: str, team: str, stat_col: str) -> dict | None:
    """Combines next_game_for_team + project_stat into the single lookup
    every stat page needs: who they play next, and the dampened
    projection for that specific stat in that game. Returns None if
    there's no upcoming game left to project for."""
    game = next_game_for_team(team)
    if game is None:
        return None
    projection = project_stat(player_name, stat_col)
    return {**game, "projection": projection}


# ---------------------------------------------------------------------------
# Performance coloring (green/red)
# ---------------------------------------------------------------------------
# "Good" and "bad" are defined relative to the player's OWN baseline
# (their 2023-2025 career average, or league position average if they
# have no career history) -- never an arbitrary fixed number like "300+
# yards is good," which would misjudge a backup and a star by the same
# yardstick. A game 15%+ above a player's own baseline reads as good;
# 15%+ below reads as bad; anything closer than that is genuinely
# unremarkable and stays neutral rather than being forced into a color.
PERF_THRESHOLD = 0.15


def performance_vs_baseline(player_name: str, stat_col: str, actual_value) -> dict:
    if actual_value is None:
        return {"css": "neutral", "pct_diff": None}
    baseline, _source = _get_baseline(player_name, stat_col)
    if not baseline:
        return {"css": "neutral", "pct_diff": None}
    pct_diff = round(100 * (actual_value - baseline) / baseline, 0)
    if pct_diff >= PERF_THRESHOLD * 100:
        css = "good"
    elif pct_diff <= -PERF_THRESHOLD * 100:
        css = "bad"
    else:
        css = "neutral"
    return {"css": css, "pct_diff": pct_diff}


def load_stats() -> pl.DataFrame:
    global _stats_cache
    if _stats_cache is None:
        _stats_cache = pl.read_parquet(DATA_DIR / "player_stats_2026.parquet")
    return _stats_cache


def load_injuries() -> pl.DataFrame:
    global _injuries_cache
    if _injuries_cache is None:
        _injuries_cache = pl.read_parquet(DATA_DIR / "injuries_2026.parquet")
    return _injuries_cache


def load_historical_stats() -> pl.DataFrame:
    """2023-2025 player stats, used for Teams/Matchups player-vs-opponent
    history. Real nflreadpy data, not synthesized."""
    global _historical_cache
    if _historical_cache is None:
        _historical_cache = pl.read_parquet(DATA_DIR / "player_stats_historical.parquet")
    return _historical_cache


def load_schedule() -> pl.DataFrame:
    global _schedules_cache
    if _schedules_cache is None:
        _schedules_cache = pl.read_parquet(DATA_DIR / "schedules_2026.parquet")
    return _schedules_cache


def weeks_available() -> list[int]:
    return sorted(load_stats()["week"].unique().to_list())


def provisional_tier(n_games: int) -> dict:
    """
    Honest, non-fabricated placeholder confidence tier based ONLY on
    sample size seen so far THIS SEASON for a given player. This is a
    display label about data volume, distinct from the MIN_SAMPLE/DAMPEN
    constants above, which govern the (not-yet-built) prediction engine's
    calibration tiers once real predictions are being graded.
    """
    if n_games < 1:
        return {"label": "No data", "css": "tier-none"}
    if n_games < 3:
        return {"label": "Provisional (low sample)", "css": "tier-low"}
    if n_games < 6:
        return {"label": "Provisional (building sample)", "css": "tier-mid"}
    return {"label": "Provisional (fuller sample)", "css": "tier-high"}


def injury_status_for(player_name: str) -> dict | None:
    """
    Returns the most informative injury signal available for a player.
    Most rows only carry a practice-participation note (no formal
    Questionable/Doubtful/Out designation yet) -- shown as-is rather than
    upgraded into something more alarming than it is.
    """
    inj = load_injuries()
    rows = inj.filter(pl.col("full_name") == player_name)
    if rows.height == 0:
        return None
    row = rows.sort("week", descending=True).row(0, named=True)
    display = row.get("report_status") or row.get("practice_status")
    if not display:
        return None
    return {
        "report_status": display,
        "is_formal": row.get("report_status") is not None,
        "primary_injury": row.get("report_primary_injury") or row.get("practice_primary_injury"),
    }


def passing_leaders(limit: int = 30) -> list[dict]:
    df = load_stats()
    qb = df.filter(pl.col("position") == "QB").sort("passing_yards", descending=True)
    out = []
    for row in qb.head(limit).iter_rows(named=True):
        n_games = 1  # only week 1 in this snapshot
        out.append({
            "player": row["player_display_name"],
            "team": row["team"],
            "team_badge": team_badge(row["team"]),
            "opponent": row["opponent_team"],
            "week": row["week"],
            "completions": row["completions"],
            "attempts": row["attempts"],
            "passing_yards": row["passing_yards"],
            "passing_tds": row["passing_tds"],
            "interceptions": row["passing_interceptions"],
            "perf": performance_vs_baseline(row["player_display_name"], "passing_yards", row["passing_yards"]),
            "next_game": next_game_projection(row["player_display_name"], row["team"], "passing_yards"),
            "tier": provisional_tier(n_games),
            "injury": injury_status_for(row["player_display_name"]),
        })
    return out


def receiving_leaders(limit: int = 40) -> list[dict]:
    df = load_stats()
    rec = df.filter(pl.col("position").is_in(["WR", "TE", "RB"])).sort(
        "receiving_yards", descending=True
    )
    out = []
    for row in rec.head(limit).iter_rows(named=True):
        n_games = 1
        out.append({
            "player": row["player_display_name"],
            "position": row["position"],
            "team": row["team"],
            "team_badge": team_badge(row["team"]),
            "opponent": row["opponent_team"],
            "week": row["week"],
            "targets": row["targets"],
            "receptions": row.get("receptions"),
            "receiving_yards": row["receiving_yards"],
            "receiving_tds": row["receiving_tds"],
            "target_share": row.get("target_share"),
            "perf": performance_vs_baseline(row["player_display_name"], "receiving_yards", row["receiving_yards"]),
            "next_game": next_game_projection(row["player_display_name"], row["team"], "receiving_yards"),
            "tier": provisional_tier(n_games),
            "injury": injury_status_for(row["player_display_name"]),
        })
    return out


def receptions_leaders(limit: int = 40) -> list[dict]:
    """
    Separate from receiving_leaders() -- receptions is priced as its own,
    distinct prop on PrizePicks/Kalshi (brief's explicit instruction not
    to merge these pages), so it gets its own sort order and page.
    """
    df = load_stats()
    rec = df.filter(pl.col("position").is_in(["WR", "TE", "RB"])).sort(
        "receptions", descending=True
    )
    out = []
    for row in rec.head(limit).iter_rows(named=True):
        n_games = 1
        out.append({
            "player": row["player_display_name"],
            "position": row["position"],
            "team": row["team"],
            "team_badge": team_badge(row["team"]),
            "opponent": row["opponent_team"],
            "week": row["week"],
            "targets": row["targets"],
            "receptions": row.get("receptions"),
            "catch_rate": (
                round(100 * row["receptions"] / row["targets"], 1)
                if row.get("targets") else None
            ),
            "perf": performance_vs_baseline(row["player_display_name"], "receptions", row.get("receptions")),
            "next_game": next_game_projection(row["player_display_name"], row["team"], "receptions"),
            "tier": provisional_tier(n_games),
            "injury": injury_status_for(row["player_display_name"]),
        })
    return out


def rushing_leaders(limit: int = 40) -> list[dict]:
    df = load_stats()
    rush = df.filter(pl.col("position").is_in(["RB", "QB", "WR"])).filter(
        pl.col("rushing_yards") > 0
    ).sort("rushing_yards", descending=True)
    out = []
    for row in rush.head(limit).iter_rows(named=True):
        n_games = 1
        out.append({
            "player": row["player_display_name"],
            "position": row["position"],
            "team": row["team"],
            "team_badge": team_badge(row["team"]),
            "opponent": row["opponent_team"],
            "week": row["week"],
            "carries": row.get("carries"),
            "rushing_yards": row["rushing_yards"],
            "rushing_tds": row["rushing_tds"],
            "perf": performance_vs_baseline(row["player_display_name"], "rushing_yards", row["rushing_yards"]),
            "next_game": next_game_projection(row["player_display_name"], row["team"], "rushing_yards"),
            "tier": provisional_tier(n_games),
            "injury": injury_status_for(row["player_display_name"]),
        })
    return out


def _project_total_td(player_name: str, team: str) -> dict | None:
    """Touchdowns don't live in a single stat column -- sums the dampened
    projection across passing/rushing/receiving TDs (whichever the
    player has any real history in) paired with their next game."""
    game = next_game_for_team(team)
    if game is None:
        return None
    total_projected = 0.0
    parts = []
    for stat_col, label in [("passing_tds", "pass"), ("rushing_tds", "rush"), ("receiving_tds", "rec")]:
        hist = load_historical_stats()
        career_vals = [v for v in hist.filter(pl.col("player_display_name") == player_name)[stat_col].to_list() if v]
        if not career_vals and stat_col != "rushing_tds":
            # skip stat types the player has literally never recorded,
            # rather than projecting a fake baseline for e.g. a WR's
            # passing TDs -- rushing_tds is kept since any position can
            # pick up a garbage-time or trick-play carry.
            continue
        proj = project_stat(player_name, stat_col)
        if proj["projected"] > 0.05:
            total_projected += proj["projected"]
            parts.append(f"{proj['projected']} {label}")
    return {**game, "projected_total": round(total_projected, 1), "breakdown": ", ".join(parts) or "—"}


def touchdown_leaders(limit: int = 40) -> list[dict]:
    """
    Cross-position anytime-TD board (brief: TDs cut across prop types,
    hence their own page). Combines passing/rushing/receiving TDs into
    one 'anytime TD' style total per player, tagged with which kind.
    """
    df = load_stats()
    out = []
    for row in df.iter_rows(named=True):
        pass_td = row.get("passing_tds") or 0
        rush_td = row.get("rushing_tds") or 0
        rec_td = row.get("receiving_tds") or 0
        total = pass_td + rush_td + rec_td
        if total <= 0:
            continue
        kinds = []
        if pass_td:
            kinds.append(f"{pass_td} pass")
        if rush_td:
            kinds.append(f"{rush_td} rush")
        if rec_td:
            kinds.append(f"{rec_td} rec")
        out.append({
            "player": row["player_display_name"],
            "position": row["position"],
            "team": row["team"],
            "team_badge": team_badge(row["team"]),
            "opponent": row["opponent_team"],
            "week": row["week"],
            "total_tds": total,
            "breakdown": ", ".join(kinds),
            "perf": {"css": "good" if total >= 2 else "neutral", "pct_diff": None},
            "next_game": _project_total_td(row["player_display_name"], row["team"]),
            "tier": provisional_tier(1),
            "injury": injury_status_for(row["player_display_name"]),
        })
    out.sort(key=lambda r: r["total_tds"], reverse=True)
    return out[:limit]


def correlated_pairs_for_receiving(limit: int = 40) -> dict:
    """
    Flags the QB<->receiver correlation explicitly (brief section 2):
    same team + same game = correlated legs. This is a flag only, not
    a computed correlation coefficient (needs a real sample to compute).
    """
    df = load_stats()
    qbs = (
        df.filter(pl.col("position") == "QB")
        .sort("attempts", descending=True)
        .select(["team", "player_display_name", "attempts"])
    )
    qb_by_team: dict[str, str] = {}
    for row in qbs.iter_rows(named=True):
        # first (highest-attempts) QB seen per team wins -- the actual starter
        qb_by_team.setdefault(row["team"], row["player_display_name"])
    return qb_by_team


def upcoming_matchups(limit_games: int = 8) -> list[dict]:
    """
    Real upcoming games from the live schedule, with each team's current
    top passer/rusher/receiver's REAL historical stat line against that
    specific opponent (2023-2025), where it exists. Honest about "no
    history" rather than inventing a number when two teams haven't met.
    """
    sched = load_schedule()
    unplayed = sched.filter(pl.col("result").is_null()).sort(["week", "gameday"])
    if unplayed.height == 0:
        return []
    next_week = unplayed["week"].min()
    games = unplayed.filter(pl.col("week") == next_week)

    stats_2026 = load_stats()
    hist = load_historical_stats()

    def top_players_for_team(team: str) -> list[tuple[str, dict, bool]]:
        """Returns (stat_col, player_row, is_current_season) tuples.
        Falls back to last season's leaders when the team hasn't played
        a 2026 game yet (most teams, this early in Week 1)."""
        team_rows = stats_2026.filter(pl.col("team") == team)
        picks = []
        qb = team_rows.filter(pl.col("position") == "QB").sort("attempts", descending=True)
        rb = team_rows.filter(pl.col("position") == "RB").sort("carries", descending=True)
        wr = team_rows.filter(pl.col("position").is_in(["WR", "TE"])).sort(
            "receiving_yards", descending=True
        )
        if qb.height:
            picks.append(("passing_yards", qb.row(0, named=True), True))
        if rb.height:
            picks.append(("rushing_yards", rb.row(0, named=True), True))
        if wr.height:
            picks.append(("receiving_yards", wr.row(0, named=True), True))

        if picks:
            return picks

        # Fallback: most recent historical season's team leaders.
        last_season = hist["season"].max()
        team_hist = hist.filter((pl.col("team") == team) & (pl.col("season") == last_season))
        qb_h = team_hist.filter(pl.col("position") == "QB").group_by(
            "player_display_name"
        ).agg(pl.col("passing_yards").sum().alias("passing_yards")).sort(
            "passing_yards", descending=True
        )
        rb_h = team_hist.filter(pl.col("position") == "RB").group_by(
            "player_display_name"
        ).agg(pl.col("rushing_yards").sum().alias("rushing_yards")).sort(
            "rushing_yards", descending=True
        )
        wr_h = team_hist.filter(pl.col("position").is_in(["WR", "TE"])).group_by(
            "player_display_name"
        ).agg(pl.col("receiving_yards").sum().alias("receiving_yards")).sort(
            "receiving_yards", descending=True
        )
        if qb_h.height:
            picks.append(("passing_yards", {"player_display_name": qb_h.row(0, named=True)["player_display_name"]}, False))
        if rb_h.height:
            picks.append(("rushing_yards", {"player_display_name": rb_h.row(0, named=True)["player_display_name"]}, False))
        if wr_h.height:
            picks.append(("receiving_yards", {"player_display_name": wr_h.row(0, named=True)["player_display_name"]}, False))
        return picks

    def history_vs_opponent(player_name: str, stat_col: str, opponent: str) -> dict | None:
        rows = hist.filter(
            (pl.col("player_display_name") == player_name)
            & (pl.col("opponent_team") == opponent)
        )
        if rows.height == 0:
            return None
        vals = [v for v in rows[stat_col].to_list() if v is not None]
        if not vals:
            return None
        return {
            "games": len(vals),
            "avg": round(sum(vals) / len(vals), 1),
            "best": max(vals),
            "seasons": sorted(rows["season"].unique().to_list()),
        }

    out = []
    for g in games.head(limit_games).iter_rows(named=True):
        matchup = {
            "week": g["week"],
            "gameday": g.get("gameday"),
            "away_team": g["away_team"],
            "home_team": g["home_team"],
            "away_badge": team_badge(g["away_team"]),
            "home_badge": team_badge(g["home_team"]),
            "players": [],
        }
        for team, opponent in [(g["away_team"], g["home_team"]), (g["home_team"], g["away_team"])]:
            for stat_col, prow, is_current in top_players_for_team(team):
                hist_line = history_vs_opponent(prow["player_display_name"], stat_col, opponent)
                projection = project_stat(prow["player_display_name"], stat_col)
                log_projection(prow["player_display_name"], stat_col, g["week"], projection)
                matchup["players"].append({
                    "player": prow["player_display_name"],
                    "team": team,
                    "team_badge": team_badge(team),
                    "opponent": opponent,
                    "opponent_badge": team_badge(opponent),
                    "stat_label": stat_col.replace("_", " "),
                    "history": hist_line,
                    "based_on_current_season": is_current,
                    "projection": projection,
                })
        out.append(matchup)
    return out


# ---------------------------------------------------------------------------
# Projection engine
# ---------------------------------------------------------------------------
# Method (brief-compliant): shrinkage toward a baseline, never a full
# override from a small sample. This is the "dampen every correction"
# principle carried over from the proven MLB approach, using the NFL-
# specific DAMPEN constant defined above.
#
#   baseline = player's own 2023-2025 career average for this stat, if
#              they have any games with a real attempt/target/carry in
#              that stat; otherwise the league-wide position average
#              (a rookie/new player has no personal baseline to shrink
#              toward, so we fall back to the position's typical rate
#              rather than guessing).
#   observed = player's 2026-season-so-far average for this stat.
#   projection = baseline + DAMPEN * (observed - baseline)
#
# With only 1 week of 2026 data, "observed" this early is a single game
# -- exactly the "never a full override from one data point" case the
# brief warns about, which is why DAMPEN keeps the correction partial.
_LEAGUE_BASELINE_CACHE: dict[str, float] = {}

_STAT_QUALIFIER = {
    "passing_yards": ("QB", "attempts", 10),
    "rushing_yards": ("RB", "carries", 1),
    "receiving_yards": ("WR", "targets", 1),
}


def _league_baseline(stat_col: str) -> float:
    if stat_col in _LEAGUE_BASELINE_CACHE:
        return _LEAGUE_BASELINE_CACHE[stat_col]
    hist = load_historical_stats()
    position, qual_col, qual_min = _STAT_QUALIFIER.get(stat_col, (None, None, 0))
    df = hist
    if position:
        df = df.filter(pl.col("position") == position)
    if qual_col:
        df = df.filter(pl.col(qual_col) > qual_min)
    vals = [v for v in df[stat_col].to_list() if v is not None]
    avg = round(sum(vals) / len(vals), 1) if vals else 0.0
    _LEAGUE_BASELINE_CACHE[stat_col] = avg
    return avg


def _get_baseline(player_name: str, stat_col: str) -> tuple[float, str]:
    """Shared baseline logic: player's own career average, or league
    position average as fallback. Used by both project_stat() and
    performance_vs_baseline() so the two stay consistent."""
    hist = load_historical_stats()
    career_rows = hist.filter(pl.col("player_display_name") == player_name)
    career_vals = [v for v in career_rows[stat_col].to_list() if v is not None]
    if career_vals:
        return round(sum(career_vals) / len(career_vals), 1), "career (2023-2025)"
    return _league_baseline(stat_col), "league position average"


def project_stat(player_name: str, stat_col: str) -> dict:
    """
    Returns a frozen-at-build-time projection for one player/stat,
    dampened per the constants above. Callers should treat the result as
    immutable once built -- per the brief's "freeze predictions, never
    let them silently drift" principle, this build.py run's projection
    should not be silently recomputed intra-week.
    """
    stats_2026 = load_stats()

    baseline, baseline_source = _get_baseline(player_name, stat_col)

    season_rows = stats_2026.filter(pl.col("player_display_name") == player_name)
    season_vals = [v for v in season_rows[stat_col].to_list() if v is not None]
    n_games = len(season_vals)
    observed = round(sum(season_vals) / n_games, 1) if n_games else baseline

    projected = round(baseline + DAMPEN * (observed - baseline), 1)

    return {
        "projected": projected,
        "baseline": baseline,
        "baseline_source": baseline_source,
        "observed_2026": observed if n_games else None,
        "n_games_2026": n_games,
        "dampen": DAMPEN,
        "min_sample": MIN_SAMPLE,
        "meets_min_sample": n_games >= MIN_SAMPLE,
        "frozen_at": FROZEN_AT,
    }


def log_projection(player_name: str, stat_col: str, week: int, projection: dict) -> None:
    """
    Appends a frozen projection to data/projections_log.csv, one row per
    player/stat/week/build. This is what makes future grading possible
    once either (a) a market line exists to define hit/miss, or (b) the
    site starts comparing projections to actual results directly. Until
    then this is a real, growing, timestamped record -- not a metric,
    just the raw material an honest History page will eventually need.
    """
    import csv

    log_path = DATA_DIR / "projections_log.csv"
    is_new = not log_path.exists()
    with open(log_path, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow([
                "frozen_at", "week", "player", "stat", "projected",
                "baseline", "baseline_source", "observed_2026", "n_games_2026",
            ])
        writer.writerow([
            projection["frozen_at"], week, player_name, stat_col,
            projection["projected"], projection["baseline"],
            projection["baseline_source"], projection["observed_2026"],
            projection["n_games_2026"],
        ])


def projections_logged_count() -> int:
    log_path = DATA_DIR / "projections_log.csv"
    if not log_path.exists():
        return 0
    with open(log_path) as f:
        return max(0, sum(1 for _ in f) - 1)  # minus header
