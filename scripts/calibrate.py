"""
Empirical calibration: reads every graded prediction, checks whether the
model's own past projections actually matched what happened, and writes
a gentle correction to data/calibration.json for project_stat() to apply
on its next run. Same technique as the MLB site's calibrate.py -- a
well-understood statistical technique (bias correction), not machine
learning, and not a change to the underlying dampening formulas.

Adapted for point-projection props instead of MLB's probability props:
MLB's HR calibration corrects a PROBABILITY (multiply toward the real
hit rate). Our yardage/receptions props don't have a probability to
correct -- what we have is a projected NUMBER, so this corrects that
number directly: average real bias (actual - projected) within a
confidence tier, added back (dampened) to future projections in that
tier. Touchdowns DO have a real probability (model_prob vs Kalshi's
market) once graded, so those get the MLB-style probability-shrink
treatment instead -- see calibrate_touchdown_shrink.

Safety rails, same as MLB:
  - MIN_SAMPLE: no correction applied to a tier until it has at least
    this many graded entries (uses this site's own NFL-appropriate
    MIN_SAMPLE=30, not MLB's daily-cadence MIN_SAMPLE=100).
  - DAMPEN: only 30% of the raw computed correction is ever applied,
    same "shrink toward the prior" philosophy used everywhere else on
    this site.
  - Bounded output, even after dampening.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import json
import dataio
import predictions

CALIBRATION_DAMPEN = 0.3
BIAS_BOUNDS = (-15.0, 15.0)  # yards/receptions, generous but real cap
PROB_SHRINK_BOUNDS = (0.3, 1.0)  # never amplify TD confidence, only ever shrink it

YARDAGE_STATS = ["passing_yards", "rushing_yards", "receiving_yards", "receptions"]


def calibrate_stat_bias(stat: str) -> dict:
    """Per-tier average real bias (actual - projected) for one yardage/
    receptions stat, gated by dataio.MIN_SAMPLE, dampened, bounded."""
    preds = predictions.load_predictions()
    graded = [
        p for p in preds.values()
        if p["stat"] == stat and p.get("graded") and p.get("actual") is not None
        and p.get("market_line") is not None  # only entries with a real market comparison
    ]

    by_tier: dict[str, list[float]] = {}
    for p in graded:
        tier = p.get("tier_at_freeze") or "unknown"
        by_tier.setdefault(tier, []).append(p["actual"] - p["projected"])

    result = {}
    for tier, biases in by_tier.items():
        n = len(biases)
        if n < dataio.MIN_SAMPLE:
            result[tier] = {"bias": 0.0, "sample_size": n, "status": "insufficient data"}
            continue
        avg_raw_bias = sum(biases) / n
        dampened = CALIBRATION_DAMPEN * avg_raw_bias
        dampened = max(BIAS_BOUNDS[0], min(BIAS_BOUNDS[1], dampened))
        result[tier] = {
            "bias": round(dampened, 2), "sample_size": n,
            "avg_raw_bias": round(avg_raw_bias, 2), "status": "active",
        }
    return result


def calibrate_touchdown_shrink() -> dict:
    """
    MLB-style probability shrink for touchdown predictions specifically,
    since those DO carry a real model_prob vs market_prob (unlike the
    yardage stats above). Finds shrinkFactor via simple linear regression
    through the origin: adjusted = 0.5 + (raw - 0.5) * shrinkFactor.
    """
    preds = predictions.load_predictions()
    valid = [
        p for p in preds.values()
        if p["stat"] == "any_td" and p.get("graded") and p.get("hit") is not None
        and p.get("model_prob") is not None and p.get("call") in ("OVER", "UNDER")
    ]
    n = len(valid)
    if n < dataio.MIN_SAMPLE:
        return {"shrink_factor": 1.0, "sample_size": n, "status": "insufficient data"}

    sum_xy, sum_xx = 0.0, 0.0
    for p in valid:
        prob = p["model_prob"] / 100
        call_confidence = prob if p["call"] == "OVER" else (1 - prob)
        x = call_confidence - 0.5
        y = (1.0 if p["hit"] else 0.0) - 0.5
        sum_xy += x * y
        sum_xx += x * x

    if sum_xx <= 0:
        return {"shrink_factor": 1.0, "sample_size": n, "status": "insufficient data"}

    raw_shrink = sum_xy / sum_xx
    dampened = 1 + CALIBRATION_DAMPEN * (raw_shrink - 1)
    dampened = max(PROB_SHRINK_BOUNDS[0], min(PROB_SHRINK_BOUNDS[1], dampened))
    return {"shrink_factor": round(dampened, 3), "sample_size": n, "raw_shrink_factor": round(raw_shrink, 3), "status": "active"}


def calibrate_yardage_prob_shrink(stat: str) -> dict:
    """
    Same real technique as calibrate_touchdown_shrink, generalized to
    yardage/receptions props -- checks whether the "Highest Confidence"
    ranking's stated probability actually matches real outcomes (is an
    "80% confident" pick really hitting 80% of the time), using the real
    model_prob frozen alongside each prediction at freeze time. Note:
    yardage model_prob is stored as a 0-1 fraction (not the 0-100 scale
    touchdowns use), so no /100 conversion here.
    """
    preds = predictions.load_predictions()
    valid = [
        p for p in preds.values()
        if p["stat"] == stat and p.get("graded") and p.get("hit") is not None
        and p.get("model_prob") is not None and p.get("call") in ("OVER", "UNDER")
    ]
    n = len(valid)
    if n < dataio.MIN_SAMPLE:
        return {"shrink_factor": 1.0, "sample_size": n, "status": "insufficient data"}

    sum_xy, sum_xx = 0.0, 0.0
    for p in valid:
        prob = p["model_prob"]
        call_confidence = prob if p["call"] == "OVER" else (1 - prob)
        x = call_confidence - 0.5
        y = (1.0 if p["hit"] else 0.0) - 0.5
        sum_xy += x * y
        sum_xx += x * x

    if sum_xx <= 0:
        return {"shrink_factor": 1.0, "sample_size": n, "status": "insufficient data"}

    raw_shrink = sum_xy / sum_xx
    dampened = 1 + CALIBRATION_DAMPEN * (raw_shrink - 1)
    dampened = max(PROB_SHRINK_BOUNDS[0], min(PROB_SHRINK_BOUNDS[1], dampened))
    return {"shrink_factor": round(dampened, 3), "sample_size": n, "raw_shrink_factor": round(raw_shrink, 3), "status": "active"}


def run() -> dict:
    calibration = {
        "min_sample": dataio.MIN_SAMPLE,
        "dampen": CALIBRATION_DAMPEN,
        "generated_at": dataio.FROZEN_AT,
        "bias": {stat: calibrate_stat_bias(stat) for stat in YARDAGE_STATS},
        "touchdown_prob_shrink": calibrate_touchdown_shrink(),
        "confidence_shrink": {stat: calibrate_yardage_prob_shrink(stat) for stat in YARDAGE_STATS},
    }
    DATA_DIR = Path(__file__).parent.parent / "data"
    (DATA_DIR / "calibration.json").write_text(json.dumps(calibration, indent=2))
    return calibration


def maturity_summary() -> dict:
    """
    Real, honest rollup of calibration progress across the whole site --
    used for the maturity indicator shown on Home and each stat page, so
    "is this trustworthy yet" has a real, visible answer instead of
    requiring a trip to History's calibration table. Reads the same
    calibration.json every other calibration display reads; no separate
    computation, so it can never say something different from History.
    """
    DATA_DIR = Path(__file__).parent.parent / "data"
    cal_path = DATA_DIR / "calibration.json"
    if not cal_path.exists():
        return {"tiers": [], "active_count": 0, "total_count": 0, "closest": None}

    cal = json.loads(cal_path.read_text())
    min_sample = cal.get("min_sample", 30)
    tiers = []

    for stat, by_tier in (cal.get("bias") or {}).items():
        for tier_label, d in by_tier.items():
            tiers.append({
                "stat": stat, "tier": tier_label, "kind": "projection bias",
                "sample_size": d.get("sample_size", 0), "min_sample": min_sample,
                "status": d.get("status", "insufficient data"),
            })

    for stat, d in (cal.get("confidence_shrink") or {}).items():
        tiers.append({
            "stat": stat, "tier": None, "kind": "confidence calibration",
            "sample_size": d.get("sample_size", 0), "min_sample": min_sample,
            "status": d.get("status", "insufficient data"),
        })

    td = cal.get("touchdown_prob_shrink")
    if td:
        tiers.append({
            "stat": "any_td", "tier": None, "kind": "touchdown probability",
            "sample_size": td.get("sample_size", 0), "min_sample": min_sample,
            "status": td.get("status", "insufficient data"),
        })

    active_count = sum(1 for t in tiers if t["status"] == "active")
    pending = [t for t in tiers if t["status"] != "active"]
    closest = max(pending, key=lambda t: t["sample_size"]) if pending else None

    return {
        "tiers": sorted(tiers, key=lambda t: t["sample_size"], reverse=True),
        "active_count": active_count,
        "total_count": len(tiers),
        "closest": closest,
        "min_sample": min_sample,
    }


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
