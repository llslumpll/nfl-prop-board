"""
Games page data, NBA-style (Nov 2026): every game pick (moneyline, spread, total) with a chance, FanDuel's
price and break-even, a GOOD PRICE check, Top Picks / Best Value lists, a live-tracker blob and the Edges tab.

Chances, honestly:
  * moneyline: the frozen pick's own number (25% our model, 75% FanDuel's no-vig price).
  * spread / total: start from FanDuel's no-vig chance at the pick's number and move toward our model's chance
    (normal curve around our projected margin / total) only as far as that market's graded record has earned:
    weight = (hit rate - 50%) x 4, capped at 50%, zero below 50% or under 30 graded picks. NFL spreads and totals
    have hit 40-43% so far, so today their chances are FanDuel's: shown, tracked, labelled leans.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from statistics import NormalDist

import edges_nfl
import oddsapi_client
import team_predictions

ND = NormalDist()
MARGIN_SD, TOTAL_SD = edges_nfl.MARGIN_SD, edges_nfl.TOTAL_SD
MKTS = [("team_moneyline", "moneyline", "Moneyline"), ("team_spread", "spread", "Spread"), ("team_total", "total", "Total")]


def _weight(track: dict) -> float:
    n, r = track.get("n") or 0, track.get("our_hit_rate")
    if n < 30 or r is None:
        return 0.0
    return max(0.0, min(0.5, (r / 100 - 0.5) * 4))


def _shift(p, d, sd):
    """FanDuel's fair chance at its number, moved to a number d points easier for the bettor."""
    p = min(max(p, 0.01), 0.99)
    return ND.cdf(ND.inv_cdf(p) + d / sd)


def _ptxt(x):
    return f"{x:+g}" if x else "PK"


def build(gp: dict, fd_games: list[dict], sched, kal_all: list[dict], now: datetime | None = None) -> dict:
    import line_attach
    now = now or datetime.now(timezone.utc)
    week = gp.get("week")
    track = {m: team_predictions.track_record(m) for m, _, _ in MKTS}
    wts = {k: _weight(track[m]) for m, k, _ in MKTS}
    cards, allp, live, eg = [], [], [], []
    for g in gp.get("games", []):
        home, away = g["home"], g["away"]
        ko = line_attach._kickoff_utc(home, week, sched)
        started = g["final"] or (ko is not None and now >= ko)
        hn, an = oddsapi_client.TEAM_NAMES.get(home), oddsapi_client.TEAM_NAMES.get(away)
        fd = next((x for x in fd_games if x.get("home_team") == hn and x.get("away_team") == an), None) or {}
        picks = []
        for m, key, label in MKTS:
            p = (g.get("picks") or {}).get(m)
            if not p or p.get("market_line") is None:
                continue
            row = {"market": key, "mlabel": label, "away": away, "home": home, "game": f"{away} @ {home}",
                   "locked": started, "result": None, "reason": p.get("reason")}
            if p.get("graded") and p.get("our_correct") is not None:
                row["result"] = "hit" if p["our_correct"] else "miss"
            if key == "moneyline":
                side = home if p["our_call"] == "HOME" else away
                chance = p["our_value"] / 100 if side == home else 1 - p["our_value"] / 100
                price = fd.get("home_price") if side == home else fd.get("away_price")
                fair = None
                if fd.get("home_price") is not None:
                    ph, pa = edges_nfl.devig_power(edges_nfl.implied(fd["home_price"]), edges_nfl.implied(fd["away_price"]))
                    fair = ph if side == home else pa
                row.update(side=side, pick=f"{side} ML", line=None, chance=chance, model=None, fair=fair,
                           note=f"25% our model, 75% FanDuel's price")
            elif key == "spread":
                side = home if p["our_call"] == "HOME" else away
                pt = p["market_line"] if side == home else -p["market_line"]      # the side's points
                m_side = p["our_value"] if side == home else -p["our_value"]      # our margin for that side
                model = 1 - ND.cdf((-pt - m_side) / MARGIN_SD)
                price, fair = None, None
                if fd.get("home_spread") is not None and fd.get("home_spread_price") is not None:
                    ph, pa = edges_nfl.devig_power(edges_nfl.implied(fd["home_spread_price"]), edges_nfl.implied(fd["away_spread_price"]))
                    fd_pt = fd["home_spread"] if side == home else fd["away_spread"]
                    fair = _shift(ph if side == home else pa, pt - fd_pt, MARGIN_SD)
                    fd_px = fd["home_spread_price"] if side == home else fd["away_spread_price"]
                    if abs(pt - fd_pt) < 1e-6:
                        price = fd_px
                    else:
                        row["now"] = f"{side} {_ptxt(fd_pt)} ({oddsapi_client.fmt_price(fd_px)})"
                chance = model if fair is None else fair + wts["spread"] * (model - fair)
                row.update(side=side, pick=f"{side} {_ptxt(pt)}", line=pt, chance=chance, model=model, fair=fair,
                           note=f"we have {home if p['our_value'] > 0 else away} by {abs(p['our_value']):.1f}")
            else:
                side = p["our_call"]
                T = p["market_line"]
                po_model = 1 - ND.cdf((T - p["our_value"]) / TOTAL_SD)
                model = po_model if side == "OVER" else 1 - po_model
                price, fair = None, None
                if fd.get("total_point") is not None and fd.get("over_price") is not None:
                    po, pu = edges_nfl.devig_power(edges_nfl.implied(fd["over_price"]), edges_nfl.implied(fd["under_price"]))
                    d = (fd["total_point"] - T) if side == "OVER" else (T - fd["total_point"])
                    fair = _shift(po if side == "OVER" else pu, d, TOTAL_SD)
                    fd_px = fd["over_price"] if side == "OVER" else fd["under_price"]
                    if abs(T - fd["total_point"]) < 1e-6:
                        price = fd_px
                    else:
                        row["now"] = f"{side} {fd['total_point']:g} ({oddsapi_client.fmt_price(fd_px)})"
                chance = model if fair is None else fair + wts["total"] * (model - fair)
                row.update(side=side, pick=f"{side} {T:g}", line=T, chance=chance, model=model, fair=fair,
                           note=f"we project {p['our_value']:.1f}")
            row["chance"] = round(min(max(row["chance"], 0.02), 0.98), 4)
            row["price"] = price
            row["price_str"] = oddsapi_client.fmt_price(price) if price is not None else None
            row["be"] = round(edges_nfl.implied(price), 4) if price is not None else None
            row["good"] = row["be"] is not None and row["chance"] > row["be"]
            row["weight"] = wts.get(key)
            picks.append(row)
        cards.append({**g, "kickoff": ko.isoformat() if ko else None, "started": started, "gpicks": picks})
        allp += [p for p in picks if not p["locked"]]
        live.append({"h": home, "a": away, "ko": ko.isoformat() if ko else None,
                     "p": [{"m": p["market"], "s": p["side"], "l": p["line"], "t": p["pick"]} for p in picks]})
        if ko and not started:
            mdl = {}
            for p in picks:
                if p["market"] == "moneyline":
                    mdl["ml_home"] = p["chance"] if p["side"] == home else 1 - p["chance"]
            gpk = g.get("picks") or {}
            if gpk.get("team_spread"):
                mdl["margin"] = gpk["team_spread"]["our_value"]
            if gpk.get("team_total"):
                mdl["total"] = gpk["team_total"]["our_value"]
            eg.append({"week": week, "away": away, "home": home, "kickoff": ko.isoformat(), "model": mdl})

    def _top(rows, n=5, key=lambda p: -p["chance"]):
        out, seen = [], set()
        for p in sorted(rows, key=key):
            if p["game"] in seen:
                continue
            seen.add(p["game"])
            out.append(p)
            if len(out) == n:
                break
        return out

    priced = [p for p in allp if p["be"] is not None]
    by_mkt = {}
    for m, key, label in MKTS:
        rows = [p for p in allp if p["market"] == key]
        by_mkt[key] = {"label": label, "best5": _top(rows), "value": sorted([p for p in rows if p["good"]], key=lambda p: -(p["chance"] - p["be"])),
                       "n": len([p for p in rows if p["be"] is not None]), "n_good": sum(1 for p in rows if p["good"]),
                       "track": track[m], "weight": wts[key]}
    try:
        found = edges_nfl.find(eg, fd_games, kal_all, now)
        found["recorded_new"] = edges_nfl.record(found, now)
    except Exception as e:  # noqa: BLE001
        print(f"Edges failed: {e}")
        found = {"edges": [], "suspect": [], "arbs": [], "middles": [], "checked": 0, "error": str(e)[:160]}
    return {"cards": cards, "top": {"best5": _top(priced), "value": _top([p for p in priced if p["good"]], key=lambda p: -(p["chance"] - p["be"]))},
            "mkts": by_mkt, "edges": found, "eds": edges_nfl.summary(), "live_json": json.dumps(live, separators=(",", ":")),
            "fd_max_age": edges_nfl.FD_MAX_AGE_H}
