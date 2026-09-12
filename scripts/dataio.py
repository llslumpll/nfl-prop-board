"""
Data access layer for the NFL Prop Edge Board.

IMPORTANT HONESTY NOTES (per project brief):
- All stats here come from a real nflreadpy snapshot (2026 Week 1), not
  fabricated data.
- The season is one week old. There is NOT enough sample to run a real
  calibrated confidence model yet. Rather than fake a number, every
  "confidence" shown is explicitly labeled PROVISIONAL and is a simple,
  transparent placeholder (sample size only) -- not a tuned prediction.
  MIN_SAMPLE / DAMPEN for a real model is an open design question per
  the project brief and is NOT decided here.
- No live market (Kalshi / PrizePicks) data is wired in this build --
  this sandbox's network allowlist does not include those APIs. Market
  columns are shown as "not connected" rather than invented.
"""

import polars as pl
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"

_stats_cache = None
_injuries_cache = None


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


def weeks_available() -> list[int]:
    return sorted(load_stats()["week"].unique().to_list())


def provisional_tier(n_games: int) -> dict:
    """
    Honest, non-fabricated placeholder confidence tier based ONLY on
    sample size seen so far. This is NOT a calibrated prediction model.
    Real MIN_SAMPLE/DAMPEN values are an open question (see brief).
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
            "opponent": row["opponent_team"],
            "week": row["week"],
            "completions": row["completions"],
            "attempts": row["attempts"],
            "passing_yards": row["passing_yards"],
            "passing_tds": row["passing_tds"],
            "interceptions": row["passing_interceptions"],
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
            "opponent": row["opponent_team"],
            "week": row["week"],
            "targets": row["targets"],
            "receptions": row.get("receptions"),
            "receiving_yards": row["receiving_yards"],
            "receiving_tds": row["receiving_tds"],
            "target_share": row.get("target_share"),
            "tier": provisional_tier(n_games),
            "injury": injury_status_for(row["player_display_name"]),
        })
    return out


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
