"""Kalshi NFL player-prop history (nfl-update-19, Nov 26 2026).

For every SETTLED Kalshi NFL player market this season (receiving / rushing / passing yards, receptions,
rush+rec yards, anytime TD, 2+ TDs) store the last ask before kickoff and how it settled. This is the NFL version of
the NBA site's leg pool: it lets the site measure which kinds of Kalshi rungs beat their price, and grade the 3x slip
and Best 5 on Kalshi replays honestly.

Market data is public (no keys). Runs in GitHub Actions (kalshi_history.yml): the first run backfills the season,
later runs only add markets not already stored. Output: data/kalshi_history_nfl.json
  {"markets": {ticker: {series, stat, player, team, rung, kickoff, ask, bid, result, week?}}, "updated": ...}
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
OUT = Path(__file__).resolve().parent.parent / "data" / "kalshi_history_nfl.json"
SERIES = {"KXNFLRECYDS": "recyd", "KXNFLRSHYDS": "rushyd", "KXNFLPASSYDS": "passyd", "KXNFLREC": "rec",
          "KXNFLRRYDS": "rr", "KXNFLANYTD": "td", "KXNFL2TD": "td2", "KXNFLTD": "td"}
SEASON_START = datetime(2026, 9, 1, tzinfo=timezone.utc)
S = requests.Session()


def _get(path: str, params: dict | None = None, tries: int = 6):
    for i in range(tries):
        try:
            r = S.get(BASE + path, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(2 + 2 * i)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            time.sleep(1 + i)
    return None


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def settled(series: str):
    cursor = None
    while True:
        p = {"series_ticker": series, "status": "settled", "limit": 1000,
             "min_close_ts": int(SEASON_START.timestamp())}
        if cursor:
            p["cursor"] = cursor
        d = _get("/markets", p)
        if not d:
            return
        for m in d.get("markets") or []:
            yield m
        cursor = d.get("cursor")
        if not cursor:
            return


def pre_kick_quote(series: str, m: dict) -> tuple[float | None, float | None]:
    """Last yes ask / bid in the 30 hours before kickoff (hourly candles)."""
    kick = _iso(m["occurrence_datetime"])
    end = int(kick.timestamp())
    d = _get(f"/series/{series}/markets/{m['ticker']}/candlesticks",
             {"start_ts": end - 30 * 3600, "end_ts": end, "period_interval": 60})
    ask = bid = None
    for c in (d or {}).get("candlesticks") or []:
        if c.get("end_period_ts", 0) > end:
            break
        a = _f(((c.get("yes_ask") or {}).get("close_dollars")))
        b = _f(((c.get("yes_bid") or {}).get("close_dollars")))
        if a is not None and 0 < a < 1:
            ask = a
        if b is not None and 0 <= b < 1:
            bid = b
    return ask, bid


def _parse(series: str, stat: str, m: dict):
    title = m.get("yes_sub_title") or m.get("title") or ""
    player = title.split(":")[0].strip()
    rung = m.get("floor_strike")
    stat_ = stat
    if series == "KXNFLTD":                    # "Player: 1+" / "2+" style
        try:
            k = int(title.split(":")[1].strip().rstrip("+"))
        except (IndexError, ValueError):
            return None
        stat_ = "td" if k == 1 else ("td2" if k == 2 else None)
        if not stat_:
            return None
        rung = k - 0.5
    elif rung is None:
        rung = 0.5 if stat == "td" else (1.5 if stat == "td2" else None)
    return {"series": series, "stat": stat_, "player": player, "event": m.get("event_ticker"), "rung": rung,
            "kickoff": m["occurrence_datetime"], "result": m["result"]}


def main(max_new: int = 12000, workers: int = 12) -> None:
    from concurrent.futures import ThreadPoolExecutor
    store = {"markets": {}}
    if OUT.exists():
        store = json.loads(OUT.read_text())
    have = store["markets"]
    todo = []
    for series, stat in SERIES.items():
        n = 0
        for m in settled(series):
            t = m["ticker"]
            if t in have or m.get("result") not in ("yes", "no") or not m.get("occurrence_datetime"):
                continue
            if _iso(m["occurrence_datetime"]) < SEASON_START:
                continue
            rec = _parse(series, stat, m)
            if rec:
                todo.append((t, series, m, rec))
                n += 1
        print(f"{series}: {n} to fetch", flush=True)
    import os
    left = max(len(todo) - max_new, 0)
    todo = todo[:int(os.environ.get("MAX_NEW", max_new))]
    print(f"{left} more left for the next run", flush=True)
    print(f"fetching pre-kickoff quotes for {len(todo)} markets with {workers} workers", flush=True)

    def job(x):
        t, series, m, rec = x
        ask, bid = pre_kick_quote(series, m)
        return t, {**rec, "ask": ask, "bid": bid}

    done = 0
    with ThreadPoolExecutor(workers) as ex:
        for t, rec in ex.map(job, todo):
            have[t] = rec
            done += 1
            if done % 1000 == 0:
                OUT.write_text(json.dumps(store, separators=(",", ":")))
                print(f"{done} stored", flush=True)
    store["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    OUT.write_text(json.dumps(store, separators=(",", ":")))
    print(f"done: {done} new, {len(have)} total")


if __name__ == "__main__":
    main()
