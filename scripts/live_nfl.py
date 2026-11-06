"""
Live pick tracker (ported from the NBA site, Nov 2026).

Every build (the 20-minute odds refresh and the daily build) asks ESPN's free public scoreboard which games are on,
and for games in progress or finished this week pulls their box score. Two small files are written for the pages to
read in the browser, so nothing has to be re-rendered for the numbers to move:

  docs/static/live.json   {"at", "week", "games": {gid: {s, d, h, a, hs, as, q, clock}}, "t": {TEAM: gid},
                           "p": {"TEAM|name": [pass_yds, rush_yds, rec_yds, receptions]}}
  docs/static/week.json   this week's frozen picks that have a line: [[team, name, stat, line, call, projected, best5], ...]

Names are matched by a normalized form (lowercase, no punctuation, no Jr./III) plus team, since ESPN and nflverse
use different player ids. Grading still comes from nflverse the next morning; the tracker only shows where picks stand.
Any failure (ESPN down, a format change) just leaves the tracker hidden -- it never stops a build.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests

API = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
HDR = {"User-Agent": "Mozilla/5.0"}
TEAM = {"WSH": "WAS", "LAR": "LA"}  # ESPN -> nflverse codes (the rest match)
STATS = ("passing_yards", "rushing_yards", "receiving_yards", "receptions")


def norm(name: str) -> str:
    s = unicodedata.normalize("NFD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z ]", "", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _get(url: str) -> dict:
    r = requests.get(url, timeout=20, headers=HDR)
    r.raise_for_status()
    return r.json()


def _num(v) -> float:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def parse_scoreboard(j: dict) -> tuple[int | None, dict]:
    week = (j.get("week") or {}).get("number")
    games = {}
    for e in j.get("events") or []:
        c = (e.get("competitions") or [{}])[0]
        st = c.get("status") or e.get("status") or {}
        ty = st.get("type") or {}
        side = {}
        for t in c.get("competitors") or []:
            ab = ((t.get("team") or {}).get("abbreviation") or "").upper()
            side[t.get("homeAway")] = (TEAM.get(ab, ab), t.get("score"))
        if "home" not in side or "away" not in side:
            continue
        state = ty.get("state") or "pre"
        games[str(e.get("id"))] = {"s": "post" if ty.get("completed") else state, "d": ty.get("shortDetail") or "",
                                   "h": side["home"][0], "a": side["away"][0],
                                   "hs": int(_num(side["home"][1])) if state != "pre" else None,
                                   "as": int(_num(side["away"][1])) if state != "pre" else None,
                                   "q": st.get("period"), "clock": st.get("displayClock")}
    return week, games


def parse_box(summary: dict) -> dict:
    """{"TEAM|name": [pass_yds, rush_yds, rec_yds, receptions]} from an ESPN game summary."""
    out = {}
    for blk in (summary.get("boxscore") or {}).get("players") or []:
        ab = ((blk.get("team") or {}).get("abbreviation") or "").upper()
        team = TEAM.get(ab, ab)
        for grp in blk.get("statistics") or []:
            name = (grp.get("name") or "").lower()
            keys = grp.get("keys") or []
            labels = [str(x).upper() for x in (grp.get("labels") or [])]

            def col(key, label):
                if key in keys:
                    return keys.index(key)
                return labels.index(label) if label in labels else None
            if name == "passing":
                idx = {0: col("passingYards", "YDS")}
            elif name == "rushing":
                idx = {1: col("rushingYards", "YDS")}
            elif name == "receiving":
                idx = {2: col("receivingYards", "YDS"), 3: col("receptions", "REC")}
            else:
                continue
            for a in grp.get("athletes") or []:
                nm = norm(((a.get("athlete") or {}).get("displayName")) or "")
                vals = a.get("stats") or []
                if not nm:
                    continue
                row = out.setdefault(f"{team}|{nm}", [0, 0, 0, 0])
                for slot, i in idx.items():
                    if i is not None and i < len(vals):
                        row[slot] = int(_num(vals[i]))
    return out


def refresh(docs: Path, preds: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    static = docs / "static"
    static.mkdir(parents=True, exist_ok=True)
    week, games = parse_scoreboard(_get(API + "scoreboard"))
    players, errors = {}, []
    for gid, g in games.items():
        if g["s"] == "pre":
            continue
        try:
            players.update(parse_box(_get(API + f"summary?event={gid}")))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{gid}: {type(e).__name__}")
    live = {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "week": week, "games": games,
            "t": {t: gid for gid, g in games.items() for t in (g["h"], g["a"])}, "p": players}
    (static / "live.json").write_text(json.dumps(live, separators=(",", ":")))
    picks = [[p["team"], norm(p["player"]), p["stat"], p["market_line"], p["call"], p.get("projected"),
              1 if p.get("best5_tags") else 0]
             for p in preds.values()
             if p.get("week") == week and p.get("stat") in STATS and p.get("market_line") is not None
             and p.get("call") in ("OVER", "UNDER")]
    (static / "week.json").write_text(json.dumps({"week": week, "picks": picks}, separators=(",", ":")))
    return {"week": week, "games": len(games), "live": sum(1 for g in games.values() if g["s"] == "in"),
            "players": len(players), "picks": len(picks), "errors": errors[:3]}
