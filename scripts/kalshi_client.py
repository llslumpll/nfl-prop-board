"""
Kalshi integration -- scoped to touchdown props and game-level props only
(spread/total/moneyline), per explicit instruction. Player yardage props
are intentionally left to PrizePicks (see prizepicks_client.py) so the
two sources cover different, non-overlapping prop types.

IMPORTANT: this hits Kalshi's real, documented, unauthenticated public
API (https://docs.kalshi.com/getting_started/quick_start_market_data).
No API key needed.

REAL FINDING FROM THE FIRST LIVE RUN (not a guess): scanning /events
broadly with no series filter returns real data (confirmed: 600 events,
correct response shape) but pagination order surfaces long-horizon
futures markets (politics, climate, IPOs) first -- NFL game markets
never showed up in the first 600. Kalshi's own guidance is explicit
about this failure mode: sports/series-based categories must be queried
via Series -> Events -> Markets, i.e. /events?series_ticker=X, never a
broad scan. This version does that instead.

Sports game tickers follow a documented pattern of
{SERIES}-{date}{TEAM1}{TEAM2}-{TEAM}, e.g. KXNHLGAME-25MAY12EDMORL-EDM
for NHL. By that pattern, NFL should be KXNFLGAME. Kalshi's touchdown
market has been independently reported (trade press, not docs) as
ticker FOOTBALLTOUCHDOWN. Both are tried below as candidates -- this is
still not 100% certain until a live run confirms it, so this probes
several plausible candidates and records which ones actually returned
events, rather than betting everything on one guess.
"""

import requests

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# Candidate series tickers to probe. Each entry: (ticker, bucket).
# "touchdown" -> touchdown_props, "game" -> game_props.
CANDIDATE_SERIES = [
    ("FOOTBALLTOUCHDOWN", "touchdown"),
    ("KXNFLTD", "touchdown"),
    ("KXNFLGAME", "game"),
    ("KXNFL", "game"),
]


def fetch_nfl_touchdown_and_game_props(timeout: int = 15) -> dict:
    """
    Returns {"touchdown_props": [...], "game_props": [...], "error": str|None,
    "diagnostics": {...}}. Never raises -- a failure here should never
    break the site build.

    Queries each candidate series ticker directly via
    /events?series_ticker=X rather than scanning the full event catalog
    (see module docstring for why the broad-scan approach failed on the
    first live run). Per-series diagnostics record which tickers were
    real (returned events) vs. wrong guesses (404 or empty), so the next
    round can drop dead candidates and add better ones without more
    guesswork.
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
                        "volume": m.get("volume"),
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

    return result
