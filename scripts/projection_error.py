"""
Projection accuracy for EVERY graded pick whose player actually played --
independent of whether a market line ever existed.

Hit/miss needs a line, so roughly 40% of picks can never get one. This answers
the other honest question: how close was our projection to what really
happened, and is that getting better week over week? Nothing here is
fabricated -- it reads only the frozen `projected` and the real `actual`.

  * Players with no stat row that week (inactive / did not play) are EXCLUDED,
    not scored as a 0 -- grade.py records a 0 for them, which would otherwise
    look like a huge miss on a player who never took the field.
  * Yardage / receptions: error = actual - projected. We report MAE (typical
    miss size) and bias (mean signed error; positive = we under-projected).
  * Touchdowns: the projection is an expected TD count, so we also score the
    implied chance of 1+ TD (Poisson) with a Brier score and compare it to a
    naive "everyone gets the league-average rate" Brier on the same sample.
    Lower Brier than naive = the model carries real information.
"""

import math

import dataio
import predictions

STAT_LABELS = {
    "passing_yards": "Passing yards",
    "receiving_yards": "Receiving yards",
    "receptions": "Receptions",
    "rushing_yards": "Rushing yards",
    "any_td": "Touchdowns",
}


def _played_set() -> set[tuple[str, int]]:
    stats = dataio.load_stats()
    return {
        (r["player_display_name"], r["week"])
        for r in stats.select(["player_display_name", "week"]).iter_rows(named=True)
    }


def projection_error_summary() -> dict:
    preds = predictions.load_predictions()
    played = _played_set()

    scored: dict[str, list[dict]] = {}
    excluded = 0
    for p in preds.values():
        if not p.get("graded") or p.get("actual") is None or p.get("projected") is None:
            continue
        if (p["player"], p["week"]) not in played:
            excluded += 1
            continue
        scored.setdefault(p["stat"], []).append(p)

    by_stat = {}
    for stat, rows in scored.items():
        errs = [p["actual"] - p["projected"] for p in rows]
        weeks: dict[int, list[float]] = {}
        for p, e in zip(rows, errs):
            weeks.setdefault(p["week"], []).append(e)
        weekly = [
            {
                "week": w,
                "n": len(es),
                "mae": round(sum(abs(e) for e in es) / len(es), 2),
                "bias": round(sum(es) / len(es), 2),
            }
            for w, es in sorted(weeks.items())
        ]
        entry = {
            "label": STAT_LABELS.get(stat, stat),
            "n": len(rows),
            "mae": round(sum(abs(e) for e in errs) / len(errs), 2),
            "bias": round(sum(errs) / len(errs), 2),
            "weekly": weekly,
            # Improving = latest week's MAE below the first week's, with 2+ weeks of data.
            "trend": (
                None if len(weekly) < 2
                else "improving" if weekly[-1]["mae"] < weekly[0]["mae"]
                else "worse" if weekly[-1]["mae"] > weekly[0]["mae"]
                else "flat"
            ),
        }
        if stat == "any_td":
            outcomes = [1.0 if p["actual"] >= 1 else 0.0 for p in rows]
            probs = [1 - math.exp(-max(p["projected"], 0.0)) for p in rows]
            base = sum(outcomes) / len(outcomes)
            entry["brier"] = round(sum((q - o) ** 2 for q, o in zip(probs, outcomes)) / len(rows), 4)
            entry["brier_naive"] = round(base * (1 - base), 4)
            entry["td_rate"] = round(100 * base, 1)
            entry["beats_naive"] = entry["brier"] < entry["brier_naive"]
        by_stat[stat] = entry

    return {"by_stat": by_stat, "excluded_did_not_play": excluded}
