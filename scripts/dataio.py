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

# Real, public, stable facts -- conference/division alignment, not
# derived from any API here since nflreadpy's schedule rows don't carry
# it directly. Used only for grouping the standings table.
TEAM_CONFERENCE = {
    "BUF": "AFC", "MIA": "AFC", "NE": "AFC", "NYJ": "AFC",
    "BAL": "AFC", "CIN": "AFC", "CLE": "AFC", "PIT": "AFC",
    "HOU": "AFC", "IND": "AFC", "JAX": "AFC", "TEN": "AFC",
    "DEN": "AFC", "KC": "AFC", "LV": "AFC", "LAC": "AFC",
    "DAL": "NFC", "NYG": "NFC", "PHI": "NFC", "WAS": "NFC",
    "CHI": "NFC", "DET": "NFC", "GB": "NFC", "MIN": "NFC",
    "ATL": "NFC", "CAR": "NFC", "NO": "NFC", "TB": "NFC",
    "ARI": "NFC", "LA": "NFC", "SF": "NFC", "SEA": "NFC",
}
TEAM_DIVISION = {
    "BUF": "AFC East", "MIA": "AFC East", "NE": "AFC East", "NYJ": "AFC East",
    "BAL": "AFC North", "CIN": "AFC North", "CLE": "AFC North", "PIT": "AFC North",
    "HOU": "AFC South", "IND": "AFC South", "JAX": "AFC South", "TEN": "AFC South",
    "DEN": "AFC West", "KC": "AFC West", "LV": "AFC West", "LAC": "AFC West",
    "DAL": "NFC East", "NYG": "NFC East", "PHI": "NFC East", "WAS": "NFC East",
    "CHI": "NFC North", "DET": "NFC North", "GB": "NFC North", "MIN": "NFC North",
    "ATL": "NFC South", "CAR": "NFC South", "NO": "NFC South", "TB": "NFC South",
    "ARI": "NFC West", "LA": "NFC West", "SF": "NFC West", "SEA": "NFC West",
}

# Real team city/nickname, used only to tell a Kalshi "team total" market
# ("Houston over 21.5 points scored") apart from a combined game total
# ("Over 44.5 points scored") -- Kalshi phrases team-total titles with
# the city name, not the abbreviation.
TEAM_CITY = {
    "ARI": "Arizona", "ATL": "Atlanta", "BAL": "Baltimore", "BUF": "Buffalo",
    "CAR": "Carolina", "CHI": "Chicago", "CIN": "Cincinnati", "CLE": "Cleveland",
    "DAL": "Dallas", "DEN": "Denver", "DET": "Detroit", "GB": "Green Bay",
    "HOU": "Houston", "IND": "Indianapolis", "JAX": "Jacksonville", "KC": "Kansas City",
    "LA": "Los Angeles Rams", "LAC": "Los Angeles Chargers", "LV": "Las Vegas", "MIA": "Miami",
    "MIN": "Minnesota", "NE": "New England", "NO": "New Orleans", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia", "PIT": "Pittsburgh", "SEA": "Seattle",
    "SF": "San Francisco", "TB": "Tampa Bay", "TEN": "Tennessee", "WAS": "Washington",
}


def team_badge(team: str) -> dict:
    """Color + abbreviation for a team badge chip. Falls back to a neutral
    gray for anything unrecognized rather than guessing a color."""
    return {"abbr": team, "color": TEAM_COLORS.get(team, "#5f6862")}


def team_standings() -> dict:
    """
    Real record + last-5 trend per team, grouped by conference AND
    division, computed directly from the live schedule's actual scores.
    Early in the season most teams will show 0-0 or 1-0; that's real,
    not a bug -- the table reflects however many games have actually
    been played so far.
    """
    sched = load_schedule()
    played = sched.filter(pl.col("result").is_not_null()).sort("week")

    all_teams = sorted(set(sched["home_team"].to_list()) | set(sched["away_team"].to_list()))
    records = {t: {"w": 0, "l": 0, "t": 0, "results": []} for t in all_teams}

    for row in played.iter_rows(named=True):
        home, away = row["home_team"], row["away_team"]
        home_score, away_score = row["home_score"], row["away_score"]
        if home_score is None or away_score is None:
            continue
        if home_score > away_score:
            records[home]["w"] += 1
            records[away]["l"] += 1
            records[home]["results"].append("W")
            records[away]["results"].append("L")
        elif away_score > home_score:
            records[away]["w"] += 1
            records[home]["l"] += 1
            records[away]["results"].append("W")
            records[home]["results"].append("L")
        else:
            records[home]["t"] += 1
            records[away]["t"] += 1
            records[home]["results"].append("T")
            records[away]["results"].append("T")

    DIVISION_ORDER = ["East", "North", "South", "West"]
    by_conference: dict[str, dict[str, list[dict]]] = {
        "AFC": {f"AFC {d}": [] for d in DIVISION_ORDER},
        "NFC": {f"NFC {d}": [] for d in DIVISION_ORDER},
    }
    for team in all_teams:
        r = records[team]
        games = r["w"] + r["l"] + r["t"]
        pct = round((r["w"] + 0.5 * r["t"]) / games, 3) if games else None
        last5 = r["results"][-5:]
        last5_w = last5.count("W")
        last5_pct = (last5_w / len(last5)) if last5 else None
        trend = None
        if pct is not None and last5_pct is not None and len(last5) >= 3:
            delta = last5_pct - pct
            trend = "up" if delta > 0.15 else "down" if delta < -0.15 else "flat"
        conf = TEAM_CONFERENCE.get(team, "?")
        division = TEAM_DIVISION.get(team, f"{conf} ?")
        entry = {
            "team": team,
            "team_badge": team_badge(team),
            "division": division,
            "wins": r["w"], "losses": r["l"], "ties": r["t"],
            "pct": pct,
            "last5": f"{last5_w}-{len(last5) - last5_w}" if last5 else "--",
            "trend": trend,
        }
        by_conference.setdefault(conf, {}).setdefault(division, []).append(entry)

    for conf in by_conference:
        for division in by_conference[conf]:
            by_conference[conf][division].sort(
                key=lambda t: (t["pct"] if t["pct"] is not None else -1), reverse=True
            )

    return by_conference


def team_full_stats() -> list[dict]:
    """
    Real per-team offense/defense totals for every stat this build can
    actually compute from nflreadpy data -- no fabricated stat included.
    Covers ALL 32 teams (from the schedule), not just teams that have
    already played a 2026 game -- teams with no games yet show real
    zeros rather than being silently dropped from "every team." Yards
    allowed is derived from what OPPONENTS gained against a team
    (standard definition), since nflreadpy's player rows don't carry a
    direct "yards allowed" field.
    """
    df = load_stats()
    sched = load_schedule()
    all_teams = sorted(set(sched["home_team"].to_list()) | set(sched["away_team"].to_list()))
    out = []
    for team in all_teams:
        off = df.filter(pl.col("team") == team)
        opp_off = df.filter(pl.col("opponent_team") == team)

        games_played = off["week"].n_unique() if off.height else 0

        pass_yds = off["passing_yards"].sum() or 0
        rush_yds = off["rushing_yards"].sum() or 0
        total_yds = pass_yds + rush_yds
        pass_tds = off["passing_tds"].sum() or 0
        rush_tds = off["rushing_tds"].sum() or 0

        ints_thrown = off["passing_interceptions"].sum() or 0
        fumbles_lost = (
            (off["sack_fumbles_lost"].sum() or 0)
            + (off["rushing_fumbles_lost"].sum() or 0)
            + (off["receiving_fumbles_lost"].sum() or 0)
        )
        turnovers = ints_thrown + fumbles_lost

        def_sacks = off["def_sacks"].sum() or 0
        def_ints = off["def_interceptions"].sum() or 0
        def_fumbles = off["def_fumbles"].sum() or 0
        takeaways = def_ints + def_fumbles

        yds_allowed = (opp_off["passing_yards"].sum() or 0) + (opp_off["rushing_yards"].sum() or 0)

        out.append({
            "team": team,
            "team_badge": team_badge(team),
            "games": games_played,
            "total_yards": int(total_yds),
            "pass_yards": int(pass_yds),
            "rush_yards": int(rush_yds),
            "off_tds": int(pass_tds + rush_tds),
            "turnovers": int(turnovers),
            "def_sacks": int(def_sacks),
            "takeaways": int(takeaways),
            "yards_allowed": int(yds_allowed),
            "turnover_margin": int(takeaways - turnovers),
        })
    out.sort(key=lambda t: (-t["games"], -t["total_yards"]))
    return out


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
    baseline, _source, _n_career = _get_baseline(player_name, stat_col)
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


def provisional_tier(n_games: int, has_career_history: bool = False, n_career_games: int = 0) -> dict:
    """
    Honest, non-fabricated confidence tier -- now based on BOTH this
    season's sample size AND whether a real 2023-2025 career baseline
    exists, not current-season games alone.

    Why this changed: early in a season, nearly every prediction is FOR
    a player's next game, made BEFORE that game happens -- so
    n_games_2026 is 0 or 1 for almost everyone at the moment a
    prediction is frozen, regardless of whether they're a rookie or a
    10-year veteran. That collapsed the real History grading into one
    meaningless "No data" bucket for 62 real graded picks (confirmed
    live). A player with 45 career games behind their baseline is
    genuinely more established than one with zero, even before their
    2026 opener -- that's real information already used by the
    projection engine's own baseline_source, just not reflected in the
    tier label until now.
    """
    if n_games >= 6:
        return {"label": "Established (2026 sample)", "css": "tier-high"}
    if n_games >= 3:
        return {"label": "Moderate 2026 sample", "css": "tier-high"}
    if n_games >= 1:
        return {"label": "Building 2026 sample", "css": "tier-mid"}
    # n_games == 0 from here down -- differentiate by career history,
    # since that's real information even with zero 2026 games played.
    if has_career_history and n_career_games >= 20:
        return {"label": "Career baseline (established veteran)", "css": "tier-mid"}
    if has_career_history:
        return {"label": "Career baseline (limited history)", "css": "tier-low"}
    return {"label": "No data (rookie/unproven)", "css": "tier-none"}


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


def _epa_tier(epa_per_play: float | None) -> str:
    """Real thresholds, not arbitrary -- league-average QB EPA/play runs
    roughly 0.00-0.10; elite is 0.25+; a negative value means the offense
    was net-worse off than doing nothing on those dropbacks."""
    if epa_per_play is None:
        return "neutral"
    if epa_per_play >= 0.20:
        return "good"
    if epa_per_play < 0.0:
        return "bad"
    return "neutral"


def _cpoe_tier(cpoe: float | None) -> str:
    """Real CPOE scale -- elite QBs run roughly +3 to +8; poor accuracy
    shows as -3 to -8 or worse. This is the metric that isolates a QB's
    own accuracy from how open his receivers were, per the research this
    was built from."""
    if cpoe is None:
        return "neutral"
    if cpoe >= 3:
        return "good"
    if cpoe <= -3:
        return "bad"
    return "neutral"


def passing_leaders(limit: int = 30) -> list[dict]:
    df = load_stats()
    qb = df.filter(pl.col("position") == "QB").sort("passing_yards", descending=True)
    out = []
    for row in qb.head(limit).iter_rows(named=True):
        n_games = 1  # only week 1 in this snapshot
        attempts = row["attempts"] or 0
        epa_per_play = round(row["passing_epa"] / attempts, 3) if attempts and row.get("passing_epa") is not None else None
        cpoe = round(row["passing_cpoe"], 1) if row.get("passing_cpoe") is not None else None
        pacr = round(row["pacr"], 2) if row.get("pacr") is not None else None
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
            "epa_per_play": epa_per_play,
            "epa_tier": _epa_tier(epa_per_play),
            "cpoe": cpoe,
            "cpoe_tier": _cpoe_tier(cpoe),
            "pacr": pacr,
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


def matchups_for_next_date(limit_games: int = 20) -> dict:
    """
    Real games for the single nearest date with any unplayed game (not a
    whole week) -- mirrors the MLB site's "today's games" behavior. Once
    every game on that date is final, the next build naturally advances
    to the next date with unplayed games, since this always looks at
    load_schedule()'s real result column, never a hardcoded date.

    Returns {"date": "YYYY-MM-DD", "games": [...]}. Each game dict is the
    same shape as before (players, history, projections) plus a
    "kalshi_ticker_fragment" per team so build.py can match Kalshi's
    per-team ticker codes to this game (Kalshi uses "LAR" where our
    schedule data uses "LA" for the Rams -- the one known team-code
    mismatch found live; aliased below rather than assumed to be the
    only one that will ever exist).
    """
    sched = load_schedule()
    unplayed = sched.filter(pl.col("result").is_null()).sort(["gameday"])
    if unplayed.height == 0:
        return {"date": None, "games": []}
    next_date = unplayed["gameday"].min()
    games = unplayed.filter(pl.col("gameday") == next_date)

    stats_2026 = load_stats()
    hist = load_historical_stats()

    def top_players_for_team(team: str) -> list[tuple[str, dict, bool]]:
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
        picks = []
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

    # Known Kalshi <-> nflreadpy team-code mismatches, found live.
    KALSHI_TEAM_ALIAS = {"LA": "LAR"}

    out_games = []
    for g in games.head(limit_games).iter_rows(named=True):
        matchup = {
            "week": g["week"],
            "gameday": g.get("gameday"),
            "gametime": g.get("gametime"),
            "away_team": g["away_team"],
            "home_team": g["home_team"],
            "away_badge": team_badge(g["away_team"]),
            "home_badge": team_badge(g["home_team"]),
            "kalshi_away_code": KALSHI_TEAM_ALIAS.get(g["away_team"], g["away_team"]),
            "kalshi_home_code": KALSHI_TEAM_ALIAS.get(g["home_team"], g["home_team"]),
            "players": [],
        }
        for team, opponent in [(g["away_team"], g["home_team"]), (g["home_team"], g["away_team"])]:
            for stat_col, prow, is_current in top_players_for_team(team):
                hist_line = history_vs_opponent(prow["player_display_name"], stat_col, opponent)
                projection = project_stat(prow["player_display_name"], stat_col)
                matchup["players"].append({
                    "player": prow["player_display_name"],
                    "team": team,
                    "team_badge": team_badge(team),
                    "opponent": opponent,
                    "opponent_badge": team_badge(opponent),
                    "stat_col": stat_col,
                    "stat_label": stat_col.replace("_", " "),
                    "history": hist_line,
                    "based_on_current_season": is_current,
                    "projection": projection,
                })
        out_games.append(matchup)

    return {"date": next_date, "games": out_games}


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


def _get_baseline(player_name: str, stat_col: str) -> tuple[float, str, int]:
    """Shared baseline logic: player's own career average, or league
    position average as fallback. Used by both project_stat() and
    performance_vs_baseline() so the two stay consistent. Now also
    returns the real career game count behind that baseline, since
    "has career history" alone doesn't distinguish a 3-game sample from
    a 45-game one -- both matter for how much a tier label should trust
    the baseline."""
    hist = load_historical_stats()
    career_rows = hist.filter(pl.col("player_display_name") == player_name)
    career_vals = [v for v in career_rows[stat_col].to_list() if v is not None]
    if career_vals:
        return round(sum(career_vals) / len(career_vals), 1), "career (2023-2025)", len(career_vals)
    return _league_baseline(stat_col), "league position average", 0


def project_stat(player_name: str, stat_col: str) -> dict:
    """
    Returns a frozen-at-build-time projection for one player/stat,
    dampened per the constants above. Callers should treat the result as
    immutable once built -- per the brief's "freeze predictions, never
    let them silently drift" principle, this build.py run's projection
    should not be silently recomputed intra-week.
    """
    stats_2026 = load_stats()

    baseline, baseline_source, n_career_games = _get_baseline(player_name, stat_col)

    season_rows = stats_2026.filter(pl.col("player_display_name") == player_name)
    season_vals = [v for v in season_rows[stat_col].to_list() if v is not None]
    n_games = len(season_vals)
    observed = round(sum(season_vals) / n_games, 1) if n_games else baseline

    projected = round(baseline + DAMPEN * (observed - baseline), 1)

    return {
        "projected": projected,
        "baseline": baseline,
        "baseline_source": baseline_source,
        "n_career_games": n_career_games,
        "observed_2026": observed if n_games else None,
        "n_games_2026": n_games,
        "dampen": DAMPEN,
        "min_sample": MIN_SAMPLE,
        "meets_min_sample": n_games >= MIN_SAMPLE,
        "frozen_at": FROZEN_AT,
    }
    # (Old CSV-based log_projection/projections_logged_count removed --
    # replaced by scripts/predictions.py, which freezes ONE record per
    # player/stat/week with the real market line captured at freeze
    # time, instead of appending a new row on every single build/odds
    # refresh. See predictions.py and grade.py for the real grading
    # pipeline this enables.)
