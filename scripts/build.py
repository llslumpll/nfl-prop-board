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

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"
TEMPLATES = Path(__file__).parent / "templates"
DATA_DIR = ROOT / "data"

NAV_PAGES = [
    ("home", "Home", "index.html"),
    ("passing", "Passing", "passing.html"),
    ("receiving", "Receiving", "receiving.html"),
    ("history", "History", "history.html"),
]
COMING_SOON = ["Touchdowns", "Rushing", "Receptions", "Kicking", "Teams/Matchups"]


def refresh_data():
    """Pull a fresh live snapshot from nflreadpy. Requires real internet
    access (GitHub Actions runners have it; some sandboxes don't)."""
    import nflreadpy as nfl

    print("Fetching live player stats...")
    nfl.load_player_stats([2026]).write_parquet(DATA_DIR / "player_stats_2026.parquet")
    print("Fetching live injuries...")
    nfl.load_injuries([2026]).write_parquet(DATA_DIR / "injuries_2026.parquet")


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

    pages = {
        "index.html": ("home", "home.html", {"weeks": dataio.weeks_available()}),
        "passing.html": ("passing", "passing.html", {"rows": dataio.passing_leaders()}),
        "receiving.html": (
            "receiving",
            "receiving.html",
            {
                "rows": dataio.receiving_leaders(),
                "qb_by_team": dataio.correlated_pairs_for_receiving(),
            },
        ),
        "history.html": ("history", "history.html", {}),
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
