"""
EDGES for the NFL Games page (ported from the NBA site, Nov 2026).

Market math, no projection: take FanDuel's two-sided price, remove its cut with the power method (fairer to
favorites than a simple split), and compare that fair chance with what Kalshi charges for the same outcome,
fee included.

  +EV         Kalshi cost (ask + fee) below the fair chance: EV = fair / cost - 1.
  arbitrage   Kalshi side + FanDuel's other side cost under $1 together: a profit whatever happens.
  middle      FanDuel and Kalshi use different numbers on a spread or total: both win if the result lands
              in between (chance from a normal curve around FanDuel's number).

Matched markets: moneylines, and spreads/totals at the same number. FanDuel is pulled about once a day (API
credits), while Kalshi refreshes every 20 minutes, so a FanDuel price older than FD_MAX_AGE_H is skipped: an old
FanDuel number against a live Kalshi price makes fake edges. Every edge is recorded (data/edges_nfl.json, by
week), kept up to date until kickoff and graded, 1U each at Kalshi's cost, so the record shows whether market
edges actually pay.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist

FILE = Path(__file__).parent.parent / "data" / "edges_nfl.json"
MIN_EV = 0.02            # show / record edges of 2%+ after Kalshi's fee
MAX_EV = 0.20            # above this it's treated as a stale or thin price, to check by hand
FD_MAX_AGE_H = 8.0
TOTAL_SD, MARGIN_SD = 13.0, 13.5
FEE_RATE = 0.07          # Kalshi fee: about 0.07 x price x (1 - price) per contract
KALSHI_ALIAS = {"LA": "LAR"}
ND = NormalDist()


def with_fee(price):
    if price is None:
        return None
    return round(min(0.995, price + FEE_RATE * price * (1 - price)), 4)


def implied(american):
    if american is None:
        return None
    a = float(american)
    return 100 / (a + 100) if a > 0 else -a / (-a + 100)


def devig_power(p1, p2):
    """Fair chances from two implied (with-vig) chances: p1^k + p2^k = 1."""
    if not p1 or not p2:
        return None, None
    lo, hi = 0.5, 5.0
    for _ in range(60):
        k = (lo + hi) / 2
        if p1 ** k + p2 ** k > 1:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    return p1 ** k, p2 ** k


def _age_h(stamp, now):
    try:
        return (now - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        return None


def _strike(m):
    if m.get("floor_strike") is not None:
        return float(m["floor_strike"])
    x = re.search(r"over (\d+(?:\.\d+)?)", (m.get("market_title") or "").lower())
    return float(x.group(1)) if x else None


def _kalshi_for(kal_all, series, away, home):
    code = KALSHI_ALIAS.get(away, away) + KALSHI_ALIAS.get(home, home)
    out = []
    for m in kal_all:
        if m.get("series_ticker") != series:
            continue
        parts = (m.get("ticker") or "").split("-")
        if len(parts) >= 3 and parts[1][7:] == code:
            out.append(m)
    return out


def _team_of(m, away, home):
    letters = "".join(ch for ch in (m.get("ticker") or "").rsplit("-", 1)[-1] if ch.isalpha())
    for t in (away, home):
        if letters == KALSHI_ALIAS.get(t, t):
            return t
    return None


def _row(kind, g, label, side, kalshi_side, ask, fair, ref, line=None, model=None):
    cost = with_fee(ask)
    ev = fair / cost - 1 if fair and cost else None
    return {"kind": kind, "week": g["week"], "away": g["away"], "home": g["home"], "kickoff": g["kickoff"],
            "label": label, "side": side, "kalshi": f"{kalshi_side} at {round(ask * 100)}%",
            "ask": round(ask, 3), "cost": cost, "fair": round(fair, 4), "ev": round(ev, 4) if ev is not None else None,
            "ref": ref, "line": line, "model": round(model, 4) if model is not None else None,
            "model_agrees": model is not None and model > cost}


def _middle(out, g, tag, market, fd_num, k_num, fav, m, fd, mu, sd):
    lo, hi = sorted((fd_num, k_num))
    if hi - lo < 1 or m.get("yes_bid") is None or m.get("yes_ask") is None:
        return
    cdf = lambda v: ND.cdf((v - mu) / sd)  # noqa: E731
    p_above_lo, p_below_hi, p_both = 1 - cdf(lo), cdf(hi), max(0.0, cdf(hi) - cdf(lo))
    if market == "total":
        if fd_num < k_num:
            fd_leg, fd_price, k_leg, k_cost = f"FanDuel OVER {fd_num:g}", fd["over_price"], f"Kalshi NO over {k_num:g}", 1 - m["yes_bid"]
        else:
            fd_leg, fd_price, k_leg, k_cost = f"FanDuel UNDER {fd_num:g}", fd["under_price"], f"Kalshi YES over {k_num:g}", m["yes_ask"]
    else:
        dog = g["away"] if fav == g["home"] else g["home"]
        if fd_num < k_num:
            fd_leg = f"FanDuel {fav} -{fd_num:g}"
            fd_price = fd["home_spread_price"] if fav == g["home"] else fd["away_spread_price"]
            k_leg, k_cost = f"Kalshi NO {fav} by over {k_num:g}", 1 - m["yes_bid"]
        else:
            fd_leg = f"FanDuel {dog} +{fd_num:g}"
            fd_price = fd["away_spread_price"] if fav == g["home"] else fd["home_spread_price"]
            k_leg, k_cost = f"Kalshi YES {fav} by over {k_num:g}", m["yes_ask"]
    if fd_price is None or not k_cost:
        return
    dec = 1 + (fd_price / 100 if fd_price > 0 else 100 / -fd_price)
    cost = with_fee(k_cost)
    # FanDuel's leg wins above the low number when FanDuel has the lower number, otherwise below the high one;
    # Kalshi's leg is the other side. (Pairing them the other way round inflated every middle.)
    p_fd, p_k = (p_above_lo, p_below_hi) if fd_num < k_num else (p_below_hi, p_above_lo)
    ev = p_fd * dec - 1 + p_k / cost - 1
    out.append({"kind": f"middle-{market}", "label": f"{tag}: {fd_leg} + {k_leg}", "both": round(p_both, 3),
                "ev": round(ev / 2, 4), "window": f"{lo:g} to {hi:g}"})


def find(games: list[dict], fd_games: list[dict], kal_all: list[dict], now: datetime | None = None) -> dict:
    """games: [{week, away, home, kickoff (iso), model: {ml_home, margin, total}}] for games not started.
    fd_games: raw Odds API FanDuel rows. kal_all: kalshi_client game_all."""
    import oddsapi_client
    now = now or datetime.now(timezone.utc)
    edges, arbs, middles, closest, stale = [], [], [], None, 0
    for g in games:
        if now >= datetime.fromisoformat(g["kickoff"]):
            continue
        hn, an = oddsapi_client.TEAM_NAMES.get(g["home"]), oddsapi_client.TEAM_NAMES.get(g["away"])
        fd = next((x for x in fd_games if x.get("home_team") == hn and x.get("away_team") == an), None)
        if not fd:
            continue
        tag, mdl = f"{g['away']} @ {g['home']}", g.get("model") or {}
        # --- moneyline
        if fd.get("home_price") is not None and fd.get("away_price") is not None:
            age = _age_h(fd.get("h2h_update") or "", now)
            if age is None or age > FD_MAX_AGE_H:
                stale += 1
            else:
                ph, pa = devig_power(implied(fd["home_price"]), implied(fd["away_price"]))
                for m in _kalshi_for(kal_all, "KXNFLGAME", g["away"], g["home"]):
                    t = _team_of(m, g["away"], g["home"])
                    if not t or not m.get("yes_ask"):
                        continue
                    fair = ph if t == g["home"] else pa
                    model = None if mdl.get("ml_home") is None else (mdl["ml_home"] if t == g["home"] else 1 - mdl["ml_home"])
                    e = _row("moneyline", g, f"{tag}: {t} wins", f"{t} ML", f"YES {t}", m["yes_ask"], fair,
                             f"FanDuel {oddsapi_client.fmt_price(fd['away_price'])}/{oddsapi_client.fmt_price(fd['home_price'])}",
                             model=model)
                    both = e["cost"] + implied(fd["away_price"] if t == g["home"] else fd["home_price"])
                    closest = both if closest is None else min(closest, both)
                    if both < 1:
                        arbs.append({**e, "both": round(both, 4),
                                     "other": f"FanDuel {g['away'] if t == g['home'] else g['home']} ML"})
                    edges.append(e)
        # --- spread at the same number (Kalshi: "<team> wins by over X points")
        if fd.get("home_spread") is not None and fd.get("home_spread_price") is not None:
            age = _age_h(fd.get("spread_update") or "", now)
            if age is not None and age <= FD_MAX_AGE_H:
                ph, pa = devig_power(implied(fd["home_spread_price"]), implied(fd["away_spread_price"]))
                hp = float(fd["home_spread"])
                fav, num = (g["home"], -hp) if hp < 0 else (g["away"], hp)
                p_fav = ph if fav == g["home"] else pa
                dog = g["away"] if fav == g["home"] else g["home"]
                mm = mdl.get("margin")   # home margin
                fav_m = None if mm is None else (mm if fav == g["home"] else -mm)
                p_model_fav = None if fav_m is None else 1 - ND.cdf((num - fav_m) / MARGIN_SD)
                for m in _kalshi_for(kal_all, "KXNFLSPREAD", g["away"], g["home"]):
                    if _team_of(m, g["away"], g["home"]) != fav:
                        continue
                    x = _strike(m)
                    if x is None or num == 0:
                        continue
                    if abs(x - num) < 1e-6:
                        if m.get("yes_ask"):
                            edges.append(_row("spread", g, f"{tag}: {fav} -{num:g}", f"{fav} -{num:g}", f"YES {fav} by over {num:g}",
                                              m["yes_ask"], p_fav, f"FanDuel {fav} -{num:g}", line=num, model=p_model_fav))
                        if m.get("yes_bid"):
                            edges.append(_row("spread", g, f"{tag}: {dog} +{num:g}", f"{dog} +{num:g}", f"NO {fav} by over {num:g}",
                                              1 - m["yes_bid"], 1 - p_fav, f"FanDuel {dog} +{num:g}", line=num,
                                              model=None if p_model_fav is None else 1 - p_model_fav))
                    else:
                        _middle(middles, g, tag, "spread", num, x, fav, m, fd, mu=num, sd=MARGIN_SD)
        # --- game total at the same number (Kalshi: "Over X points"; team totals name a city and are skipped)
        if fd.get("total_point") is not None and fd.get("over_price") is not None:
            age = _age_h(fd.get("total_update") or "", now)
            if age is not None and age <= FD_MAX_AGE_H:
                po, pu = devig_power(implied(fd["over_price"]), implied(fd["under_price"]))
                T = float(fd["total_point"])
                mt = mdl.get("total")
                p_model_o = None if mt is None else 1 - ND.cdf((T - mt) / TOTAL_SD)
                for m in _kalshi_for(kal_all, "KXNFLTOTAL", g["away"], g["home"]):
                    if not (m.get("market_title") or "").lower().startswith("over "):
                        continue
                    x = _strike(m)
                    if x is None:
                        continue
                    if abs(x - T) < 1e-6:
                        ref = f"FanDuel {T:g} ({oddsapi_client.fmt_price(fd['over_price'])}/{oddsapi_client.fmt_price(fd['under_price'])})"
                        if m.get("yes_ask"):
                            edges.append(_row("total", g, f"{tag}: OVER {T:g}", f"OVER {T:g}", f"YES over {T:g}",
                                              m["yes_ask"], po, ref, line=T, model=p_model_o))
                        if m.get("yes_bid"):
                            edges.append(_row("total", g, f"{tag}: UNDER {T:g}", f"UNDER {T:g}", f"NO over {T:g}",
                                              1 - m["yes_bid"], pu, ref, line=T,
                                              model=None if p_model_o is None else 1 - p_model_o))
                    else:
                        _middle(middles, g, tag, "total", T, x, None, m, fd, mu=T, sd=TOTAL_SD)
    good = sorted([e for e in edges if e["ev"] is not None and MIN_EV <= e["ev"] <= MAX_EV], key=lambda e: -e["ev"])
    suspect = sorted([e for e in edges if e["ev"] is not None and e["ev"] > MAX_EV], key=lambda e: -e["ev"])[:10]
    middles.sort(key=lambda x: -x["ev"])
    return {"edges": good, "suspect": suspect, "arbs": arbs, "middles": [x for x in middles if x["ev"] > 0][:10],
            "closest_arb": round(closest, 4) if closest is not None else None, "checked": len(edges),
            "stale_fd": stale}


# --------------------------------------------------------------------------- tracking

def load() -> dict:
    try:
        return json.loads(FILE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save(d):
    txt = json.dumps(d, indent=1, sort_keys=True)
    if not FILE.exists() or FILE.read_text() != txt:
        FILE.write_text(txt)


def record(found: dict, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    d, n = load(), 0
    for e in found.get("edges", []):
        if now >= datetime.fromisoformat(e["kickoff"]):
            continue
        wk = d.setdefault(str(e["week"]), {})
        k = f"{e['kind']}|{e['label']}"
        if k not in wk:
            wk[k] = {**e, "first_seen": stamp, "first_ev": e["ev"], "status": "pending"}
            n += 1
        elif wk[k]["status"] == "pending":
            wk[k].update({x: e[x] for x in ("ask", "cost", "fair", "ev", "model", "model_agrees", "kalshi")}, last_seen=stamp)
    _save(d)
    return n


def grade(sched) -> int:
    """Grade from final scores in the nflreadpy schedule."""
    done = {}
    for r in sched.iter_rows(named=True):
        if r.get("home_score") is not None and r.get("away_score") is not None and r.get("result") is not None:
            done[(r["week"], r["away_team"], r["home_team"])] = (r["home_score"], r["away_score"])
    d, n = load(), 0
    for wk, rows in d.items():
        for e in rows.values():
            if e["status"] != "pending":
                continue
            sc = done.get((int(wk), e["away"], e["home"]))
            if not sc:
                continue
            hs, as_ = sc
            side, win = e["side"], None
            if e["kind"] == "moneyline":
                win = None if hs == as_ else (hs > as_) == (side.split()[0] == e["home"])
            elif e["kind"] == "spread":
                team, num = side.split()[0], float(side.split()[1])
                margin = (hs - as_) if team == e["home"] else (as_ - hs)
                win = None if margin + num == 0 else margin + num > 0
            elif e["kind"] == "total":
                tot = hs + as_
                win = None if tot == e["line"] else ((tot > e["line"]) == side.startswith("OVER"))
            e["status"] = "void" if win is None else "graded"
            e["hit"] = win
            e["units"] = 0.0 if win is None else round((1 / e["cost"] - 1) if win else -1.0, 4)
            n += 1
    if n:
        _save(d)
    return n


def summary() -> dict:
    allr = [e for rows in load().values() for e in rows.values()]
    g = [e for e in allr if e["status"] == "graded"]
    h = sum(1 for e in g if e["hit"])
    u = sum(e["units"] for e in g)
    agree = [e for e in g if e.get("model_agrees")]
    return {"n": len(g), "hits": h, "units": round(u, 2), "roi": round(100 * u / len(g), 1) if g else None,
            "pending": sum(1 for e in allr if e["status"] == "pending"),
            "fair_avg": round(100 * sum(e["fair"] for e in g) / len(g), 1) if g else None,
            "rate": round(100 * h / len(g), 1) if g else None,
            "agree": {"n": len(agree), "hits": sum(1 for e in agree if e["hit"]), "units": round(sum(e["units"] for e in agree), 2)},
            "recent": sorted(allr, key=lambda e: (int(e["week"]), e.get("first_seen", "")), reverse=True)[:40]}
