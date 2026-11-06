"""
The site's own weekly parlays (ported from the NBA site's Parlays tab, Nov 2026).

Four parlays a week, each rebuilt from the best open picks every build until its first leg kicks off, then locked
and graded once every leg is final:

  ml2 / ml3      2- and 3-leg moneyline parlays: the most likely winners by our blended win chance (25% model,
                 75% FanDuel's price -- the blend that went 45/73 on the graded weeks), one leg per game. The
                 payout is FanDuel's real price for each leg multiplied together, captured when the parlay locks.
  prop2 / prop3  2- and 3-leg player-prop parlays from this week's Best 5 picks (highest confidence), one leg per
                 game and per player. The chance shown is honest: Best 5's own graded hit rate per leg, not the
                 model's confidence (which has run far ahead of reality on this site).

Tracking only: no stakes until a type proves itself. data/parlays.json  {"<week>": {"<id>": parlay}}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

import dataio
import line_attach
import oddsapi_client
import predictions
import team_predictions

PATH = Path(__file__).parent.parent / "data" / "parlays.json"
TYPES = {"ml2": ("Moneyline", 2), "ml3": ("Moneyline", 3), "prop2": ("Player props", 2), "prop3": ("Player props", 3)}
SHORT = {"passing_yards": "PASS YDS", "rushing_yards": "RUSH YDS", "receiving_yards": "REC YDS", "receptions": "REC"}


def load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def save(d: dict) -> None:
    PATH.write_text(json.dumps(d, indent=1, sort_keys=True))


def _dec(american) -> float | None:
    try:
        a = float(american)
    except (TypeError, ValueError):
        return None
    return 1 + (a / 100 if a > 0 else 100 / -a)


def _best5_rate() -> float:
    g = [p for p in predictions.load_predictions().values()
         if "highest_confidence" in (p.get("best5_tags") or []) and p.get("graded") and p.get("hit") is not None
         and not p.get("void") and p["stat"] != "any_td"]
    return (sum(1 for p in g if p["hit"]) / len(g)) if len(g) >= 30 else 0.5


def _candidates(week: int, fd_games: list, sched: pl.DataFrame, now: datetime) -> tuple[list, list]:
    def ko(team):
        return line_attach._kickoff_utc(team, week, sched)

    ml = []
    for p in team_predictions.load_team_predictions().values():
        if p["week"] != week or p["market"] != "team_moneyline" or p.get("graded") or p.get("market_line") is None:
            continue
        k = ko(p["home_team"])
        if k is None or now >= k:
            continue
        side = p["home_team"] if p["our_call"] == "HOME" else p["away_team"]
        chance = (p["our_value"] if p["our_call"] == "HOME" else 100 - p["our_value"]) / 100
        odds = (oddsapi_client.odds_for_game(fd_games, p["home_team"], p["away_team"]) or {}).get("moneyline") or {}
        price = odds.get("home_price") if p["our_call"] == "HOME" else odds.get("away_price")
        ml.append({"kind": "ml", "game": f"{p['away_team']}@{p['home_team']}", "home": p["home_team"],
                   "away": p["away_team"], "team": side, "call": p["our_call"], "label": f"{side} to win",
                   "chance": round(chance, 3), "price": price, "kickoff": k.isoformat()})
    rate = _best5_rate()
    props = []
    for p in predictions.load_predictions().values():
        if p["week"] != week or p.get("graded") or p["stat"] not in SHORT or p.get("market_line") is None \
                or p.get("call") not in ("OVER", "UNDER") or "highest_confidence" not in (p.get("best5_tags") or []):
            continue
        k = ko(p["team"])
        if k is None or now >= k:
            continue
        mp = p.get("model_prob")
        conf = (mp if p["call"] == "OVER" else 1 - mp) if mp is not None else 0.5
        opp = p.get("opponent") or ""
        props.append({"kind": "prop", "game": "@".join(sorted([p["team"], opp])), "team": p["team"], "player": p["player"],
                      "stat": p["stat"], "line": p["market_line"], "call": p["call"], "projected": p.get("projected"),
                      "label": f"{p['player']} {p['call']} {p['market_line']:g} {SHORT[p['stat']]}",
                      "chance": round(rate, 3), "conf": conf, "kickoff": k.isoformat()})
    ml.sort(key=lambda x: -x["chance"])
    props.sort(key=lambda x: -x["conf"])
    return ml, props


def _pick(cands: list, n: int) -> list:
    out, games, players = [], set(), set()
    for c in cands:
        if c["game"] in games or c.get("player") in players:
            continue
        out.append(c)
        games.add(c["game"])
        if c.get("player"):
            players.add(c["player"])
        if len(out) == n:
            break
    return out if len(out) == n else []


def sync(week: int | None, fd_games: list, now: datetime | None = None) -> dict:
    """Rebuild this week's open parlays, lock started ones, grade finished ones (every week on file)."""
    now = now or datetime.now(timezone.utc)
    d = load()
    sched = dataio.load_schedule()
    if week is not None:
        wk = d.setdefault(str(week), {})
        ml, props = _candidates(week, fd_games, sched, now)
        for pid, (fam, n) in TYPES.items():
            cur = wk.get(pid)
            if cur and (cur.get("locked") or cur.get("status") != "pending"):
                continue
            if cur and now >= datetime.fromisoformat(min(l["kickoff"] for l in cur["legs"])):
                cur["locked"] = now.strftime("%Y-%m-%d %H:%M UTC")
                continue
            legs = _pick(ml if pid.startswith("ml") else props, n)
            if not legs:
                continue
            chance = 1.0
            for l in legs:
                chance *= l["chance"]
            decs = [_dec(l.get("price")) for l in legs] if pid.startswith("ml") else []
            wk[pid] = {"id": pid, "family": fam, "size": n, "week": week, "legs": legs, "chance": round(chance, 4),
                       "decimal": round(_prod(decs), 3) if decs and all(decs) else None, "status": "pending",
                       "updated": now.strftime("%Y-%m-%d %H:%M UTC")}
    n_graded = _grade(d, sched)
    save(d)
    return {"week": week, "open": sum(1 for p in d.get(str(week), {}).values() if p["status"] == "pending"),
            "graded_now": n_graded}


def _prod(xs):
    out = 1.0
    for x in xs:
        out *= x
    return out


def _grade(d: dict, sched: pl.DataFrame) -> int:
    preds = predictions.load_predictions()
    n = 0
    for wk in d.values():
        for par in wk.values():
            if par["status"] != "pending":
                continue
            results = []
            for l in par["legs"]:
                if l["kind"] == "ml":
                    r = sched.filter((pl.col("home_team") == l["home"]) & (pl.col("away_team") == l["away"])
                                     & (pl.col("week") == par["week"]))
                    if not r.height or r["result"][0] is None:
                        results.append(None)
                        continue
                    hs, as_ = r["home_score"][0], r["away_score"][0]
                    winner = l["home"] if hs > as_ else l["away"] if as_ > hs else None
                    l["result"] = "void" if winner is None else ("hit" if winner == l["team"] else "miss")
                else:
                    p = preds.get(predictions._key(l["player"], l["stat"], par["week"]))
                    if not p or not p.get("graded"):
                        results.append(None)
                        continue
                    a = p.get("actual")
                    l["actual"] = a
                    l["result"] = "void" if (p.get("void") or a is None or a == l["line"]) else \
                        ("hit" if (a > l["line"]) == (l["call"] == "OVER") else "miss")
                results.append(l["result"])
            if "miss" in results:
                par["status"] = "miss"
            elif None in results:
                continue
            elif all(r == "void" for r in results):
                par["status"] = "void"
            else:
                par["status"] = "hit"
            par["graded"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if par.get("decimal") and par["status"] in ("hit", "miss"):
                live = [_dec(l.get("price")) for l in par["legs"] if l.get("result") != "void"]
                par["units"] = round(_prod(live) - 1, 2) if par["status"] == "hit" and all(live) else -1.0
            n += 1
    return n


def page(week: int | None) -> dict:
    d = load()
    cur = d.get(str(week), {}) if week is not None else {}
    rec = {}
    for wk in d.values():
        for par in wk.values():
            if par["status"] in ("hit", "miss"):
                r = rec.setdefault(par["id"], {"n": 0, "hits": 0, "units": 0.0, "priced": 0})
                r["n"] += 1
                r["hits"] += par["status"] == "hit"
                if par.get("units") is not None:
                    r["units"] = round(r["units"] + par["units"], 2)
                    r["priced"] += 1
    past = sorted(((int(w), p) for w, ps in d.items() for p in ps.values() if p["status"] in ("hit", "miss", "void")),
                  key=lambda x: (-x[0], x[1]["id"]))
    return {"week": week, "now": [cur[k] for k in TYPES if k in cur], "types": TYPES, "record": rec,
            "past": [p for _, p in past][:24]}
