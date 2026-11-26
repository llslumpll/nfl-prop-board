"""Player Lab for the NFL site (nfl-update-22, Nov 26 2026): one page per player with a pick or a Kalshi rung this week,
modelled on the NBA site's player pages.

Each page: this week's opponent and injury status, our projection and pick per stat, every open Kalshi rung with its
price and the site's "really hits" chance (kbest_nfl calibration) and how often he cleared it this season, a splits
table (season, last 5, last season, home, away, vs this opponent since 2023), the 2026 game log with our graded
picks, our record on him, and tipster picks on him. docs/players.html lists them all by team.
"""
from __future__ import annotations

import json
import re
import statistics as S
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

DATA = Path(__file__).resolve().parent.parent / "data"
STATS = {"passyd": ("passing_yards", "PASS YDS"), "rushyd": ("rushing_yards", "RUSH YDS"),
         "recyd": ("receiving_yards", "REC YDS"), "rec": ("receptions", "REC"), "td": ("any_td", "TDs")}
BY_POS = {"QB": ["passyd", "rushyd", "td"], "RB": ["rushyd", "recyd", "rec", "td"], "WR": ["recyd", "rec", "td"],
          "TE": ["recyd", "rec", "td"], "FB": ["rushyd", "recyd", "rec", "td"]}


def slug(name: str) -> str:
    return "player-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") + ".html"


def _norm(n: str) -> str:
    n = (n or "").lower()
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", n)
    return re.sub(r"[^a-z]", "", n)


def _v(r: dict, st: str) -> float:
    g = lambda c: r.get(c) or 0  # noqa: E731
    return {"passyd": g("passing_yards"), "rushyd": g("rushing_yards"), "recyd": g("receiving_yards"),
            "rec": g("receptions"), "td": g("rushing_tds") + g("receiving_tds")}[st]


def _am(a: float) -> str:
    return f"-{round(100 * a / (1 - a))}" if a >= 0.5 else f"+{round(100 * (1 - a) / a)}"


def build(week: int) -> dict:
    import kbest_nfl as KB
    import kalshi_history_nfl as KH
    cur = pl.read_parquet(DATA / "player_stats_2026.parquet").filter(pl.col("season_type") == "REG").sort("week")
    hist = pl.read_parquet(DATA / "player_stats_historical.parquet").filter(pl.col("season_type") == "REG")
    sched = pl.read_parquet(DATA / "schedules_2026.parquet").filter(pl.col("game_type") == "REG")
    games = defaultdict(list)
    info = {}
    for r in cur.iter_rows(named=True):
        k = _norm(r["player_display_name"])
        games[k].append(r)
        info[k] = {"name": r["player_display_name"], "team": r["team"], "pos": r.get("position"),
                   "headshot": r.get("headshot_url")}
    past = defaultdict(list)
    for r in hist.iter_rows(named=True):
        past[_norm(r["player_display_name"])].append(r)
    home_of = {}
    for g in sched.iter_rows(named=True):
        home_of[(g["week"], g["home_team"])] = True
        home_of[(g["week"], g["away_team"])] = False
    opp_wk = {}
    for g in sched.filter(pl.col("week") == week).iter_rows(named=True):
        when = f"{g['weekday'][:3]} {g['gametime']} ET" if g.get("weekday") and g.get("gametime") else ""
        opp_wk[g["home_team"]] = (g["away_team"], True, when)
        opp_wk[g["away_team"]] = (g["home_team"], False, when)
    inj = {}
    try:
        i = pl.read_parquet(DATA / "injuries_2026.parquet").filter(pl.col("week") == week)
        for r in i.iter_rows(named=True):
            if r.get("report_status"):
                inj[_norm(r["full_name"])] = r["report_status"]
    except Exception:  # noqa: BLE001
        pass
    preds = json.loads((DATA / "predictions.json").read_text())
    pw = defaultdict(dict)
    rec = defaultdict(lambda: {"n": 0, "w": 0, "by_week": {}})
    for p in preds.values():
        k = _norm(p["player"])
        if p["week"] == week:
            pw[k][p["stat"]] = p
        if p.get("graded") and p.get("hit") is not None and p.get("call") in ("OVER", "UNDER"):
            rec[k]["n"] += 1
            rec[k]["w"] += bool(p["hit"])
            rec[k]["by_week"].setdefault(p["week"], []).append(
                {"stat": p["stat"], "call": p["call"][0].lower(), "line": p.get("market_line"), "hit": bool(p["hit"])})
    # Kalshi rungs this week, with the site's real-chance estimate
    feat = KB.Features()
    cal = KB.calibration(feat)
    rungs = defaultdict(lambda: defaultdict(list))
    started = set()
    try:
        for l in json.loads((DATA / "kalshi_ladders_nfl.json").read_text())["legs"]:
            ko = KH.event_kickoff(l.get("event") or "")
            if ko and ko[1] == week and datetime.now(timezone.utc) >= ko[0]:
                started.add(_norm(l["player"]))
                continue
            if not ko or ko[1] != week or not l.get("ask") or l["stat"] not in ("passyd", "rushyd", "recyd", "rec", "td"):
                continue
            x = feat.add({**l, "week": week})
            est = KB.estimate(x, cal) if x.get("g", 0) >= 3 else None
            rungs[_norm(l["player"])][l["stat"]].append(
                {"rung": l["rung"], "label": "anytime" if l["stat"] == "td" else f"{int(l['rung'] + 0.5)}+",
                 "ask": l["ask"], "odds": _am(l["ask"]), "est": round(100 * est) if est else None,
                 "edge": round(100 * (est - l["ask"])) if est else None})
    except Exception as e:  # noqa: BLE001
        print(f"Player Lab: Kalshi rungs failed: {e}")
    try:
        import tipsters_nfl as TN
        tips = defaultdict(list)
        box = TN.Box()
        for p in TN._load():
            r = TN.grade_pick(p, box)
            for l in r.get("legs") or []:
                tips[_norm(l["player"])].append({"who": TN.TIPSTERS[p["tipster"]]["name"], "week": p["week"],
                                                 "text": f"{'o' if l.get('side', 'over') == 'over' else 'u'}{l['line']} {TN.LAB.get(l['stat'], l['stat'])}",
                                                 "res": l.get("res"), "actual": l.get("actual")})
    except Exception:  # noqa: BLE001
        tips = {}

    keys = set(pw) | set(rungs) | started
    pages, index = [], defaultdict(list)
    for k in keys:
        if k not in info:
            continue
        me = info[k]
        g = games[k]
        pos = me["pos"] if me["pos"] in BY_POS else "WR"
        stats = BY_POS[pos]
        opp, home, when = opp_wk.get(me["team"], (None, None, ""))
        rows = []
        for st in stats:
            vals = [_v(r, st) for r in g]
            l5 = vals[-5:]
            last = [_v(r, st) for r in past[k] if r["season"] == 2025]
            hm = [_v(r, st) for r in g if home_of.get((r["week"], r["team"])) is True]
            aw = [_v(r, st) for r in g if home_of.get((r["week"], r["team"])) is False]
            vs = [_v(r, st) for r in past[k] + g if opp and r.get("opponent_team") == opp]
            p = pw[k].get(STATS[st][0])
            rs = sorted(rungs[k].get(st, []), key=lambda x: x["rung"])
            for x in rs:
                x["cleared"] = sum(v > x["rung"] for v in vals)
                x["games"] = len(vals)
            f = lambda xs: round(S.mean(xs), 1) if xs else None  # noqa: E731
            rows.append({"stat": st, "label": STATS[st][1], "proj": p.get("projected") if p else None,
                         "line": p.get("market_line") if p else None, "call": p.get("call") if p else None,
                         "season": f(vals), "n": len(vals), "l5": f(l5), "last": f(last), "last_n": len(last),
                         "home": f(hm), "away": f(aw), "vs": f(vs), "vs_n": len(vs), "rungs": rs})
        log = []
        for r in reversed(g):
            picks = rec[k]["by_week"].get(r["week"], [])
            log.append({"week": r["week"], "opp": r.get("opponent_team"), "home": home_of.get((r["week"], r["team"])),
                        "cells": {st: _v(r, st) for st in stats},
                        "picks": [f"{x['call']}{x['line']} {STATS.get({'passing_yards': 'passyd', 'rushing_yards': 'rushyd', 'receiving_yards': 'recyd', 'receptions': 'rec'}.get(x['stat'], 'td'), ('', ''))[1]} {'✔' if x['hit'] else '✘'}" for x in picks]})
        page = {"slug": slug(me["name"]), "name": me["name"], "team": me["team"], "pos": me["pos"],
                "headshot": me["headshot"], "started": k in started, "opp": opp, "home": home, "when": when, "inj": inj.get(k),
                "rows": rows, "log": log, "stats": stats, "labels": {s: STATS[s][1] for s in stats},
                "record": {"n": rec[k]["n"], "w": rec[k]["w"]}, "tips": sorted(tips.get(k, []), key=lambda x: -x["week"])[:12],
                "best": max((x for r_ in rows for x in r_["rungs"] if x.get("edge") is not None),
                            key=lambda x: x["edge"], default=None)}
        pages.append(page)
        index[me["team"]].append({"name": me["name"], "slug": page["slug"], "pos": me["pos"], "opp": opp, "home": home,
                                  "inj": page["inj"], "n_rungs": sum(len(r_["rungs"]) for r_ in rows)})
    for t in index.values():
        t.sort(key=lambda x: ({"QB": 0, "RB": 1, "WR": 2, "TE": 3}.get(x["pos"], 4), x["name"]))
    return {"week": week, "pages": sorted(pages, key=lambda p: p["name"]),
            "index": dict(sorted(index.items())), "built": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
