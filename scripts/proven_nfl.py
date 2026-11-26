"""Proven picks for the NFL site (nfl-update-21, Nov 26 2026): every kind of pick the site tracks, graded against
what it cost, the NFL version of the NBA site's "What's beating the price".

A kind's price p is what you'd have paid: Kalshi ask, 1 / parlay payout, the tipster's posted odds (implied), or the
PrizePicks 5/6-pick flex break-even (54.2%) for the site's own prop calls. edge = hit rate - average p.
Status (same rules as the NBA site):
  PROVEN    50+ picks and the hit rate's 80% low bound is above the price
  PROMISING 15+ picks and 2+ points above the price
  AVOID     30+ picks and 2+ points below the price
  TRACKING  everything else
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
PP_BREAK_EVEN = 0.542
Z = 1.28
PROVEN_N, PROMISING_N, AVOID_N, EDGE = 50, 15, 30, 0.02

KINDS = {
    "b5k": ("Best 5 on Kalshi", "Home page: the 5 Kalshi rungs a week with the biggest real edge, each leg at its ask"),
    "t3k": ("3x slip", "Home page: one Kalshi combo at a 3x+ estimated quote"),
    "doc_game": ("Dr. Profit spreads/ML", "His spreads and moneylines at his posted odds"),
    "doc_prop": ("Dr. Profit props", "His straight player props (not ladders) at his posted odds"),
    "doc_lad": ("Dr. Profit ladder base rungs", "The first rung of his yardage/reception ladders"),
    "nlb": ("Next Level Bets", "His written NFL plays at posted odds"),
    "jake": ("Jake Venom TD Watch", "Players in his TD Watch video, at his app's TD %"),
    "bhb": ("BlazinHotBets slips", "His TD lotto and SGP slips, 1U a slip"),
    "edge_ml": ("Edges: moneyline", "Games page Edges tab: Kalshi moneylines priced below FanDuel's no-vig"),
    "edge_spread": ("Edges: spread", "Games page Edges tab: Kalshi spreads priced below FanDuel's no-vig"),
    "edge_total": ("Edges: total", "Games page Edges tab: Kalshi totals priced below FanDuel's no-vig"),
    "pz_props": ("Parlays: safest props", "Parlays page props slips at PrizePicks Power payouts"),
    "pz_ml": ("Parlays: moneyline", "Parlays page moneyline slips at FanDuel prices"),
    "pz_td": ("Parlays: touchdowns", "Parlays page TD slips at Kalshi prices"),
}
STAT_NAME = {"passing_yards": "passing yards", "rushing_yards": "rushing yards", "receiving_yards": "receiving yards",
             "receptions": "receptions"}
for st, nm in STAT_NAME.items():
    for call in ("OVER", "UNDER"):
        KINDS[f"model|{st}|{call}"] = (f"Site {call.lower()}s: {nm}", f"The site's own {call} calls on {nm} vs the PrizePicks line, priced at the 5/6-pick flex break-even (54.2%)")


def _imp(odds) -> float | None:
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return None
    return 100 / (o + 100) if o > 0 else -o / (-o + 100)


def _stat(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0, "w": 0, "l": 0, "rate": None, "price": None, "edge": None, "u": 0.0, "status": "TRACKING"}
    w = sum(r["hit"] for r in rows)
    p = sum(r["p"] for r in rows) / n
    rate = w / n
    u = sum((1 - r["p"]) / r["p"] if r["hit"] else -1 for r in rows)
    lo = rate - Z * math.sqrt(max(rate * (1 - rate), 1e-9) / n)
    edge = rate - p
    if n >= PROVEN_N and lo > p:
        status = "PROVEN"
    elif n >= PROMISING_N and edge >= EDGE:
        status = "PROMISING"
    elif n >= AVOID_N and edge <= -EDGE:
        status = "AVOID"
    else:
        status = "TRACKING"
    return {"n": n, "w": w, "l": n - w, "rate": round(100 * rate, 1), "price": round(100 * p, 1),
            "edge": round(100 * edge, 1), "u": round(u, 2), "status": status}


def build(tp: dict | None, kb: dict | None) -> dict:
    hist = defaultdict(list)
    # the site's Kalshi cards
    for wk in (kb or {}).get("best5_weeks") or []:
        for l in wk["legs"]:
            if l.get("res") in ("hit", "miss"):
                hist["b5k"].append({"hit": l["res"] == "hit", "p": l["ask"]})
    for wk in (kb or {}).get("triple_weeks") or []:
        if wk.get("hit") is not None:
            hist["t3k"].append({"hit": bool(wk["hit"]), "p": 1 / wk["quote_est"]})
    # tipsters
    try:
        import tipsters_nfl as TN
        box = TN.Box()
        for p in TN._load():
            r = TN.grade_pick(p, box)
            if r["res"] not in ("hit", "miss"):
                continue
            pr = _imp(p["odds"])
            if pr is None:
                continue
            t = p["tipster"]
            if t == "doc":
                k = "doc_game" if p["kind"] in ("spread", "ml") else ("doc_lad" if p.get("rungs") else "doc_prop")
            else:
                k = t
            if k in KINDS:
                hist[k].append({"hit": r["res"] == "hit", "p": pr})
    except Exception as e:  # noqa: BLE001
        print(f"Proven picks: tipsters failed: {e}")
    # edges
    try:
        for wk in json.loads((DATA / "edges_nfl.json").read_text()).values():
            for v in wk.values():
                if v.get("hit") is None or not v.get("cost"):
                    continue
                k = {"moneyline": "edge_ml", "spread": "edge_spread", "total": "edge_total"}.get(v.get("kind"))
                if k:
                    hist[k].append({"hit": bool(v["hit"]), "p": v["cost"]})
    except Exception as e:  # noqa: BLE001
        print(f"Proven picks: edges failed: {e}")
    # parlays
    try:
        for wk in json.loads((DATA / "parlays.json").read_text()).values():
            for v in wk.values():
                if v.get("status") not in ("hit", "miss") or not v.get("decimal"):
                    continue
                k = {"props": "pz_props", "ml": "pz_ml", "td": "pz_td"}.get(v.get("family"))
                if k:
                    hist[k].append({"hit": v["status"] == "hit", "p": 1 / v["decimal"]})
    except Exception as e:  # noqa: BLE001
        print(f"Proven picks: parlays failed: {e}")
    # the site's own prop calls
    try:
        for p in json.loads((DATA / "predictions.json").read_text()).values():
            if p.get("graded") and p.get("hit") is not None and p.get("call") in ("OVER", "UNDER") and p["stat"] in STAT_NAME:
                hist[f"model|{p['stat']}|{p['call']}"].append({"hit": bool(p["hit"]), "p": PP_BREAK_EVEN})
    except Exception as e:  # noqa: BLE001
        print(f"Proven picks: predictions failed: {e}")
    order = {"PROVEN": 0, "PROMISING": 1, "TRACKING": 2, "AVOID": 3}
    kinds = [{"key": k, "name": n, "what": w, **_stat(hist[k])} for k, (n, w) in KINDS.items()]
    kinds.sort(key=lambda k: (order[k["status"]], -(k["edge"] if k["edge"] is not None else -99), -k["n"]))
    return {"kinds": kinds}
