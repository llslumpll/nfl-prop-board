"""
Data for the History page's per-prop "Best 5" / "Everything Else" views.

For each prop (passing, receiving, receptions, rushing, touchdowns):
  * Best 5        -- built from grade.best5_track_record() in build.py (unchanged).
  * Everything Else -- every frozen pick for that prop that was NOT tagged into
    either Best 5 list, grouped by week, most recent first, each with its real
    projection, line, edge, call, actual and result.

Reads frozen predictions only; never modifies them.
"""

import predictions

PROPS = [
    # key used by build.py's best5_track / templates, stat column, label, unit
    ("passing", "passing_yards", "Passing yards", "yds"),
    ("receiving", "receiving_yards", "Receiving yards", "yds"),
    ("receptions", "receptions", "Receptions", "rec"),
    ("rushing", "rushing_yards", "Rushing yards", "yds"),
    ("touchdowns", "any_td", "Touchdowns", "TD"),
]


def _result(p: dict) -> str:
    if not p.get("graded"):
        return "pending"
    if p.get("hit") is None:
        return "no_line"
    return "hit" if p["hit"] else "miss"


def _sort_key(p: dict):
    # Picks with a real line first (biggest edge first), then lineless picks
    # by projection, so the ones that can be judged are at the top.
    has_line = p.get("market_line") is not None
    edge = abs(p.get("edge") or 0)
    return (0 if has_line else 1, -edge if has_line else 0, -(p.get("projected") or 0), p.get("player", ""))


def everything_else_by_prop() -> dict:
    preds = list(predictions.load_predictions().values())
    out = {}
    for key, stat, label, unit in PROPS:
        rows = [
            p for p in preds
            if p["stat"] == stat and not (p.get("best5_tags") or [])
        ]
        by_week: dict[int, list[dict]] = {}
        for p in rows:
            by_week.setdefault(p["week"], []).append(p)

        weeks = []
        t_hits = t_graded = t_noline = t_pending = 0
        for week in sorted(by_week, reverse=True):
            picks = sorted(by_week[week], key=_sort_key)
            entries = [{**p, "result": _result(p)} for p in picks]
            hits = sum(1 for e in entries if e["result"] == "hit")
            graded = sum(1 for e in entries if e["result"] in ("hit", "miss"))
            no_line = sum(1 for e in entries if e["result"] == "no_line")
            pending = sum(1 for e in entries if e["result"] == "pending")
            weeks.append({
                "week": week, "picks": entries, "tracked": len(entries),
                "hits": hits, "graded": graded,
                "rate": round(100 * hits / graded, 1) if graded else None,
                "no_line": no_line, "pending": pending,
            })
            t_hits += hits; t_graded += graded; t_noline += no_line; t_pending += pending

        out[key] = {
            "stat": stat, "label": label, "unit": unit, "weeks": weeks,
            "total_tracked": len(rows), "total_hits": t_hits, "total_graded": t_graded,
            "overall_rate": round(100 * t_hits / t_graded, 1) if t_graded else None,
            "total_no_line": t_noline, "total_pending": t_pending,
        }
    return out
