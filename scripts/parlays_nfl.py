"""
The site's own weekly parlays, with units (rebuilt Nov 2026 to work like the NBA site's Parlays tab).

Families
  props  "Best props"     2, 3, 4 legs   player props (passing / rushing / receiving yards, receptions), the kinds of
                                         pick with the best real hit rate first (stat & side, graded history), then
                                         the model's confidence. Paid like a PrizePicks Power Play: 3x / 6x / 10x.
  value  "Good price"     2, 3 legs      only props whose kind of pick has actually hit above what that Power Play
                                         needs per leg (2 legs: 57.7%, 3 legs: 55.0%) over 30+ graded -- GOOD PRICE.
  ml     "Moneyline"      2, 3, 4 legs   game winners by our blended chance (25% model, 75% FanDuel's price). Paid at
                                         FanDuel's real prices multiplied together.
  td     "Touchdowns"     1, 2, 3, 4     anytime touchdown scorers (rushing + receiving TDs; passing TDs don't count)
                                         with a Kalshi price. GOOD PRICE legs (our chance beats Kalshi's price) first,
                                         by chance; a player can appear in more than one TD parlay. Priced at each
                                         leg's Kalshi price multiplied (a sportsbook's TD parlay price will differ).

Rules (same as the NBA site)
  * legs come from picks the site already froze, games not started; one leg per game except touchdowns, never the
    same player twice in one parlay; props and moneyline legs aren't reused across parlays in a week.
  * rebuilt every build until its first leg kicks off ("UPDATES TILL KICKOFF"), then locked exactly as it was.
  * a leg whose player didn't play (or pushes) is dropped and the parlay pays as the smaller one; LOST as soon as
    any leg misses.
  * stakes: singles 0.5U, 2 legs 1U, 3 legs 0.5U, 4 legs 0.25U; after 10 graded of a type, doubled while that type is
    in profit, halved if it's down 25%+ per parlay; capped at 3U. Units only on the site (1U = $100 in Discord).

data/parlays.json  {"<week>": {"<family>-<size>": parlay}}
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

import dataio
import line_attach
import oddsapi_client
import predictions
import team_predictions

PATH = Path(__file__).parent.parent / "data" / "parlays.json"
SHORT = {"passing_yards": "PASS YDS", "rushing_yards": "RUSH YDS", "receiving_yards": "REC YDS", "receptions": "REC"}
PP_POWER = {2: 3.0, 3: 6.0, 4: 10.0}            # PrizePicks Power Play payouts (prizepicks.com help center, Nov 2026)
FAMILIES = [
    ("props", "Best props", (2, 3, 4), "The player props with the best real track record for their kind of pick, one per game. Paid like a PrizePicks Power Play (3x, 6x, 10x)."),
    ("value", "Good price", (2, 3), "Only props whose kind of pick has actually hit above what a Power Play needs per leg (57.7% for 2, 55.0% for 3) over 30+ graded picks."),
    ("ml", "Moneyline", (2, 3, 4), "The most likely winners by our blended chance (25% model, 75% FanDuel's price), one per game, at FanDuel's real prices."),
    ("td", "Touchdowns", (1, 2, 3, 4), "Anytime touchdown scorers (rushing or receiving) with a Kalshi price. Legs where our chance beats the price come first; a player can be in more than one TD parlay."),
]
FAMILY = {f[0]: f for f in FAMILIES}
BASE_STAKE = {1: 0.5, 2: 1.0, 3: 0.5, 4: 0.25}
EARN_N, EARN_MARGIN = 30, 0.0
TD_GOOD_MARGIN = 0.02


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


def _prod(xs) -> float:
    out = 1.0
    for x in xs:
        out *= x
    return out


def _pp_be(n: int) -> float:
    return PP_POWER[n] ** (-1 / n)


def _records() -> dict:
    import grade
    return grade.pick_records()


# ---------------------------------------------------------------- candidates
def _candidates(week: int, fd_games: list, td_cands: list, sched: pl.DataFrame, now: datetime) -> dict:
    def ko(team):
        return line_attach._kickoff_utc(team, week, sched)

    rec = _records()
    props = []
    for p in predictions.load_predictions().values():
        if p["week"] != week or p.get("graded") or p["stat"] not in SHORT or p.get("market_line") is None \
                or p.get("call") not in ("OVER", "UNDER"):
            continue
        k = ko(p["team"])
        if k is None or now >= k:
            continue
        r = rec.get(f"{p['stat']}|{p['call']}") or {}
        rate = r.get("rate", 50.0) / 100 if r.get("n", 0) >= 20 else 0.5
        mp = p.get("model_prob")
        conf = (mp if p["call"] == "OVER" else 1 - mp) if mp is not None else 0.5
        props.append({"kind": "prop", "game": "@".join(sorted([p["team"], p.get("opponent") or ""])), "team": p["team"],
                      "player": p["player"], "stat": p["stat"], "line": p["market_line"], "call": p["call"],
                      "projected": p.get("projected"), "chance": round(rate, 3), "conf": round(conf, 3),
                      "n": r.get("n", 0), "label": f"{p['player']} {p['call']} {p['market_line']:g} {SHORT[p['stat']]}",
                      "kickoff": k.isoformat()})
    props.sort(key=lambda x: (-x["chance"], -x["conf"]))

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
        dec = _dec(price)
        ml.append({"kind": "ml", "game": f"{p['away_team']}@{p['home_team']}", "home": p["home_team"], "away": p["away_team"],
                   "team": side, "call": p["our_call"], "label": f"{side} to win", "chance": round(chance, 3), "price": price,
                   "decimal": round(dec, 3) if dec else None, "good": bool(dec and chance > 1 / dec),
                   "kickoff": k.isoformat()})
    ml.sort(key=lambda x: -x["chance"])

    td = []
    for c in td_cands:
        k = ko(c["team"])
        if k is None or now >= k or not c.get("price"):
            continue
        td.append({"kind": "td", "game": "@".join(sorted([c["team"], c.get("opp") or ""])), "team": c["team"],
                   "player": c["player"], "label": f"{c['player']} anytime TD", "chance": round(c["chance"], 3),
                   "price": round(c["price"], 3), "decimal": round(1 / c["price"], 3),
                   "good": c["chance"] > c["price"] + TD_GOOD_MARGIN, "kickoff": k.isoformat()})
    td.sort(key=lambda x: (not x["good"], -x["chance"]))
    return {"props": props, "ml": ml, "td": td}


def _pick(cands: list, n: int, used: set, one_per_game: bool = True) -> list:
    out, games, players = [], set(), set()
    for c in cands:
        key = c.get("player") or c["game"]
        if key in used or key in players or (one_per_game and c["game"] in games):
            continue
        out.append(c)
        games.add(c["game"])
        players.add(key)
        if len(out) == n:
            return out
    return []


def _form(d: dict) -> dict:
    out = {}
    for wk in d.values():
        for par in wk.values():
            if par.get("status") in ("hit", "miss") and par.get("units") is not None:
                g, u = out.get((par["family"], par["size"]), (0, 0.0))
                out[(par["family"], par["size"])] = (g + 1, u + par["units"] / max(par.get("stake") or 1, 1e-9))
    return out


def _stake(fam: str, n: int, form: dict) -> float:
    base = BASE_STAKE.get(n, 0.25)
    g, u = form.get((fam, n), (0, 0.0))
    if g >= 10:
        roi = u / g
        base = base * 2 if roi > 0 else (base / 2 if roi < -0.25 else base)
    return min(3.0, base)


def _price(fam: str, legs: list) -> float | None:
    if fam in ("props", "value"):
        return PP_POWER.get(len(legs))
    decs = [l.get("decimal") for l in legs]
    return round(_prod(decs), 3) if decs and all(decs) else None


# ---------------------------------------------------------------- sync / grade
def sync(week: int | None, fd_games: list, td_cands: list | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    d = load()
    sched = dataio.load_schedule()
    # parlays saved by the first version (ml2, ml3, prop2, prop3) carry over as their new family names
    for wk in d.values():
        for old, new in (("ml2", "ml-2"), ("ml3", "ml-3"), ("prop2", "props-2"), ("prop3", "props-3")):
            if old in wk and new not in wk:
                par = wk.pop(old)
                par.update(id=new, family=new.split("-")[0])
                par.setdefault("stake", 0.0)   # built before units: tracked with no stake
    if week is not None:
        wk = d.setdefault(str(week), {})
        c = _candidates(week, fd_games, td_cands or [], sched, now)
        form = _form(d)
        # legs already in locked parlays can't be reused (props / moneyline)
        used = set()
        for par in wk.values():
            if par.get("locked") or par.get("status") != "pending":
                if par["family"] in ("props", "value", "ml"):
                    used |= {l.get("player") or l["game"] for l in par["legs"]}
        for fam, name, sizes, _desc in FAMILIES:
            for n in sizes:
                pid = f"{fam}-{n}"
                cur = wk.get(pid)
                if cur and (cur.get("locked") or cur.get("status") != "pending"):
                    continue
                if cur and now >= datetime.fromisoformat(min(l["kickoff"] for l in cur["legs"])):
                    cur["locked"] = now.strftime("%Y-%m-%d %H:%M UTC")
                    if fam in ("props", "value", "ml"):
                        used |= {l.get("player") or l["game"] for l in cur["legs"]}
                    continue
                if fam == "props":
                    pool, one = c["props"], True
                elif fam == "value":
                    pool, one = [x for x in c["props"] if x["n"] >= EARN_N and x["chance"] > _pp_be(n) + EARN_MARGIN], True
                elif fam == "ml":
                    pool, one = c["ml"], True
                else:
                    good = [x for x in c["td"] if x["good"]]
                    pool, one = (good if len(good) >= n else c["td"]), False
                legs = _pick(pool, n, used if fam != "td" else set(), one_per_game=one)
                if not legs:
                    if cur and not cur.get("locked"):
                        del wk[pid]   # not enough open legs any more
                    continue
                if fam in ("props", "value", "ml"):
                    used |= {l.get("player") or l["game"] for l in legs}
                chance = _prod(l["chance"] for l in legs)
                pays = _price(fam, legs)
                good = all(l.get("good") for l in legs) if fam in ("ml", "td") else \
                    all(l["n"] >= EARN_N and l["chance"] > _pp_be(n) + EARN_MARGIN for l in legs)
                stake = _stake(fam, n, form)
                wk[pid] = {"id": pid, "family": fam, "name": name, "size": n, "week": week, "legs": legs,
                           "chance": round(chance, 4), "decimal": pays, "good": good, "stake": stake,
                           "ev": round(chance * pays - 1, 3) if pays else None, "status": "pending",
                           "updated": now.strftime("%Y-%m-%d %H:%M UTC")}
    n_graded = _grade(d, sched)
    save(d)
    return {"week": week, "open": sum(1 for p in d.get(str(week), {}).values() if p["status"] == "pending"),
            "graded_now": n_graded}


def _grade(d: dict, sched: pl.DataFrame) -> int:
    preds = predictions.load_predictions()
    stats = dataio.load_stats()
    n = 0
    for wk in d.values():
        for par in wk.values():
            if par["status"] != "pending":
                continue
            results = []
            for l in par["legs"]:
                if l.get("result"):
                    results.append(l["result"])
                    continue
                if l["kind"] == "ml":
                    r = sched.filter((pl.col("home_team") == l["home"]) & (pl.col("away_team") == l["away"])
                                     & (pl.col("week") == par["week"]))
                    if not r.height or r["result"][0] is None:
                        results.append(None)
                        continue
                    hs, as_ = r["home_score"][0], r["away_score"][0]
                    winner = l["home"] if hs > as_ else l["away"] if as_ > hs else None
                    l["result"] = "void" if winner is None else ("hit" if winner == l["team"] else "miss")
                elif l["kind"] == "td":
                    g = sched.filter((pl.col("week") == par["week"]) & ((pl.col("home_team") == l["team"]) | (pl.col("away_team") == l["team"])))
                    if not g.height or g["result"][0] is None:
                        results.append(None)
                        continue
                    row = stats.filter((pl.col("player_display_name") == l["player"]) & (pl.col("week") == par["week"]))
                    if not row.height:
                        if stats["week"].max() < par["week"]:   # that week's stats haven't landed yet
                            results.append(None)
                            continue
                        l["result"] = "void"   # didn't play
                    else:
                        r0 = row.row(0, named=True)
                        tds = (r0.get("rushing_tds") or 0) + (r0.get("receiving_tds") or 0)
                        l["actual"] = tds
                        l["result"] = "hit" if tds >= 1 else "miss"
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
            else:
                live = [l for l in par["legs"] if l.get("result") == "hit"]
                if not live or (par["family"] in ("props", "value") and len(live) < 2):
                    par["status"] = "void"
                else:
                    par["status"] = "hit"
            par["graded"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            stake = par.get("stake")
            if stake is None:
                stake = 0.0
            if par["status"] == "hit":
                live = [l for l in par["legs"] if l.get("result") == "hit"]
                pays = _price(par["family"], live)
                par["paid"] = pays
                par["units"] = round(stake * (pays - 1), 2) if pays else None
            elif par["status"] == "miss":
                par["units"] = -stake
            else:
                par["units"] = 0.0
            n += 1
    return n


# ---------------------------------------------------------------- page
def page(week: int | None) -> dict:
    d = load()
    cur = d.get(str(week), {}) if week is not None else {}
    fams = []
    for fam, name, sizes, desc in FAMILIES:
        ps = [cur[f"{fam}-{n}"] for n in sizes if f"{fam}-{n}" in cur]
        fams.append({"key": fam, "name": name, "desc": desc, "parlays": ps})
    rec, tot = {}, {"n": 0, "hits": 0, "units": 0.0, "risked": 0.0}
    for wk in d.values():
        for par in wk.values():
            if par["status"] not in ("hit", "miss"):
                continue
            r = rec.setdefault(par["family"], {"n": 0, "hits": 0, "units": 0.0, "risked": 0.0})
            for x in (r, tot):
                x["n"] += 1
                x["hits"] += par["status"] == "hit"
                x["units"] = round(x["units"] + (par.get("units") or 0), 2)
                x["risked"] = round(x["risked"] + (par.get("stake") or 0), 2)
    past = sorted(((int(w), p) for w, ps in d.items() for p in ps.values() if p["status"] in ("hit", "miss", "void")),
                  key=lambda x: (-x[0], x[1]["id"]))
    open_stake = sum(p.get("stake") or 0 for p in cur.values() if p["status"] == "pending")
    return {"week": week, "families": fams, "record": rec, "total": tot, "past": [p for _, p in past][:30],
            "open_stake": round(open_stake, 2), "names": {f[0]: f[1] for f in FAMILIES}, "pp": PP_POWER}
