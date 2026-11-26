"""Tipsters page for the NFL site (nfl-update-18, Nov 26 2026), modelled on the NBA site's Tipsters page.

Every pick is logged from the tipster's public posts (data/tipsters/picks.json, by NFL week) and graded against
nflverse box scores (data/player_stats_2026.parquet) and snap counts (data/snap_counts_2026.parquet: a player who
played with no catches counts as 0; a player who didn't play is void). Spreads and moneylines are graded from the
schedule's final scores. 1U per pick at the odds the account posted; ladders are graded rung by rung at his stakes.

Pick format: {id, week, tipster, kind: prop|spread|ml, legs: [{player, stat, line, side}], odds, rungs?: [{line, odds,
u?}], team/line (spread, ml), posted (UTC), odds_est?, label?, note?, why?}
Stats: recyd rec rushyd rushatt passyd rr (rush+rec yards) td (rushing + receiving TDs; line 0.5 = anytime,
1.5 = 2+) ftd (first TD: not gradeable from box scores, left open).
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

import polars as pl

DATA = Path(__file__).resolve().parent.parent / "data"
PATH = DATA / "tipsters" / "picks.json"

TIPSTERS = {
    "doc": {"name": "Dr. Profit", "handle": "@DoctorProfit", "what": "props, ladders, spreads", "honest": True,
            "note": "Rush-yard and receiving-yard ladders plus spreads. Posts his misses with a reason. Best on spreads/moneylines and the first two rungs of yardage ladders; rungs priced +800 and longer rarely hit."},
    "nlb": {"name": "Next Level Bets", "handle": "@NextLevelBets_", "what": "props + ladders", "honest": False,
            "note": "Written 'NFL Play' overs and the odd ladder. Posts results only on winning days. His NBA assists picks are the part that wins; NFL has been weak."},
    "bhb": {"name": "BlazinHotBets", "handle": "@BlazinHotBets", "what": "TD lottos and SGPs", "honest": False,
            "note": "Anytime-TD parlays, QB rushing-TD lottos and 100-yard + TD same-game parlays at +1,000 to +20,000. Graded 1U per slip; the legs record shows which TD picks land."},
    "jake": {"name": "Jake Venom TD Watch", "handle": "@jakevenomapp", "what": "TD Watch players", "honest": None,
             "note": "Players he opens in his Venom app on YouTube, priced at the app's own TD % (no sportsbook odds shown). A pick wins if he scores a rushing or receiving TD."},
}
LAB = {"recyd": "REC YDS", "rec": "REC", "rushyd": "RUSH YDS", "rushatt": "RUSH ATT", "passyd": "PASS YDS",
       "rr": "RUSH+REC YDS", "td": "TD", "ftd": "FIRST TD"}
LIVE = {"recyd": "receiving_yards", "rec": "receptions", "rushyd": "rushing_yards", "passyd": "passing_yards"}


def _norm(n: str) -> str:
    n = (n or "").lower()
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", n)
    return re.sub(r"[^a-z]", "", n)


def _load() -> list[dict]:
    try:
        return json.loads(PATH.read_text())["picks"]
    except Exception:  # noqa: BLE001
        return []


def _pay(o: int) -> float:
    return o / 100 if o > 0 else 100 / -o


class Box:
    def __init__(self):
        ps = pl.read_parquet(DATA / "player_stats_2026.parquet").filter(pl.col("season_type") == "REG")
        self.rows = defaultdict(dict)         # week -> norm name -> row
        self.last = defaultdict(dict)         # week -> (last name, first initial) -> [rows]
        self.team = {}                        # norm name -> latest team
        for r in ps.sort("week").iter_rows(named=True):
            k = _norm(r["player_display_name"])
            self.rows[r["week"]][k] = r
            nm = (r["player_display_name"] or "").split()
            if nm:
                self.last[r["week"]].setdefault((_norm(nm[-1]), nm[0][0].lower()), []).append(r)
            self.team[k] = r["team"]
        self.snaps = defaultdict(set)
        try:
            sn = pl.read_parquet(DATA / "snap_counts_2026.parquet").filter(pl.col("offense_snaps") > 0)
            for r in sn.iter_rows(named=True):
                self.snaps[r["week"]].add(_norm(r["player"]))
                self.team.setdefault(_norm(r["player"]), r["team"])
        except Exception:  # noqa: BLE001
            pass
        sc = pl.read_parquet(DATA / "schedules_2026.parquet")
        self.games = defaultdict(list)
        for g in sc.iter_rows(named=True):
            self.games[g["week"]].append(g)

    def row(self, week: int, player: str):
        k = _norm(player)
        r = self.rows[week].get(k)
        if r:
            return r, True
        nm = player.split()
        c = self.last[week].get((_norm(nm[-1]), nm[0][0].lower())) if nm else None
        if c and len(c) == 1:
            return c[0], True
        return None, k in self.snaps[week]

    def team_of(self, player: str) -> str | None:
        return self.team.get(_norm(player))

    def game(self, week: int, team: str):
        return next((g for g in self.games[week] if team in (g["home_team"], g["away_team"])), None)


def _val(r: dict | None, st: str) -> float | None:
    if r is None:
        return 0.0
    g = lambda c: r.get(c) or 0  # noqa: E731
    return {"recyd": g("receiving_yards"), "rec": g("receptions"), "rushyd": g("rushing_yards"), "rushatt": g("carries"),
            "passyd": g("passing_yards"), "rr": g("rushing_yards") + g("receiving_yards"),
            "td": g("rushing_tds") + g("receiving_tds")}.get(st)


def grade_leg(leg: dict, week: int, box: Box) -> tuple[str | None, float | None]:
    if leg["stat"] == "ftd":
        return None, None
    r, played = box.row(week, leg["player"])
    if r is None and not played:
        team = box.team_of(leg["player"])
        g = box.game(week, team) if team else None
        if g and g.get("home_score") is not None:
            return "void", None          # his team played, he didn't
        return None, None                # not played yet
    v = _val(r, leg["stat"])
    if v is None:
        return None, None
    if v == leg["line"]:
        return "push", v
    hit = v > leg["line"] if leg.get("side", "over") == "over" else v < leg["line"]
    return ("hit" if hit else "miss"), v


def grade_pick(p: dict, box: Box) -> dict:
    out = {**p, "res": None, "units": 0.0}
    if p["kind"] in ("spread", "ml"):
        g = box.game(p["week"], p["team"])
        if not g or g.get("home_score") is None:
            return out
        home = g["home_team"] == p["team"]
        my, op = (g["home_score"], g["away_score"]) if home else (g["away_score"], g["home_score"])
        out["game"] = f"{g['away_team']} @ {g['home_team']}"
        out["score"] = f"{my}-{op}"
        if p["kind"] == "ml":
            res = "hit" if my > op else "miss"
        else:
            ln = p.get("line")
            if ln is None:
                ln = -g["spread_line"] if home else g["spread_line"]
            m = my - op + ln
            res = "push" if m == 0 else ("hit" if m > 0 else "miss")
        out["res"] = res
    else:
        legs, res = [], []
        for l in p["legs"]:
            g, v = grade_leg(l, p["week"], box)
            legs.append({**l, "res": g, "actual": v, "team": box.team_of(l["player"])})
            res.append(g)
        out["legs"] = legs
        if "miss" in res:
            out["res"] = "miss"
        elif res and all(r in ("hit", "push", "void") for r in res) and "hit" in res:
            out["res"] = "hit"
        elif res and all(r in ("void", "push") for r in res):
            out["res"] = "void"
        if p.get("rungs") and len(p["legs"]) == 1 and legs[0]["actual"] is not None:
            v = legs[0]["actual"]
            over = p["legs"][0].get("side", "over") == "over"
            out["rungs"] = [{**r, "hit": (v > r["line"]) if over else (v < r["line"])} for r in p["rungs"]]
    if out["res"] == "hit":
        out["units"] = round(_pay(p["odds"]), 3)
    elif out["res"] == "miss":
        out["units"] = -1.0
    return out


def _rec(rs: list[dict]) -> dict:
    g = [r for r in rs if r["res"] in ("hit", "miss")]
    n = len(g)
    w = sum(r["res"] == "hit" for r in g)
    u = sum(r["units"] for r in g)
    last = g[-10:]
    return {"n": n, "w": w, "l": n - w, "u": round(u, 2), "rate": round(100 * w / n) if n else None,
            "per": round(100 * u / n) if n else None,
            "last10": f"{sum(r['res'] == 'hit' for r in last)}/{len(last)}" if last else None}


def current_week() -> int:
    sc = pl.read_parquet(DATA / "schedules_2026.parquet").filter(pl.col("game_type") == "REG")
    open_ = sc.filter(pl.col("home_score").is_null())
    return int(open_["week"].min()) if open_.height else int(sc["week"].max())


def summary() -> dict:
    box = Box()
    week = current_week()
    rows = [grade_pick(p, box) for p in _load()]
    rows.sort(key=lambda r: (r["week"], r["id"]))
    per = []
    for k, t in TIPSTERS.items():
        rs = [r for r in rows if r["tipster"] == k]
        by = defaultdict(list)
        for r in rs:
            key = ("Spreads/ML" if r["kind"] in ("spread", "ml") else
                   ("Parlays" if len(r["legs"]) > 1 else LAB.get(r["legs"][0]["stat"], r["legs"][0]["stat"]) +
                    (" ladders" if r.get("rungs") else "")))
            by[key].append(r)
        lad = [r for r in rs if r.get("rungs") and isinstance(r["rungs"], list) and r["res"] in ("hit", "miss")]
        lr = None
        if lad:
            base = sum(r["res"] == "hit" for r in lad)
            nxt = [r["rungs"][0] for r in lad if r["rungs"] and "hit" in r["rungs"][0]]
            top = [x for r in lad for x in r["rungs"][1:] if "hit" in x]
            u = 0.0
            st = 0.0
            for r in lad:
                st += 1
                u += r["units"]
                for i, x in enumerate(r["rungs"]):
                    if "hit" not in x or x.get("odds") is None:
                        continue
                    s = x.get("u") or 1.0
                    st += s
                    u += s * _pay(x["odds"]) if x["hit"] else -s
            lr = {"n": len(lad), "base": base, "next": sum(x["hit"] for x in nxt), "next_n": len(nxt),
                  "top": sum(x["hit"] for x in top), "top_n": len(top), "u": round(u, 2), "staked": round(st, 1)}
        legs = [l for r in rs if r["kind"] == "prop" and len(r["legs"]) > 1 for l in r["legs"] if l["res"] in ("hit", "miss")]
        legrec = {"n": len(legs), "w": sum(l["res"] == "hit" for l in legs)} if legs else None
        per.append({"key": k, **t, **_rec(rs), "by": {kk: _rec(v) for kk, v in sorted(by.items())}, "ladders": lr,
                    "legrec": legrec})
    this_week = [r for r in rows if r["week"] == week]
    for r in this_week:
        for l in r.get("legs") or []:
            if l["res"] is None and l["stat"] in LIVE and l.get("team"):
                l["lv"] = f"{l['team']}|{l['player']}|{LIVE[l['stat']]}|{l['line']}|{'OVER' if l.get('side', 'over') == 'over' else 'UNDER'}|0"
    weeks = []
    for w in sorted({r["week"] for r in rows}, reverse=True):
        rs = [r for r in rows if r["week"] == w and r["res"] in ("hit", "miss")]
        if rs:
            weeks.append({"week": w, "rows": rs, "w": sum(r["res"] == "hit" for r in rs),
                          "l": sum(r["res"] == "miss" for r in rs), "u": round(sum(r["units"] for r in rs), 2)})
    return {"tipsters": per, "week": week, "this_week": this_week, "weeks": weeks, "lab": LAB}
