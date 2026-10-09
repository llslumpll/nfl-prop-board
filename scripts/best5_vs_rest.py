"""
Do the Best 5 lists actually beat the site's other picks?

For every prop type and each of the two Best 5 lists, compares:
  * Best 5   = graded picks that were tagged into that list (with a real
               line, so a real hit/miss exists), against
  * The rest = every OTHER graded pick for the same prop that had a real line
               and was not in that list.

If the Best 5 hit rate is clearly higher, the ranking is finding the site's
better calls. If not, it isn't adding anything yet -- and the page says so.

Reads only frozen, graded predictions. Never changes them.
"""

import math

import predictions

STATS = [
    ("passing_yards", "Passing yards"),
    ("receiving_yards", "Receiving yards"),
    ("receptions", "Receptions"),
    ("rushing_yards", "Rushing yards"),
    ("any_td", "Touchdowns"),
]
LISTS = [("highest_confidence", "Highest Confidence"), ("best_value", "Best Value")]

# Below this many picks on EITHER side, a gap is mostly luck -- say so.
MIN_PER_SIDE = 20


def _rate(rows):
    n = len(rows)
    hits = sum(1 for p in rows if p["hit"])
    return n, hits, (round(100 * hits / n, 1) if n else None)


def _verdict(n_a, h_a, n_b, h_b):
    if n_a < MIN_PER_SIDE or n_b < MIN_PER_SIDE:
        return "too early"
    p_a, p_b = h_a / n_a, h_b / n_b
    pooled = (h_a + h_b) / (n_a + n_b)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n_a + 1 / n_b))
    if se == 0:
        return "no difference"
    z = (p_a - p_b) / se
    if z >= 1.96:
        return "Best 5 is clearly better"
    if z <= -1.96:
        return "Best 5 is clearly worse"
    return "no clear difference"


def best5_vs_rest() -> list[dict]:
    preds = [
        p for p in predictions.load_predictions().values()
        if p.get("graded") and p.get("hit") is not None
    ]
    out = []
    for stat, stat_label in STATS:
        stat_preds = [p for p in preds if p["stat"] == stat]
        for list_name, list_label in LISTS:
            best = [p for p in stat_preds if list_name in (p.get("best5_tags") or [])]
            rest = [p for p in stat_preds if list_name not in (p.get("best5_tags") or [])]
            n_b, h_b, r_b = _rate(best)
            n_r, h_r, r_r = _rate(rest)
            out.append({
                "stat": stat, "stat_label": stat_label,
                "list": list_name, "list_label": list_label,
                "best_n": n_b, "best_hits": h_b, "best_rate": r_b,
                "rest_n": n_r, "rest_hits": h_r, "rest_rate": r_r,
                "gap_pp": round(r_b - r_r, 1) if r_b is not None and r_r is not None else None,
                "verdict": _verdict(n_b, h_b, n_r, h_r) if n_b and n_r else "need graded picks on both sides",
                "min_per_side": MIN_PER_SIDE,
            })
    return out
