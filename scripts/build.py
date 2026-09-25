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
import calibrate  # noqa: E402
import grade  # noqa: E402
import pipeline_health  # noqa: E402
import coverage_log  # noqa: E402
import svgchart  # noqa: E402

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
    env.globals["line_chart"] = svgchart.line_chart
    env.globals["bar_chart_with_threshold"] = svgchart.bar_chart_with_threshold
    build_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    # A URL-safe, ever-changing value tied to this exact build -- used
    # as a query-string cache-buster on static/style.css so every real
    # rebuild forces browsers to fetch the new CSS instead of reusing a
    # cached older one. Unix timestamp is simplest and always increases.
    cache_bust = str(int(datetime.now(timezone.utc).timestamp()))

    common = {
        "nav_pages": NAV_PAGES,
        "coming_soon": COMING_SOON,
        "build_time": build_time,
        "cache_bust": cache_bust,
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

    # --- Vegas-implied game environment: real Kalshi Team Total markets,
    # where quoted, feeding a final adjustment layer onto every
    # projection. Applied to both the Matchups page's players AND the
    # individual stat pages' "next game" projections (see below, after
    # the leader rows are computed), using the SAME implied totals so
    # every page agrees with every other page.
    import re

    def extract_implied_team_total(kalshi_props_for_game_, team_city):
        """Finds the threshold whose implied probability is closest to
        50% among this team's real Kalshi Team Total markets -- that
        threshold IS the market's implied point total, the same
        convention a sportsbook's own total line uses."""
        candidates = []
        for m in kalshi_props_for_game_:
            if m.get("total_scope") != "team":
                continue
            if team_city not in (m.get("market_title") or ""):
                continue
            if m.get("implied_pct") is None:
                continue
            match = re.search(r"(\d+\.?\d*)", m.get("market_title") or "")
            if not match:
                continue
            candidates.append((abs(m["implied_pct"] - 50), float(match.group(1))))
        if not candidates:
            return None
        candidates.sort(key=lambda c: c[0])
        return candidates[0][1]

    league_avg_points = dataio.league_avg_team_points_per_game()
    implied_team_totals: dict[str, float] = {}
    for g in matchups_data["games"]:
        away_city = dataio.TEAM_CITY.get(g["away_team"], g["away_team"])
        home_city = dataio.TEAM_CITY.get(g["home_team"], g["home_team"])
        implied_team_totals[g["away_team"]] = extract_implied_team_total(g["kalshi_props"], away_city)
        implied_team_totals[g["home_team"]] = extract_implied_team_total(g["kalshi_props"], home_city)
        for p in g["players"]:
            env_factor = dataio.environment_factor(implied_team_totals.get(p["team"]), league_avg_points)
            p["projection"]["environment_factor"] = env_factor
            p["projection"]["projected"] = round(p["projection"]["projected"] * env_factor["factor"], 1)

    quoted_environments = sum(1 for v in implied_team_totals.values() if v is not None)
    print(f"Vegas-implied game environment: {quoted_environments}/{len(implied_team_totals)} team(s) have a real quoted Kalshi Team Total.")

    # --- Freeze real predictions (one per player/stat/week, never
    # overwritten) using the real PrizePicks line as the market side.
    # This is what makes the History page's grading meaningful -- a
    # prediction with no real market line gets frozen too (so we can
    # still show the projection), but never gets a call/edge, since
    # there's nothing real to grade it against.
    STAT_TO_POSITION = {"passing_yards": "QB", "rushing_yards": "RB", "receiving_yards": "WR", "receptions": "WR"}

    def _signals_snapshot(projection: dict, row: dict | None = None) -> dict:
        """Real snapshot of which context signals were active at freeze
        time, from whatever the projection (and now, the matchups player
        row itself) carries -- used later by grade.py to check whether
        picks tagged with a given signal actually hit more or less often
        than picks without it."""
        snap = {}
        inj = projection.get("injury_factor") or {}
        if inj.get("status"):
            snap["injury_flagged"] = inj["status"]
        rf = projection.get("opponent_recent_form")
        if rf:
            snap["opponent_recent_form"] = rf["direction"]
        if row:
            ut = row.get("usage_trend")
            if ut:
                snap["usage_trend"] = ut["direction"]
            tst = row.get("target_share_trend")
            if tst:
                snap["target_share_trend"] = tst["direction"]
            if row.get("opportunity_signal"):
                snap["opportunity_signal"] = True
        return snap

    frozen_count = 0
    for g in matchups_data["games"]:
        for p in g["players"]:
            market_line = pp_props.get(p["player"], {}).get(p["stat_col"])
            m_prob = None
            if market_line is not None:
                std_dev = dataio.player_std_dev(p["player"], p["stat_col"], STAT_TO_POSITION.get(p["stat_col"], "WR"))
                m_prob = dataio.model_prob_over(p["projection"]["projected"], market_line, std_dev)
            wrote = predictions.freeze_prediction(
                player=p["player"], team=p["team"], opponent=p["opponent"],
                week=g["week"],
                stat=p["stat_col"], projected=p["projection"]["projected"],
                tier_label=p["projection"]["tier"]["label"],
                market_line=market_line,
                market_source="prizepicks" if market_line is not None else None,
                model_prob=m_prob,
                signals=_signals_snapshot(p["projection"], p),
            )
            if wrote:
                frozen_count += 1
    print(f"Froze {frozen_count} new prediction(s) this build.")
    matched_count = sum(
        1 for g in matchups_data["games"] for p in g["players"]
        if pp_props.get(p["player"], {}).get(p["stat_col"]) is not None
    )
    total_this_build = sum(len(g["players"]) for g in matchups_data["games"])
    pipeline_health.log_health(total_this_build, matched_count, len(predictions.load_predictions()))

    # Real, live 2026 cushion-based coverage-tendency proxy, logged
    # once per team per week -- not verified yet (real 2026 man/zone
    # charting isn't released by nflverse, checked and confirmed), but
    # gives an honest trail to check it against once that real data
    # does land.
    n_logged = coverage_log.log_all_teams(list(dataio.TEAM_CONFERENCE.keys()), dataio.team_cushion_profile_2026)
    print(f"Logged coverage proxy for {n_logged} team(s) this build.")

    # --- Grade any predictions whose games have now finished ---
    migrated = predictions.migrate_stale_tiers()
    if migrated:
        print(f"Migrated {migrated} prediction(s) from the old flat tier label to real career-based tiers.")
    grade_result = grade.grade_all()
    print(f"Grading: {grade_result['newly_graded']} newly graded, "
          f"{grade_result['total_pending']} still pending a final score.")
    accuracy = grade.accuracy_summary()

    # --- Recalibrate from graded history for the NEXT build to use.
    # Same lag-by-one-cycle pattern as the MLB site's run_daily.py: THIS
    # build's projections already used whatever calibration.json existed
    # from the previous build (project_stat() reads it at call time,
    # earlier in this same build, before fresh grading happened) --
    # recalibrating now, after fresh grading, produces the corrected
    # file the NEXT build will read. Gated by MIN_SAMPLE the same way
    # everywhere else on this site is; "insufficient data" is a real,
    # honest, expected status early in a season, not a bug. ---
    calibration_result = calibrate.run()
    active_bias_tiers = sum(
        1 for stat_cal in calibration_result["bias"].values()
        for tier_cal in stat_cal.values() if tier_cal.get("status") == "active"
    )
    print(f"Calibration: {active_bias_tiers} stat/tier combination(s) have enough graded "
          f"history (MIN_SAMPLE={dataio.MIN_SAMPLE}) to apply a real bias correction.")

    # --- Best 5: real edge between our projections and real market lines ---
    passing_rows = dataio.passing_leaders()
    receiving_rows = dataio.receiving_leaders()
    receptions_rows = dataio.receptions_leaders()
    rushing_rows = dataio.rushing_leaders()
    touchdown_rows = dataio.touchdown_leaders()

    # Apply the SAME Vegas-implied environment factors computed above to
    # every stat page's "next game" projection, so Matchups and the
    # individual stat pages never silently disagree on the same
    # player/game the way they did before the matchup/pace/wind fix.
    for rows in (passing_rows, receiving_rows, receptions_rows, rushing_rows):
        for r in rows:
            if not r.get("next_game"):
                continue
            env_factor = dataio.environment_factor(implied_team_totals.get(r["team"]), league_avg_points)
            r["next_game"]["projection"]["environment_factor"] = env_factor
            r["next_game"]["projection"]["projected"] = round(
                r["next_game"]["projection"]["projected"] * env_factor["factor"], 1
            )

    # --- Real "why" reasoning for EVERY row on every stat page, not just
    # Best 5 -- same reason_text() used there, applied consistently so
    # every prop on the site follows the same explain-yourself pattern. ---
    STAT_LABELS = {
        "passing_yards": "passing yards", "rushing_yards": "rushing yards",
        "receiving_yards": "receiving yards", "receptions": "receptions",
    }
    for rows, stat_col in (
        (passing_rows, "passing_yards"), (receiving_rows, "receiving_yards"),
        (receptions_rows, "receptions"), (rushing_rows, "rushing_yards"),
    ):
        for r in rows:
            if r.get("next_game"):
                r["next_game"]["reason"] = dataio.reason_text(r["next_game"]["projection"], STAT_LABELS[stat_col])
    for r in touchdown_rows:
        ng = r.get("next_game")
        if ng and ng.get("projected_total"):
            r["next_game"]["reason"] = f"Projected {ng['projected_total']} total TDs ({ng.get('breakdown', '—')}), summed from real per-stat career and season-to-date baselines."

    # --- Best 5: TWO genuinely different rankings per prop, same split
    # as the MLB site -- "Best Value" (biggest edge vs the market line)
    # and "Highest Confidence" (real model probability, via a normal-
    # approximation using real per-player game-to-game variance for
    # yardage stats, or real Poisson probability for touchdowns). These
    # can and often will name different players for the same week.
    best5_data = {
        "passing": {
            "best_value": best5.best5_yardage(passing_rows, pp_props, "passing_yards", "yds"),
            "highest_confidence": best5.best5_highest_confidence(passing_rows, pp_props, "passing_yards", "yds", "QB"),
        },
        "receiving": {
            "best_value": best5.best5_yardage(receiving_rows, pp_props, "receiving_yards", "yds"),
            "highest_confidence": best5.best5_highest_confidence(receiving_rows, pp_props, "receiving_yards", "yds", "WR"),
        },
        "receptions": {
            "best_value": best5.best5_yardage(receptions_rows, pp_props, "receptions", "rec"),
            "highest_confidence": best5.best5_highest_confidence(receptions_rows, pp_props, "receptions", "rec", "WR"),
        },
        "rushing": {
            "best_value": best5.best5_yardage(rushing_rows, pp_props, "rushing_yards", "yds"),
            "highest_confidence": best5.best5_highest_confidence(rushing_rows, pp_props, "rushing_yards", "yds", "RB"),
        },
        "touchdowns": {
            "best_value": best5.best5_touchdowns(touchdown_rows, kalshi_data["touchdown_props"]),
            "highest_confidence": best5.best5_touchdowns_most_likely(touchdown_rows),
        },
    }
    for label, rankings in best5_data.items():
        for rank_type, picks in rankings.items():
            print(f"Best 5 {label} ({rank_type}): {len(picks)} eligible pick(s)")

    # Tag each Best 5 pick's ALREADY-frozen individual prediction with
    # which list(s) it belonged to this week -- metadata only, doesn't
    # touch the frozen prediction itself. This is what lets History
    # compute a real week-by-week Best 5 hit rate later, just by
    # querying graded predictions filtered by this tag.
    STAT_COL_BY_LABEL = {
        "passing": "passing_yards", "receiving": "receiving_yards",
        "receptions": "receptions", "rushing": "rushing_yards", "touchdowns": "any_td",
    }
    for label, rankings in best5_data.items():
        stat_col = STAT_COL_BY_LABEL[label]
        for rank_type, picks in rankings.items():
            for p in picks:
                predictions.tag_best5(p["player"], stat_col, p["week"], rank_type)

    # Real correlation check across each finished Best 5 list -- same
    # team or same game showing up twice in one list of 5, flagged
    # explicitly rather than left for the person to notice on their own.
    best5_correlation_warnings = {
        label: {rank_type: best5.flag_correlated_picks(picks) for rank_type, picks in rankings.items()}
        for label, rankings in best5_data.items()
    }

    # --- Freeze Receptions predictions too. Real bug fixed here: these
    # were never being frozen at all before, since Receptions isn't one
    # of the 3 marquee stats (passing/rushing/receiving) the Matchups
    # page shows per game -- meaning that entire prop type could never
    # show up in History no matter how long the season ran. ---

    receptions_frozen = 0
    for r in receptions_rows:
        ng = r.get("next_game")
        if not ng:
            continue
        market_line = pp_props.get(r["player"], {}).get("receptions")
        m_prob = None
        if market_line is not None:
            std_dev = dataio.player_std_dev(r["player"], "receptions", "WR")
            m_prob = dataio.model_prob_over(ng["projection"]["projected"], market_line, std_dev)
        wrote = predictions.freeze_prediction(
            player=r["player"], team=r["team"], opponent=ng["opponent"], week=ng["week"],
            stat="receptions", projected=ng["projection"]["projected"], tier_label=ng["projection"]["tier"]["label"],
            market_line=market_line, market_source="prizepicks" if market_line is not None else None,
            model_prob=m_prob,
            signals=_signals_snapshot(ng["projection"], r),
        )
        if wrote:
            receptions_frozen += 1
    print(f"Froze {receptions_frozen} new receptions prediction(s) this build.")

    # --- Freeze Touchdown predictions too. Structurally different from
    # yardage props: there's no real "line" to beat, so this uses a
    # synthetic 0.5-TD threshold (the real breakeven point for "does
    # this player score at all") and calls OVER/UNDER based on whether
    # our model's probability of 1+ TD is higher or lower than Kalshi's
    # real quoted price for the exact same market -- same comparison
    # Best 5 Touchdowns already makes, just frozen for later grading
    # instead of only shown live. ---
    td_frozen = 0
    for r in touchdown_rows:
        ng = r.get("next_game")
        if not ng or not ng.get("projected_total"):
            continue
        market_price = best5.find_kalshi_1plus_td_price(kalshi_data["touchdown_props"], r["player"])
        model_prob = best5.poisson_prob_at_least(1, ng["projected_total"])
        td_signals = {}
        if r.get("usage_trend"):
            td_signals["usage_trend"] = r["usage_trend"]["direction"]
        if r.get("target_share_trend"):
            td_signals["target_share_trend"] = r["target_share_trend"]["direction"]
        if r.get("opportunity_signal"):
            td_signals["opportunity_signal"] = True
        if (r.get("injury") or {}).get("report_status"):
            td_signals["injury_flagged"] = r["injury"]["report_status"]
        rz_rush = r.get("red_zone_rush_share")
        if rz_rush and rz_rush["share_pct"] >= 20:
            td_signals["red_zone_rush_share_high"] = True
        rz_target = r.get("red_zone_target_share")
        if rz_target and rz_target["share_pct"] >= 20:
            td_signals["red_zone_target_share_high"] = True
        gl_rush = r.get("goal_line_rush_share")
        if gl_rush and gl_rush["share_pct"] >= 40:
            td_signals["goal_line_rush_share_high"] = True
        gl_target = r.get("goal_line_target_share")
        if gl_target and gl_target["share_pct"] >= 30:
            td_signals["goal_line_target_share_high"] = True
        xtd = r.get("xtd")
        if xtd and xtd["touches_priced"] >= 15:
            if xtd["debt"] >= 1.0:
                td_signals["xtd_debt"] = "positive"
            elif xtd["debt"] <= -1.0:
                td_signals["xtd_debt"] = "negative"
        if r.get("due_signal"):
            td_signals["due_for_td"] = True
        wrote = predictions.freeze_prediction(
            player=r["player"], team=r["team"], opponent=ng["opponent"], week=ng["week"],
            stat="any_td", projected=ng["projected_total"],
            tier_label=dataio.provisional_tier(1)["label"],
            market_line=0.5 if market_price is not None else None,
            market_source="kalshi_1plus_td" if market_price is not None else None,
            signals=td_signals,
        )
        # freeze_prediction sets call from projected vs market_line, but
        # for touchdowns the real comparison is model_prob vs the
        # market's own probability, not the raw point estimate vs 0.5 --
        # overwrite the call/edge with the correct comparison right after
        # freezing, only on the write path (never touches an
        # already-frozen entry).
        if wrote and market_price is not None:
            preds = predictions.load_predictions()
            key = f"{r['player']}|any_td|{ng['week']}"
            if key in preds:
                edge = round(model_prob - market_price, 3)
                preds[key]["call"] = "OVER" if edge > 0 else "UNDER"
                preds[key]["edge"] = round(edge * 100, 1)
                preds[key]["model_prob"] = round(model_prob * 100, 1)
                preds[key]["market_prob"] = round(market_price * 100, 1)
                predictions.save_predictions(preds)
        if wrote:
            td_frozen += 1
    print(f"Froze {td_frozen} new touchdown prediction(s) this build.")

    # Re-grade immediately in case any of the newly-frozen Receptions/TD
    # predictions belong to an already-finished game (only realistic
    # right after a code deploy adds a new stat type mid-week).
    grade_result2 = grade.grade_all()
    if grade_result2["newly_graded"]:
        print(f"Grading (2nd pass, new stat types): {grade_result2['newly_graded']} newly graded.")
        accuracy = grade.accuracy_summary()

    # Real per-game grouping for each stat page's Projections tab --
    # computed once here so both the game list and the "no upcoming
    # game" leftover list are available without recomputing per page.
    passing_games, passing_no_game = dataio.group_rows_by_game(passing_rows)
    receiving_games, receiving_no_game = dataio.group_rows_by_game(receiving_rows)
    receptions_games, receptions_no_game = dataio.group_rows_by_game(receptions_rows)
    rushing_games, rushing_no_game = dataio.group_rows_by_game(rushing_rows)
    touchdown_games, touchdown_no_game = dataio.group_rows_by_game(touchdown_rows)

    pages = {
        "index.html": (
            "home",
            "home.html",
            {
                "weeks": dataio.weeks_available(),
                "best5": best5_data,
                "recent_results": grade.recent_graded_results(),
                "maturity": calibrate.maturity_summary(),
            },
        ),
        "matchups.html": (
            "matchups",
            "matchups.html",
            {
                "matchups_data": matchups_data,
                "standings": standings,
                "full_team_stats": full_team_stats,
                "coverage_profiles": dataio.all_team_coverage_profiles(),
                "kalshi_error": kalshi_data["error"],
            },
        ),
        "passing.html": (
            "passing", "passing.html",
            {
                "rows": passing_rows, "pp_props": pp_props, "pp_error": pp_error,
                "games": passing_games, "no_game_players": passing_no_game,
                "best5_confidence": best5_data["passing"]["highest_confidence"],
                "best5_value": best5_data["passing"]["best_value"],
                "best5_confidence_warning": best5_correlation_warnings["passing"]["highest_confidence"],
                "best5_value_warning": best5_correlation_warnings["passing"]["best_value"],
                "maturity": calibrate.maturity_summary(),
            },
        ),
        "receiving.html": (
            "receiving",
            "receiving.html",
            {
                "rows": receiving_rows,
                "games": receiving_games, "no_game_players": receiving_no_game,
                "qb_by_team": dataio.correlated_pairs_for_receiving(),
                "pp_props": pp_props,
                "pp_error": pp_error,
                "best5_confidence": best5_data["receiving"]["highest_confidence"],
                "best5_value": best5_data["receiving"]["best_value"],
                "best5_confidence_warning": best5_correlation_warnings["receiving"]["highest_confidence"],
                "best5_value_warning": best5_correlation_warnings["receiving"]["best_value"],
                "maturity": calibrate.maturity_summary(),
            },
        ),
        "receptions.html": (
            "receptions",
            "receptions.html",
            {
                "rows": receptions_rows,
                "games": receptions_games, "no_game_players": receptions_no_game,
                "qb_by_team": dataio.correlated_pairs_for_receiving(),
                "pp_props": pp_props,
                "pp_error": pp_error,
                "best5_confidence": best5_data["receptions"]["highest_confidence"],
                "best5_value": best5_data["receptions"]["best_value"],
                "best5_confidence_warning": best5_correlation_warnings["receptions"]["highest_confidence"],
                "best5_value_warning": best5_correlation_warnings["receptions"]["best_value"],
                "maturity": calibrate.maturity_summary(),
            },
        ),
        "rushing.html": (
            "rushing", "rushing.html",
            {
                "rows": rushing_rows, "pp_props": pp_props, "pp_error": pp_error,
                "games": rushing_games, "no_game_players": rushing_no_game,
                "best5_confidence": best5_data["rushing"]["highest_confidence"],
                "best5_value": best5_data["rushing"]["best_value"],
                "best5_confidence_warning": best5_correlation_warnings["rushing"]["highest_confidence"],
                "best5_value_warning": best5_correlation_warnings["rushing"]["best_value"],
                "maturity": calibrate.maturity_summary(),
            },
        ),
        "touchdowns.html": (
            "touchdowns",
            "touchdowns.html",
            {
                "games": touchdown_games, "no_game_players": touchdown_no_game,
                "rows": touchdown_rows, "kalshi_td_props": kalshi_data["touchdown_props"], "kalshi_error": kalshi_data["error"],
                "best5_confidence": best5_data["touchdowns"]["highest_confidence"],
                "best5_value": best5_data["touchdowns"]["best_value"],
                "best5_confidence_warning": best5_correlation_warnings["touchdowns"]["highest_confidence"],
                "best5_value_warning": best5_correlation_warnings["touchdowns"]["best_value"],
                "maturity": calibrate.maturity_summary(),
                "xtd_debt_watch": dataio.xtd_debt_watch(rushing_rows, receiving_rows),
            },
        ),
        "history.html": (
            "history",
            "history.html",
            {
                "accuracy": accuracy,
                "edge_buckets": grade.edge_bucket_accuracy(),
                "weekly": grade.weekly_breakdown(),
                "trend": grade.accuracy_trend_by_stat(),
                "health_log": pipeline_health.load_health(),
                "has_multi_week_trend": any(len(d["weeks"]) > 1 for d in grade.accuracy_trend_by_stat().values()),
                "calibration": calibration_result,
                "signal_effectiveness": grade.signal_effectiveness(),
                "best5_track": {
                    "passing": {
                        "highest_confidence": grade.best5_track_record("passing_yards", "highest_confidence"),
                        "best_value": grade.best5_track_record("passing_yards", "best_value"),
                    },
                    "receiving": {
                        "highest_confidence": grade.best5_track_record("receiving_yards", "highest_confidence"),
                        "best_value": grade.best5_track_record("receiving_yards", "best_value"),
                    },
                    "receptions": {
                        "highest_confidence": grade.best5_track_record("receptions", "highest_confidence"),
                        "best_value": grade.best5_track_record("receptions", "best_value"),
                    },
                    "rushing": {
                        "highest_confidence": grade.best5_track_record("rushing_yards", "highest_confidence"),
                        "best_value": grade.best5_track_record("rushing_yards", "best_value"),
                    },
                    "touchdowns": {
                        "highest_confidence": grade.best5_track_record("any_td", "highest_confidence"),
                        "best_value": grade.best5_track_record("any_td", "best_value"),
                    },
                },
            },
        ),
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
