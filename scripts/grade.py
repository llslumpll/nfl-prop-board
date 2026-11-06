"""
Grades every frozen prediction whose game has actually finished, against
the real result -- mirrors the MLB site's grade.py exactly: safe to run
repeatedly, already-graded entries are left alone, games that haven't
finished yet are simply skipped and picked up on a future run.

Only predictions with a real market_line (a real OVER/UNDER call) can be
graded hit/miss. A prediction with no market line still gets its real
"actual" value filled in for transparency, but hit stays None -- there
was never a bet to grade in the first place, and showing a fabricated
hit/miss against nothing would be exactly the kind of dishonesty this
whole project is built to avoid.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import polars as pl
import dataio
import predictions
from datetime import datetime, timezone


def grade_all() -> dict:
    preds = predictions.load_predictions()
    if not preds:
        return {"newly_graded": 0, "total_pending": 0}

    sched = dataio.load_schedule()
    stats = dataio.load_stats()

    newly_graded = 0
    still_pending = 0

    for key, p in preds.items():
        if p.get("graded"):
            continue

        game_rows = sched.filter(
            ((pl.col("home_team") == p["team"]) | (pl.col("away_team") == p["team"]))
            & (pl.col("week") == p["week"])
        )
        if game_rows.height == 0:
            still_pending += 1
            continue
        result = game_rows.row(0, named=True).get("result")
        if result is None:
            still_pending += 1  # game hasn't finished yet -- check again next run
            continue

        player_rows = stats.filter(
            (pl.col("player_display_name") == p["player"]) & (pl.col("week") == p["week"])
        )
        if player_rows.height == 0:
            # Game is final but the player has no stat row that week: he didn't play. PrizePicks and the
            # books void a prop for a player who doesn't play, so this is void too -- not a 0 that hands
            # every UNDER a free win (and every OVER a loss). Fixed Nov 6 2026 (33 such picks had been graded).
            p.update(actual=None, hit=None, void="did not play", graded=True,
                     graded_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
            newly_graded += 1
            continue
        if p["stat"] == "any_td":
            # Touchdowns aren't a single load_stats() column -- real
            # actual is the sum of passing/rushing/receiving TDs for
            # that player/week, matching how the projection itself
            # (_project_total_td in dataio.py) combines them.
            if player_rows.height == 0:
                actual = 0
            else:
                row0 = player_rows.row(0, named=True)
                actual = (row0.get("passing_tds") or 0) + (row0.get("rushing_tds") or 0) + (row0.get("receiving_tds") or 0)
        elif player_rows.height == 0:
            # Game is final but this player has no stat row for that week
            # -- most likely inactive/didn't play. Grade as final with a
            # real actual of 0 rather than leaving it pending forever.
            actual = 0
        else:
            actual = player_rows[0, p["stat"]]
            if actual is None:
                actual = 0

        p["actual"] = actual
        if p.get("market_line") is not None and p.get("call") in ("OVER", "UNDER"):
            if p["call"] == "OVER":
                p["hit"] = actual > p["market_line"]
            else:
                p["hit"] = actual < p["market_line"]
        else:
            p["hit"] = None  # no real market line -- nothing to grade

        p["graded"] = True
        p["graded_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        newly_graded += 1

    # Picks graded before that fix: a player with no stat row that week is re-marked void (once).
    played = {(r[0], r[1]) for r in stats.select("player_display_name", "week").iter_rows()}
    finals = {(r[0], r[2]) for r in sched.filter(pl.col("result").is_not_null()).select("home_team", "away_team", "week").iter_rows()} | \
             {(r[1], r[2]) for r in sched.filter(pl.col("result").is_not_null()).select("home_team", "away_team", "week").iter_rows()}
    revoided = 0
    for p in preds.values():
        if p.get("graded") and not p.get("void") and (p["player"], p["week"]) not in played and (p["team"], p["week"]) in finals:
            p.update(actual=None, hit=None, void="did not play")
            revoided += 1

    if newly_graded or revoided:
        predictions.save_predictions(preds)

    return {"newly_graded": newly_graded, "total_pending": still_pending, "voided_dnp": revoided}


def accuracy_summary() -> dict:
    """
    Real accuracy stats from graded history -- overall, and broken down
    by the confidence tier that was in effect AT FREEZE TIME (not
    recomputed later), so this actually answers "did our stated
    confidence mean anything" rather than grading against a tier that
    changed after the fact.
    """
    preds = predictions.load_predictions()
    graded = [p for p in preds.values() if p.get("graded") and p.get("hit") is not None]

    total = len(graded)
    hits = sum(1 for p in graded if p["hit"])
    overall_rate = round(100 * hits / total, 1) if total else None

    by_tier: dict[str, dict] = {}
    for p in graded:
        tier = p.get("tier_at_freeze") or "unknown"
        by_tier.setdefault(tier, {"total": 0, "hits": 0})
        by_tier[tier]["total"] += 1
        if p["hit"]:
            by_tier[tier]["hits"] += 1
    for tier, d in by_tier.items():
        d["rate"] = round(100 * d["hits"] / d["total"], 1) if d["total"] else None

    ungraded = sum(1 for p in preds.values() if not p.get("graded"))
    no_market = sum(1 for p in preds.values() if p.get("graded") and p.get("hit") is None)

    recent_graded = sorted(
        [p for p in preds.values() if p.get("graded")],
        key=lambda p: p.get("graded_at") or "",
        reverse=True,
    )[:20]

    return {
        "total_frozen": len(preds),
        "total_graded": total + no_market,
        "total_hit_eligible": total,
        "hits": hits,
        "overall_rate": overall_rate,
        "by_tier": by_tier,
        "still_pending": ungraded,
        "graded_no_market": no_market,
        "recent_graded": recent_graded,
    }


def edge_bucket_accuracy() -> list[dict]:
    """
    Real version of the MLB site's "Does Edge Actually Mean Anything?"
    check, adapted for point-projection props instead of probability
    props: does a BIGGER edge (our projection disagreeing with the
    market line by more, as a % of the line) actually predict a HIGHER
    real hit rate? If this climbs going down the table, edge means
    something real. If it stays flat or reverses, the market may
    already be efficient here, and a big edge isn't extra signal beyond
    what the line already reflects.

    Edge is normalized as a % of the market line (not raw yards) so
    stats on very different scales (passing yards vs. receptions) can
    be bucketed together meaningfully.
    """
    preds = predictions.load_predictions()
    graded = [
        p for p in preds.values()
        if p.get("graded") and p.get("hit") is not None
        and p.get("edge") is not None and p.get("market_line")
    ]
    buckets = [(0, 5), (5, 15), (15, 25), (25, float("inf"))]
    labels = ["0-5%", "5-15%", "15-25%", "25%+"]
    result = []
    for (lo, hi), label in zip(buckets, labels):
        in_bucket = [
            p for p in graded
            if lo <= abs(p["edge"]) / p["market_line"] * 100 < hi
        ]
        if not in_bucket:
            result.append({"bucket": label, "total": 0, "hits": 0, "rate": None})
            continue
        hits = sum(1 for p in in_bucket if p["hit"])
        result.append({
            "bucket": label, "total": len(in_bucket), "hits": hits,
            "rate": round(100 * hits / len(in_bucket), 1),
        })
    return result


def best5_track_record(stat: str, list_name: str) -> dict:
    """
    Real week-by-week track record for ONE Best 5 list (e.g.
    passing_yards + "highest_confidence"), using the best5_tags metadata
    written at build time. Only predictions actually tagged into this
    specific list AND actually graded count toward the hit rate -- an
    untagged or ungraded prediction never counts, whether it would have
    helped or hurt the number.
    """
    preds = predictions.load_predictions()
    tagged = [
        p for p in preds.values()
        if p["stat"] == stat and list_name in (p.get("best5_tags") or [])
    ]
    by_week: dict[int, list[dict]] = {}
    for p in tagged:
        by_week.setdefault(p["week"], []).append(p)

    weekly = []
    total_hits = total_graded = 0
    for week in sorted(by_week.keys(), reverse=True):
        entries = sorted(by_week[week], key=lambda p: p["player"])
        graded = [p for p in entries if p.get("graded") and p.get("hit") is not None]
        hits = sum(1 for p in graded if p["hit"])
        weekly.append({
            "week": week, "picks": entries,
            "graded": len(graded), "hits": hits,
            "rate": round(100 * hits / len(graded), 1) if graded else None,
        })
        total_hits += hits
        total_graded += len(graded)

    trend_weeks = [w["week"] for w in reversed(weekly) if w["graded"] > 0]
    trend_rates = [w["rate"] for w in reversed(weekly) if w["graded"] > 0]

    return {
        "weekly": weekly,
        "total_hits": total_hits,
        "total_graded": total_graded,
        "overall_rate": round(100 * total_hits / total_graded, 1) if total_graded else None,
        "trend_weeks": trend_weeks,
        "trend_rates": trend_rates,
    }


def recent_graded_results(limit: int = 8) -> list[dict]:
    """Most recently graded predictions across every prop type, most
    recent first -- real results only (a real market line and a real
    hit/miss), matching the compact 'Recent Results' feed on the MLB
    site's Home page."""
    preds = predictions.load_predictions()
    graded = [
        p for p in preds.values()
        if p.get("graded") and p.get("hit") is not None and p.get("graded_at")
    ]
    graded.sort(key=lambda p: p["graded_at"], reverse=True)
    return graded[:limit]


def weekly_breakdown() -> list[dict]:
    """
    Real week-by-week record -- the NFL-cadence equivalent of the MLB
    site's Day By Day tab. Every frozen prediction shown, most recent
    week first, with real hit/miss/pending status -- not a highlight
    reel.
    """
    preds = predictions.load_predictions()
    by_week: dict[int, list[dict]] = {}
    for p in preds.values():
        by_week.setdefault(p["week"], []).append(p)

    out = []
    for week in sorted(by_week.keys(), reverse=True):
        entries = sorted(by_week[week], key=lambda p: p["player"])
        graded = [p for p in entries if p.get("graded") and p.get("hit") is not None]
        hits = sum(1 for p in graded if p["hit"])
        out.append({
            "week": week,
            "total": len(entries),
            "graded": len(graded),
            "hits": hits,
            "rate": round(100 * hits / len(graded), 1) if graded else None,
            "entries": entries,
        })
    return out


def accuracy_trend_by_stat() -> dict:
    """
    Real rolling accuracy per stat type, per week -- the NFL-cadence
    equivalent of the MLB site's daily rolling-accuracy charts. Will
    naturally start as a short series (one point per week played so
    far) and fill in as real weeks accumulate -- this is honest given
    NFL's weekly (not daily) cadence, not a shorter/lesser version of
    the same idea.
    """
    preds = predictions.load_predictions()
    stats = sorted(set(p["stat"] for p in preds.values()))
    weeks = sorted(set(p["week"] for p in preds.values()))

    trend = {}
    for stat in stats:
        series = []
        for week in weeks:
            graded = [
                p for p in preds.values()
                if p["stat"] == stat and p["week"] == week
                and p.get("graded") and p.get("hit") is not None
            ]
            if not graded:
                series.append(None)
                continue
            hits = sum(1 for p in graded if p["hit"])
            series.append(round(100 * hits / len(graded), 1))
        trend[stat] = {"weeks": weeks, "rates": series}
    return trend


if __name__ == "__main__":
    result = grade_all()
    print(f"Graded {result['newly_graded']} newly-finished prediction(s); "
          f"{result['total_pending']} still pending a final score.")
    summary = accuracy_summary()
    print(f"Accuracy: {summary['hits']}/{summary['total_hit_eligible']} "
          f"({summary['overall_rate']}%) across all graded picks with a real market line.")


def signal_effectiveness(min_sample: int = 20) -> list[dict]:
    """
    Real check on whether each context signal (usage trend, target
    share trend, opportunity flag, recent-form matchup context, injury
    status) actually correlates with real graded outcomes -- not
    assumed, checked. For each real signal value seen at freeze time,
    compares the real hit rate of picks tagged with it against picks
    without it, gated by a real minimum sample on BOTH sides so an early
    read isn't mistaken for a real finding.

    This is what lets a signal graduate from "shown as context" to
    "worth folding into the real projection math" -- but only once
    there's a real, checkable difference, never assumed in advance.
    """
    preds = predictions.load_predictions()
    graded = [p for p in preds.values() if p.get("graded") and p.get("hit") is not None]
    if not graded:
        return []

    # Collect every distinct (signal_key, signal_value) pair seen
    seen = set()
    for p in graded:
        for k, v in (p.get("signals") or {}).items():
            seen.add((k, v))

    results = []
    for key, value in sorted(seen):
        with_signal = [p for p in graded if (p.get("signals") or {}).get(key) == value]
        without_signal = [p for p in graded if key not in (p.get("signals") or {})]
        n_with, n_without = len(with_signal), len(without_signal)
        if n_with < min_sample or n_without < min_sample:
            results.append({
                "signal": key, "value": value,
                "n_with": n_with, "n_without": n_without, "min_sample": min_sample,
                "status": "insufficient data",
            })
            continue
        hit_rate_with = round(100 * sum(1 for p in with_signal if p["hit"]) / n_with, 1)
        hit_rate_without = round(100 * sum(1 for p in without_signal if p["hit"]) / n_without, 1)
        results.append({
            "signal": key, "value": value,
            "n_with": n_with, "n_without": n_without, "min_sample": min_sample,
            "hit_rate_with": hit_rate_with, "hit_rate_without": hit_rate_without,
            "difference_pp": round(hit_rate_with - hit_rate_without, 1),
            "status": "active",
        })
    return results


PP_BREAK_EVEN = 54.2  # % a pick has to hit to break even on a PrizePicks 5/6-pick flex


def home_track_record() -> dict:
    """Home "Track record" tiles (ported from the NBA site, Nov 2026): the lists the site stands behind first,
    every pick and the game picks after, each all season and over the last two weeks. Voided picks (player
    didn't play) never count."""
    import team_predictions
    preds = [p for p in predictions.load_predictions().values()
             if p.get("graded") and p.get("hit") is not None and not p.get("void") and p["stat"] != "any_td"]
    weeks = sorted({p["week"] for p in preds})
    recent = set(weeks[-2:])

    def tile(label, rows, sub=None):
        n, h = len(rows), sum(1 for p in rows if p["hit"])
        rn = [p for p in rows if p["week"] in recent]
        rh = sum(1 for p in rn if p["hit"])
        return {"label": label, "n": n, "hits": h, "rate": round(100 * h / n, 1) if n else None,
                "rn": len(rn), "rh": rh, "rrate": round(100 * rh / len(rn)) if rn else None, "sub": sub}

    tiles = [tile("Best 5", [p for p in preds if "highest_confidence" in (p.get("best5_tags") or [])]),
             tile("Best Value", [p for p in preds if "best_value" in (p.get("best5_tags") or [])]),
             tile("Every prop pick", preds, f"each player & stat with a line, not bets")]
    games = [g for g in team_predictions.load_team_predictions().values()
             if g.get("graded") and g.get("our_correct") is not None]
    gw = sorted({g["week"] for g in games})
    grecent = set(gw[-2:])
    gn, gh = len(games), sum(1 for g in games if g["our_correct"])
    gr = [g for g in games if g["week"] in grecent]
    ml = [g for g in games if g["market"] == "team_moneyline"]
    fav = sum(1 for g in ml if g.get("market_correct"))
    tiles.append({"label": "Game picks", "n": gn, "hits": gh, "rate": round(100 * gh / gn, 1) if gn else None,
                  "rn": len(gr), "rh": sum(1 for g in gr if g["our_correct"]),
                  "rrate": round(100 * sum(1 for g in gr if g["our_correct"]) / len(gr)) if gr else None,
                  "sub": (f"moneyline, spread & total; the betting favorite won {fav} of {len(ml)} games" if ml else None)})
    try:
        clv = clv_summary()
    except Exception:  # noqa: BLE001
        clv = None
    return {"tiles": tiles, "need": PP_BREAK_EVEN, "recent_weeks": sorted(recent), "clv": clv}


def pick_records() -> dict:
    """Real hit rate by stat and side ("receptions|OVER"), voids excluded: the honest number shown on cards in
    place of the model's confidence, which has run far ahead of reality (60-80% claimed, about 50% hit)."""
    out = {}
    for p in predictions.load_predictions().values():
        if not p.get("graded") or p.get("hit") is None or p.get("void") or p["stat"] == "any_td" \
                or p.get("market_line") is None or p.get("call") not in ("OVER", "UNDER"):
            continue
        r = out.setdefault(f"{p['stat']}|{p['call']}", {"n": 0, "h": 0})
        r["n"] += 1
        r["h"] += bool(p["hit"])
    for r in out.values():
        r["rate"] = round(100 * r["h"] / r["n"], 1)
    return out


def clv_summary() -> dict:
    """Beat the closing line: for picks whose line moved between our pick (frozen at the first line seen) and the
    last line seen before kickoff, how often it moved our way -- and how picks did at the line we picked."""
    import line_movement_log
    log = line_movement_log._load()
    c = {"toward": [0, 0, 0], "away": [0, 0, 0], "flat": [0, 0, 0]}   # [picks, graded, hits]
    for p in predictions.load_predictions().values():
        if p.get("market_line") is None or p.get("call") not in ("OVER", "UNDER") or p["stat"] == "any_td":
            continue
        e = log.get(f"{p['player']}|{p['stat']}|{p['week']}")
        if not e:
            continue
        mv = e["current_line"] - p["market_line"]
        k = "flat" if mv == 0 else ("toward" if (mv > 0) == (p["call"] == "OVER") else "away")
        c[k][0] += 1
        if p.get("graded") and p.get("hit") is not None and not p.get("void"):
            c[k][1] += 1
            c[k][2] += bool(p["hit"])
    moved = c["toward"][0] + c["away"][0]
    rate = lambda k: round(100 * c[k][2] / c[k][1], 1) if c[k][1] else None  # noqa: E731
    return {"moved": moved, "toward": c["toward"][0], "away": c["away"][0],
            "pct": round(100 * c["toward"][0] / moved) if moved else None,
            "toward_hit": rate("toward"), "away_hit": rate("away"), "flat_hit": rate("flat"),
            "toward_n": c["toward"][1], "away_n": c["away"][1]}
