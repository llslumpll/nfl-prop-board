"""Best 5 on Kalshi + 3x slip for the NFL site (nfl-update-20, Nov 26 2026), the NFL versions of the NBA site's
Your board cards.

Evidence (data/kalshi_history_nfl.json: 49,137 settled Kalshi NFL player markets, weeks 2-11, last ask before the
scheduled kickoff):
  - Kalshi NFL player rungs are slightly overpriced overall (43,542 priced legs: 29.3% hit vs 30.9% priced,
    -8%/bet). Rushing yards worst (-10%), passing yards about fair.
  - Each leg's real chance = Kalshi's ask + how legs like it did against their price in OTHER weeks, bucketed by
    stat x price band x our projection vs the rung x how often he cleared that rung in his last 5 games, shrunk
    n/(n+80) at each level.
  - Best 5 a week (5 legs priced 60-90%, one per player, edge > 1 point), each week judged only on the other
    weeks: 36-14, +6.0U (+12%/bet); weeks 2-6 20-5, weeks 7-11 16-9. Fragile: 55-90% or no recent-form bucket
    turns it negative. TESTING.
  - 3x slip a week (the combo most likely to cash at a 3x+ estimated Kalshi quote): cashed 3/10, -1.0U (it expected
    ~40%). TESTING.
Each Best 5 leg locks at its kickoff (open slots refill until then); the 3x slip locks at its first kickoff.
Graded from nflverse box scores (tipsters_nfl.Box): 1U per leg at the ask; the slip at 1U at its estimated quote.
"""
from __future__ import annotations

import itertools
import json
import math
import re
import statistics as S
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
HIST = DATA / "kalshi_history_nfl.json"
LIVE = DATA / "kalshi_ladders_nfl.json"
STATE = DATA / "kbest_nfl.json"
UNIT = {"recyd": 10, "rushyd": 10, "passyd": 25, "rec": 1, "rr": 10}
PRED_STAT = {"recyd": "receiving_yards", "rushyd": "rushing_yards", "passyd": "passing_yards", "rec": "receptions",
             "td": "any_td"}
K = 80
B5_LO, B5_HI, B5_SIZE, MIN_EDGE = 0.60, 0.90, 5, 0.01
T3_LO, T3_HI, T3_TARGET, T3_POOL, T3_MAX = 0.50, 0.97, 3.0, 16, 6


def _norm(n: str) -> str:
    n = (n or "").lower()
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", n)
    return re.sub(r"[^a-z]", "", n)


def haircut(k: int) -> float:
    return max(0.65, 1 - 0.028 * k)


class Features:
    """Projection (data/predictions.json) and last-5 form (player_stats_2026) for a player/stat/week."""

    def __init__(self):
        import polars as pl
        self.proj = {}
        try:
            for p in json.loads((DATA / "predictions.json").read_text()).values():
                self.proj[(_norm(p["player"]), p["stat"], p["week"])] = p.get("projected")
        except Exception:  # noqa: BLE001
            pass
        self.games = defaultdict(list)
        ps = pl.read_parquet(DATA / "player_stats_2026.parquet").filter(pl.col("season_type") == "REG").sort("week")
        for r in ps.iter_rows(named=True):
            self.games[_norm(r["player_display_name"])].append(r)

    @staticmethod
    def _val(r, st):
        g = lambda c: r.get(c) or 0  # noqa: E731
        return {"recyd": g("receiving_yards"), "rushyd": g("rushing_yards"), "passyd": g("passing_yards"),
                "rec": g("receptions"), "rr": g("rushing_yards") + g("receiving_yards"),
                "td": g("rushing_tds") + g("receiving_tds"), "td2": g("rushing_tds") + g("receiving_tds")}[st]

    def add(self, l: dict) -> dict:
        k = _norm(l["player"])
        w = l.get("week") or 0
        l["proj"] = self.proj.get((k, PRED_STAT.get(l["stat"]), w))
        prior = [r for r in self.games.get(k, []) if r["week"] < w]
        l["g"] = len(prior)
        if prior:
            v = [self._val(r, l["stat"]) for r in prior[-5:]]
            l["l5"] = sum(x > l["rung"] for x in v) / len(v)
            l["avg5"] = round(S.mean(v), 1)
        return l


def bucket(l: dict) -> tuple:
    p = l.get("proj")
    if l["stat"] in UNIT and p is not None:
        c = (p - l["rung"]) / UNIT[l["stat"]]
        pc = -1 if c < -1 else (0 if c < 1 else (1 if c < 2 else 2))
    elif l["stat"] == "td" and p is not None:
        d = (1 - math.exp(-p)) - l["ask"]
        pc = 1 if d > 0.05 else (0 if d > -0.05 else -1)
    else:
        pc = "n"
    f = l.get("l5")
    fc = "n" if f is None or l.get("g", 0) < 3 else (0 if f < 0.4 else (1 if f < 0.8 else 2))
    return (l["stat"], min(int(l["ask"] * 10), 9), pc, fc)


def calibration(feat: Features, before_week: int | None = None) -> dict:
    hist = json.loads(HIST.read_text())["markets"] if HIST.exists() else {}
    agg = defaultdict(lambda: [0, 0.0])
    for v in hist.values():
        if not v.get("ask") or not 0.03 <= v["ask"] <= 0.97 or not v.get("week"):
            continue
        if before_week and v["week"] >= before_week:
            continue
        l = feat.add({**v})
        c = bucket(l)
        hit = v["result"] == "yes"
        for d in (2, 3, 4):
            a = agg[c[:d]]
            a[0] += 1
            a[1] += hit - v["ask"]
    return dict(agg)


def estimate(l: dict, cal: dict) -> float:
    e = l["ask"]
    c = bucket(l)
    for d in (2, 3, 4):
        n, s = cal.get(c[:d], (0, 0.0))
        if n:
            e += (s / n - (e - l["ask"])) * (n / (n + K))
    return min(0.99, max(0.01, e))


def _bad_players(week: int) -> set:
    import polars as pl
    try:
        i = pl.read_parquet(DATA / "injuries_2026.parquet").filter(pl.col("week") == week)
        return {_norm(r["full_name"]) for r in i.iter_rows(named=True)
                if (r.get("report_status") or "") in ("Out", "Doubtful", "Questionable")}
    except Exception:  # noqa: BLE001
        return set()


def candidates(week: int, now: datetime, feat: Features, cal: dict) -> list[dict]:
    import kalshi_history_nfl as KH
    try:
        live = json.loads(LIVE.read_text())["legs"]
    except Exception:  # noqa: BLE001
        return []
    bad = _bad_players(week)
    out = []
    for l in live:
        ko = KH.event_kickoff(l.get("event") or "")
        if not ko or ko[1] != week or now >= ko[0]:
            continue
        if _norm(l["player"]) in bad or not l.get("ask") or not 0.03 <= l["ask"] <= 0.97:
            continue
        x = feat.add({**l, "week": week, "kickoff": ko[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "game": (l.get("event") or "").split("-")[1][7:]})
        if x.get("g", 0) < 3:
            continue
        x["est"] = round(estimate(x, cal), 4)
        x["edge"] = round(x["est"] - x["ask"], 4)
        out.append(x)
    return out


def _tip(l):
    try:
        return datetime.fromisoformat(str(l.get("kickoff")).replace("Z", "+00:00"))
    except ValueError:
        return None


def choose_triple(legs: list[dict]) -> dict | None:
    pool, seen = [], set()
    for l in sorted([l for l in legs if T3_LO <= l["ask"] <= T3_HI and l["edge"] > 0], key=lambda l: -l["edge"]):
        if l["player"] in seen:
            continue
        pool.append(l)
        seen.add(l["player"])
        if len(pool) == T3_POOL:
            break
    best = None
    for k in range(2, T3_MAX + 1):
        for c in itertools.combinations(pool, k):
            q = 1 / math.prod(x["ask"] for x in c) * haircut(k)
            if q < T3_TARGET:
                continue
            ch = math.prod(x["est"] for x in c)
            if best is None or ch > best["chance"]:
                best = {"legs": sorted(c, key=lambda x: x["kickoff"]), "chance": round(ch, 4),
                        "pays_k": round(1 / math.prod(x["ask"] for x in c), 2), "quote_est": round(q, 2)}
    return best


def _load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        return {"best5": {}, "triple": {}}


def sync(week: int, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    st = _load()
    feat = Features()
    cal = calibration(feat)
    cands = candidates(week, now, feat, cal)
    wk = str(week)
    # Best 5: locked legs stay, open slots refill with the best edges
    cur = st["best5"].get(wk) or {"legs": []}
    locked = [l for l in cur["legs"] if (_tip(l) or now) <= now]
    players = {l["player"] for l in locked}
    add = []
    for c in sorted([c for c in cands if B5_LO <= c["ask"] <= B5_HI], key=lambda c: -c["edge"]):
        if len(locked) + len(add) >= B5_SIZE:
            break
        if c["edge"] <= MIN_EDGE or c["player"] in players:
            continue
        add.append(c)
        players.add(c["player"])
    legs = sorted(locked + add, key=lambda l: (l["kickoff"], -l.get("edge", 0)))
    if legs:
        st["best5"][wk] = {"legs": legs, "built": now.strftime("%Y-%m-%d %H:%M UTC")}
    # 3x slip: rebuilt until its first kickoff, then locked
    t = st["triple"].get(wk)
    if not (t and t.get("legs") and min(_tip(l) for l in t["legs"]) <= now):
        pick = choose_triple(cands)
        if pick:
            st["triple"][wk] = {**pick, "built": now.strftime("%Y-%m-%d %H:%M UTC")}
    STATE.write_text(json.dumps(st, indent=1, sort_keys=True, default=str))
    return f"{len(cands)} live legs, Best 5 {len(legs)} legs, 3x slip {'yes' if st['triple'].get(wk) else 'none'}"


def _grade(l: dict, box) -> tuple[str | None, float | None]:
    import tipsters_nfl as TN
    st = "td" if l["stat"] in ("td", "td2") else l["stat"]
    return TN.grade_leg({"player": l["player"], "stat": st, "line": l["rung"], "side": "over"}, l["week"], box)


def summary(week: int) -> dict:
    import tipsters_nfl as TN
    box = TN.Box()
    st = _load()
    b5w, b5 = [], {"n": 0, "w": 0, "u": 0.0}
    for wk, s in sorted(st["best5"].items(), key=lambda x: -int(x[0])):
        legs = []
        for l in s["legs"]:
            g, v = _grade(l, box)
            legs.append({**l, "res": g, "actual": v})
            if g in ("hit", "miss"):
                b5["n"] += 1
                b5["w"] += g == "hit"
                b5["u"] += (1 - l["ask"]) / l["ask"] if g == "hit" else -1
        gr = [l for l in legs if l["res"] in ("hit", "miss")]
        b5w.append({"week": int(wk), "legs": legs, "w": sum(l["res"] == "hit" for l in gr),
                    "l": sum(l["res"] == "miss" for l in gr),
                    "u": round(sum((1 - l["ask"]) / l["ask"] if l["res"] == "hit" else -1 for l in gr), 2)})
    t3w, t3 = [], {"n": 0, "w": 0, "u": 0.0}
    for wk, s in sorted(st["triple"].items(), key=lambda x: -int(x[0])):
        legs = []
        for l in s["legs"]:
            g, v = _grade(l, box)
            legs.append({**l, "res": g, "actual": v})
        res = [l["res"] for l in legs]
        hit = None
        if "miss" in res:
            hit = False
        elif all(r in ("hit", "void", "push") for r in res):
            hit = True
        if hit is not None:
            t3["n"] += 1
            t3["w"] += hit
            t3["u"] += (s["quote_est"] - 1) if hit else -1
        t3w.append({**s, "week": int(wk), "legs": legs, "hit": hit})
    b5["u"], t3["u"] = round(b5["u"], 2), round(t3["u"], 2)
    live = {"recyd": "receiving_yards", "rushyd": "rushing_yards", "passyd": "passing_yards", "rec": "receptions"}
    for x in (b5w, t3w):
        for n in x:
            for l in n["legs"]:
                l["team"] = l.get("team") or box.team_of(l["player"])
                if l["res"] is None and l["stat"] in live and l["team"]:
                    l["lv"] = f"{l['team']}|{l['player']}|{live[l['stat']]}|{l['rung']}|OVER|{l.get('proj') or 0}"
    return {"week": week, "best5": next((x for x in b5w if x["week"] == week), None), "best5_weeks": b5w,
            "best5_rec": b5, "triple": next((x for x in t3w if x["week"] == week), None), "triple_weeks": t3w,
            "triple_rec": t3}
