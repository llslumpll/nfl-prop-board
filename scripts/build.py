"""
Static site builder for the NFL Prop Edge Board.

Run this to (re)generate docs/*.html from live nflreadpy data. This is
what a scheduled GitHub Actions job runs automatically -- see
.github/workflows/build.yml. No server runs at request time; GitHub
Pages just serves whatever this script last wrote to docs/.

Usage:
    python scripts/build.py            # refresh data from nflreadpy, then build
    python scripts/build.py --no-fetch # rebuild from the cached parquet only
"""

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import dataio  # noqa: E402
import kalshi_client  # noqa: E402
import prizepicks_client  # noqa: E402
import best5  # noqa: E402
import predictions  # noqa: E402
import grade  # noqa: E402

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"
TEMPLATES = Path(__file__).parent / "templates"
DATA_DIR = ROOT / "data"

NAV_PAGES = [
    ("home", "Home", "index.html"),
    ("matchups", "Matchups", "matchups.html"),
    ("passing", "Passing", "passing.html"),
    ("receiving", "Receiving", "receiving.html"),
    ("receptions", "Receptions", "receptions.html"),
    ("rushing", "Rushing", "rushing.html"),
    ("touchdowns", "Touchdowns", "touchdowns.html"),
    ("history", "History", "history.html"),
]
COMING_SOON: list[str] = []


def refresh_data():
    """Pull a fresh live snapshot from nflreadpy. Requires real internet
    access (GitHub Actions runners have it; some sandboxes don't)."""
    import nflreadpy as nfl

    print("Fetching live player stats...")
    nfl.load_player_stats([2026]).write_parquet(DATA_DIR / "player_stats_2026.parquet")
    print("Fetching live injuries...")
    nfl.load_injuries([2026]).write_parquet(DATA_DIR / "injuries_2026.parquet")
    print("Fetching live schedule...")
    nfl.load_schedules([2026]).write_parquet(DATA_DIR / "schedules_2026.parquet")
    # Historical stats (for Teams/Matchups) change slowly -- only refetch
    # if we don't already have a cached copy, to save a bigger pull on
    # every scheduled run.
    hist_path = DATA_DIR / "player_stats_historical.parquet"
    if not hist_path.exists():
        print("Fetching historical player stats (2023-2025, one-time pull)...")
        nfl.load_player_stats([2023, 2024, 2025]).write_parquet(hist_path)


def build():
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)))
    build_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    common = {
        "nav_pages": NAV_PAGES,
        "coming_soon": COMING_SOON,
        "build_time": build_time,
    }

    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").touch()  # tell GitHub Pages not to run Jekyll on this

    # Remove stale pages that are no longer part of the site (e.g. kicking,
    # dropped as unreliable to project honestly) so they don't linger as
    # orphaned, unlinked URLs.
    stale = (DOCS / "kicking.html",)
    for f in stale:
        if f.exists():
            f.unlink()
            print(f"removed stale {f.name}")

    # Kalshi: real public API, scoped to touchdown + game-level props only
    # (player yardage props are PrizePicks' lane -- see prizepicks_client.py
    # once that's added). Fails soft -- see kalshi_client.py docstring.
    print("Fetching Kalshi touchdown/game props...")
    kalshi_data = kalshi_client.fetch_nfl_touchdown_and_game_props()
    if kalshi_data["error"]:
        print(f"WARNING: Kalshi fetch failed ({kalshi_data['error']}); site builds without it.")
    else:
        diag = kalshi_data["diagnostics"]
        print(f"Kalshi: found {diag.get('total_touchdown_props_found', 0)} TD props, "
              f"{diag.get('total_game_props_found', 0)} game props "
              f"(showing top {len(kalshi_data['touchdown_props'])}/{len(kalshi_data['game_props'])} by volume).")
        print(f"Kalshi quoted markets: {diag.get('touchdown_props_with_a_quote', 0)} TD, "
              f"{diag.get('game_props_with_a_quote', 0)} game (out of the totals above -- "
              f"the rest are real listed markets with no price quoted yet).")
    print(f"Kalshi per-series results: {kalshi_data['diagnostics']['per_series']}")
    import json
    (DATA_DIR / "kalshi_raw.json").write_text(json.dumps(kalshi_data, indent=2))

    # PrizePicks: unofficial, no key needed, but a documented risk of
    # datacenter-IP blocking (see prizepicks_client.py docstring). Fails
    # soft exactly like Kalshi. Matched against our OWN known 2026
    # roster rather than trusting an unverified league_id.
    print("Fetching PrizePicks player props...")
    pp_data = prizepicks_client.fetch_nfl_player_props()
    if pp_data["error"]:
        print(f"WARNING: PrizePicks fetch failed ({pp_data['error']}); site builds without it.")
    else:
        print(f"PrizePicks diagnostics: {pp_data['diagnostics']}")
    (DATA_DIR / "prizepicks_raw.json").write_text(json.dumps(pp_data, indent=2))
    pp_props = pp_data["props"]
    pp_error = pp_data["error"]

    # --- Matchups: single next date, with per-game Kalshi props attached ---
    matchups_data = dataio.matchups_for_next_date()
    standings = dataio.team_standings()
    full_team_stats = dataio.team_full_stats()

    def kalshi_props_for_game(game: dict) -> list[dict]:
        """Filters the full Kalshi game_props list down to just the two
        teams in this specific game, matching by ticker substring (e.g.
        'KXNFLGAME-26SEP14DENKC-KC' contains both 'DENKC' concatenated
        and the single-team suffix). Uses each game's Kalshi-aliased team
        codes so the known LA/LAR mismatch doesn't silently drop matches.

        Also classifies each KXNFLTOTAL record as a combined "game" total
        or a specific "team" total -- Kalshi bundles both under the same
        series/event (e.g. "Over 44.5 points scored" alongside "Houston
        over 21.5 points scored"), and the only reliable way to tell them
        apart is that a team-total market's title names a specific city,
        while the combined one doesn't."""
        away, home = game["kalshi_away_code"], game["kalshi_home_code"]
        away_city = dataio.TEAM_CITY.get(game["away_team"], game["away_team"])
        home_city = dataio.TEAM_CITY.get(game["home_team"], game["home_team"])
        out = []
        for m in kalshi_data["game_props"]:
            ticker = m.get("ticker") or ""
            if away not in ticker or home not in ticker:
                continue
            record = dict(m)
            if record.get("series_ticker") == "KXNFLTOTAL":
                title = record.get("market_title") or ""
                if away_city in title or home_city in title:
                    record["total_scope"] = "team"
                else:
                    record["total_scope"] = "game"
            out.append(record)
        return out

    for g in matchups_data["games"]:
        g["kalshi_props"] = kalshi_props_for_game(g)

    # --- Freeze real predictions (one per player/stat/week, never
    # overwritten) using the real PrizePicks line as the market side.
    # This is what makes the History page's grading meaningful -- a
    # prediction with no real market line gets frozen too (so we can
    # still show the projection), but never gets a call/edge, since
    # there's nothing real to grade it against.
    frozen_count = 0
    for g in matchups_data["games"]:
        for p in g["players"]:
            market_line = pp_props.get(p["player"], {}).get(p["stat_col"])
            wrote = predictions.freeze_prediction(
                player=p["player"], team=p["team"], opponent=p["opponent"],
                week=g["week"],
                stat=p["stat_col"], projected=p["projection"]["projected"],
                tier_label=dataio.provisional_tier(p["projection"]["n_games_2026"])["label"],
                market_line=market_line,
                market_source="prizepicks" if market_line is not None else None,
            )
            if wrote:
                frozen_count += 1
    print(f"Froze {frozen_count} new prediction(s) this build.")

    # --- Grade any predictions whose games have now finished ---
    grade_result = grade.grade_all()
    print(f"Grading: {grade_result['newly_graded']} newly graded, "
          f"{grade_result['total_pending']} still pending a final score.")
    accuracy = grade.accuracy_summary()

    # --- Best 5: real edge between our projections and real market lines ---
    passing_rows = dataio.passing_leaders()
    receiving_rows = dataio.receiving_leaders()
    receptions_rows = dataio.receptions_leaders()
    rushing_rows = dataio.rushing_leaders()
    touchdown_rows = dataio.touchdown_leaders()

    best5_data = {
        "passing": best5.best5_yardage(passing_rows, pp_props, "passing_yards", "yds"),
        "receiving": best5.best5_yardage(receiving_rows, pp_props, "receiving_yards", "yds"),
        "receptions": best5.best5_yardage(receptions_rows, pp_props, "receptions", "rec"),
        "rushing": best5.best5_yardage(rushing_rows, pp_props, "rushing_yards", "yds"),
        "touchdowns": best5.best5_touchdowns(touchdown_rows, kalshi_data["touchdown_props"]),
    }
    for label, picks in best5_data.items():
        print(f"Best 5 {label}: {len(picks)} eligible pick(s)")

    pages = {
        "index.html": (
            "home",
            "home.html",
            {"weeks": dataio.weeks_available(), "best5": best5_data},
        ),
        "matchups.html": (
            "matchups",
            "matchups.html",
            {
                "matchups_data": matchups_data,
                "standings": standings,
                "full_team_stats": full_team_stats,
                "kalshi_error": kalshi_data["error"],
            },
        ),
        "passing.html": ("passing", "passing.html", {"rows": passing_rows, "pp_props": pp_props, "pp_error": pp_error}),
        "receiving.html": (
            "receiving",
            "receiving.html",
            {
                "rows": receiving_rows,
                "qb_by_team": dataio.correlated_pairs_for_receiving(),
                "pp_props": pp_props,
                "pp_error": pp_error,
            },
        ),
        "receptions.html": (
            "receptions",
            "receptions.html",
            {
                "rows": receptions_rows,
                "qb_by_team": dataio.correlated_pairs_for_receiving(),
                "pp_props": pp_props,
                "pp_error": pp_error,
            },
        ),
        "rushing.html": ("rushing", "rushing.html", {"rows": rushing_rows, "pp_props": pp_props, "pp_error": pp_error}),
        "touchdowns.html": (
            "touchdowns",
            "touchdowns.html",
            {"rows": touchdown_rows, "kalshi_td_props": kalshi_data["touchdown_props"], "kalshi_error": kalshi_data["error"]},
        ),
        "history.html": ("history", "history.html", {"accuracy": accuracy}),
    }

    for filename, (slug, template_name, ctx) in pages.items():
        template = env.get_template(template_name)
        html = template.render(current_page=slug, **common, **ctx)
        (DOCS / filename).write_text(html)
        print(f"wrote {filename}")

    static_src = TEMPLATES.parent.parent / "docs" / "static"
    static_dst = DOCS / "static"
    if static_src != static_dst:
        static_dst.mkdir(exist_ok=True)
        for f in static_src.glob("*"):
            shutil.copy(f, static_dst / f.name)

    print(f"Build complete at {build_time}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-fetch", action="store_true", help="skip live data refresh, use cached parquet")
    args = parser.parse_args()

    if not args.no_fetch:
        try:
            refresh_data()
        except Exception as e:
            print(f"WARNING: live data refresh failed ({e}); building from cached data.")

    # invalidate dataio's in-memory cache in case refresh_data ran
    dataio._stats_cache = None
    dataio._injuries_cache = None

    build()
