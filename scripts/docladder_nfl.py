"""Site ladders vs Dr. Profit (nfl-update-24, Nov 26 2026): the site builds a Dr. Profit-style receiving-yards ladder
each week and races his real ladders, the NFL version of the NBA site's "Site ladders vs Next Level Bets".

How he builds them (his 33 graded NFL ladders): mostly receiving yards on secondary targets (WR2/3s, tight ends), a low
base line near -115, then about +20 yards (~+200) and +40 yards (~+500 to +1,200). His base rung: 19 of 32.
The site's safer version (the user's targets: base rung 70%+, ladder cashing more often than not):
  - stat: receiving yards; base = the player's safest Kalshi rung priced 70-90%;
  - next rung = the first Kalshi rung at least 20 yards higher, bonus rung = 40 yards higher; stakes 1U / 0.25U / 0.1U;
  - players ranked by the site's real-chance edge on the base rung (kbest_nfl calibration), 3+ games, not
    out/doubtful/questionable, game not started; TWO ladders a week.
Replay on Kalshi history (weeks 4-11, each week judged only on the other weeks): base 15/16 (94%), ladder cashed
(base + next) 15/16, +3.2U on 21.6U; 50-75% base: 15/16 base, 9/16 cashed, +9.6U. Only 16 ladders: TESTING.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
STATE = DATA / "docladder_nfl.json"
ASK_LO, ASK_HI, PER_WEEK, STEP = 0.70, 0.90, 2, 20.0
STAKES = (1.0, 0.25, 0.1)


def _load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _ko(x) -> datetime:
    return datetime.fromisoformat(str(x["kickoff"]).replace("Z", "+00:00"))


def sync(week: int, now: datetime | None = None) -> str:
    import kbest_nfl as KB
    import kalshi_history_nfl as KH
    now = now or datetime.now(timezone.utc)
    st = _load()
    cur = st.get(str(week)) or {"ladders": []}
    locked = [lad for lad in cur["ladders"] if _ko(lad) <= now]
    if len(locked) >= PER_WEEK:
        return f"{len(locked)} locked"
    feat = KB.Features()
    cal = KB.calibration(feat)
    cands = KB.candidates(week, now, feat, cal)
    ladders = {}
    for c in cands:
        ladders.setdefault(c["player"], {}).setdefault(c["stat"], {})[c["rung"]] = c
    used = {lad["player"] for lad in locked}
    pool = []
    for p, by in ladders.items():
        R = by.get("recyd") or {}
        q = [r for r, l in R.items() if ASK_LO <= l["ask"] <= ASK_HI]
        if not q or p in used:
            continue
        b = max(q, key=lambda r: R[r]["ask"])
        pool.append((R[b]["edge"], p, b, R))
    add = []
    for edge, p, b, R in sorted(pool, key=lambda x: -x[0]):
        if len(locked) + len(add) >= PER_WEEK:
            break
        tiers, taken = [], set()
        for i, off in enumerate((0.0, STEP, 2 * STEP)):
            r = b if not off else min((x for x in R if x >= b + off - 0.1), default=None)
            if r is None or r in taken:
                continue
            taken.add(r)
            l = R[r]
            tiers.append({"player": p, "stat": "recyd", "rung": r, "ask": l["ask"], "est": l["est"], "stake": STAKES[i],
                          "step": i, "week": week, "kickoff": l["kickoff"], "team": l.get("team"), "game": l.get("game")})
        base = R[b]
        add.append({"player": p, "kickoff": base["kickoff"], "game": base.get("game"), "proj": base.get("proj"),
                    "avg5": base.get("avg5"), "l5": base.get("l5"), "tiers": tiers})
    st[str(week)] = {"ladders": locked + add, "built": now.strftime("%Y-%m-%d %H:%M UTC")}
    STATE.write_text(json.dumps(st, indent=1, sort_keys=True))
    return f"{len(locked)} locked + {len(add)} new"


def _pay_ask(a: float) -> float:
    return (1 - a) / a


def _pay_am(o: int) -> float:
    return o / 100 if o > 0 else 100 / -o


def summary(week: int) -> dict:
    import tipsters_nfl as TN
    box = TN.Box()
    live = {"recyd": "receiving_yards"}

    def blank():
        return [{"n": 0, "w": 0, "price": []} for _ in STAKES]

    def fin(steps):
        for s in steps:
            p = s.pop("price")
            s["price"] = round(100 * sum(p) / len(p)) if p else None
        return steps

    weeks, steps, u, staked, n_lad, cashed = [], blank(), 0.0, 0.0, 0, 0
    for wk, s in sorted(_load().items(), key=lambda x: -int(x[0])):
        lads = []
        for lad in s["ladders"]:
            tiers = []
            for t in lad["tiers"]:
                g, v = TN.grade_leg({"player": t["player"], "stat": "recyd", "line": t["rung"], "side": "over"}, t["week"], box)
                t2 = {**t, "res": g, "actual": v, "team": t.get("team") or box.team_of(t["player"])}
                if g is None and t2["team"]:
                    t2["lv"] = f"{t2['team']}|{t['player']}|{live['recyd']}|{t['rung']}|OVER|{lad.get('proj') or 0}"
                tiers.append(t2)
            lads.append({**lad, "tiers": tiers})
            if all(t["res"] in ("hit", "miss") for t in tiers) and tiers:
                n_lad += 1
                two = [t for t in tiers if t["step"] in (0, 1)]
                cashed += len(two) == 2 and all(t["res"] == "hit" for t in two)
                for t in tiers:
                    x = steps[t["step"]]
                    x["n"] += 1
                    x["w"] += t["res"] == "hit"
                    x["price"].append(t["ask"])
                    staked += t["stake"]
                    u += t["stake"] * _pay_ask(t["ask"]) if t["res"] == "hit" else -t["stake"]
        weeks.append({"week": int(wk), "ladders": lads})
    site = {"ladders": n_lad, "full": cashed, "u": round(u, 2), "staked": round(staked, 2), "steps": fin(steps)}

    # his real ladders (data/tipsters/picks.json, tipster "doc" with rungs), same stakes for a fair race
    hs, hu, hst, hn, hfull, recent = blank(), 0.0, 0.0, 0, 0, []
    for p in TN._load():
        if p.get("tipster") != "doc" or not p.get("rungs") or len(p.get("legs") or []) != 1:
            continue
        r = TN.grade_pick(p, box)
        if r["res"] not in ("hit", "miss"):
            continue
        rows = [{"hit": r["res"] == "hit", "odds": p["odds"]}] + [{"hit": x.get("hit", False), "odds": x.get("odds")}
                                                                   for x in r["rungs"]]
        hn += 1
        hfull += len(rows) >= 2 and rows[0]["hit"] and rows[1]["hit"]
        for i, x in enumerate(rows):
            k = min(i, 2)
            hs[k]["n"] += 1
            hs[k]["w"] += x["hit"]
            if x["odds"] is not None:
                hs[k]["price"].append(1 / (1 + _pay_am(int(x["odds"]))))
                s_ = STAKES[k]
                hst += s_
                hu += s_ * _pay_am(int(x["odds"])) if x["hit"] else -s_
        leg = r["legs"][0]
        recent.append({"week": p["week"], "player": leg["player"], "stat": TN.LAB.get(leg["stat"], leg["stat"]),
                       "actual": leg.get("actual"),
                       "rungs": [{"line": leg["line"], "hit": r["res"] == "hit"}] + [{"line": x["line"], "hit": x.get("hit")} for x in r["rungs"]]})
    his = {"ladders": hn, "full": hfull, "u": round(hu, 2), "staked": round(hst, 2), "steps": fin(hs),
           "recent": sorted(recent, key=lambda x: -x["week"])[:6]}
    return {"week": week, "this": next((x for x in weeks if x["week"] == week), None),
            "last": next((x for x in weeks if x["week"] < week and x["ladders"]), None), "site": site, "his": his}
