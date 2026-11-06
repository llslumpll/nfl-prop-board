"""
Injury-news plays (ported from the NBA site's best edge, Nov 2026). TRACKING ONLY -- no stakes until they prove it.

When a starter-level player (8+ targets + carries a game, or 20+ pass attempts, this season) shows up OUT on the
injury report, his teammates who have a line get checked: in this season's games the team played without him,
did that teammate's stat go up? If it went up 10%+ (in at least one game without him), it's a news play: OVER
the teammate's line as it stood when the news was seen. Each play is graded at that line.

Why: on the graded weeks, teammates of a ruled-out starter whose line hadn't moved went OVER 62% of the time
(38/61), and 67% (12/18) where we'd seen him do better without the starter. Small samples -- hence tracking.

Injury reports come from nflverse (the daily build, and every odds refresh Thursday-Sunday re-pulls that one
small file when it's over an hour old, so Friday's final report is seen within the hour).

data/news.json  {"<week>": {"<out player>|<team>": event}}
"""

from __future__ import annotations

import json
import statistics as S
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

import dataio
import line_movement_log
import predictions

DATA = Path(__file__).parent.parent / "data"
PATH = DATA / "news.json"
STATS = ("passing_yards", "rushing_yards", "receiving_yards", "receptions")
SHORT = {"passing_yards": "PASS YDS", "rushing_yards": "RUSH YDS", "receiving_yards": "REC YDS", "receptions": "REC"}
MIN_BUMP = 0.10


def load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def refresh_injuries(now: datetime | None = None) -> str:
    """Odds-refresh builds (no full data pull): re-pull the injury report Thu-Sun if the file is over an hour old."""
    now = now or datetime.now(timezone.utc)
    f = DATA / "injuries_2026.parquet"
    if now.weekday() not in (3, 4, 5, 6):
        return "not a report day"
    if f.exists() and (now.timestamp() - f.stat().st_mtime) < 3600:
        return "fresh"
    import nflreadpy as nfl
    nfl.load_injuries([2026]).write_parquet(f)
    dataio._injuries_cache = None
    return "re-pulled"


def _usage(rows: list[dict], name: str, wk: int) -> bool:
    g = [r for r in rows if r["player_display_name"] == name and r["week"] < wk]
    if len(g) < 2:
        return False
    return (S.mean((r["targets"] or 0) + (r["carries"] or 0) for r in g) >= 8
            or S.mean(r["attempts"] or 0 for r in g) >= 20)


def sync(week: int | None, pp_props: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    d = load()
    if week is not None:
        stats = dataio.load_stats()
        rows = stats.select("player_display_name", "team", "week", "targets", "carries", "attempts", *STATS).to_dicts()
        by_team = {}
        for r in rows:
            by_team.setdefault(r["team"], []).append(r)
        inj = dataio.load_injuries()
        outs = inj.filter((pl.col("week") == week) & (pl.col("report_status") == "Out")).to_dicts()
        sched = dataio.load_schedule()
        wk = d.setdefault(str(week), {})
        stamp = now.strftime("%Y-%m-%d %H:%M UTC")
        for o in outs:
            team, name = o["team"], o["full_name"]
            key = f"{name}|{team}"
            if key in wk:
                continue
            trows = by_team.get(team, [])
            if not _usage(trows, name, week):
                continue
            g = sched.filter((pl.col("week") == week) & ((pl.col("home_team") == team) | (pl.col("away_team") == team)))
            if not g.height or g["result"][0] is not None:
                continue  # no game this week, or it's already been played
            played = {r["week"] for r in trows if r["player_display_name"] == name}
            team_weeks = {r["week"] for r in trows if r["week"] < week}
            missed = sorted(w for w in team_weeks if w not in played)
            plays = []
            for mate in sorted({r["player_display_name"] for r in trows if r["player_display_name"] and r["player_display_name"] != name}):
                lines = pp_props.get(mate) or {}
                mine = [r for r in trows if r["player_display_name"] == mate and r["week"] < week]
                for stat in STATS:
                    line = lines.get(stat)
                    if line is None:
                        continue
                    wo = [r[stat] or 0 for r in mine if r["week"] in missed]
                    wi = [r[stat] or 0 for r in mine if r["week"] in played]
                    if not wo or len(wi) < 2:
                        continue
                    base = S.mean(wi)
                    bump = (S.mean(wo) - base) / max(1.0, base)
                    if bump < MIN_BUMP:
                        continue
                    plays.append({"player": mate, "stat": stat, "short": SHORT[stat], "line": line, "call": "OVER",
                                  "with": round(base, 1), "without": round(S.mean(wo), 1), "n_wo": len(wo),
                                  "bump": round(100 * bump), "result": None})
            wk[key] = {"week": week, "team": team, "out": name, "pos": o.get("position"), "seen": stamp,
                       "opp": g["away_team"][0] if g["home_team"][0] == team else g["home_team"][0],
                       "plays": plays}
    _track_and_grade(d)
    PATH.write_text(json.dumps(d, indent=1, sort_keys=True))
    s = summary(d)
    return {"week": week, "events": len(d.get(str(week), {})) if week is not None else 0, "plays": s["n"], "hits": s["hits"]}


def _track_and_grade(d: dict) -> None:
    preds = predictions.load_predictions()
    log = line_movement_log._load()
    for wk in d.values():
        for ev in wk.values():
            for b in ev["plays"]:
                if b["result"] is not None:
                    continue
                e = log.get(f"{b['player']}|{b['stat']}|{ev['week']}")
                if e:
                    b["now"] = e["current_line"]
                    b["moved"] = e["current_line"] != b["line"]
                p = preds.get(predictions._key(b["player"], b["stat"], ev["week"]))
                if p and p.get("graded"):
                    a = p.get("actual")
                    if p.get("void") or a is None or a == b["line"]:
                        b["result"] = "void"
                    else:
                        b["result"] = "hit" if a > b["line"] else "miss"
                    b["actual"] = a


def summary(d: dict | None = None) -> dict:
    d = d if d is not None else load()
    plays = [b for wk in d.values() for ev in wk.values() for b in ev["plays"] if b.get("result") in ("hit", "miss")]
    h = sum(1 for b in plays if b["result"] == "hit")
    return {"n": len(plays), "hits": h, "rate": round(100 * h / len(plays), 1) if plays else None}


def page(week: int | None) -> dict:
    d = load()
    evs = sorted(d.get(str(week), {}).values(), key=lambda e: e["seen"]) if week is not None else []
    return {"week": week, "events": [e for e in evs if e["plays"]], "quiet": [e for e in evs if not e["plays"]],
            "summary": summary(d)}
