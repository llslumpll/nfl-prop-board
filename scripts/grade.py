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

    if newly_graded:
        predictions.save_predictions(preds)

    return {"newly_graded": newly_graded, "total_pending": still_pending}


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
