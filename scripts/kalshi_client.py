"""
Kalshi integration -- scoped to touchdown props and game-level props only
(spread/total/moneyline), per explicit instruction. Player yardage props
are intentionally left to PrizePicks (see prizepicks_client.py) so the
two sources cover different, non-overlapping prop types.

CONFIRMED WORKING against a real live GitHub Actions run:
  - KXNFLTD    -> anytime-touchdown markets
  - KXNFLGAME  -> game-level moneyline markets
Both series tickers are real and return real events/markets. However,
the first live run pulled pricing from markets nested inside /events
responses, and every yes_bid/yes_ask/volume came back as 0 -- this
version queries /markets?series_ticker=X directly instead, which is
Kalshi's dedicated live-pricing endpoint (confirmed against their
published schema). If prices are STILL empty after this change, that's
real information too: it means these specific markets genuinely have no
trades yet, not a bug in this code.

Two other candidates (FOOTBALLTOUCHDOWN, KXNFL) were tried and confirmed
NOT to be the real tickers (0 events each) -- dropped rather than kept
as dead weight.
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

    Queries /markets?series_ticker=X directly (not /events with nested
    markets) -- a live run found real events and real ticker names, but
    every yes_bid/yes_ask/volume came back as 0 or null. The likely
    cause: markets embedded inside an /events response may be a lighter
    summary object, while /markets is Kalshi's dedicated live-pricing
    endpoint (confirmed against their published Market schema, which
    lists yes_bid/yes_ask/volume/liquidity as real fields there). This
    is the fix to try; if prices are STILL empty after this, the real
    explanation is simply that these specific markets have no trades
    yet (genuinely illiquid), not a field-name bug.

    Each bucket is sorted by volume (highest-traded markets first) and
    capped at max_per_bucket for display. The uncapped, unsorted full
    result is still written to data/kalshi_raw.json by build.py.
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
            markets = []
            cursor = None
            while True:
                params = {"series_ticker": ticker, "status": "open", "limit": 200}
                if cursor:
                    params["cursor"] = cursor
                resp = requests.get(f"{BASE_URL}/markets", params=params, timeout=timeout)
                if resp.status_code == 404:
                    result["diagnostics"]["per_series"][ticker] = "404 (series does not exist)"
                    break
                resp.raise_for_status()
                data = resp.json()
                batch = data.get("markets", [])
                markets.extend(batch)
                cursor = data.get("cursor")
                if not cursor or not batch:
                    break

            if not markets and ticker not in result["diagnostics"]["per_series"]:
                result["diagnostics"]["per_series"][ticker] = "0 market(s)"
                continue
            elif markets:
                result["diagnostics"]["per_series"][ticker] = f"{len(markets)} market(s)"
                any_success = True

            for m in markets:
                record = {
                    "event_title": m.get("title") or m.get("event_ticker") or ticker,
                    "market_title": m.get("title") or m.get("subtitle") or m.get("ticker"),
                    "ticker": m.get("ticker"),
                    "yes_bid": m.get("yes_bid"),
                    "yes_ask": m.get("yes_ask"),
                    "volume": m.get("volume") or 0,
                    "close_time": m.get("close_time"),
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
