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
    print(f"Kalshi per-series results: {kalshi_data['diagnostics']['per_series']}")
    import json
    (DATA_DIR / "kalshi_raw.json").write_text(json.dumps(kalshi_data, indent=2))

    pages = {
        "index.html": ("home", "home.html", {"weeks": dataio.weeks_available()}),
        "matchups.html": (
            "matchups",
            "matchups.html",
            {"matchups": dataio.upcoming_matchups(), "kalshi_game_props": kalshi_data["game_props"], "kalshi_error": kalshi_data["error"]},
        ),
        "passing.html": ("passing", "passing.html", {"rows": dataio.passing_leaders()}),
        "receiving.html": (
            "receiving",
            "receiving.html",
            {
                "rows": dataio.receiving_leaders(),
                "qb_by_team": dataio.correlated_pairs_for_receiving(),
            },
        ),
        "receptions.html": (
            "receptions",
            "receptions.html",
            {
                "rows": dataio.receptions_leaders(),
                "qb_by_team": dataio.correlated_pairs_for_receiving(),
            },
        ),
        "rushing.html": ("rushing", "rushing.html", {"rows": dataio.rushing_leaders()}),
        "touchdowns.html": (
            "touchdowns",
            "touchdowns.html",
            {"rows": dataio.touchdown_leaders(), "kalshi_td_props": kalshi_data["touchdown_props"], "kalshi_error": kalshi_data["error"]},
        ),
        "history.html": ("history", "history.html", {"projections_logged": dataio.projections_logged_count()}),
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
