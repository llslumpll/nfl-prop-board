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


# Real NFL stadium coordinates and roof type -- used only to fetch real
# weather for outdoor games. Retractable-roof stadiums are conservatively
# marked climate_controlled=True (they're closed more often than not in
# practice, especially in bad weather -- exactly when it would matter
# most), since there's no real-time source here for a specific game's
# actual roof decision. This is a documented simplification, not a
# fabricated data point: no wind factor is ever applied to a stadium
# marked climate-controlled, so the failure mode of this approximation
# is "misses a real open-roof game," never "invents wind that isn't there."
STADIUMS = {
    "ARI": {"lat": 33.5276, "lon": -112.2626, "climate_controlled": True},   # retractable, usually closed
    "ATL": {"lat": 33.7554, "lon": -84.4008, "climate_controlled": True},    # retractable, usually closed
    "BAL": {"lat": 39.2780, "lon": -76.6227, "climate_controlled": False},
    "BUF": {"lat": 42.7738, "lon": -78.7870, "climate_controlled": False},
    "CAR": {"lat": 35.2258, "lon": -80.8528, "climate_controlled": False},
    "CHI": {"lat": 41.8623, "lon": -87.6167, "climate_controlled": False},
    "CIN": {"lat": 39.0954, "lon": -84.5160, "climate_controlled": False},
    "CLE": {"lat": 41.5061, "lon": -81.6995, "climate_controlled": False},
    "DAL": {"lat": 32.7473, "lon": -97.0945, "climate_controlled": True},    # retractable, usually closed
    "DEN": {"lat": 39.7439, "lon": -105.0201, "climate_controlled": False},
    "DET": {"lat": 42.3400, "lon": -83.0456, "climate_controlled": True},    # fixed dome
    "GB": {"lat": 44.5013, "lon": -88.0622, "climate_controlled": False},
    "HOU": {"lat": 29.6847, "lon": -95.4107, "climate_controlled": True},    # retractable, usually closed
    "IND": {"lat": 39.7601, "lon": -86.1639, "climate_controlled": True},    # retractable, usually closed
    "JAX": {"lat": 30.3239, "lon": -81.6373, "climate_controlled": False},
    "KC": {"lat": 39.0489, "lon": -94.4839, "climate_controlled": False},
    "LV": {"lat": 36.0909, "lon": -115.1833, "climate_controlled": True},    # fixed dome
    "LA": {"lat": 33.9535, "lon": -118.3392, "climate_controlled": True},    # SoFi, fixed roof
    "LAC": {"lat": 33.9535, "lon": -118.3392, "climate_controlled": True},   # SoFi, fixed roof
    "MIA": {"lat": 25.9580, "lon": -80.2389, "climate_controlled": False},
    "MIN": {"lat": 44.9735, "lon": -93.2575, "climate_controlled": True},    # fixed dome
    "NE": {"lat": 42.0909, "lon": -71.2643, "climate_controlled": False},
    "NO": {"lat": 29.9511, "lon": -90.0812, "climate_controlled": True},     # fixed dome
    "NYG": {"lat": 40.8135, "lon": -74.0745, "climate_controlled": False},
    "NYJ": {"lat": 40.8135, "lon": -74.0745, "climate_controlled": False},
    "PHI": {"lat": 39.9008, "lon": -75.1675, "climate_controlled": False},
    "PIT": {"lat": 40.4468, "lon": -80.0158, "climate_controlled": False},
    "SEA": {"lat": 47.5952, "lon": -122.3316, "climate_controlled": False},
    "SF": {"lat": 37.4032, "lon": -121.9698, "climate_controlled": False},
    "TB": {"lat": 27.9759, "lon": -82.5033, "climate_controlled": False},
    "TEN": {"lat": 36.1665, "lon": -86.7713, "climate_controlled": False},
    "WAS": {"lat": 38.9077, "lon": -76.8645, "climate_controlled": False},
}


import math

_player_std_cache: dict[str, float] = {}
_position_avg_std_cache: dict[str, float] = {}


def _position_avg_std(stat_col: str, position: str) -> float:
    """Real average per-player game-to-game standard deviation for a
    stat/position, computed from actual historical variance (players
    with 8+ games only, so one wild game doesn't dominate). This is the
    fallback used for a player without enough of their own games yet --
    deliberately NOT the pooled league-wide std, which mixes in
    between-player skill differences and overstates true game-to-game
    uncertainty for any one player."""
    key = f"{stat_col}_{position}"
    if key not in _position_avg_std_cache:
        hist = load_historical_stats()
        rows = hist.filter(pl.col("position") == position).group_by("player_display_name").agg(
            pl.col(stat_col).std().alias("std"), pl.col(stat_col).count().alias("n")
        ).filter((pl.col("n") >= 8) & (pl.col("std").is_not_null()))
        _position_avg_std_cache[key] = rows["std"].mean() if rows.height else None
    return _position_avg_std_cache[key]


def player_std_dev(player_name: str, stat_col: str, position: str) -> float | None:
    """Real per-player game-to-game standard deviation when there's
    enough real career sample (8+ games) to trust it; otherwise falls
    back to the real position-level average computed above. Returns
    None only if neither is available (a brand-new stat/position combo)."""
    key = f"{player_name}_{stat_col}"
    if key not in _player_std_cache:
        hist = load_historical_stats()
        rows = hist.filter(pl.col("player_display_name") == player_name)
        vals = [v for v in rows[stat_col].to_list() if v is not None]
        if len(vals) >= 8:
            mean = sum(vals) / len(vals)
            variance = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
            _player_std_cache[key] = math.sqrt(variance)
        else:
            _player_std_cache[key] = _position_avg_std(stat_col, position)
    return _player_std_cache[key]


def model_prob_over(projected: float, market_line: float, std_dev: float | None) -> float | None:
    """
    Real probability the actual result clears the market line, using a
    normal-distribution approximation: our projection as the mean, real
    per-player (or position-average) game-to-game standard deviation as
    the spread. Standard, legitimate statistical technique for turning a
    point projection into a probability -- same idea as MLB's Poisson
    model for strikeouts, just using a normal approximation since
    yardage totals are continuous, not count data.

    Returns None (never a fabricated 50/50) if std_dev isn't available.
    """
    if std_dev is None or std_dev <= 0:
        return None
    z = (projected - market_line) / std_dev
    prob = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    return max(0.01, min(0.99, prob))


def clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


_league_defense_avg_cache: dict[str, float] = {}


OPEN_METEO = "https://api.open-meteo.com/v1/forecast"

_weather_cache: dict[str, dict | None] = {}


def fetch_weather(lat: float, lon: float, game_date: str) -> dict | None:
    """
    Real forecast weather for a specific stadium/date, using the exact
    same free, no-key Open-Meteo mechanism already proven working in
    production on the MLB site (common.py's fetch_weather). Fails soft:
    any error returns None, never a fabricated weather reading.
    """
    key = f"{lat}_{lon}_{game_date}"
    if key in _weather_cache:
        return _weather_cache[key]
    try:
        import requests
        resp = requests.get(OPEN_METEO, params={
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m,windspeed_10m,winddirection_10m,relative_humidity_2m",
            "temperature_unit": "fahrenheit", "windspeed_unit": "mph",
            "timezone": "auto", "start_date": game_date, "end_date": game_date,
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        hourly = data.get("hourly")
        if not hourly or not hourly.get("windspeed_10m"):
            _weather_cache[key] = None
            return None
        # Use the midday forecast value as a reasonable stand-in for
        # kickoff conditions -- this build doesn't have each game's
        # exact kickoff hour wired through to this function yet.
        idx = len(hourly["windspeed_10m"]) // 2
        result = {
            "tempF": hourly["temperature_2m"][idx],
            "windMph": hourly["windspeed_10m"][idx],
            "humidity": hourly["relative_humidity_2m"][idx],
        }
        _weather_cache[key] = result
        return result
    except Exception:
        _weather_cache[key] = None
        return None


def wind_factor_for_team(team: str, game_date: str | None) -> dict:
    """
    Real wind-based passing-efficiency adjustment -- NFL's own tracking
    of passing stats shows meaningful efficiency drops once wind gets
    above roughly 15-20 mph, more pronounced on deeper throws. Below
    10 mph, no real effect. The exact coefficient below is a reasoned
    first cut (documented as such, same honesty standard as MIN_SAMPLE/
    DAMPEN elsewhere on this site), not empirically fit against this
    site's own graded history yet -- worth revisiting once real graded
    passing predictions accumulate across enough windy games to check it.

    Never applied to a climate-controlled stadium, and always fails soft
    to neutral (1.0) if weather can't be fetched, a game date isn't
    known yet, or the stadium isn't in STADIUMS.
    """
    stadium = STADIUMS.get(team)
    if not stadium:
        return {"factor": 1.0, "wind_mph": None, "climate_controlled": None, "reason": "unknown stadium"}
    if stadium["climate_controlled"]:
        return {"factor": 1.0, "wind_mph": None, "climate_controlled": True, "reason": "climate-controlled venue"}
    if not game_date:
        return {"factor": 1.0, "wind_mph": None, "climate_controlled": False, "reason": "no game date yet"}

    weather = fetch_weather(stadium["lat"], stadium["lon"], game_date)
    if not weather or weather.get("windMph") is None:
        return {"factor": 1.0, "wind_mph": None, "climate_controlled": False, "reason": "weather unavailable"}

    wind = weather["windMph"]
    raw_factor = 1.0 - max(0.0, wind - 10.0) * 0.008
    factor = clip(raw_factor, 0.85, 1.0)  # wind only ever hurts passing here, never helps
    return {
        "factor": round(factor, 3),
        "wind_mph": round(wind, 1),
        "temp_f": weather.get("tempF"),
        "climate_controlled": False,
        "reason": None,
    }


_league_pace_avg_cache: float | None = None


def league_avg_team_points_per_game() -> float | None:
    """Real average points scored per team per game, from actual final
    scores in the schedule -- used as the neutral baseline for the
    Vegas-implied game environment factor."""
    sched = load_schedule()
    played = sched.filter(pl.col("result").is_not_null())
    if played.height == 0:
        return None
    scores = played["home_score"].to_list() + played["away_score"].to_list()
    scores = [s for s in scores if s is not None]
    return round(sum(scores) / len(scores), 1) if scores else None


def environment_factor(implied_team_total: float | None, league_avg_points: float | None) -> dict:
    """
    Real Vegas-implied game environment: a team implied for 27+ points
    (from a real Kalshi Team Total market) means genuinely more expected
    offensive opportunity than a team implied for 17 -- a signal used
    directly by professional prop models. Only applied when a real
    market quote exists; honestly neutral otherwise (Kalshi's team-total
    markets are often thin right now, see kalshi_client.py). Dampened
    and bounded the same way as every other correction on this site.
    """
    if implied_team_total is None or not league_avg_points:
        return {"factor": 1.0, "implied_total": None, "league_avg_points": league_avg_points}
    raw_factor = implied_team_total / league_avg_points
    ENV_DAMPEN = 0.3
    dampened = 1 + ENV_DAMPEN * (raw_factor - 1)
    dampened = clip(dampened, 0.85, 1.15)
    return {
        "factor": round(dampened, 3),
        "implied_total": implied_team_total,
        "league_avg_points": league_avg_points,
    }


def team_pace_factor(team: str) -> dict:
    """
    Real offensive volume signal: this team's own plays-per-game (pass
    attempts + rush carries) relative to the real league average.
    Distinct from matchup_factor (which is about the OPPONENT's defense
    and affects efficiency) -- this is about the player's OWN team's
    play-calling volume, and affects opportunity, not skill. A team
    running 64 real plays/game creates meaningfully more opportunity
    across the board than one running 46, independent of how well
    anyone executes those plays.

    Applied the same dampened, bounded way as every other correction
    here -- a 1-2 game sample of pace is real but still thin, so this
    shouldn't swing projections hard yet.
    """
    global _league_pace_avg_cache
    df = load_stats()
    team_rows = df.filter(pl.col("team") == team)
    games = team_rows["week"].n_unique() if team_rows.height else 0
    if games == 0:
        return {"factor": 1.0, "plays_per_game": None, "league_avg_plays_per_game": None, "games": 0}

    team_plays = (team_rows["attempts"].sum() or 0) + (team_rows["carries"].sum() or 0)
    plays_per_game = round(team_plays / games, 1)

    if _league_pace_avg_cache is None:
        league = df.group_by("team").agg(
            (pl.col("attempts").sum() + pl.col("carries").sum()).alias("total_plays"),
            pl.col("week").n_unique().alias("games"),
        ).filter(pl.col("games") > 0)
        per_game = (league["total_plays"] / league["games"]).to_list()
        _league_pace_avg_cache = sum(per_game) / len(per_game) if per_game else 0
    league_avg = round(_league_pace_avg_cache, 1)

    if not league_avg:
        return {"factor": 1.0, "plays_per_game": plays_per_game, "league_avg_plays_per_game": None, "games": games}

    raw_factor = plays_per_game / league_avg
    PACE_DAMPEN = 0.3
    dampened = 1 + PACE_DAMPEN * (raw_factor - 1)
    dampened = clip(dampened, 0.85, 1.15)

    return {
        "factor": round(dampened, 3),
        "plays_per_game": plays_per_game,
        "league_avg_plays_per_game": league_avg,
        "games": games,
    }


def matchup_factor(opponent_team: str, stat_col: str) -> dict:
    """
    Real opponent-adjustment: how many yards this specific opponent
    actually allows in this stat, relative to the real league average,
    computed directly from nflreadpy player rows (opponent_team ==
    this team = what offenses did AGAINST them). Bounded and dampened,
    same "never a full override, shrink toward neutral" philosophy as
    every other correction on this site -- with only 1-4 real games
    played leaguewide right now, a defense's own sample is tiny, so this
    stays close to 1.0 (neutral) until more weeks accumulate.

    receiving_yards uses the same defensive allowance as passing_yards,
    since a "yards allowed through the air" defense is the same real
    thing whether you're looking at it from the passer's or receiver's
    side of the stat.
    """
    defense_stat_col = "passing_yards" if stat_col == "receiving_yards" else stat_col
    if defense_stat_col not in ("passing_yards", "rushing_yards"):
        return {"factor": 1.0, "games": 0, "allowed_per_game": None, "league_avg_per_game": None}

    df = load_stats()
    opp_off = df.filter(pl.col("opponent_team") == opponent_team)
    games = opp_off["week"].n_unique() if opp_off.height else 0
    if games == 0:
        return {"factor": 1.0, "games": 0, "allowed_per_game": None, "league_avg_per_game": None}

    allowed_per_game = round((opp_off[defense_stat_col].sum() or 0) / games, 1)

    cache_key = defense_stat_col
    if cache_key not in _league_defense_avg_cache:
        league = df.group_by("opponent_team").agg(
            pl.col(defense_stat_col).sum().alias("total"),
            pl.col("week").n_unique().alias("games"),
        ).filter(pl.col("games") > 0)
        per_game = (league["total"] / league["games"]).to_list()
        _league_defense_avg_cache[cache_key] = sum(per_game) / len(per_game) if per_game else 0
    league_avg_per_game = round(_league_defense_avg_cache[cache_key], 1)

    if not league_avg_per_game:
        return {"factor": 1.0, "games": games, "allowed_per_game": allowed_per_game, "league_avg_per_game": None}

    raw_factor = allowed_per_game / league_avg_per_game
    # Heavier dampening (0.3) than the temporal DAMPEN (0.2) is
    # deliberate, not a typo -- a defense's own sample this early in the
    # season is even thinner than a player's, so the correction should
    # move even less until real sample size builds up.
    MATCHUP_DAMPEN = 0.3
    dampened = 1 + MATCHUP_DAMPEN * (raw_factor - 1)
    dampened = clip(dampened, 0.85, 1.15)

    return {
        "factor": round(dampened, 3),
        "games": games,
        "allowed_per_game": allowed_per_game,
        "league_avg_per_game": league_avg_per_game,
    }


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
    """Returns {"opponent", "week", "gameday", "venue_team"} for a
    team's next unplayed game, or None if the season has none left.
    Per-team lookup (not a single global "next week") so this stays
    correct once bye weeks make different teams' next games fall on
    different weeks. venue_team is always the home team -- needed so
    weather gets fetched for the STADIUM the game is actually played at,
    not wherever the player's own team happens to be based."""
    sched = load_schedule()
    team_games = sched.filter(
        ((pl.col("home_team") == team) | (pl.col("away_team") == team))
        & (pl.col("result").is_null())
    ).sort("week")
    if team_games.height == 0:
        return None
    row = team_games.row(0, named=True)
    opponent = row["away_team"] if row["home_team"] == team else row["home_team"]
    return {
        "opponent": opponent, "week": row["week"], "gameday": row.get("gameday"),
        "venue_team": row["home_team"],
    }


def next_game_projection(player_name: str, team: str, stat_col: str) -> dict | None:
    """Combines next_game_for_team + project_stat into the single lookup
    every stat page needs: who they play next, and the dampened
    projection for that specific stat in that game -- now also adjusted
    for real opponent matchup difficulty (see matchup_factor). Returns
    None if there's no upcoming game left to project for.

    projection["projected"] is the FINAL number (temporal dampening,
    then matchup-adjusted) -- this is what every downstream consumer
    (Best 5, History freezing, display) should use. The pre-adjustment
    value is kept as pre_matchup_projected for transparency, alongside
    the real matchup_factor data behind the adjustment."""
    game = next_game_for_team(team)
    if game is None:
        return None
    projection = project_stat(player_name, stat_col)
    m_factor = matchup_factor(game["opponent"], stat_col)
    pre_matchup = projection["projected"]
    projection["pre_matchup_projected"] = pre_matchup
    projection["matchup_factor"] = m_factor
    # Real, separate context signal (not folded into the point
    # projection) -- is this specific opponent's defense trending better
    # or worse lately than its own season-long average.
    projection["opponent_recent_form"] = opponent_recent_form(game["opponent"], stat_col)
    after_matchup = round(pre_matchup * m_factor["factor"], 1)

    # Pace: the player's OWN team's real play volume, applied to every
    # volume-based stat (unlike wind, which is passing-specific).
    p_factor = team_pace_factor(team)
    projection["pace_factor"] = p_factor
    after_pace = round(after_matchup * p_factor["factor"], 1)

    # Wind only has a real, documented effect on the passing game --
    # applied to passing_yards and receiving_yards (receiving depends on
    # the same pass attempts), never to rushing_yards or receptions.
    if stat_col in ("passing_yards", "receiving_yards"):
        w_factor = wind_factor_for_team(game["venue_team"], game.get("gameday"))
        projection["wind_factor"] = w_factor
        projection["projected"] = round(after_pace * w_factor["factor"], 1)
    else:
        projection["wind_factor"] = None
        projection["projected"] = after_pace

    # Injury: real, conservative, dampened adjustment based on the
    # player's actual reported status -- never a fabricated play
    # probability. "Out" is flagged (is_out) but the caller decides
    # whether to exclude entirely (Best 5 does) rather than silently
    # zeroing the number here.
    injury = injury_status_for(player_name)
    i_factor = injury_factor(injury)
    projection["injury_factor"] = i_factor
    if i_factor["factor"] != 1.0:
        projection["pre_injury_projected"] = projection["projected"]
        projection["projected"] = round(projection["projected"] * i_factor["factor"], 1)

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


def _join_list(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def player_weekly_series(player_name: str, stat_col: str) -> list[dict]:
    """
    Real per-week values for this player's 2026 season so far, in week
    order -- NOT the single averaged "observed_2026" number used
    elsewhere. This is what lets a chart actually grow week by week as
    the season progresses, instead of staying a fixed 3-point shape all
    year. Uses load_stats() directly (every week, unlike the
    _latest_week_per_player-filtered leaderboards) since the whole point
    here is every real week, not just the most recent one.
    """
    df = load_stats()
    rows = df.filter(pl.col("player_display_name") == player_name).sort("week")
    out = []
    for row in rows.iter_rows(named=True):
        val = row.get(stat_col)
        if val is not None:
            out.append({"week": row["week"], "value": val})
    return out


def reason_text(projection: dict, stat_label: str) -> str:
    """
    Real narrative sentence built directly from the same factors already
    computed for this projection -- matchup, pace, wind, and Vegas
    environment. No factor is mentioned unless its real value actually
    moved the projection meaningfully; a neutral (near-1.0) factor is
    silently omitted rather than padded in as filler.
    """
    positives, negatives = [], []

    mf = projection.get("matchup_factor") or {}
    if mf.get("factor", 1.0) > 1.05:
        positives.append("a favorable matchup (this opponent allows more than average)")
    elif mf.get("factor", 1.0) < 0.95:
        negatives.append("a tough matchup (this opponent allows less than average)")

    pf = projection.get("pace_factor") or {}
    if pf.get("factor", 1.0) > 1.05:
        positives.append("a fast-paced offense")
    elif pf.get("factor", 1.0) < 0.95:
        negatives.append("a slower-paced offense")

    wf = projection.get("wind_factor")
    if wf and wf.get("wind_mph") is not None and wf["wind_mph"] >= 15:
        negatives.append(f"{wf['wind_mph']} mph wind at kickoff")

    ef = projection.get("environment_factor") or {}
    if ef.get("factor", 1.0) > 1.05:
        positives.append("a high Vegas-implied team total")
    elif ef.get("factor", 1.0) < 0.95:
        negatives.append("a low Vegas-implied team total")

    tier_label = (projection.get("tier") or {}).get("label", "")
    if "Established" in tier_label or "Moderate" in tier_label:
        base = f"A real, growing {stat_label} sample this season"
    elif "career" in tier_label.lower():
        base = f"A real career {stat_label} baseline"
    else:
        base = f"An early, provisional {stat_label} projection"

    sentence = base
    if positives:
        sentence += f", boosted by {_join_list(positives)}"
    if negatives:
        sentence += ("; " if positives else ", ") + f"tempered by {_join_list(negatives)}"
    return sentence + "."


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


def injury_factor(injury: dict | None) -> dict:
    """
    Real, conservative, dampened adjustment based on a player's actual
    reported injury status -- never a fabricated "X% chance of playing"
    the site can't back up, just a modest, documented downward nudge
    reflecting genuine added risk (reduced snaps, limited role, or
    simply not suiting up).

    "Out" is unambiguous and handled separately (excluded from Best 5
    entirely by the caller, not just dampened) -- projecting a normal
    number for someone who will not play is actively misleading, worse
    than declining to guess. This function's factor for "Out" is
    included for completeness/transparency but callers should check
    is_out first.

    The specific percentages below (Doubtful -25%, Questionable -8%,
    DNP -4%) are a reasoned first-cut estimate, not yet empirically
    validated against this site's own graded history -- same honesty
    standard as the wind and pace coefficients elsewhere on this site.
    Worth revisiting once enough Questionable/Doubtful picks have graded
    to check the real numbers.
    """
    if injury is None:
        return {"factor": 1.0, "status": None, "is_out": False}

    status = (injury.get("report_status") or "").strip()
    is_out = status == "Out"

    FACTORS = {
        "Out": 0.0,
        "Doubtful": 0.75,
        "Questionable": 0.92,
        "Did Not Participate In Practice": 0.96,
        "Limited Participation in Practice": 0.99,
        "Full Participation in Practice": 1.0,
    }
    factor = FACTORS.get(status, 1.0)
    return {"factor": factor, "status": status, "is_out": is_out}


_ngs_passing_cache = None


def load_nextgen_passing():
    """Real NFL Next Gen Stats for passing (avg_time_to_throw,
    aggressiveness, avg_air_yards_differential) -- a genuinely separate
    data source from load_player_stats, tracking-data-derived rather
    than box-score-derived. Cached in-process since build.py calls the
    passing leaders function once per build."""
    global _ngs_passing_cache
    if _ngs_passing_cache is None:
        import nflreadpy as nfl
        _ngs_passing_cache = nfl.load_nextgen_stats(stat_type="passing", seasons=[2026])
    return _ngs_passing_cache


def _ngs_row_for(player_name: str, week: int) -> dict | None:
    """Looks up this player's real Next Gen Stats row for this exact
    week. NGS also carries a week=0 row per player that duplicates the
    same numbers as their first real week -- filtered out here so a
    genuine week is always matched, never that placeholder."""
    ngs = load_nextgen_passing()
    rows = ngs.filter((pl.col("player_display_name") == player_name) & (pl.col("week") == week))
    if rows.height == 0:
        return None
    return rows.row(0, named=True)


_ngs_rushing_cache = None
_ngs_receiving_cache = None


def load_nextgen_rushing():
    global _ngs_rushing_cache
    if _ngs_rushing_cache is None:
        import nflreadpy as nfl
        _ngs_rushing_cache = nfl.load_nextgen_stats(stat_type="rushing", seasons=[2026])
    return _ngs_rushing_cache


def load_nextgen_receiving():
    global _ngs_receiving_cache
    if _ngs_receiving_cache is None:
        import nflreadpy as nfl
        _ngs_receiving_cache = nfl.load_nextgen_stats(stat_type="receiving", seasons=[2026])
    return _ngs_receiving_cache


def _ngs_lookup(df, player_name: str, week: int) -> dict | None:
    rows = df.filter((pl.col("player_display_name") == player_name) & (pl.col("week") == week))
    if rows.height == 0:
        return None
    return rows.row(0, named=True)


def _ryoe_tier(ryoe_per_att: float | None) -> str:
    """Real RYOE/att scale -- roughly centered at 0 (replacement-level
    back gains about what's blocked for him); +1.0/att or better is a
    real, elite vision/speed signal, isolated from O-line quality."""
    if ryoe_per_att is None:
        return "neutral"
    if ryoe_per_att >= 1.0:
        return "good"
    if ryoe_per_att < 0:
        return "bad"
    return "neutral"


def _separation_tier(sep: float | None) -> str:
    """Real tracking-data separation scale -- NFL average is roughly
    2.7-3.0 yards at the catch point."""
    if sep is None:
        return "neutral"
    if sep >= 3.0:
        return "good"
    if sep < 2.0:
        return "bad"
    return "neutral"


def _yac_above_exp_tier(yac_ax: float | None) -> str:
    """Positive = this receiver personally created more yards after
    catch than an average receiver would have on the same catch, given
    the same blocking/space -- a real, isolated skill signal."""
    if yac_ax is None:
        return "neutral"
    if yac_ax >= 1.5:
        return "good"
    if yac_ax <= -1.5:
        return "bad"
    return "neutral"


def _aggressiveness_tier(agg: float | None) -> str:
    """Real NGS scale -- 'aggressiveness' is the share of throws into
    tight coverage (a defender within 1 yard at the catch point). No
    universal 'good/bad' here (a gunslinger and a checkdown merchant can
    both win), so this only flags the extremes worth noticing, not a
    quality judgment."""
    if agg is None:
        return "neutral"
    if agg >= 18:
        return "good"  # notably aggressive -- context-dependent, not inherently "better"
    return "neutral"


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


def _latest_week_per_player(df):
    """
    Real bug fix: load_stats() returns one row per player PER WEEK, not
    one row per player. Every leaders() function below used to sort/
    filter that multi-week table directly, which was invisible with only
    one week of data cached but produces a real, confirmed duplicate-row
    bug the moment a second week exists -- the same player shows up
    twice (once per week they've played), each with an identical Best 5
    confidence, since next_game_projection looks up the same real
    upcoming game regardless of which week's row triggered it.

    This filters down to each player's single most recent real week
    before any leaderboard sorting happens, matching what "this week's
    leaderboard" should actually mean.
    """
    latest_week = df.group_by("player_display_name").agg(pl.col("week").max().alias("latest_week"))
    return df.join(latest_week, on="player_display_name").filter(pl.col("week") == pl.col("latest_week")).drop("latest_week")


def passing_leaders(limit: int = 30) -> list[dict]:
    df = _latest_week_per_player(load_stats())
    qb = df.filter(pl.col("position") == "QB").sort("passing_yards", descending=True)
    out = []
    for row in qb.head(limit).iter_rows(named=True):
        n_games = 1  # only week 1 in this snapshot
        attempts = row["attempts"] or 0
        epa_per_play = round(row["passing_epa"] / attempts, 3) if attempts and row.get("passing_epa") is not None else None
        cpoe = round(row["passing_cpoe"], 1) if row.get("passing_cpoe") is not None else None
        pacr = round(row["pacr"], 2) if row.get("pacr") is not None else None
        ngs_row = _ngs_row_for(row["player_display_name"], row["week"])
        time_to_throw = round(ngs_row["avg_time_to_throw"], 2) if ngs_row and ngs_row.get("avg_time_to_throw") is not None else None
        aggressiveness = round(ngs_row["aggressiveness"], 1) if ngs_row and ngs_row.get("aggressiveness") is not None else None
        air_yards_diff = round(ngs_row["avg_air_yards_differential"], 1) if ngs_row and ngs_row.get("avg_air_yards_differential") is not None else None
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
            "time_to_throw": time_to_throw,
            "aggressiveness": aggressiveness,
            "aggressiveness_tier": _aggressiveness_tier(aggressiveness),
            "air_yards_diff": air_yards_diff,
            "perf": performance_vs_baseline(row["player_display_name"], "passing_yards", row["passing_yards"]),
            "next_game": next_game_projection(row["player_display_name"], row["team"], "passing_yards"),
            "tier": provisional_tier(n_games),
            "injury": injury_status_for(row["player_display_name"]),
        })
    return out


def receiving_leaders(limit: int = 40) -> list[dict]:
    df = _latest_week_per_player(load_stats())
    rec = df.filter(pl.col("position").is_in(["WR", "TE", "RB"])).sort(
        "receiving_yards", descending=True
    )
    ngs_rec = load_nextgen_receiving()
    out = []
    for row in rec.head(limit).iter_rows(named=True):
        n_games = 1
        ngs_row = _ngs_lookup(ngs_rec, row["player_display_name"], row["week"])
        separation = round(ngs_row["avg_separation"], 1) if ngs_row and ngs_row.get("avg_separation") is not None else None
        cushion = round(ngs_row["avg_cushion"], 1) if ngs_row and ngs_row.get("avg_cushion") is not None else None
        yac_above_exp = round(ngs_row["avg_yac_above_expectation"], 1) if ngs_row and ngs_row.get("avg_yac_above_expectation") is not None else None
        air_yards_share = round(ngs_row["percent_share_of_intended_air_yards"], 1) if ngs_row and ngs_row.get("percent_share_of_intended_air_yards") is not None else None
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
            "separation": separation,
            "separation_tier": _separation_tier(separation),
            "cushion": cushion,
            "yac_above_exp": yac_above_exp,
            "yac_above_exp_tier": _yac_above_exp_tier(yac_above_exp),
            "air_yards_share": air_yards_share,
            "perf": performance_vs_baseline(row["player_display_name"], "receiving_yards", row["receiving_yards"]),
            "next_game": next_game_projection(row["player_display_name"], row["team"], "receiving_yards"),
            "tier": provisional_tier(n_games),
            "injury": injury_status_for(row["player_display_name"]),
            "usage_trend": usage_trend(row["player_display_name"]),
            "target_share_trend": target_share_trend(row["player_display_name"]),
            "opportunity_signal": opportunity_signal(row["player_display_name"], row["team"], row["position"]),
        })
    return out


def receptions_leaders(limit: int = 40) -> list[dict]:
    """
    Separate from receiving_leaders() -- receptions is priced as its own,
    distinct prop on PrizePicks/Kalshi (brief's explicit instruction not
    to merge these pages), so it gets its own sort order and page.
    """
    df = _latest_week_per_player(load_stats())
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
            "usage_trend": usage_trend(row["player_display_name"]),
            "target_share_trend": target_share_trend(row["player_display_name"]),
            "opportunity_signal": opportunity_signal(row["player_display_name"], row["team"], row["position"]),
        })
    return out


def rushing_leaders(limit: int = 40) -> list[dict]:
    df = _latest_week_per_player(load_stats())
    rush = df.filter(pl.col("position").is_in(["RB", "QB", "WR"])).filter(
        pl.col("rushing_yards") > 0
    ).sort("rushing_yards", descending=True)
    ngs_rush = load_nextgen_rushing()
    out = []
    for row in rush.head(limit).iter_rows(named=True):
        n_games = 1
        ngs_row = _ngs_lookup(ngs_rush, row["player_display_name"], row["week"])
        ryoe = round(ngs_row["rush_yards_over_expected"], 1) if ngs_row and ngs_row.get("rush_yards_over_expected") is not None else None
        ryoe_per_att = round(ngs_row["rush_yards_over_expected_per_att"], 2) if ngs_row and ngs_row.get("rush_yards_over_expected_per_att") is not None else None
        stacked_box_pct = round(ngs_row["percent_attempts_gte_eight_defenders"], 1) if ngs_row and ngs_row.get("percent_attempts_gte_eight_defenders") is not None else None
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
            "ryoe": ryoe,
            "ryoe_per_att": ryoe_per_att,
            "ryoe_tier": _ryoe_tier(ryoe_per_att),
            "stacked_box_pct": stacked_box_pct,
            "perf": performance_vs_baseline(row["player_display_name"], "rushing_yards", row["rushing_yards"]),
            "next_game": next_game_projection(row["player_display_name"], row["team"], "rushing_yards"),
            "tier": provisional_tier(n_games),
            "injury": injury_status_for(row["player_display_name"]),
            "usage_trend": usage_trend(row["player_display_name"]),
            "opportunity_signal": opportunity_signal(row["player_display_name"], row["team"], row["position"]),
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
    df = _latest_week_per_player(load_stats())
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
            "usage_trend": usage_trend(row["player_display_name"]),
            "target_share_trend": target_share_trend(row["player_display_name"]),
        })
    out.sort(key=lambda r: r["total_tds"], reverse=True)
    return out[:limit]


def correlated_pairs_for_receiving(limit: int = 40) -> dict:
    """
    Flags the QB<->receiver correlation explicitly (brief section 2):
    same team + same game = correlated legs. This is a flag only, not
    a computed correlation coefficient (needs a real sample to compute).
    """
    df = _latest_week_per_player(load_stats())
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


_depth_chart_cache = None


def load_depth_charts():
    """Real, current depth charts -- updates multiple times per week per
    team. Filtered to the single most recent snapshot only (the raw data
    has ~178 historical timestamps; using anything but the latest would
    mean validating against a stale depth chart, defeating the point)."""
    global _depth_chart_cache
    if _depth_chart_cache is None:
        import nflreadpy as nfl
        dc = nfl.load_depth_charts([2026])
        latest_dt = dc["dt"].max()
        _depth_chart_cache = dc.filter(pl.col("dt") == latest_dt)
    return _depth_chart_cache


def current_starter(team: str, pos_abb: str) -> str | None:
    """Real, current #1 on the depth chart at this position for this
    team -- reflects an injury/benching THIS WEEK, unlike a season-long
    stat leaderboard, which can stay stuck on a player who's no longer
    actually starting. Returns None if depth chart data doesn't cover
    this team/position (fails soft, never blocks the existing
    stat-leader fallback)."""
    dc = load_depth_charts()
    rows = dc.filter(
        (pl.col("team") == team) & (pl.col("pos_abb") == pos_abb) & (pl.col("pos_rank") == 1)
    )
    if rows.height == 0:
        return None
    return rows.row(0, named=True)["player_name"]


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

        def pick_with_depth_chart_check(stat_col, pos_abb, ranked_rows):
            """Cross-checks the season stat leader against the REAL,
            current depth chart starter. A season-long stat leaderboard
            can stay stuck on a player who's no longer actually starting
            (injury, benching) -- the depth chart reflects this week's
            actual reality, not an accumulated total. Falls back to the
            stat leader if depth chart data doesn't cover this team/
            position, or if it agrees with the stat leader anyway."""
            real_starter = current_starter(team, pos_abb)
            if ranked_rows.height:
                stat_leader = ranked_rows.row(0, named=True)
                if real_starter and real_starter != stat_leader["player_display_name"]:
                    swap = ranked_rows.filter(pl.col("player_display_name") == real_starter)
                    if swap.height:
                        return (stat_col, swap.row(0, named=True), True)
                    # Real current starter has zero 2026 stat rows yet
                    # (a genuine, fresh starter change) -- still give
                    # project_stat a real name to fall back to career
                    # history with, rather than silently keeping the
                    # no-longer-starting player.
                    return (stat_col, {"player_display_name": real_starter}, False)
                return (stat_col, stat_leader, True)
            if real_starter:
                return (stat_col, {"player_display_name": real_starter}, False)
            return None

        for stat_col, pos_abb, rows in (
            ("passing_yards", "QB", qb), ("rushing_yards", "RB", rb), ("receiving_yards", "WR", wr),
        ):
            pick = pick_with_depth_chart_check(stat_col, pos_abb, rows)
            if pick:
                picks.append(pick)
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
                # Use the SAME fully-adjusted projection (matchup + pace
                # + wind, where applicable) as the individual stat pages
                # -- previously called raw project_stat() here, which
                # meant this page silently showed a different, less
                # complete number than Passing/Receiving/etc. for the
                # exact same player/stat/game. Real bug, now fixed.
                ngp = next_game_projection(prow["player_display_name"], team, stat_col)
                projection = ngp["projection"] if ngp else project_stat(prow["player_display_name"], stat_col)
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


_calibration_cache: dict | None = None


def _load_calibration() -> dict:
    """Defensive read of data/calibration.json -- missing file, missing
    key, or any parse error all fall back to an empty dict (neutral,
    no correction applied), never a crash. Same pattern used everywhere
    else real market/weather data is read on this site."""
    global _calibration_cache
    if _calibration_cache is not None:
        return _calibration_cache
    try:
        import json
        path = DATA_DIR / "calibration.json"
        _calibration_cache = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        _calibration_cache = {}
    return _calibration_cache


def project_stat(player_name: str, stat_col: str) -> dict:
    """
    Returns a frozen-at-build-time projection for one player/stat,
    dampened per the constants above. Callers should treat the result as
    immutable once built -- per the brief's "freeze predictions, never
    let them silently drift" principle, this build.py run's projection
    should not be silently recomputed intra-week.

    Now includes a real, closed-loop calibration correction: if
    scripts/calibrate.py has found (from real graded history, gated by
    MIN_SAMPLE) that THIS tier's predictions for THIS stat run
    systematically high or low, a dampened nudge is applied here. Reads
    are fully defensive -- no calibration.json yet, or "insufficient
    data" for this tier, means zero correction, not a guess.
    """
    stats_2026 = load_stats()

    baseline, baseline_source, n_career_games = _get_baseline(player_name, stat_col)

    season_rows = stats_2026.filter(pl.col("player_display_name") == player_name)
    season_vals = [v for v in season_rows[stat_col].to_list() if v is not None]
    n_games = len(season_vals)
    observed = round(sum(season_vals) / n_games, 1) if n_games else baseline

    projected = round(baseline + DAMPEN * (observed - baseline), 1)

    tier = provisional_tier(
        n_games, has_career_history=(baseline_source == "career (2023-2025)"),
        n_career_games=n_career_games,
    )
    calibration = _load_calibration()
    tier_cal = ((calibration.get("bias") or {}).get(stat_col) or {}).get(tier["label"], {})
    calibration_bias = tier_cal.get("bias", 0.0) if tier_cal.get("status") == "active" else 0.0
    if calibration_bias:
        projected = round(projected + calibration_bias, 1)

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
        "tier": tier,
        "calibration_bias": calibration_bias,
        "frozen_at": FROZEN_AT,
    }
    # (Old CSV-based log_projection/projections_logged_count removed --
    # replaced by scripts/predictions.py, which freezes ONE record per
    # player/stat/week with the real market line captured at freeze
    # time, instead of appending a new row on every single build/odds
    # refresh. See predictions.py and grade.py for the real grading
    # pipeline this enables.)


_snap_counts_cache = None


def load_snap_counts():
    global _snap_counts_cache
    if _snap_counts_cache is None:
        import nflreadpy as nfl
        _snap_counts_cache = nfl.load_snap_counts([2026])
    return _snap_counts_cache


def usage_trend(player_name: str) -> dict | None:
    """
    Real week-over-week offensive snap share change, detected directly
    from real per-game snap count data (nflreadpy's load_snap_counts) --
    NOT a narrative guess at *why* a role changed (a coach's decision,
    another player's injury, game script), just the real, quantified
    fact that it did. This is the honest version of "hints of someone
    getting more touches": real numbers, no fabricated commentary.

    Only flags a change of 15+ percentage points -- smaller week-to-week
    swings are normal noise for most players and not a real signal.
    Returns None if there's under 2 real weeks to compare, or the
    change is too small to be meaningful.
    """
    snaps = load_snap_counts()
    rows = snaps.filter(pl.col("player") == player_name).sort("week")
    if rows.height < 2:
        return None
    weeks = rows.to_dicts()
    latest, previous = weeks[-1], weeks[-2]
    latest_pct, prev_pct = latest.get("offense_pct"), previous.get("offense_pct")
    if latest_pct is None or prev_pct is None:
        return None
    delta = round((latest_pct - prev_pct) * 100, 1)
    if abs(delta) < 15:
        return None
    return {
        "direction": "up" if delta > 0 else "down",
        "delta_pp": abs(delta),
        "latest_week": latest["week"],
        "latest_pct": round(latest_pct * 100, 1),
        "previous_week": previous["week"],
        "previous_pct": round(prev_pct * 100, 1),
    }


def target_share_trend(player_name: str) -> dict | None:
    """
    Real week-over-week TARGET share change (this player's real targets
    divided by his team's real total targets that week) -- complements
    usage_trend (snap share): a player's snaps can hold steady while his
    target trust within those snaps rises or falls, which is often the
    more direct signal for receiving/reception props specifically than
    snap share alone.

    Flags changes of 10+ percentage points -- a slightly lower bar than
    snap share's 15pp, since target share naturally swings a bit more
    game to game (game script, matchup, etc.) even for a stable role.
    """
    df = load_stats()
    rows = df.filter(pl.col("player_display_name") == player_name).sort("week")
    if rows.height < 2:
        return None
    team_targets = df.group_by(["team", "week"]).agg(pl.col("targets").sum().alias("team_targets"))
    weeks = rows.to_dicts()
    latest, previous = weeks[-1], weeks[-2]

    def share(row):
        tt = team_targets.filter((pl.col("team") == row["team"]) & (pl.col("week") == row["week"]))
        if tt.height == 0 or tt["team_targets"][0] in (None, 0):
            return None
        targets = row.get("targets")
        if targets is None:
            return None
        return targets / tt["team_targets"][0]

    latest_share, prev_share = share(latest), share(previous)
    if latest_share is None or prev_share is None:
        return None
    delta = round((latest_share - prev_share) * 100, 1)
    if abs(delta) < 10:
        return None
    return {
        "direction": "up" if delta > 0 else "down",
        "delta_pp": abs(delta),
        "latest_week": latest["week"], "latest_pct": round(latest_share * 100, 1),
        "previous_week": previous["week"], "previous_pct": round(prev_share * 100, 1),
    }


def opponent_recent_form(opponent_team: str, stat_col: str) -> dict | None:
    """
    Real check on whether a defense has been trending better or worse
    lately, compared to its own full-season average -- a separate,
    transparent signal from matchup_factor (which uses the full-season
    average for the actual projection math). This doesn't feed into the
    point projection; it's context for reading a matchup with more
    current information than a single season-long number gives.

    Requires at least 3 real weeks played by this defense before
    flagging anything -- with fewer games, "their last 1-2 games" is too
    small a sample to call a real trend rather than noise, same
    real-sample-size discipline used everywhere else on this site.
    """
    defense_stat_col = "passing_yards" if stat_col == "receiving_yards" else stat_col
    if defense_stat_col not in ("passing_yards", "rushing_yards"):
        return None
    df = load_stats()
    opp_off = df.filter(pl.col("opponent_team") == opponent_team)
    if opp_off.height == 0:
        return None
    weekly = opp_off.group_by("week").agg(pl.col(defense_stat_col).sum().alias("allowed")).sort("week")
    if weekly.height < 3:
        return None
    weeks = weekly.to_dicts()
    recent = weeks[-2:]
    recent_avg = sum(w["allowed"] for w in recent) / len(recent)
    season_avg = sum(w["allowed"] for w in weeks) / len(weeks)
    if season_avg == 0:
        return None
    pct_change = round((recent_avg - season_avg) / season_avg * 100, 1)
    if abs(pct_change) < 15:
        return None
    return {
        "direction": "worse" if pct_change > 0 else "better",
        "pct_change": abs(pct_change),
        "recent_avg": round(recent_avg, 1),
        "season_avg": round(season_avg, 1),
        "recent_games": len(recent),
    }


def opportunity_signal(player_name: str, team: str, pos_abb: str) -> dict | None:
    """
    Real "next man up" flag -- when a teammate ranked HIGHER on the real
    depth chart at the same position is reported Out or Doubtful, this
    surfaces the real fact (who's hurt, their real status, and that this
    player now sits higher in the real pecking order) WITHOUT fabricating
    a specific magnitude of expected increased usage. We have no honest
    way to quantify "how much more" precisely -- the real signal is
    *that* the door has opened, not a made-up percentage of how far.

    Only checks teammates ranked ABOVE this player at the same position
    (pos_rank < this player's own rank) -- a backup below them being
    hurt doesn't open anything up.
    """
    dc = load_depth_charts()
    my_row = dc.filter(
        (pl.col("team") == team) & (pl.col("pos_abb") == pos_abb) & (pl.col("player_name") == player_name)
    )
    if my_row.height == 0:
        return None
    my_rank = my_row.row(0, named=True)["pos_rank"]
    if my_rank is None or my_rank <= 1:
        return None
    higher_ranked = dc.filter(
        (pl.col("team") == team) & (pl.col("pos_abb") == pos_abb) & (pl.col("pos_rank") < my_rank)
    ).sort("pos_rank")
    for row in higher_ranked.iter_rows(named=True):
        inj = injury_status_for(row["player_name"])
        if inj and inj.get("report_status") in ("Out", "Doubtful"):
            return {"injured_player": row["player_name"], "status": inj["report_status"], "my_rank": my_rank}
    return None


# ---------------------------------------------------------------------------
# Team defensive coverage identity -- real 2025 charted man/zone rates,
# plus a live 2026 cushion-based proxy (checked and confirmed: nflverse's
# real participation charting, which has the actual MAN_COVERAGE /
# ZONE_COVERAGE tags, is not yet released for the 2026 season -- only
# through 2025 so far). The proxy is logged weekly (see
# log_coverage_proxy below) so it can be checked against real 2026
# charting once nflverse eventually publishes it.
# ---------------------------------------------------------------------------

_coverage_2025_cache = None


def team_coverage_profile_2025(team: str) -> dict | None:
    """
    Real, human-charted 2025 season coverage tendency for this team's
    DEFENSE -- from nflreadpy's load_participation(), genuinely charted
    play-by-play (not inferred). Explicitly 2025, not 2026: nflverse
    hasn't released 2026 participation/coverage charting yet (checked
    live, confirmed). A defensive coordinator's scheme identity often
    carries over year to year, but this is real last-season data, not
    this season's -- labeled as such everywhere it's shown.
    """
    global _coverage_2025_cache
    if _coverage_2025_cache is None:
        import nflreadpy as nfl
        part = nfl.load_participation([2025])
        sched = nfl.load_schedules([2025])
        joined = part.join(
            sched.select(["game_id", "home_team", "away_team"]),
            left_on="nflverse_game_id", right_on="game_id", how="left",
        )
        joined = joined.with_columns(
            pl.when(pl.col("possession_team") == pl.col("home_team"))
            .then(pl.col("away_team")).otherwise(pl.col("home_team")).alias("defense_team")
        )
        valid = joined.filter(pl.col("defense_man_zone_type").is_in(["MAN_COVERAGE", "ZONE_COVERAGE"]))
        _coverage_2025_cache = valid.group_by("defense_team").agg([
            (pl.col("defense_man_zone_type") == "MAN_COVERAGE").mean().alias("man_rate"),
            pl.len().alias("plays"),
        ])
    row = _coverage_2025_cache.filter(pl.col("defense_team") == team)
    if row.height == 0:
        return None
    r = row.row(0, named=True)
    return {"season": 2025, "man_rate": round(r["man_rate"] * 100, 1), "zone_rate": round((1 - r["man_rate"]) * 100, 1), "plays": r["plays"]}


_cushion_2026_cache = None


def team_cushion_profile_2026(team: str) -> dict | None:
    """
    Real, LIVE 2026 proxy for coverage tendency -- average cushion (real
    NGS data) that opposing receivers have actually faced against this
    defense this season, derived by joining NGS receiving to each
    receiver's real opponent that week. Tighter cushion suggests more
    man-coverage-like tendencies; looser suggests more zone -- but this
    is a proxy, not real charted coverage type (see
    team_coverage_profile_2025 for that, though only current through
    2025). Updates automatically as real 2026 games are played.
    """
    global _cushion_2026_cache
    if _cushion_2026_cache is None:
        ngs = load_nextgen_receiving()
        stats = load_stats()
        joined = ngs.join(
            stats.select(["player_display_name", "week", "opponent_team"]).unique(),
            on=["player_display_name", "week"], how="inner",
        )
        _cushion_2026_cache = joined.group_by("opponent_team").agg([
            pl.col("avg_cushion").mean().alias("avg_cushion_allowed"),
            pl.col("week").n_unique().alias("games"),
        ])
    row = _cushion_2026_cache.filter(pl.col("opponent_team") == team)
    if row.height == 0:
        return None
    r = row.row(0, named=True)
    return {"season": 2026, "avg_cushion_allowed": round(r["avg_cushion_allowed"], 2), "games": r["games"]}


def all_team_coverage_profiles() -> list[dict]:
    """
    Real defensive coverage identity for all 32 teams -- both signals
    side by side, clearly labeled for what each actually is: real 2025
    human-charted man/zone rates (last season, genuinely charted), and
    the live 2026 cushion-based proxy (this season, real data, but a
    proxy, not charted coverage type -- that doesn't exist for 2026 yet).
    """
    out = []
    for team in sorted(TEAM_CONFERENCE.keys()):
        out.append({
            "team": team,
            "team_badge": team_badge(team),
            "coverage_2025": team_coverage_profile_2025(team),
            "cushion_2026": team_cushion_profile_2026(team),
        })
    return out
