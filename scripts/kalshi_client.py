"""
Kalshi integration -- scoped to touchdown props and game-level props only
(spread/total/moneyline), per explicit instruction. Player yardage props
are intentionally left to PrizePicks (see prizepicks_client.py) so the
two sources cover different, non-overlapping prop types.

CONFIRMED WORKING against a real live GitHub Actions run (not a guess):
  - KXNFLTD    -> anytime-touchdown markets (14 open events, 632 markets
                  the day this was confirmed -- one event per game, with
                  a market per player)
  - KXNFLGAME  -> game-level moneyline markets (30 open events, 60
                  markets -- two mutually exclusive YES markets per game,
                  one per team, matching Kalshi's documented sports-game
                  ticker pattern)

Two other candidates (FOOTBALLTOUCHDOWN, KXNFL) were tried and confirmed
NOT to be the real tickers (0 events each) -- dropped rather than kept
as dead weight. This is the real, live-verified list, not a first guess
anymore.
"""

import requests

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

CANDIDATE_SERIES = [
    ("KXNFLTD", "touchdown"),
    ("KXNFLGAME", "game"),
]


def fetch_nfl_touchdown_and_game_props(timeout: int = 15, max_per_bucket: int = 40) -> dict:
    """
    Returns {"touchdown_props": [...], "game_props": [...], "error": str|None,
    "diagnostics": {...}}. Never raises -- a failure here should never
    break the site build.

    Each bucket is sorted by volume (highest-traded markets first) and
    capped at max_per_bucket for display -- a live run found 632 raw TD
    markets, far too many for a readable table. The uncapped, unsorted
    full result is still written to data/kalshi_raw.json by build.py for
    transparency; this cap only affects what's rendered on the page.
    """
    result = {
        "touchdown_props": [],
        "game_props": [],
        "error": None,
        "diagnostics": {"per_series": {}},
    }
    any_success = False
    last_error = None

    for ticker, bucket in CANDIDATE_SERIES:
        try:
            resp = requests.get(
                f"{BASE_URL}/events",
                params={"series_ticker": ticker, "status": "open", "with_nested_markets": "true", "limit": 200},
                timeout=timeout,
            )
            if resp.status_code == 404:
                result["diagnostics"]["per_series"][ticker] = "404 (series does not exist)"
                continue
            resp.raise_for_status()
            data = resp.json()
            events = data.get("events", [])
            result["diagnostics"]["per_series"][ticker] = f"{len(events)} event(s)"
            any_success = True

            for event in events:
                event_title = event.get("title", "") or ticker
                for m in event.get("markets", []):
                    record = {
                        "event_title": event_title,
                        "market_title": m.get("title") or event_title,
                        "ticker": m.get("ticker"),
                        "yes_bid": m.get("yes_bid"),
                        "yes_ask": m.get("yes_ask"),
                        "volume": m.get("volume") or 0,
                        "close_time": event.get("close_time") or m.get("close_time"),
                        "series_ticker": ticker,
                    }
                    if bucket == "touchdown":
                        result["touchdown_props"].append(record)
                    else:
                        result["game_props"].append(record)

        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            result["diagnostics"]["per_series"][ticker] = f"error: {last_error}"

    if not any_success and last_error:
        result["error"] = last_error

    result["diagnostics"]["total_touchdown_props_found"] = len(result["touchdown_props"])
    result["diagnostics"]["total_game_props_found"] = len(result["game_props"])

    result["touchdown_props"].sort(key=lambda r: r["volume"], reverse=True)
    result["game_props"].sort(key=lambda r: r["volume"], reverse=True)
    result["touchdown_props"] = result["touchdown_props"][:max_per_bucket]
    result["game_props"] = result["game_props"][:max_per_bucket]

    return result
