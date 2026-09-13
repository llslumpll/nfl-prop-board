"""
Kalshi integration -- scoped to touchdown props and game-level props only
(spread/total/moneyline), per explicit instruction. Player yardage props
are intentionally left to PrizePicks (see prizepicks_client.py) so the
two sources cover different, non-overlapping prop types.

REBUILT to match the MLB site's proven-working pattern exactly, after
two earlier approaches both returned real markets with null prices:
  1. /events?series_ticker=X&with_nested_markets=true -- returned real
     events/markets but every price field was null.
  2. /markets?series_ticker=X (bulk, whole series at once) -- same
     result, all null, even via individual /markets/{ticker} hydration.
The MLB script's working approach is neither of these: it fetches
events via /events?series_ticker=X (no nested markets), then for EACH
event makes a SEPARATE /markets?event_ticker=X&status=open call. This
per-event-scoped markets call is the one this file never tried. Copied
directly from that confirmed-working reference rather than guessing a
fourth theory.
"""

import time
import requests

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

CANDIDATE_SERIES = [
    ("KXNFLTD", "touchdown"),
    ("KXNFLFIRSTTD", "touchdown"),
    ("KXNFLGAME", "game"),
    ("KXNFLSPREAD", "game"),
    ("KXNFLTOTAL", "game"),
]


def _get(url, params=None, timeout=15):
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _fetch_events(series_ticker):
    events, cursor = [], None
    while True:
        params = {"series_ticker": series_ticker, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = _get(f"{BASE_URL}/events", params=params)
        batch = data.get("events") or []
        events.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    return events


def _fetch_markets_for_event(event_ticker):
    data = _get(f"{BASE_URL}/markets", params={"event_ticker": event_ticker, "status": "open"})
    return data.get("markets") or []


def _to_num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fetch_nfl_touchdown_and_game_props(max_per_bucket: int = 40) -> dict:
    """
    Returns {"touchdown_props": [...], "game_props": [...], "error": str|None,
    "diagnostics": {...}}. Never raises -- a failure here should never
    break the site build.
    """
    result = {
        "touchdown_props": [],
        "game_props": [],
        "error": None,
        "diagnostics": {"per_series": {}},
    }
    any_success = False
    last_error = None

    for series_ticker, bucket in CANDIDATE_SERIES:
        try:
            events = _fetch_events(series_ticker)
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            result["diagnostics"]["per_series"][series_ticker] = f"error fetching events: {last_error}"
            continue

        if not events:
            result["diagnostics"]["per_series"][series_ticker] = "0 event(s)"
            continue

        any_success = True
        n_markets = 0
        for ev in events:
            event_ticker = ev.get("event_ticker")
            if not event_ticker:
                continue
            try:
                markets = _fetch_markets_for_event(event_ticker)
            except Exception:
                continue
            for m in markets:
                yes_bid = _to_num(m.get("yes_bid_dollars"))
                yes_ask = _to_num(m.get("yes_ask_dollars"))
                # Bid/ask midpoint strips the market-maker spread back
                # out -- same de-vig approach as the MLB reference.
                # Falls back to whichever single side exists if a
                # market is too thin to have both quoted.
                if yes_bid and yes_ask:
                    price = round((yes_bid + yes_ask) / 2, 2)
                else:
                    price = yes_ask or yes_bid
                record = {
                    "event_title": ev.get("title") or event_ticker,
                    "market_title": m.get("yes_sub_title") or m.get("title") or m.get("ticker"),
                    "ticker": m.get("ticker"),
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "yes_bid_cents": round(yes_bid * 100) if yes_bid is not None else None,
                    "yes_ask_cents": round(yes_ask * 100) if yes_ask is not None else None,
                    "price": price,
                    "volume": m.get("volume") or 0,
                    "close_time": ev.get("close_time") or m.get("close_time"),
                    "series_ticker": series_ticker,
                }
                if bucket == "touchdown":
                    result["touchdown_props"].append(record)
                else:
                    result["game_props"].append(record)
                n_markets += 1
            time.sleep(0.1)  # be polite to a public, unauthenticated endpoint

        result["diagnostics"]["per_series"][series_ticker] = f"{len(events)} event(s), {n_markets} market(s)"

    if not any_success and last_error:
        result["error"] = last_error

    result["diagnostics"]["total_touchdown_props_found"] = len(result["touchdown_props"])
    result["diagnostics"]["total_game_props_found"] = len(result["game_props"])

    def _sort_key(r):
        has_quote = r["yes_bid"] is not None or r["yes_ask"] is not None
        return (has_quote, r["price"] or 0)

    result["touchdown_props"].sort(key=_sort_key, reverse=True)
    result["game_props"].sort(key=_sort_key, reverse=True)
    result["diagnostics"]["touchdown_props_with_a_quote"] = sum(
        1 for r in result["touchdown_props"] if r["yes_bid"] is not None or r["yes_ask"] is not None
    )
    result["diagnostics"]["game_props_with_a_quote"] = sum(
        1 for r in result["game_props"] if r["yes_bid"] is not None or r["yes_ask"] is not None
    )
    result["touchdown_props"] = result["touchdown_props"][:max_per_bucket]
    result["game_props"] = result["game_props"][:max_per_bucket]

    return result
